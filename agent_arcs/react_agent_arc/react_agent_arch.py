from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ModelRetryMiddleware,
    SummarizationMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_openai import ChatOpenAI

from agent_arcs.mcp_and_tools import tavily_mcp_tools
from agent_arcs.react_agent_arc.react_agent_prompts import REACT_AGENT_SYSTEM_PROMPT

MAX_RETRIES = 3
REQUEST_TIMEOUT = 180
AI_MODEL_TEMPERATURE = 0.05
OPENROUTER_PROMPT_CACHE_TTL = os.getenv("OPENROUTER_PROMPT_CACHE_TTL", "1h")
REACT_CONTEXT_BUDGET_TOKENS = 262_000
REACT_SUMMARIZATION_TRIGGER_TOKENS = int(REACT_CONTEXT_BUDGET_TOKENS * 0.80)
SUMMARIZATION_KEEP_MESSAGES = 30
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DR_BENCH_ROOT = PROJECT_ROOT / "dr_bench"
DEFAULT_WORKSPACE_ROOT = DR_BENCH_ROOT / "q0" / "react_agent_arc"
WORKSPACE_ROOT = Path(os.getenv("REACT_AGENT_WORKSPACE_ROOT", str(DEFAULT_WORKSPACE_ROOT))).resolve()
REPORTS_DIR = WORKSPACE_ROOT / "report"
TMP_DIR = WORKSPACE_ROOT / "tmp"
DRAFTS_DIR = TMP_DIR / "drafts"
REVIEW_DIR = TMP_DIR / "review"
CHECKPOINTER_DB_PATH = DR_BENCH_ROOT / ".langgraph" / "checkpoints.sqlite"


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Set {name} in .env.")
    return value


def _openrouter_extra_body(model_name: str) -> dict[str, Any] | None:
    normalized = model_name.lower()
    if normalized.startswith("anthropic/claude"):
        return {"cache_control": {"type": "ephemeral", "ttl": OPENROUTER_PROMPT_CACHE_TTL}}
    return None


def _supports_explicit_prompt_caching(model_name: str) -> bool:
    normalized = model_name.lower()
    return normalized.startswith("anthropic/claude")


