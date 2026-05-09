from __future__ import annotations

import asyncio
import ast
import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessageChunk

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_arcs.deep_agent_arc.deep_agent_arch import (
    CONVERSATION_HISTORY_DIR,
    LARGE_TOOL_RESULTS_DIR,
    REPORTS_DIR,
    WORKSPACE_ROOT,
    build_deep_research_agent,
)
from agent_arcs.diagnostic_metrics import (
    estimate_text_tokens,
    extract_urls,
    reset_runtime_diagnostics,
    snapshot_runtime_diagnostics,
    source_overlap_between_handoffs,
    summarize_handoffs,
    search_tool_efficiency,
)
from agent_arcs.langfuse_tracing import LangfuseTraceConfig

DEFAULT_PROMPT = "Write a concise note on how Model context protocol works, ue only 3 todos at max"

DEFAULT_THREAD_ID = "deep-agent-smoke-test35"
RUN_METRICS_DIR = WORKSPACE_ROOT / "run_metrics"


USE_COLOR = sys.stdout.isatty() and os.getenv("NO_COLOR") is None
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
BLUE = "\033[94m"
CYAN = "\033[96m"
GRAY = "\033[90m"
GREEN = "\033[92m"
MAGENTA = "\033[95m"
YELLOW = "\033[93m"


@dataclass(slots=True)
class StreamRunResult:
    article_text: str
    metrics_path: Path
    prompt: str
    report_path: Path | None
    run_id: str
    thread_id: str


def _style(text: str, *codes: str) -> str:
    if not USE_COLOR or not codes:
        return text
    return "".join(codes) + text + RESET


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _truncate(text: str, limit: int = 220) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _report_placeholder_issues(article_text: str) -> list[str]:
    lowered = article_text.lower()
    checks = {
        "contains html placeholder comments": "<!-- placeholder",
        "contains placeholder table cells": "| <!-- placeholder -->",
        "contains placeholder marker": "placeholder",
        "contains unfinished-after-research placeholder": "will be completed after all research",
        "contains unfinished-after-sections placeholder": "will be completed after all sections",
        "contains unfinished-references placeholder": "references will be populated",
        "contains generic placeholder marker": "*this section will be completed",
    }
    return [label for label, needle in checks.items() if needle in lowered]


