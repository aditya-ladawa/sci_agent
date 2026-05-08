from __future__ import annotations

import os
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRetryMiddleware, TodoListMiddleware, ToolRetryMiddleware
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_openai import ChatOpenAI

from agent_arcs.deep_agent_arc.deep_agent_prompts import MAIN_AGENT_SYSTEM_PROMPT, RESEARCH_SUBAGENT_SYSTEM_PROMPT
from agent_arcs.mcp_and_tools import tavily_mcp_tools

MAX_RETRIES = 3
REQUEST_TIMEOUT = 180
AI_MODEL_TEMPERATURE = 0.05
SUB_MODEL_TEMPERATURE = 0.7
OPENROUTER_PROMPT_CACHE_TTL = os.getenv("OPENROUTER_PROMPT_CACHE_TTL", "1h")
MAIN_SUMMARIZATION_TRIGGER_TOKENS = int(262_000 * 0.85)
SUBAGENT_SUMMARIZATION_TRIGGER_TOKENS = int(262_000 * 0.85)
SUMMARIZATION_KEEP_MESSAGES = 30
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DR_BENCH_ROOT = PROJECT_ROOT / "dr_bench"
DEFAULT_WORKSPACE_ROOT = DR_BENCH_ROOT / "q0" / "deep_agent_arc"
WORKSPACE_ROOT = Path(os.getenv("DEEP_AGENT_WORKSPACE_ROOT", str(DEFAULT_WORKSPACE_ROOT))).resolve()
RUN_ROOT = WORKSPACE_ROOT.parent if WORKSPACE_ROOT.name.endswith("_workspace") else WORKSPACE_ROOT
REPORTS_DIR = WORKSPACE_ROOT / "report"
LARGE_TOOL_RESULTS_DIR = WORKSPACE_ROOT / "large_tool_results"
TMP_DIR = WORKSPACE_ROOT / "tmp"
EVIDENCE_DIR = TMP_DIR / "evidence"
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
        "Use think_tool to diagnose the failure, preserve this exact error in relevant notes, "
        "then retry with a changed strategy or mark the evidence gap explicitly."
    )


@tool(parse_docstring=True)
def think_tool(reflection: str) -> str:
    """Tool for strategic reflection during long-running agent work.

    Use this as a lightweight checkpoint for concise status/evaluation notes. Do not write long
    chains of reasoning here.

    Args:
        reflection: Concise operational reflection on progress, evidence quality, gaps, and next action.

    Returns:
        Confirmation that reflection was recorded for decision-making.
    """
    return f"Reflection recorded: {reflection}"


def _build_main_model() -> ChatOpenAI:
    return _build_model(model_name=_required_env("AI_MODEL"), temperature=AI_MODEL_TEMPERATURE)


def _build_sub_model() -> ChatOpenAI:
    return _build_model(model_name=_required_env("SUB_MODEL"), temperature=SUB_MODEL_TEMPERATURE)


def _build_filesystem_backend() -> FilesystemBackend:
    return FilesystemBackend(root_dir=str(WORKSPACE_ROOT), virtual_mode=True)


def _build_main_middleware(*, backend: FilesystemBackend, summary_model: ChatOpenAI, research_subagent: dict[str, Any]) -> list[Any]:
    return [
        TodoListMiddleware(),
        FilesystemMiddleware(backend=backend),
        SubAgentMiddleware(backend=backend, subagents=[research_subagent]),
        SummarizationMiddleware(
            model=summary_model,
            backend=backend,
            trigger=("tokens", MAIN_SUMMARIZATION_TRIGGER_TOKENS),
            keep=("messages", SUMMARIZATION_KEEP_MESSAGES),
            trim_tokens_to_summarize=None,
            truncate_args_settings={
                "trigger": ("tokens", MAIN_SUMMARIZATION_TRIGGER_TOKENS),
                "keep": ("messages", SUMMARIZATION_KEEP_MESSAGES),
                "max_length": 2000,
                "truncation_text": "...(argument truncated)",
            },
        ),
        PatchToolCallsMiddleware(),
        ToolRetryMiddleware(max_retries=3, backoff_factor=2.0, initial_delay=1.0, on_failure=_format_tool_failure),
        ModelRetryMiddleware(max_retries=3, backoff_factor=2.0, initial_delay=1.0),
    ]


