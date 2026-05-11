from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph_supervisor import create_supervisor
from langchain_openai import ChatOpenAI

from agent_arcs.diagnostic_metrics import (
    DiagnosticModelRetryMiddleware,
    DiagnosticSummarizationMiddleware,
    DiagnosticToolRetryMiddleware,
    record_summarization_event,
)
from agent_arcs.mcp_and_tools import ddgs_mcp_tools
from agent_arcs.multi_agent_arc.multi_agent_prompts import (
    MULTI_AGENT_RESEARCH_PROMPT,
    MULTI_AGENT_SCOUT_PROMPT,
    MULTI_AGENT_SYSTEM_PROMPT,
)
from agent_arcs.react_agent_arc.react_agent_arch import (
    edit_file,
    find_files,
    list_files,
    read_file,
    search_files,
    write_file,
)

MAX_RETRIES = 3
REQUEST_TIMEOUT = 180
AI_MODEL_TEMPERATURE = 0.00
SUB_MODEL_TEMPERATURE = 0.6
SUB_MODEL_REASONING_EFFORT = "low"
OPENROUTER_PROMPT_CACHE_TTL = os.getenv("OPENROUTER_PROMPT_CACHE_TTL", "1h")
MULTI_AGENT_CONTEXT_BUDGET_TOKENS = 262_000
MULTI_AGENT_SUMMARIZATION_TRIGGER_TOKENS = int(MULTI_AGENT_CONTEXT_BUDGET_TOKENS * 0.60)
SUMMARIZATION_KEEP_MESSAGES = 30
SUPERVISOR_MIN_TAIL_MESSAGES = 8
SUPERVISOR_TOOL_MESSAGE_CHAR_BUDGET = 4_000
SUPERVISOR_SUMMARY_TRANSCRIPT_MAX_CHARS = 80_000
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DR_BENCH_ROOT = PROJECT_ROOT / "dr_bench"
DEFAULT_WORKSPACE_ROOT = DR_BENCH_ROOT / "q0" / "multi_agent_arc"
WORKSPACE_ROOT = Path(os.getenv("MULTI_AGENT_WORKSPACE_ROOT", str(DEFAULT_WORKSPACE_ROOT))).resolve()
REPORTS_DIR = WORKSPACE_ROOT / "report"
TMP_DIR = WORKSPACE_ROOT / "tmp"
DRAFTS_DIR = TMP_DIR / "drafts"
REVIEW_DIR = TMP_DIR / "review"
CHECKPOINTER_DB_PATH = DR_BENCH_ROOT / ".langgraph" / "checkpoints.sqlite"
SUPERVISOR_NAME = "multi_agent_supervisor"
SCOUT_AGENT_NAME = "scout_agent"
RESEARCH_AGENT_NAME = "research_agent"


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
        super().__init__()
        self.enabled = enabled

    def _with_prompt_cache(self, request: ModelRequest) -> ModelRequest:
        if not self.enabled:
            return request
        if request.system_message is None:
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


def _build_model(
    model_name: str,
    temperature: float,
    *,
    reasoning_effort: str | None = None,
) -> ChatOpenAI:
    kwargs: dict[str, Any] = {}
    extra_body = _openrouter_extra_body(model_name)
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort

    return ChatOpenAI(
        model=model_name,
        api_key=_required_env("OPENROUTER_API_KEY"),
        base_url=_required_env("OPENROUTER_BASE_URL"),
        temperature=temperature,
        max_retries=MAX_RETRIES,
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )


def _format_tool_failure(error: Exception) -> str:
    return (
        f"Tool failed after all retry attempts with {type(error).__name__}: {error}. "
        "Retry with a changed strategy or mark the evidence gap explicitly."
    )


def _message_to_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return str(content)


def _estimate_messages_tokens(messages: list[BaseMessage]) -> int:
    total_chars = sum(len(_message_to_text(message)) for message in messages)
    return max(1, round(total_chars / 2.5))


def _split_messages_for_summary(messages: list[BaseMessage]) -> tuple[list[BaseMessage], list[BaseMessage]]:
    if len(messages) <= 1:
        return [], list(messages)

    tail_keep = min(SUMMARIZATION_KEEP_MESSAGES, len(messages) - 1)
    if len(messages) <= SUMMARIZATION_KEEP_MESSAGES:
        tail_keep = min(max(SUPERVISOR_MIN_TAIL_MESSAGES, 1), len(messages) - 1)

    if tail_keep <= 0:
        return [], list(messages)

    return messages[:-tail_keep], messages[-tail_keep:]


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n...[truncated]"


def _truncate_message_content(message: BaseMessage) -> BaseMessage:
    msg_type = getattr(message, "type", None)
    if msg_type == "human":
        return message
    if msg_type == "system":
        return message

    truncated = _truncate_text(_message_to_text(message), SUPERVISOR_TOOL_MESSAGE_CHAR_BUDGET)
    if truncated == _message_to_text(message):
        return message
    return message.model_copy(update={"content": truncated})


