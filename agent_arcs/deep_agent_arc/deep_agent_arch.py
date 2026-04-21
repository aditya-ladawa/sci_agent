from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelRetryMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_openai import ChatOpenAI

from agent_arcs.deep_agent_arc.deep_agent_prompts import (
    MAIN_AGENT_SYSTEM_PROMPT,
    RESEARCH_SUBAGENT_SYSTEM_PROMPT,
)
from agent_arcs.mcp_and_tools import tavily_mcp_tools

MAX_RETRIES = 10
REQUEST_TIMEOUT = 120
AI_MODEL_TEMPERATURE = 0.05
SUB_MODEL_TEMPERATURE = 0.7
SUMMARIZATION_TRIGGER_TOKENS = 170_000
SUMMARIZATION_KEEP_MESSAGES = 40
WORKSPACE_ROOT = Path(__file__).resolve().parent
REPORTS_DIR = WORKSPACE_ROOT / "reports"
LARGE_TOOL_RESULTS_DIR = WORKSPACE_ROOT / "large_tool_results"
CHECKPOINTER_DB_PATH = WORKSPACE_ROOT / ".langgraph" / "checkpoints.sqlite"


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Set {name}.")
    return value


def _build_model(model_name: str, temperature: float) -> ChatOpenAI:
    return ChatOpenAI(
        model=model_name,
        api_key=_required_env("OPENROUTER_API_KEY"),
        base_url=_required_env("OPENROUTER_BASE_URL"),
        temperature=temperature,
        max_retries=MAX_RETRIES,
        timeout=REQUEST_TIMEOUT,
    )


@tool(parse_docstring=True)
def think_tool(reflection: str) -> str:
    """Tool for strategic reflection on research progress and decision-making.

    Use this tool after each search to analyze results and plan next steps systematically.
    This creates a deliberate pause in the research workflow for quality decision-making.

    When to use:
    - After receiving search results: What key information did I find?
    - Before deciding next steps: Do I have enough to answer comprehensively?
    - When assessing research gaps: What specific information am I still missing?
    - Before concluding research: Can I provide a complete answer now?

    Reflection should address:
    1. Analysis of current findings - What concrete information have I gathered?
    2. Gap assessment - What crucial information is still missing?
    3. Quality evaluation - Do I have sufficient evidence/examples for a good answer?
    4. Strategic decision - Should I continue searching or provide my answer?

    Args:
        reflection: Your detailed reflection on research progress, findings, gaps, and next steps

    Returns:
        Confirmation that reflection was recorded for decision-making
    """
    return f"Reflection recorded: {reflection}"


def _build_main_model() -> ChatOpenAI:
    return _build_model(
        model_name=_required_env("AI_MODEL"),
        temperature=AI_MODEL_TEMPERATURE,
    )


def _build_sub_model() -> ChatOpenAI:
    return _build_model(
        model_name=_required_env("SUB_MODEL"),
        temperature=SUB_MODEL_TEMPERATURE,
    )


def _build_main_middleware(
    *,
    backend: FilesystemBackend,
    summary_model: ChatOpenAI,
    research_subagent: dict[str, Any],
) -> list[Any]:
    return [
        TodoListMiddleware(),
        FilesystemMiddleware(backend=backend),
        SubAgentMiddleware(
            backend=backend,
            subagents=[research_subagent],
        ),
        SummarizationMiddleware(
            model=summary_model,
            trigger=("tokens", SUMMARIZATION_TRIGGER_TOKENS),
            keep=("messages", SUMMARIZATION_KEEP_MESSAGES),
        ),
        PatchToolCallsMiddleware(),
        ToolRetryMiddleware(max_retries=3, backoff_factor=2.0, initial_delay=1.0),
        ModelRetryMiddleware(max_retries=3, backoff_factor=2.0, initial_delay=1.0),
    ]


def _build_subagent_middleware(
    *,
    backend: FilesystemBackend,
    summary_model: ChatOpenAI,
) -> list[Any]:
    return [
        FilesystemMiddleware(backend=backend),
        SummarizationMiddleware(
            model=summary_model,
            trigger=("tokens", SUMMARIZATION_TRIGGER_TOKENS),
            keep=("messages", SUMMARIZATION_KEEP_MESSAGES),
        ),
        PatchToolCallsMiddleware(),
        ToolRetryMiddleware(max_retries=3, backoff_factor=2.0, initial_delay=1.0),
        ModelRetryMiddleware(max_retries=3, backoff_factor=2.0, initial_delay=1.0),
    ]


@asynccontextmanager
async def build_deep_research_agent() -> AsyncIterator[Any]:
    main_model = _build_main_model()
    sub_model = _build_sub_model()
    backend = FilesystemBackend(
        root_dir=str(WORKSPACE_ROOT),
        virtual_mode=True,
    )
    CHECKPOINTER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    LARGE_TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINTER_DB_PATH)) as checkpointer:
        await checkpointer.setup()

        async with tavily_mcp_tools() as internet_tools:
            research_subagent = {
                "name": "research-agent",
                "description": "Used to research narrow questions in depth using Tavily MCP tools.",
                "system_prompt": RESEARCH_SUBAGENT_SYSTEM_PROMPT,
                "model": sub_model,
                "tools": [think_tool, *internet_tools],
                "middleware": _build_subagent_middleware(
                    backend=backend,
                    summary_model=sub_model,
                ),
            }

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
    "LARGE_TOOL_RESULTS_DIR",
    "MAX_RETRIES",
    "REPORTS_DIR",
    "REQUEST_TIMEOUT",
    "SUB_MODEL_TEMPERATURE",
    "SUMMARIZATION_KEEP_MESSAGES",
    "SUMMARIZATION_TRIGGER_TOKENS",
    "WORKSPACE_ROOT",
    "build_deep_research_agent",
    "think_tool",
]