def _build_subagent_middleware(*, backend: FilesystemBackend, summary_model: ChatOpenAI) -> list[Any]:
    return [
        FilesystemMiddleware(backend=backend),
        SummarizationMiddleware(
            model=summary_model,
            backend=backend,
            trigger=("tokens", SUBAGENT_SUMMARIZATION_TRIGGER_TOKENS),
            keep=("messages", SUMMARIZATION_KEEP_MESSAGES),
            trim_tokens_to_summarize=None,
            truncate_args_settings={
                "trigger": ("tokens", SUBAGENT_SUMMARIZATION_TRIGGER_TOKENS),
                "keep": ("messages", SUMMARIZATION_KEEP_MESSAGES),
                "max_length": 2000,
                "truncation_text": "...(argument truncated)",
            },
        ),
        PatchToolCallsMiddleware(),
        ToolRetryMiddleware(max_retries=3, backoff_factor=2.0, initial_delay=1.0, on_failure=_format_tool_failure),
        ModelRetryMiddleware(max_retries=3, backoff_factor=2.0, initial_delay=1.0),
    ]


def _build_research_subagent(*, backend: FilesystemBackend, internet_tools: Sequence[Any], model: ChatOpenAI) -> dict[str, Any]:
    return {
        "name": "research-agent",
        "description": "Researches bounded evidence questions using Tavily Internet Search MCP tools and returns section-ready handoffs.",
        "system_prompt": RESEARCH_SUBAGENT_SYSTEM_PROMPT,
        "model": model,
        "tools": [think_tool, *internet_tools],
        "middleware": _build_subagent_middleware(backend=backend, summary_model=model),
    }


@asynccontextmanager
async def build_deep_research_agent() -> AsyncIterator[Any]:
    main_model = _build_main_model()
    sub_model = _build_sub_model()
    backend = _build_filesystem_backend()

    CHECKPOINTER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    LARGE_TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINTER_DB_PATH)) as checkpointer:
        await checkpointer.setup()
        async with tavily_mcp_tools() as internet_tools:
            research_subagent = _build_research_subagent(backend=backend, internet_tools=internet_tools, model=sub_model)
            agent = create_agent(
                model=main_model,
                tools=[think_tool],
                system_prompt=MAIN_AGENT_SYSTEM_PROMPT,
                middleware=_build_main_middleware(
                    backend=backend,
                    summary_model=main_model,
                    research_subagent=research_subagent,
                ),
                checkpointer=checkpointer,
                name="main-agent",
            )
            yield agent


__all__ = [
    "AI_MODEL_TEMPERATURE",
    "CHECKPOINTER_DB_PATH",
    "DEFAULT_WORKSPACE_ROOT",
    "DR_BENCH_ROOT",
    "DRAFTS_DIR",
    "EVIDENCE_DIR",
    "LARGE_TOOL_RESULTS_DIR",
    "MAIN_SUMMARIZATION_TRIGGER_TOKENS",
    "MAX_RETRIES",
    "PROJECT_ROOT",
    "REPORTS_DIR",
    "REQUEST_TIMEOUT",
    "RUN_ROOT",
    "REVIEW_DIR",
    "SUB_MODEL_TEMPERATURE",
    "SUBAGENT_SUMMARIZATION_TRIGGER_TOKENS",
    "SUMMARIZATION_KEEP_MESSAGES",
    "TMP_DIR",
    "WORKSPACE_ROOT",
    "build_deep_research_agent",
    "think_tool",
]
