import asyncio
import base64
import mimetypes
import os
import readline
import re
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

from daytona import CreateSandboxFromSnapshotParams, Daytona, DaytonaConfig
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from deepagents.middleware.summarization import SummarizationMiddleware
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelRequest,
    ModelResponse,
    ModelRetryMiddleware,
    TodoListMiddleware,
    ToolRetryMiddleware,
    wrap_model_call,
)
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.messages import AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.tools import tool
from langchain_daytona import DaytonaSandbox
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from prompts import (
    CODE_SUBAGENT_DESCRIPTION,
    CODE_SUBAGENT_PROMPT,
    INTERNET_SUBAGENT_DESCRIPTION,
    INTERNET_SUBAGENT_PROMPT,
    MAIN_AGENT_PROMPT,
)
from tools import get_local_tools, get_mcp_tools


load_dotenv()

DEFAULT_PROMPT = "What time is it right now?"
CHECKPOINT_DB = os.getenv("CHECKPOINT_DB", "checkpoints.db")

THREAD_ID = os.getenv("THREAD_ID", "t23")
AI_MODEL_MULTIMODAL = os.getenv("AI_MODEL_MULTIMODAL", "false").lower() == "true"

OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DAYTONA_API_URL = os.getenv("DAYTONA_API_URL")
DAYTONA_API_KEY = os.getenv("DAYTONA_API_KEY")
DAYTONA_AUTO_DELETE_INTERVAL = int(os.getenv("DAYTONA_AUTO_DELETE_INTERVAL", "86400"))
LOCAL_THREAD_ROOT = Path(os.getenv("LOCAL_THREAD_ROOT", "THREADS")) / THREAD_ID
SANDBOX_ARTIFACT_SUBDIR = os.getenv("SANDBOX_ARTIFACT_SUBDIR", "sandbox_artifacts")
SANDBOX_UPLOAD_SUBDIR = os.getenv("SANDBOX_UPLOAD_SUBDIR", "uploads")
SANDBOX_HOME = Path("/home/daytona")
SANDBOX_WORK_ROOT = SANDBOX_HOME / "workspace" / THREAD_ID
SANDBOX_UPLOAD_ROOT = SANDBOX_HOME / SANDBOX_UPLOAD_SUBDIR / THREAD_ID
SANDBOX_HISTORY_ROOT = SANDBOX_HOME / "conversation_history" / THREAD_ID
CLI_HISTORY_FILE = Path(os.getenv("CLI_HISTORY_FILE", ".cli_history"))
CLI_SHOW_TOOL_CALLS = os.getenv("CLI_SHOW_TOOL_CALLS", "true").lower() == "true"
CLI_SHOW_TODOS = os.getenv("CLI_SHOW_TODOS", "false").lower() == "true"
CLI_SHOW_REASONING = os.getenv("CLI_SHOW_REASONING", "false").lower() == "true"
CLI_VERBOSE_TOOLS = os.getenv("CLI_VERBOSE_TOOLS", "false").lower() == "true"
USE_COLOR = sys.stdout.isatty() and os.getenv("NO_COLOR") is None
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_CYAN = "\033[36m"
ANSI_BLUE = "\033[94m"
ANSI_GREEN = "\033[92m"
ANSI_YELLOW = "\033[93m"
ANSI_MAGENTA = "\033[95m"
ANSI_GRAY = "\033[90m"
ANSI_SOFT_PURPLE = "\033[38;5;183m"
ANSI_ORANGE = "\033[38;5;214m"
DDGS_TOOL_NAMES = [
    "search_text",
    "search_images",
    "search_news",
    "search_videos",
    "search_books",
    "extract_content",
]


class FilesOnlyBackend:
    """Expose sandbox file operations without advertising command execution support."""

    def __init__(self, backend: DaytonaSandbox):
        self._backend = backend

    def __getattr__(self, name: str):
        return getattr(self._backend, name)