class PromptCachingMiddleware(AgentMiddleware):
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled

    def _with_prompt_cache(self, request: ModelRequest) -> ModelRequest:
        if not self.enabled:
            return request

        content_blocks = list(request.system_message.content_blocks)
        if not content_blocks:
            return request

        cached_blocks: list[dict[str, Any]] = []
        cache_added = False
        last_index = len(content_blocks) - 1
        for index, block in enumerate(content_blocks):
            updated = dict(block)
            if index == last_index and updated.get("type") == "text":
                updated.setdefault("cache_control", {"type": "ephemeral"})
                cache_added = True
            cached_blocks.append(updated)

        if not cache_added:
            return request

        return request.override(
            system_message=request.system_message.model_copy(update={"content": cached_blocks}),
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._with_prompt_cache(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._with_prompt_cache(request))


def _build_model(model_name: str, temperature: float) -> ChatOpenAI:
    kwargs: dict[str, Any] = {}
    extra_body = _openrouter_extra_body(model_name)
    if extra_body is not None:
        kwargs["extra_body"] = extra_body

    return ChatOpenAI(
        model=model_name,
        api_key=_required_env("OPENROUTER_API_KEY"),
        base_url=_required_env("OPENROUTER_BASE_URL"),
        temperature=temperature,
        max_retries=MAX_RETRIES,
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )


def _ensure_safe_path(path: str) -> Path:
    virtual_path = path if path.startswith("/") else "/" + path
    if ".." in virtual_path or virtual_path.startswith("~"):
        raise ValueError("Path traversal is not allowed.")

    full_path = (WORKSPACE_ROOT / virtual_path.lstrip("/")).resolve()
    try:
        full_path.relative_to(WORKSPACE_ROOT)
    except ValueError:
        raise ValueError(f"Access denied: {path} is outside the workspace.") from None
    return full_path


def _virtual_path(path: Path) -> str:
    return "/" + str(path.relative_to(WORKSPACE_ROOT))


def _format_with_line_numbers(content: str, start_line: int = 1) -> str:
    return "\n".join(f"{line_no:6d}\t{line}" for line_no, line in enumerate(content.splitlines(), start=start_line))


def _format_tool_failure(error: Exception) -> str:
    return (
        f"Tool failed after all retry attempts with {type(error).__name__}: {error}. "
        "Retry with a changed strategy or mark the evidence gap explicitly."
    )


@tool(parse_docstring=True)
def list_files(path: str = "/") -> list[dict[str, Any]]:
    """List files and directories under the workspace.

    Args:
        path: Virtual directory path to list.
    """

    try:
        dir_path = _ensure_safe_path(path)
        if not dir_path.exists() or not dir_path.is_dir():
            return []

        results: list[dict[str, Any]] = []
        for item in sorted(dir_path.iterdir()):
            try:
                is_file = item.is_file()
                is_dir = item.is_dir()
                item_path = _virtual_path(item)
                if is_dir:
                    item_path += "/"
                stat = item.stat()
                results.append(
                    {
                        "path": item_path,
                        "is_dir": is_dir,
                        "size": stat.st_size if is_file else 0,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    }
                )
            except OSError:
                continue
        return results
    except ValueError as error:
        return [{"error": str(error)}]


@tool(parse_docstring=True)
def read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read a UTF-8 text file with line numbers.

    Args:
        path: Virtual file path to read.
        offset: Zero-based line offset.
        limit: Maximum number of lines to return.
    """

    try:
        file_path = _ensure_safe_path(path)
        if not file_path.exists() or not file_path.is_file():
            return f"Error: File '{path}' not found."

        content = file_path.read_text(encoding="utf-8")
        if not content.strip():
            return "File exists but has empty contents."

        lines = content.splitlines()
        if offset >= len(lines):
            return f"Error: Line offset {offset} exceeds file length ({len(lines)} lines)."

        selected_lines = lines[offset : offset + limit]
        return _format_with_line_numbers("\n".join(selected_lines), start_line=offset + 1)
    except ValueError as error:
        return f"Error: {error}"
    except (OSError, UnicodeDecodeError) as error:
        return f"Error reading file '{path}': {error}"


@tool(parse_docstring=True)
def write_file(file_path: str, content: str) -> str:
    """Create a new UTF-8 text file.

    Args:
        file_path: Virtual file path to create.
        content: Content to write.
    """

    try:
        target = _ensure_safe_path(file_path)
        if target.exists():
            return f"Error: Cannot write to {file_path} because it already exists. Use edit_file to modify it."

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Successfully wrote to {file_path}."
    except ValueError as error:
        return f"Error: {error}"
    except (OSError, UnicodeEncodeError) as error:
        return f"Error writing file '{file_path}': {error}"


@tool(parse_docstring=True)
def edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Edit a file by replacing exact string occurrences.

    Args:
        file_path: Virtual file path to edit.
        old_string: Exact string to replace.
        new_string: Replacement string.
        replace_all: Whether to replace all occurrences.
    """

    try:
        target = _ensure_safe_path(file_path)
        if not target.exists() or not target.is_file():
            return f"Error: File '{file_path}' not found."

        content = target.read_text(encoding="utf-8")
        occurrences = content.count(old_string)
        if occurrences == 0:
            return f"Error: String not found in file: '{old_string}'."
        if occurrences > 1 and not replace_all:
            return (
                f"Error: String appears {occurrences} times. Set replace_all=True or provide more specific context."
            )

        updated = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
        target.write_text(updated, encoding="utf-8")
        return f"Successfully edited {file_path} ({occurrences if replace_all else 1} occurrence(s) replaced)."
    except ValueError as error:
        return f"Error: {error}"
    except (OSError, UnicodeDecodeError, UnicodeEncodeError) as error:
        return f"Error editing file '{file_path}': {error}"


@tool(parse_docstring=True)
def search_files(pattern: str, path: str = "/", file_pattern: str | None = None) -> str:
    """Search workspace file contents with a regex pattern.

    Args:
        pattern: Regex pattern to search for.
        path: Virtual file or directory path to search under.
        file_pattern: Optional glob filter such as *.md.
    """

    try:
        regex = re.compile(pattern)
    except re.error as error:
        return f"Invalid regex pattern: {error}"

    try:
        base_path = _ensure_safe_path(path)
        if not base_path.exists():
            return "No matches found."

        search_root = base_path if base_path.is_dir() else base_path.parent
        results: list[str] = []
        for candidate in search_root.rglob("*"):
            if not candidate.is_file():
                continue
            if file_pattern and not candidate.match(file_pattern):
                continue
            try:
                content = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError, OSError):
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                if regex.search(line):
                    results.append(f"{_virtual_path(candidate)}:{line_number}: {line}")
                    if len(results) >= 100:
                        return "\n".join(results)
        return "\n".join(results) if results else "No matches found."
    except ValueError as error:
        return f"Error: {error}"