def _truncate_tail_tool_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    return [_truncate_message_content(message) for message in messages]


def _build_subagent_middleware(*, model: ChatOpenAI, model_name: str, diagnostic_label: str) -> list[Any]:
    return [
        DiagnosticSummarizationMiddleware(
            model=model,
            diagnostic_label=diagnostic_label,
            trigger=("tokens", MULTI_AGENT_SUMMARIZATION_TRIGGER_TOKENS),
            keep=("messages", SUMMARIZATION_KEEP_MESSAGES),
            trim_tokens_to_summarize=50_000,
        ),
        PromptCachingMiddleware(enabled=_supports_explicit_prompt_caching(model_name)),
        PatchToolCallsMiddleware(),
        DiagnosticToolRetryMiddleware(
            max_retries=3,
            backoff_factor=2.0,
            initial_delay=1.0,
            on_failure=_format_tool_failure,
        ),
        DiagnosticModelRetryMiddleware(max_retries=3, backoff_factor=2.0, initial_delay=1.0),
    ]


def _build_supervisor_pre_model_hook(*, summary_model: ChatOpenAI, model_name: str):
    summarizer = DiagnosticSummarizationMiddleware(
        model=summary_model,
        diagnostic_label=SUPERVISOR_NAME,
        trigger=("tokens", MULTI_AGENT_SUMMARIZATION_TRIGGER_TOKENS),
        keep=("messages", SUMMARIZATION_KEEP_MESSAGES),
        trim_tokens_to_summarize=50_000,
    )

    async def _pre_model_hook(state: dict[str, Any]) -> dict[str, Any]:
        result = await summarizer.abefore_model(state, runtime=None)
        if result is not None:
            return result
        return {"llm_input_messages": state.get("messages", [])}

    return _pre_model_hook


@asynccontextmanager
async def build_multi_agent_research_agent() -> AsyncIterator[Any]:
    model_name = _required_env("AI_MODEL")
    sub_model_name = os.getenv("SUB_MODEL") or model_name
    supervisor_model = _build_model(model_name=model_name, temperature=AI_MODEL_TEMPERATURE)
    sub_model = _build_model(
        model_name=sub_model_name,
        temperature=SUB_MODEL_TEMPERATURE,
        reasoning_effort=SUB_MODEL_REASONING_EFFORT,
    )

    CHECKPOINTER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINTER_DB_PATH)) as checkpointer:
        await checkpointer.setup()
        async with ddgs_mcp_tools() as internet_tools:
            scout_agent = create_agent(
                model=sub_model,
                tools=list(internet_tools),
                system_prompt=MULTI_AGENT_SCOUT_PROMPT,
                middleware=_build_subagent_middleware(
                    model=sub_model,
                    model_name=sub_model_name,
                    diagnostic_label=SCOUT_AGENT_NAME,
                ),
                name=SCOUT_AGENT_NAME,
            )
            research_agent = create_agent(
                model=sub_model,
                tools=list(internet_tools),
                system_prompt=MULTI_AGENT_RESEARCH_PROMPT,
                middleware=_build_subagent_middleware(
                    model=sub_model,
                    model_name=sub_model_name,
                    diagnostic_label=RESEARCH_AGENT_NAME,
                ),
                name=RESEARCH_AGENT_NAME,
            )

            workflow = create_supervisor(
                [scout_agent, research_agent],
                model=supervisor_model,
                tools=[list_files, read_file, write_file, edit_file, search_files, find_files],
                prompt=MULTI_AGENT_SYSTEM_PROMPT,
                pre_model_hook=_build_supervisor_pre_model_hook(summary_model=supervisor_model),
                parallel_tool_calls=False,
                output_mode="full_history",
                handoff_tool_prefix="delegate_to_",
                add_handoff_messages=True,
                add_handoff_back_messages=True,
                supervisor_name=SUPERVISOR_NAME,
                include_agent_name="inline",
            )
            app = workflow.compile(checkpointer=checkpointer)
            yield app


__all__ = [
    "AI_MODEL_TEMPERATURE",
    "CHECKPOINTER_DB_PATH",
    "DEFAULT_WORKSPACE_ROOT",
    "DR_BENCH_ROOT",
    "DRAFTS_DIR",
    "MULTI_AGENT_CONTEXT_BUDGET_TOKENS",
    "MULTI_AGENT_SUMMARIZATION_TRIGGER_TOKENS",
    "PROJECT_ROOT",
    "REPORTS_DIR",
    "REQUEST_TIMEOUT",
    "RESEARCH_AGENT_NAME",
    "REVIEW_DIR",
    "SCOUT_AGENT_NAME",
    "SUB_MODEL_REASONING_EFFORT",
    "SUMMARIZATION_KEEP_MESSAGES",
    "SUPERVISOR_NAME",
    "TMP_DIR",
    "WORKSPACE_ROOT",
    "build_multi_agent_research_agent",
]
