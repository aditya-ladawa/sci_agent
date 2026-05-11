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
OPENROUTER_PROMPT_CACHE_TTL = os.getenv("OPENROUTER_PROMPT_CACHE_TTL", "1h")
MULTI_AGENT_CONTEXT_BUDGET_TOKENS = 262_000
MULTI_AGENT_SUMMARIZATION_TRIGGER_TOKENS = int(MULTI_AGENT_CONTEXT_BUDGET_TOKENS * 0.80)
SUMMARIZATION_KEEP_MESSAGES = 30
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
    return max(0, round(sum(len(_message_to_text(message)) for message in messages) / 4))


def _build_subagent_middleware(*, model: ChatOpenAI, model_name: str, diagnostic_label: str) -> list[Any]:
    return [
        DiagnosticSummarizationMiddleware(
            model=model,
            diagnostic_label=diagnostic_label,
            trigger=("tokens", MULTI_AGENT_SUMMARIZATION_TRIGGER_TOKENS),
            keep=("messages", SUMMARIZATION_KEEP_MESSAGES),
            trim_tokens_to_summarize=None,
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


def _build_supervisor_pre_model_hook(*, summary_model: ChatOpenAI):
    async def _pre_model_hook(state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state.get("messages") or [])
        if not messages:
            return {"llm_input_messages": messages}

        if _estimate_messages_tokens(messages) < MULTI_AGENT_SUMMARIZATION_TRIGGER_TOKENS:
            return {"llm_input_messages": messages}

        if len(messages) <= SUMMARIZATION_KEEP_MESSAGES:
            return {"llm_input_messages": messages}

        head_messages = messages[:-SUMMARIZATION_KEEP_MESSAGES]
        tail_messages = messages[-SUMMARIZATION_KEEP_MESSAGES:]
        transcript = "\n\n".join(
            f"[{getattr(message, 'type', type(message).__name__)}]\n{_message_to_text(message)}"
            for message in head_messages
        )
        response = await summary_model.ainvoke(
            [
                SystemMessage(
                    content=(
                        "Summarize the earlier conversation for a research supervisor. You must preserve "
                        "the following categories of information with high fidelity:\n"
                        "1. User intent: the original research question, requested dimensions, and constraints.\n"
                        "2. Research plan: current decomposition, section structure, and what has been assigned.\n"
                        "3. Evidence status: which sections/dimensions have evidence, which are weak or empty.\n"
                        "4. Source distinctions: key sources found, source quality assessments, and which sources "
                        "support which claims.\n"
                        "5. Unresolved gaps: open questions, failed searches, contradictions, and material uncertainties.\n"
                        "6. File/report paths: exact virtual paths for the report, review artifacts, drafts, and "
                        "any offloaded evidence.\n"
                        "7. Citation and review obligations: whether coverage review and citation audit are done, "
                        "pending citation repairs, and any structural citation issues.\n"
                        "8. Key findings: concrete numbers, dates, definitions, and evidence-backed conclusions.\n\n"
                        "Discard: redundant tool outputs, verbose search logs, repeated search snippets, and "
                        "chatty narration. Preserve actionable state, not process narrative."
                    )
                ),
                SystemMessage(content=f"Earlier conversation to summarize:\n\n{transcript}"),
            ]
        )
        summary_text = _message_to_text(response)
        summary_message = SystemMessage(content=f"Conversation summary:\n{summary_text}")
        record_summarization_event(SUPERVISOR_NAME)
        return {"llm_input_messages": [summary_message, *tail_messages]}

    return _pre_model_hook


@asynccontextmanager
async def build_multi_agent_research_agent() -> AsyncIterator[Any]:
    model_name = _required_env("AI_MODEL")
    sub_model_name = os.getenv("SUB_MODEL") or model_name
    supervisor_model = _build_model(model_name=model_name, temperature=AI_MODEL_TEMPERATURE)
    sub_model = _build_model(model_name=sub_model_name, temperature=SUB_MODEL_TEMPERATURE)

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
    "SUMMARIZATION_KEEP_MESSAGES",
    "SUPERVISOR_NAME",
    "TMP_DIR",
    "WORKSPACE_ROOT",
    "build_multi_agent_research_agent",
]