@tool(parse_docstring=True)
def find_files(pattern: str, path: str = "/") -> str:
    """Find files matching a glob pattern.

    Args:
        pattern: Glob pattern such as *.md or **/*.json.
        path: Virtual directory path to search under.
    """

    try:
        search_path = _ensure_safe_path(path)
        if not search_path.exists() or not search_path.is_dir():
            return "No files found."

        matches: list[tuple[str, float]] = []
        for candidate in search_path.rglob(pattern):
            if candidate.is_file():
                matches.append((_virtual_path(candidate), candidate.stat().st_mtime))
        if not matches:
            return "No files found."
        matches.sort(key=lambda item: item[1], reverse=True)
        return "\n".join(path for path, _ in matches)
    except ValueError as error:
        return f"Error: {error}"


def _build_middleware(*, model: ChatOpenAI, model_name: str) -> list[Any]:
    return [
        SummarizationMiddleware(
            model=model,
            trigger=("tokens", REACT_SUMMARIZATION_TRIGGER_TOKENS),
            keep=("messages", SUMMARIZATION_KEEP_MESSAGES),
            trim_tokens_to_summarize=None,
        ),
        PromptCachingMiddleware(enabled=_supports_explicit_prompt_caching(model_name)),
        PatchToolCallsMiddleware(),
        ToolRetryMiddleware(
            max_retries=3,
            backoff_factor=2.0,
            initial_delay=1.0,
            on_failure=_format_tool_failure,
        ),
        ModelRetryMiddleware(max_retries=3, backoff_factor=2.0, initial_delay=1.0),
    ]


@asynccontextmanager
async def build_react_research_agent() -> AsyncIterator[Any]:
    model_name = _required_env("AI_MODEL")
    model = _build_model(
        model_name=model_name,
        temperature=AI_MODEL_TEMPERATURE,
    )

    CHECKPOINTER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINTER_DB_PATH)) as checkpointer:
        await checkpointer.setup()
        async with tavily_mcp_tools() as internet_tools:
            agent = create_agent(
                model=model,
                tools=[
                    list_files,
                    read_file,
                    write_file,
                    edit_file,
                    search_files,
                    find_files,
                    *internet_tools,
                ],
                system_prompt=REACT_AGENT_SYSTEM_PROMPT,
                middleware=_build_middleware(model=model, model_name=model_name),
                checkpointer=checkpointer,
                name="react-agent",
            )
            yield agent


__all__ = [
    "AI_MODEL_TEMPERATURE",
    "CHECKPOINTER_DB_PATH",
    "DEFAULT_WORKSPACE_ROOT",
    "DR_BENCH_ROOT",
    "DRAFTS_DIR",
    "PromptCachingMiddleware",
    "PROJECT_ROOT",
    "REACT_SUMMARIZATION_TRIGGER_TOKENS",
    "REPORTS_DIR",
    "REQUEST_TIMEOUT",
    "REVIEW_DIR",
    "SUMMARIZATION_KEEP_MESSAGES",
    "TMP_DIR",
    "WORKSPACE_ROOT",
    "build_react_research_agent",
    "edit_file",
    "find_files",
    "list_files",
    "read_file",
    "search_files",
    "write_file",
]