def get_model(model_env: str, temperature_env: str) -> ChatOpenAI:
    model_name = os.getenv(model_env)
    if not model_name:
        raise ValueError(f"Missing required environment variable: {model_env}")

    if not OPENROUTER_BASE_URL:
        raise ValueError("Missing required environment variable: OPENROUTER_BASE_URL")

    if not OPENROUTER_API_KEY:
        raise ValueError("Missing required environment variable: OPENROUTER_API_KEY")

    temperature = float(os.getenv(temperature_env, "0"))

    return ChatOpenAI(
        model=model_name,
        temperature=temperature,
        base_url=OPENROUTER_BASE_URL,
        api_key=OPENROUTER_API_KEY,
        max_retries=10,
        timeout=120,
    )


def configure_cli_readline() -> None:
    try:
        readline.parse_and_bind("tab: complete")
        readline.parse_and_bind("set editing-mode emacs")
        readline.parse_and_bind(r'"\e[A": history-search-backward')
        readline.parse_and_bind(r'"\e[B": history-search-forward')
        if CLI_HISTORY_FILE.exists():
            readline.read_history_file(str(CLI_HISTORY_FILE))
    except Exception:
        return


def save_cli_history() -> None:
    try:
        CLI_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        readline.write_history_file(str(CLI_HISTORY_FILE))
    except Exception:
        pass


def style(text: str, *codes: str) -> str:
    if not USE_COLOR or not codes:
        return text
    return "".join(codes) + text + ANSI_RESET


def readline_style(text: str, *codes: str) -> str:
    if not USE_COLOR or not codes:
        return text
    start = "\001" + "".join(codes) + "\002"
    end = "\001" + ANSI_RESET + "\002"
    return start + text + end


def separator() -> str:
    return style("-" * 30, ANSI_DIM, ANSI_GRAY)


def prompt_label() -> str:
    return readline_style("You", ANSI_BOLD, ANSI_ORANGE) + readline_style(": ", ANSI_ORANGE)


def section_label(name: str) -> str:
    return style(f"[{name}]", ANSI_BOLD, ANSI_MAGENTA)


def tool_label(name: str) -> str:
    return style(f"[tool:{name}]", ANSI_BOLD, ANSI_BLUE)


def todo_label() -> str:
    return style("[todos]", ANSI_BOLD, ANSI_YELLOW)


def upload_label(name: str) -> str:
    return style(f"[{name}]", ANSI_BOLD, ANSI_GREEN)


def _content_is_multimodal(content: object) -> bool:
    if isinstance(content, str) or content is None:
        return False

    if isinstance(content, dict):
        block_type = content.get("type")
        if block_type in {"image", "audio", "video", "file", "image_url", "input_image", "input_audio", "input_file"}:
            return True

        if "image_url" in content or "file_id" in content:
            return True

        return any(_content_is_multimodal(value) for value in content.values())

    if isinstance(content, list):
        return any(_content_is_multimodal(item) for item in content)

    return False


def state_has_multimodal_content(messages: list[object]) -> bool:
    for message in messages:
        if isinstance(message, BaseMessage):
            if _content_is_multimodal(message.content):
                return True
            continue

        if isinstance(message, dict) and _content_is_multimodal(message.get("content")):
            return True

    return False