def _report_is_complete(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return bool(text.strip()) and not _report_placeholder_issues(text)


def _looks_like_tool_error(output: str) -> bool:
    normalized = output.strip().lower()
    return normalized.startswith("error:") or "tool failed after all retry attempts" in normalized


def _task_handoff_entry(*, subagent_type: str | None, output: str) -> dict[str, Any]:
    urls = extract_urls(output)
    return {
        "subagent_type": subagent_type or "unknown",
        "estimated_tokens": estimate_text_tokens(output),
        "char_count": len(output),
        "url_count": len(urls),
        "unique_url_count": len(set(urls)),
        "urls": urls,
    }


DDGS_TOOL_NAMES = {"search_text", "search_images", "search_news", "search_videos", "search_books", "extract_content"}


def _is_ddgs_tool(tool_name: str) -> bool:
    return tool_name in DDGS_TOOL_NAMES


def _format_todos(content: object) -> str:
    if isinstance(content, str):
        raw = content.strip()
        try:
            parsed = json.loads(raw)
        except Exception:
            prefix = "Updated todo list to "
            if raw.startswith(prefix):
                raw = raw[len(prefix) :].strip()
            try:
                parsed = ast.literal_eval(raw)
            except Exception:
                return raw or "updated"
    else:
        parsed = content

    if not isinstance(parsed, list):
        return str(content).strip() or "updated"

    status_map = {
        "pending": "todo",
        "in_progress": "doing",
        "completed": "done",
    }
    lines: list[str] = []
    for item in parsed:
        if not isinstance(item, dict):
            lines.append(f"- {item}")
            continue
        status = status_map.get(str(item.get("status", "")).strip(), "todo")
        text = str(item.get("content", "")).strip() or "<unnamed task>"
        lines.append(f"- [{status}] {text}")
    return "\n".join(lines) if lines else "updated"


def _format_tool_args(raw_args: object, *, agent_name: str = "", tool_name: str = "") -> str:
    if raw_args is None:
        return ""
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except Exception:
            return _truncate(raw_args)
    else:
        parsed = raw_args
    if not isinstance(parsed, dict):
        return _truncate(str(parsed))

    if agent_name == "main-agent" and tool_name == "task":
        lines: list[str] = []
        for key in ("subagent_type", "name", "description", "prompt"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(f"{key}: {value.strip()}")
        return "\n".join(lines) if lines else json.dumps(parsed, ensure_ascii=True, indent=2)
    if tool_name == "think_tool" and "reflection" in parsed:
        return str(parsed["reflection"])
    if tool_name in {"write_file", "edit_file"}:
        lines = []
        file_path = parsed.get("file_path")
        if isinstance(file_path, str):
            lines.append(f"file_path: {file_path}")
        for key in ("old_string", "new_string", "content"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(f"{key}:\n{value.strip()}")
        return "\n".join(lines) if lines else json.dumps(parsed, ensure_ascii=True, indent=2)

    if "description" in parsed:
        return _truncate(str(parsed["description"]))
    if "reflection" in parsed:
        return _truncate(str(parsed["reflection"]))
    if "query" in parsed:
        return _truncate(str(parsed["query"]))
    if "urls" in parsed:
        return _truncate(json.dumps(parsed, ensure_ascii=True))
    return _truncate(json.dumps(parsed, ensure_ascii=True))


def _task_target_name(tool_input: object) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    for key in ("subagent_type", "agent", "name"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    description = tool_input.get("description") or tool_input.get("task") or tool_input.get("prompt")
    if not isinstance(description, str):
        return None
    lowered = description.lower()
    if "scout-agent" in lowered or "scout agent" in lowered:
        return "scout-agent"
    if "research-agent" in lowered or "research agent" in lowered:
        return "research-agent"
    return None


def _normalize_agent_name(name: object) -> str:
    if isinstance(name, str) and name.strip():
        return name
    return "main-agent"


def _extract_usage(usage_metadata: object) -> tuple[int, int, int]:
    if not isinstance(usage_metadata, dict):
        return 0, 0, 0
    return (
        int(usage_metadata.get("input_tokens", 0) or 0),
        int(usage_metadata.get("output_tokens", 0) or 0),
        int(usage_metadata.get("total_tokens", 0) or 0),
    )


def _resolve_virtual_path(path_str: str) -> Path:
    normalized = path_str.strip()
    if normalized.startswith("/"):
        normalized = normalized[1:]
    return WORKSPACE_ROOT / normalized


def _artifact_file_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for candidate in path.rglob("*") if candidate.is_file())


def _prompt_with_run_metadata(
    *,
    prompt: str,
    thread_id: str,
    expected_report_path: str | None,
) -> str:
    report_path = expected_report_path or "/report/final_report.md"
    metadata = f"""\
<run_metadata>
Thread ID: {thread_id}
Virtual workspace root: /
Accessible artifact directories:
- /report/ final report files
- /tmp/drafts/ intermediate drafts and oversized appendices
- /tmp/review/ coverage reviews, source registries, citation self-checks, and repair notes
- /large_tool_results/ automatically offloaded large tool results
- /conversation_history/ summarized conversation history that can be recovered if exact prior details are needed
Virtual final report path: {report_path}

Use virtual absolute paths only. Subagents should return findings directly; use /tmp/drafts/ only
for unusually large supplementary appendices. Use /tmp/review/ for coverage and citation self-checks.
If a useful result is offloaded to /large_tool_results/ or prior history is summarized to
/conversation_history/, inspect or search the referenced path before relying on missing details.
Research and deliverables are text-only: do not use image search, include images, embed Markdown
images, collect visual assets, or use direct image URLs as report content.
Do not output the full report body in chat. Put report content only in write_file/edit_file calls;
ordinary assistant messages should be brief status or final-path notes.
For non-trivial sourced research, your first todo list must contain exactly one in-progress scout
todo. Do not include skeleton, research batch, synthesis, review, citation, or finalization todos
until the scout handoff has returned. After scout: write /report/ skeleton, then create the detailed
plan, then launch the first research batch.
Use an iterative research-to-writing cycle: use scout-agent only for the first bounded landscape
scout, then process each research-agent batch into report edits before launching the next broad
batch. Use write_file for the report path only to create the initial skeleton if it is missing; after
that, read/search the current report and use edit_file section-by-section. Never write the complete
report in one call: the initial write_file may only create a skeleton/outline, and the polished report
must emerge through later targeted edit_file calls. You may add, update, delete, move, or rewrite lines
as needed, but do it through targeted section edits rather than whole-report rewrites. Update todos
based on the current draft's remaining gaps.
Parallelism is for independent research-agent calls within a batch. After a batch returns, switch
to synthesis/editing and update /report/ before launching another broad batch.
</run_metadata>
"""
    return metadata + "\n" + prompt


def _save_run_metrics(
    *,
    run_id: str,
    thread_id: str,
    prompt: str,
    token_usage_by_agent: dict[str, dict[str, int]],
    tool_call_counts: dict[str, int],
    tool_call_counts_by_agent: dict[str, int],
    tool_error_counts: dict[str, int],
    ddgs_call_counts: dict[str, int],
    stream_tool_error_counts: dict[str, int],
    runtime_diagnostics: dict[str, Any],
    subagent_handoffs: list[dict[str, Any]],
    artifact_activity: dict[str, int | list[str]],
) -> Path:
    RUN_METRICS_DIR.mkdir(parents=True, exist_ok=True)

    main_usage = token_usage_by_agent.get(
        "main-agent",
        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    )
    subagent_names = sorted(name for name in token_usage_by_agent if name != "main-agent")
    subagent_totals = {
        "input_tokens": sum(token_usage_by_agent[name]["input_tokens"] for name in subagent_names),
        "output_tokens": sum(token_usage_by_agent[name]["output_tokens"] for name in subagent_names),
        "total_tokens": sum(token_usage_by_agent[name]["total_tokens"] for name in subagent_names),
    }

    payload = {
        "run_id": run_id,
        "thread_id": thread_id,
        "prompt": prompt,
        "saved_at": datetime.now(UTC).isoformat(),
        "main_agent_tokens": main_usage,
        "subagents_total_tokens": subagent_totals,
        "subagent_tokens_by_name": {
            name: token_usage_by_agent[name] for name in subagent_names
        },
        "tool_calls": {
            "total": sum(tool_call_counts.values()),
            "by_tool": dict(sorted(tool_call_counts.items())),
            "by_agent": dict(sorted(tool_call_counts_by_agent.items())),
        },
        "tool_error_results": {
            "total": sum(tool_error_counts.values()),
            "by_tool": dict(sorted(tool_error_counts.items())),
        },
        "stream_tool_error_events": {
            "total": sum(stream_tool_error_counts.values()),
            "by_tool": dict(sorted(stream_tool_error_counts.items())),
        },
        "runtime_diagnostics": runtime_diagnostics,
        "ddgs_tool_calls": {
            "total": sum(ddgs_call_counts.values()),
            "by_tool": dict(sorted(ddgs_call_counts.items())),
        },
        "ddgs_tool_efficiency": search_tool_efficiency(ddgs_call_counts),
        "subagent_handoff_metrics": summarize_handoffs(subagent_handoffs),
        "source_overlap_between_subagents": source_overlap_between_handoffs(subagent_handoffs),
        "context_engineering_artifacts": {
            "large_tool_results_file_count": _artifact_file_count(LARGE_TOOL_RESULTS_DIR),
            "conversation_history_file_count": _artifact_file_count(CONVERSATION_HISTORY_DIR),
        },
        "artifact_activity": artifact_activity,
    }

    output_path = RUN_METRICS_DIR / f"{run_id}.json"
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output_path


async def _stream_run(
    prompt: str,
    thread_id: str,
    *,
    expected_report_path: str | None = None,
    fail_on_incomplete_report: bool = True,
    langfuse_trace: LangfuseTraceConfig | None = None,
) -> StreamRunResult:
    agent_prompt = _prompt_with_run_metadata(
        prompt=prompt,
        thread_id=thread_id,
        expected_report_path=expected_report_path,
    )
    current_agent: str | None = None
    root_run_id: str | None = None
    shown_tool_calls: set[tuple[str, str]] = set()
    shown_tool_results: set[tuple[str, str, str]] = set()
    task_targets_by_run_id: dict[str, str | None] = {}
    subagent_handoffs: list[dict[str, Any]] = []
    active_model_text: dict[str, list[str]] = defaultdict(list)
    completed_model_text: dict[str, list[str]] = defaultdict(list)
    report_file_candidates: list[Path] = []
    token_usage_by_agent: dict[str, dict[str, int]] = defaultdict(
        lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    )
    tool_call_counts: dict[str, int] = defaultdict(int)
    tool_call_counts_by_agent: dict[str, int] = defaultdict(int)
    tool_error_counts: dict[str, int] = defaultdict(int)
    stream_tool_error_counts: dict[str, int] = defaultdict(int)
    ddgs_call_counts: dict[str, int] = defaultdict(int)
    artifact_activity: dict[str, int | list[str]] = {
        "subagent_task_calls": 0,
        "scout_agent_task_calls": 0,
        "research_agent_task_calls": 0,
        "citation_self_checks": 0,
        "unknown_subagent_task_calls": 0,
        "report_file_updates": 0,
        "review_file_updates": 0,
        "other_file_updates": 0,
        "report_write_count": 0,
        "report_edit_count": 0,
        "edit_file_failure_count": 0,
        "max_subagent_tasks_between_report_updates": 0,
        "max_subagent_tasks_between_report_or_review_updates": 0,
        "warnings": [],
    }
    subagent_tasks_since_report_update = 0
    subagent_tasks_since_artifact_update = 0

    print(_style(f"Thread: {thread_id}", BOLD, CYAN))
    print(_style("Prompt:", BOLD, CYAN), prompt)
    print(_style("-" * 40, DIM, GRAY))

    reset_runtime_diagnostics()
    async with build_deep_research_agent() as agent:
        run_config: dict[str, Any] = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 10_000,
        }
        if langfuse_trace is not None:
            run_config = langfuse_trace.update_langchain_config(run_config)
        async for event in agent.astream_events(
            {"messages": [{"role": "user", "content": agent_prompt}]},
            config=run_config,
            version="v2",
        ):
            event_type = event.get("event")
            metadata = event.get("metadata", {}) or {}
            data = event.get("data", {}) or {}
            agent_name = _normalize_agent_name(metadata.get("lc_agent_name"))

            if langfuse_trace is not None and event_type in {
                "on_chain_start",
                "on_chain_end",
                "on_chat_model_end",
                "on_tool_end",
                "on_tool_error",
            }:
                langfuse_trace.flush_if_due()

            if (
                root_run_id is None
                and event_type == "on_chain_start"
                and event.get("name") == "LangGraph"
            ):
                root_run_id = str(event.get("run_id") or "")

            if event_type == "on_chat_model_start":
                active_model_text[agent_name] = []
                continue

            if event_type == "on_chat_model_stream":
                token = data.get("chunk")
                if agent_name != current_agent:
                    if current_agent is not None:
                        print()
                    print()
                    print(_style(f"[{agent_name}]", BOLD, GREEN))
                    current_agent = agent_name

                if not isinstance(token, AIMessageChunk):
                    continue

                text = str(token.text)
                if text:
                    active_model_text[agent_name].append(text)
                    print(text, end="", flush=True)
                continue

            if event_type == "on_chat_model_end":
                output = data.get("output")
                full_text = "".join(active_model_text.pop(agent_name, [])).strip()
                if full_text:
                    completed_model_text[agent_name].append(full_text)
                input_tokens, output_tokens, total_tokens = _extract_usage(
                    getattr(output, "usage_metadata", None)
                )
                token_usage_by_agent[agent_name]["input_tokens"] += input_tokens
                token_usage_by_agent[agent_name]["output_tokens"] += output_tokens
                token_usage_by_agent[agent_name]["total_tokens"] += total_tokens
                continue

            if event_type == "on_tool_start":
                tool_name = str(event.get("name") or "")
                run_id = str(event.get("run_id") or "")
                tool_call_counts[tool_name] += 1
                tool_call_counts_by_agent[agent_name] += 1
                key = (tool_name, run_id)
                if key not in shown_tool_calls:
                    shown_tool_calls.add(key)
                    arg_preview = _format_tool_args(data.get("input"), agent_name=agent_name, tool_name=tool_name)
                    print()
                    print(_style(f"[call:{tool_name}]", BOLD, MAGENTA), arg_preview)
                tool_input = data.get("input")
                if agent_name == "main-agent" and tool_name == "task":
                    artifact_activity["subagent_task_calls"] = int(artifact_activity["subagent_task_calls"]) + 1
                    target_name = _task_target_name(tool_input)
                    task_targets_by_run_id[run_id] = target_name
                    if target_name == "scout-agent":
                        artifact_activity["scout_agent_task_calls"] = int(artifact_activity["scout_agent_task_calls"]) + 1
                    elif target_name == "research-agent":
                        artifact_activity["research_agent_task_calls"] = int(artifact_activity["research_agent_task_calls"]) + 1
                    else:
                        artifact_activity["unknown_subagent_task_calls"] = int(artifact_activity["unknown_subagent_task_calls"]) + 1
                    subagent_tasks_since_report_update += 1
                    subagent_tasks_since_artifact_update += 1
                    artifact_activity["max_subagent_tasks_between_report_updates"] = max(
                        int(artifact_activity["max_subagent_tasks_between_report_updates"]),
                        subagent_tasks_since_report_update,
                    )
                    artifact_activity["max_subagent_tasks_between_report_or_review_updates"] = max(
                        int(artifact_activity["max_subagent_tasks_between_report_or_review_updates"]),
                        subagent_tasks_since_artifact_update,
                    )
                if agent_name == "main-agent" and tool_name in {"write_file", "edit_file"} and isinstance(tool_input, dict):
                    file_path = tool_input.get("file_path")
                    if isinstance(file_path, str):
                        if file_path.startswith("/report/"):
                            artifact_activity["report_file_updates"] = int(artifact_activity["report_file_updates"]) + 1
                            if tool_name == "write_file":
                                artifact_activity["report_write_count"] = int(artifact_activity["report_write_count"]) + 1
                            elif tool_name == "edit_file":
                                artifact_activity["report_edit_count"] = int(artifact_activity["report_edit_count"]) + 1
                            subagent_tasks_since_report_update = 0
                            subagent_tasks_since_artifact_update = 0
                            if file_path.endswith(".md"):
                                report_file_candidates.append(_resolve_virtual_path(file_path))
                        elif file_path.startswith("/tmp/review/"):
                            artifact_activity["review_file_updates"] = int(artifact_activity["review_file_updates"]) + 1
                            if file_path.endswith("/citation_self_check.md"):
                                artifact_activity["citation_self_checks"] = int(artifact_activity["citation_self_checks"]) + 1
                            subagent_tasks_since_artifact_update = 0
                        else:
                            artifact_activity["other_file_updates"] = int(artifact_activity["other_file_updates"]) + 1
                if _is_ddgs_tool(tool_name):
                    ddgs_call_counts[tool_name] += 1
                continue

            if event_type == "on_tool_error":
                tool_name = str(event.get("name") or "")
                stream_tool_error_counts[tool_name] += 1
                continue

            if event_type != "on_tool_end":
                continue

            tool_name = str(event.get("name") or "")
            run_id = str(event.get("run_id") or "")
            key = (agent_name, tool_name, run_id)
            if key in shown_tool_results:
                continue
            shown_tool_results.add(key)

            if tool_name == "write_todos":
                todos = data.get("input", {}).get("todos")
                print()
                print(_style("[todos]", BOLD, YELLOW))
                print(_format_todos(todos))
                continue

            output = str(data.get("output", ""))
            if _looks_like_tool_error(output):
                tool_error_counts[tool_name] += 1
                if tool_name == "edit_file":
                    artifact_activity["edit_file_failure_count"] = int(artifact_activity["edit_file_failure_count"]) + 1
            if agent_name == "main-agent" and tool_name == "task":
                subagent_handoffs.append(
                    _task_handoff_entry(subagent_type=task_targets_by_run_id.get(run_id), output=output)
                )
            preview = output if tool_name == "think_tool" else _truncate(output)
            if preview:
                print()
                print(_style(f"[tool:{tool_name}]", BOLD, BLUE), preview)

        print()
        print()
        print(_style("-" * 40, DIM, GRAY))
        print(_style("Usage summary", BOLD, CYAN))

        main_usage = token_usage_by_agent.get(
            "main-agent",
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        print(
            f"main-agent tokens: input={main_usage['input_tokens']}, "
            f"output={main_usage['output_tokens']}, total={main_usage['total_tokens']}"
        )

        subagent_names = [name for name in token_usage_by_agent if name != "main-agent"]
        sub_input = sum(token_usage_by_agent[name]["input_tokens"] for name in subagent_names)
        sub_output = sum(token_usage_by_agent[name]["output_tokens"] for name in subagent_names)
        sub_total = sum(token_usage_by_agent[name]["total_tokens"] for name in subagent_names)
        print(
            f"subagents total tokens: input={sub_input}, output={sub_output}, total={sub_total}"
        )
        for name in sorted(subagent_names):
            usage = token_usage_by_agent[name]
            print(
                f"  {name}: input={usage['input_tokens']}, "
                f"output={usage['output_tokens']}, total={usage['total_tokens']}"
            )

        ddgs_total = sum(ddgs_call_counts.values())
        tool_total = sum(tool_call_counts.values())
        tool_error_total = sum(tool_error_counts.values())
        runtime_diagnostics = snapshot_runtime_diagnostics()
        print(f"Tool calls: total={tool_total}")
        for tool_name in sorted(tool_call_counts):
            print(f"  {tool_name}: {tool_call_counts[tool_name]}")
        print(f"Tool error-like results: total={tool_error_total}")
        for tool_name in sorted(tool_error_counts):
            print(f"  {tool_name}: {tool_error_counts[tool_name]}")

        print(f"DDGS tool calls: total={ddgs_total}")
        for tool_name in sorted(ddgs_call_counts):
            print(f"  {tool_name}: {ddgs_call_counts[tool_name]}")
        print(
            "runtime diagnostics: "
            f"tool_retries={runtime_diagnostics['tool_retry_count']}, "
            f"model_retries={runtime_diagnostics['model_retry_count']}, "
            f"failed_tools={runtime_diagnostics['failed_tool_call_count']}, "
            f"summarizations={runtime_diagnostics['summarization_count']}"
        )

        warnings = artifact_activity["warnings"]
        if not isinstance(warnings, list):
            warnings = []
            artifact_activity["warnings"] = warnings
        subagent_task_calls = int(artifact_activity["subagent_task_calls"])
        scout_task_calls = int(artifact_activity["scout_agent_task_calls"])
        research_task_calls = int(artifact_activity["research_agent_task_calls"])
        citation_self_checks = int(artifact_activity["citation_self_checks"])
        report_updates = int(artifact_activity["report_file_updates"])
        max_without_artifact = int(artifact_activity["max_subagent_tasks_between_report_or_review_updates"])
        if subagent_task_calls >= 2 and report_updates <= 1:
            warnings.append(
                "Agent made multiple subagent task calls but updated /report/ once or less; living-report behavior may be weak."
            )
        if max_without_artifact > 2:
            warnings.append(
                f"Agent made {max_without_artifact} subagent task calls between /report/ or /tmp/review/ updates."
            )
        print(
            "artifact activity: "
            f"subagent_tasks={subagent_task_calls}, "
            f"scout_tasks={scout_task_calls}, "
            f"research_tasks={research_task_calls}, "
            f"citation_self_checks={citation_self_checks}, "
            f"report_updates={report_updates}, "
            f"report_writes={artifact_activity['report_write_count']}, "
            f"report_edits={artifact_activity['report_edit_count']}, "
            f"edit_failures={artifact_activity['edit_file_failure_count']}, "
            f"review_updates={artifact_activity['review_file_updates']}"
        )
        for warning in warnings:
            print(_style(f"WARNING: {warning}", BOLD, YELLOW))

        metrics_run_id = root_run_id or f"{thread_id}-{int(datetime.now(UTC).timestamp())}"
        metrics_path = _save_run_metrics(
            run_id=metrics_run_id,
            thread_id=thread_id,
            prompt=agent_prompt,
            token_usage_by_agent=dict(token_usage_by_agent),
            tool_call_counts=dict(tool_call_counts),
            tool_call_counts_by_agent=dict(tool_call_counts_by_agent),
            tool_error_counts=dict(tool_error_counts),
            ddgs_call_counts=dict(ddgs_call_counts),
            stream_tool_error_counts=dict(stream_tool_error_counts),
            runtime_diagnostics=runtime_diagnostics,
            subagent_handoffs=subagent_handoffs,
            artifact_activity=artifact_activity,
        )
        print(f"metrics saved: {metrics_path}")
        print(_style("Stream complete.", BOLD, CYAN))
        report_path: Path | None = None
        if expected_report_path:
            candidate = _resolve_virtual_path(expected_report_path)
            if candidate.exists():
                report_path = candidate
        if report_path is None:
            for candidate in reversed(report_file_candidates):
                if candidate.exists():
                    report_path = candidate
                    break

        article_text = ""
        if report_path is not None:
            article_text = report_path.read_text(encoding="utf-8")
            if expected_report_path:
                placeholder_issues = _report_placeholder_issues(article_text)
                if placeholder_issues:
                    issue_text = "; ".join(placeholder_issues)
                    message = f"Report at {report_path} is still a skeleton: {issue_text}"
                    if fail_on_incomplete_report:
                        raise RuntimeError(message)
                    print(_style(f"WARNING: {message}", BOLD, YELLOW))
        elif expected_report_path:
            message = f"Expected report was not written: {expected_report_path}"
            if fail_on_incomplete_report:
                raise RuntimeError(message)
            print(_style(f"WARNING: {message}", BOLD, YELLOW))
        else:
            for candidate in reversed(completed_model_text.get("main-agent", [])):
                if candidate.strip():
                    article_text = candidate
                    break
        return StreamRunResult(
            article_text=article_text,
            metrics_path=metrics_path,
            prompt=prompt,
            report_path=report_path,
            run_id=metrics_run_id,
            thread_id=thread_id,
        )


async def main() -> None:
    _load_env_file(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Stream a deep-agent run.")
    parser.add_argument("prompt", nargs="*", help="Prompt text.")
    parser.add_argument(
        "--thread-id",
        default=DEFAULT_THREAD_ID,
        help="LangGraph thread id to use for the run.",
    )
    args = parser.parse_args()

    prompt = " ".join(args.prompt).strip() or DEFAULT_PROMPT
    await _stream_run(prompt=prompt, thread_id=args.thread_id)


if __name__ == "__main__":
    asyncio.run(main())