def make_model_router(default_model: ChatOpenAI, multimodal_model: ChatOpenAI | None):
    @wrap_model_call
    async def route_model(request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        if AI_MODEL_MULTIMODAL or multimodal_model is None:
            return await handler(request)

        messages = request.state.get("messages", [])
        if state_has_multimodal_content(messages):
            return await handler(request.override(model=multimodal_model))

        return await handler(request.override(model=default_model))

    return route_model


def get_daytona_backend() -> DaytonaSandbox:
    if not DAYTONA_API_KEY:
        raise ValueError("Missing required environment variable: DAYTONA_API_KEY")

    config_kwargs = {"api_key": DAYTONA_API_KEY}
    if DAYTONA_API_URL:
        config_kwargs["api_url"] = DAYTONA_API_URL

    client = Daytona(DaytonaConfig(**config_kwargs))

    sandbox = None
    try:
        sandbox = client.find_one(labels={"thread_id": THREAD_ID})
    except Exception:
        sandbox = None

    if sandbox is None:
        sandbox = client.create(
            CreateSandboxFromSnapshotParams(
                labels={"thread_id": THREAD_ID},
                auto_delete_interval=DAYTONA_AUTO_DELETE_INTERVAL,
            )
        )

    return DaytonaSandbox(sandbox=sandbox)


def ensure_sandbox_layout(backend: DaytonaSandbox) -> None:
    backend.execute(
        "mkdir -p "
        f"{shlex.quote(str(SANDBOX_WORK_ROOT))} "
        f"{shlex.quote(str(SANDBOX_UPLOAD_ROOT))} "
        f"{shlex.quote(str(SANDBOX_HISTORY_ROOT))}"
    )


def get_download_target_path(sandbox_path: Path) -> Path | None:
    try:
        return sandbox_path.relative_to(SANDBOX_WORK_ROOT)
    except ValueError:
        pass

    try:
        return Path(SANDBOX_UPLOAD_SUBDIR) / sandbox_path.relative_to(SANDBOX_UPLOAD_ROOT)
    except ValueError:
        pass

    try:
        return sandbox_path.relative_to(SANDBOX_HOME)
    except ValueError:
        return Path(str(sandbox_path).lstrip("/")) if sandbox_path.parts else None


def rewrite_downloaded_text_references(
    local_path: Path,
    sandbox_to_local: dict[str, Path],
) -> None:
    if local_path.suffix.lower() not in {".md", ".markdown", ".html", ".htm"}:
        return

    try:
        content = local_path.read_text(encoding="utf-8")
    except Exception:
        return

    updated = content
    for sandbox_source, target_path in sorted(sandbox_to_local.items(), key=lambda item: len(item[0]), reverse=True):
        try:
            relative_target = os.path.relpath(target_path, start=local_path.parent)
        except ValueError:
            continue

        normalized_target = relative_target.replace(os.sep, "/")
        pattern = re.escape(sandbox_source)
        updated = re.sub(pattern, normalized_target, updated)

    if updated != content:
        local_path.write_text(updated, encoding="utf-8")


def make_download_sandbox_files_tool(backend: DaytonaSandbox):
    @tool
    async def download_sandbox_files(paths: list[str]) -> str:
        """Download selected final sandbox files to the local thread artifacts directory."""
        local_root = LOCAL_THREAD_ROOT / SANDBOX_ARTIFACT_SUBDIR
        local_root.mkdir(parents=True, exist_ok=True)

        downloads = backend.download_files(paths)
        downloaded_paths: list[str] = []
        failed_paths: list[str] = []
        sandbox_to_local: dict[str, Path] = {}
        downloaded_text_files: list[Path] = []

        for download in downloads:
            content = getattr(download, "content", None)
            if content is None:
                failed_paths.append(getattr(download, "path", "<unknown>"))
                continue

            sandbox_path = Path(download.path)
            relative_path = get_download_target_path(sandbox_path)

            if relative_path is None or not relative_path.parts:
                failed_paths.append(str(sandbox_path))
                continue

            target_path = local_root / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(content)
            downloaded_paths.append(str(target_path))
            sandbox_to_local[str(sandbox_path)] = target_path
            if target_path.suffix.lower() in {".md", ".markdown", ".html", ".htm"}:
                downloaded_text_files.append(target_path)

        for text_file in downloaded_text_files:
            rewrite_downloaded_text_references(text_file, sandbox_to_local)

        parts: list[str] = []
        if downloaded_paths:
            parts.append("Downloaded files:\n" + "\n".join(downloaded_paths))
        if failed_paths:
            parts.append("Failed downloads:\n" + "\n".join(failed_paths))
        if not parts:
            return "No files were downloaded."
        return "\n\n".join(parts)

    return download_sandbox_files


async def build_agent():
    main_model = get_model("AI_MODEL", "AI_MODEL_TEMPERATURE")
    sub_model = get_model("SUB_MODEL", "SUB_MODEL_TEMPERATURE")
    mm_model = get_model("MM_MODEL", "AI_MODEL_TEMPERATURE") if os.getenv("MM_MODEL") else None
    main_model_router = make_model_router(main_model, mm_model)
    sub_model_router = make_model_router(sub_model, mm_model)
    summary_model = mm_model or main_model
    ddgs_tools = await get_mcp_tools()
    sandbox_backend = get_daytona_backend()
    ensure_sandbox_layout(sandbox_backend)
    main_files_backend = FilesOnlyBackend(sandbox_backend)
    local_tools = [*get_local_tools(), make_download_sandbox_files_tool(sandbox_backend)]

    research_subagent = {
        "name": "general-purpose",
        "description": INTERNET_SUBAGENT_DESCRIPTION,
        "system_prompt": INTERNET_SUBAGENT_PROMPT,
        "model": sub_model,
        "tools": ddgs_tools,
        "middleware": [
            sub_model_router,
            SummarizationMiddleware(
                model=summary_model,
                backend=sandbox_backend,
                history_path_prefix=str(SANDBOX_HISTORY_ROOT),
            ),
            AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
            PatchToolCallsMiddleware(),
            ToolRetryMiddleware(
                max_retries=3,
                tools=DDGS_TOOL_NAMES,
                on_failure="continue",
                initial_delay=1.0,
                backoff_factor=2.0,
            ),
            ModelRetryMiddleware(
                max_retries=3,
                on_failure="continue",
                initial_delay=1.0,
                backoff_factor=2.0,
            ),
        ],
    }

    code_subagent = {
        "name": "code_executor",
        "description": CODE_SUBAGENT_DESCRIPTION,
        "system_prompt": CODE_SUBAGENT_PROMPT,
        "model": main_model,
        "tools": [],
        "middleware": [
            main_model_router,
            FilesystemMiddleware(backend=sandbox_backend),
            SummarizationMiddleware(
                model=summary_model,
                backend=sandbox_backend,
                history_path_prefix=str(SANDBOX_HISTORY_ROOT),
            ),
            AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
            PatchToolCallsMiddleware(),
            ToolRetryMiddleware(
                max_retries=2,
                tools=["execute"],
                on_failure="continue",
                initial_delay=1.0,
                backoff_factor=2.0,
            ),
            ModelRetryMiddleware(
                max_retries=3,
                on_failure="continue",
                initial_delay=1.0,
                backoff_factor=2.0,
            ),
        ],
    }

    middleware = [
        main_model_router,
        TodoListMiddleware(),
        FilesystemMiddleware(backend=main_files_backend),
        SubAgentMiddleware(
            backend=sandbox_backend,
            subagents=[research_subagent, code_subagent],
        ),
        SummarizationMiddleware(
            model=summary_model,
            backend=sandbox_backend,
            history_path_prefix=str(SANDBOX_HISTORY_ROOT),
        ),
        AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
        PatchToolCallsMiddleware(),
        ToolRetryMiddleware(
            max_retries=2,
            on_failure="continue",
            initial_delay=1.0,
            backoff_factor=2.0,
        ),
        ModelRetryMiddleware(
            max_retries=3,
            on_failure="continue",
            initial_delay=1.0,
            backoff_factor=2.0,
        ),
    ]

    return main_model, local_tools, sandbox_backend, middleware


async def main() -> None:
    configure_cli_readline()
    main_model, local_tools, sandbox_backend, middleware = await build_agent()

    async with AsyncSqliteSaver.from_conn_string(CHECKPOINT_DB) as checkpointer:
        agent = create_agent(
            model=main_model,
            tools=local_tools,
            system_prompt=MAIN_AGENT_PROMPT,
            middleware=middleware,
            checkpointer=checkpointer,
            name="main-agent",
        )

        def format_tool_output(content: object, limit: int = 200) -> str:
            def sanitize(value: object) -> object:
                if isinstance(value, dict):
                    sanitized: dict[object, object] = {}
                    for key, item in value.items():
                        if key == "base64" and isinstance(item, str):
                            sanitized[key] = f"<base64:{len(item)} chars>"
                        else:
                            sanitized[key] = sanitize(item)
                    return sanitized

                if isinstance(value, list):
                    return [sanitize(item) for item in value]

                return value

            text = str(sanitize(content)).replace("\n", " ").strip()
            if len(text) <= limit:
                return text
            return text[: limit - 3] + "..."

        def parse_upload_command(command_text: str, command_name: str) -> tuple[list[str], str | None]:
            tokens = shlex.split(command_text)
            if not tokens:
                return [], None

            prompt: str | None = None
            if "--" in tokens:
                split_index = tokens.index("--")
                prompt_tokens = tokens[split_index + 1 :]
                tokens = tokens[:split_index]
                prompt = " ".join(prompt_tokens).strip() or None

            if tokens and tokens[0] == command_name:
                tokens = tokens[1:]

            return tokens, prompt

        pending_attachments: list[dict[str, str]] = []

        def build_sandbox_upload_path(local_path: Path) -> str:
            return str(SANDBOX_UPLOAD_ROOT / local_path.name)

        async def stage_local_files(local_paths: list[str], content_type: str) -> None:
            uploads: list[tuple[str, bytes]] = []
            staged: list[dict[str, str]] = []
            missing: list[str] = []

            for raw_path in local_paths:
                path = Path(raw_path).expanduser().resolve()
                if not path.exists() or not path.is_file():
                    missing.append(raw_path)
                    continue

                mime_type, _ = mimetypes.guess_type(path.name)
                if content_type == "image" and not (mime_type or "").startswith("image/"):
                    print(f"{upload_label('upload-skip')} {path} is not a recognized image file")
                    continue

                file_bytes = path.read_bytes()
                sandbox_path = build_sandbox_upload_path(path)
                uploads.append((sandbox_path, file_bytes))
                staged.append(
                    {
                        "local_path": str(path),
                        "filename": path.name,
                        "sandbox_path": sandbox_path,
                        "mime_type": mime_type or ("image/png" if content_type == "image" else "application/octet-stream"),
                        "base64": base64.b64encode(file_bytes).decode("ascii"),
                        "content_type": content_type,
                    }
                )

            if uploads:
                await sandbox_backend.aupload_files(uploads)
                pending_attachments.extend(staged)
                print(f"{upload_label('upload')} staged {len(staged)} file(s) for the next prompt")
                for item in staged:
                    print(f"  {style(item['local_path'], ANSI_GRAY)} {style('->', ANSI_DIM)} {style(item['sandbox_path'], ANSI_GRAY)}")

            if missing:
                print(f"{upload_label('upload-missing')} {', '.join(missing)}")

        def build_user_content(user_prompt: str) -> str | list[dict[str, str]]:
            if not pending_attachments:
                return user_prompt

            image_attachments = [item for item in pending_attachments if item["content_type"] == "image"]
            file_attachments = [item for item in pending_attachments if item["content_type"] != "image"]

            text_parts = [user_prompt]
            if image_attachments:
                text_parts.append(f"Attached image count: {len(image_attachments)}.")
            if file_attachments:
                sandbox_paths = "\n".join(f"- {item['sandbox_path']}" for item in file_attachments)
                text_parts.append(f"Uploaded sandbox files available at:\n{sandbox_paths}")

            content: list[dict[str, str]] = [{"type": "text", "text": "\n\n".join(text_parts)}]

            for item in pending_attachments:
                block = {
                    "type": item["content_type"],
                    "base64": item["base64"],
                    "mime_type": item["mime_type"],
                    "filename": item["filename"],
                }
                content.append(block)

            return content

        async def run_turn(user_content: str | list[dict[str, str]]) -> None:
            current_agent = None
            seen_tool_calls: set[tuple[str, str]] = set()
            stream_modes = ["messages"]
            if CLI_SHOW_TODOS or CLI_VERBOSE_TOOLS:
                stream_modes.append("updates")

            async for chunk in agent.astream(
                {"messages": [{"role": "user", "content": user_content}]},
                {"configurable": {"thread_id": THREAD_ID}},
                stream_mode=stream_modes,
                subgraphs=True,
                version="v2",
            ):
                if chunk["type"] == "updates":
                    if not (CLI_SHOW_TODOS or CLI_VERBOSE_TOOLS):
                        continue

                    for data in chunk["data"].values():
                        messages = data.get("messages", []) if isinstance(data, dict) else []
                        if hasattr(messages, "value"):
                            messages = messages.value
                        if not isinstance(messages, list):
                            continue

                        for message in messages:
                            if not isinstance(message, ToolMessage):
                                continue

                            if message.name == "write_todos":
                                if not CLI_SHOW_TODOS:
                                    continue
                                print(f"\n{todo_label()} {message.content}")
                                continue

                            if not CLI_VERBOSE_TOOLS:
                                continue

                            tool_output = format_tool_output(message.content)
                            if tool_output:
                                print(f"\n{tool_label(message.name)} {tool_output}")
                    continue

                if chunk["type"] != "messages":
                    continue

                token, metadata = chunk["data"]
                agent_name = metadata.get("lc_agent_name", "main-agent")
                if agent_name != current_agent:
                    if current_agent is not None:
                        print()
                    print(f"\n{section_label(agent_name)}")
                    current_agent = agent_name

                if not isinstance(token, AIMessageChunk):
                    continue

                if CLI_SHOW_TOOL_CALLS and token.tool_call_chunks:
                    for tool_call in token.tool_call_chunks:
                        tool_name = tool_call.get("name")
                        if not tool_name:
                            continue

                        tool_id = tool_call.get("id") or f"{agent_name}:{tool_name}:{tool_call.get('index', 0)}"
                        key = (agent_name, tool_id)
                        args = tool_call.get("args")

                        if key in seen_tool_calls:
                            continue

                        seen_tool_calls.add(key)
                        print(style(f"[tool] {tool_name}", ANSI_DIM, ANSI_BLUE))
                        if CLI_VERBOSE_TOOLS and args:
                            print(style(f"args: {args}", ANSI_DIM, ANSI_GRAY))

                for block in token.content_blocks:
                    if block["type"] == "reasoning":
                        if not CLI_SHOW_REASONING:
                            continue
                        reasoning = block.get("reasoning") or block.get("text")
                        if reasoning:
                            print(reasoning, end="", flush=True)
                    elif block["type"] == "text":
                        text = block.get("text")
                        if text:
                            print(text, end="", flush=True)

            print("\n")

        cli_prompt = " ".join(sys.argv[1:]).strip()
        if cli_prompt:
            await run_turn(cli_prompt)
            return

        print(f"{style('Thread', ANSI_BOLD, ANSI_CYAN)}: {style(THREAD_ID, ANSI_BOLD)}")
        print(style("Type a message and press Enter. Type 'exit' or 'quit' to stop.", ANSI_SOFT_PURPLE))
        print(style("Use /img <path...> or /fl <path...> to stage files. Add `-- <prompt>` to send them immediately.", ANSI_SOFT_PURPLE))

        while True:
            try:
                user_prompt = input("\n\n" + prompt_label()).strip()
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                break

            if not user_prompt:
                user_prompt = DEFAULT_PROMPT

            if user_prompt.lower() in {"exit", "quit"}:
                break

            if user_prompt.startswith("/img "):
                paths, prompt = parse_upload_command(user_prompt, "/img")
                if not paths:
                    print(f"{upload_label('upload')} usage: /img <path1> <path2> ... [-- prompt]")
                    continue
                print(separator())
                await stage_local_files(paths, "image")
                if prompt:
                    user_content = build_user_content(prompt)
                    await run_turn(user_content)
                    pending_attachments.clear()
                continue

            if user_prompt.startswith("/fl "):
                paths, prompt = parse_upload_command(user_prompt, "/fl")
                if not paths:
                    print(f"{upload_label('upload')} usage: /fl <path1> <path2> ... [-- prompt]")
                    continue
                print(separator())
                await stage_local_files(paths, "file")
                if prompt:
                    user_content = build_user_content(prompt)
                    await run_turn(user_content)
                    pending_attachments.clear()
                continue

            print(separator())
            user_content = build_user_content(user_prompt)
            await run_turn(user_content)
            pending_attachments.clear()

        save_cli_history()


if __name__ == "__main__":
    asyncio.run(main())
