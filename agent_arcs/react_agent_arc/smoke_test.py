from __future__ import annotations

import argparse
import asyncio
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

from agent_arcs.react_agent_arc.react_agent_arch import WORKSPACE_ROOT, build_react_research_agent
from agent_arcs.diagnostic_metrics import (
    reset_runtime_diagnostics,
    snapshot_runtime_diagnostics,
    search_tool_efficiency,
)
from agent_arcs.langfuse_tracing import LangfuseTraceConfig

DEFAULT_PROMPT = "Write a concise sourced note on Japan's aging market."
DEFAULT_THREAD_ID = "react-agent-smoke-test"
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

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
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
        "contains unfinished-section placeholder": "will be completed after all research",
        "contains unfinished-references placeholder": "references will be populated",
        "contains generic placeholder marker": "*this section will be completed",
    }
    return [label for label, needle in checks.items() if needle in lowered]


def _looks_like_tool_error(output: str) -> bool:
    normalized = output.strip().lower()
    return normalized.startswith("error:") or "tool failed after all retry attempts" in normalized


DDGS_TOOL_NAMES = {"search_text", "search_images", "search_news", "search_videos", "search_books", "extract_content"}


def _is_ddgs_tool(tool_name: str) -> bool:
    return tool_name in DDGS_TOOL_NAMES


def _format_tool_args(raw_args: object) -> str:
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

    for key in ["description", "reflection", "query", "file_path", "path", "pattern", "urls"]:
        if key in parsed:
            return _truncate(json.dumps({key: parsed[key]}, ensure_ascii=True))
    return _truncate(json.dumps(parsed, ensure_ascii=True))


def _normalize_agent_name(name: object) -> str:
    if isinstance(name, str) and name.strip():
        return name
    return "react-agent"


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
- /tmp/drafts/ optional intermediate drafts
- /tmp/review/ coverage and citation self-checks
Virtual final report path: {report_path}

Use virtual absolute paths only. Work independently with the available tools; do not delegate or
reference unavailable subagents. Write the final report to the exact virtual path above. Never write
the complete report in one call: the initial write_file may only create a skeleton/outline, and the
polished report must emerge through later targeted edit_file calls. After the report exists, read or
search the current draft and use edit_file section-by-section. Section-by-section means a coherent
section, subsection, table, or contiguous placeholder edit. Use surgical edits only for localized
repairs. Never globally replace a bare citation marker such as [10]; anchor citation repairs to the
surrounding sentence, table row, or reference entry. Write coverage and citation self-checks under
/tmp/review/. Research and deliverables are text-only: do not use image search, include images, embed
Markdown images, collect visual assets, or use direct image URLs as report content. For non-trivial
sourced research, first do a bounded scout, then run a broad multi-aspect discovery sweep across
several different angles before narrowing into targeted section-level source inspection. For complex
research tasks, expect roughly 30-50 total DDGS MCP tool calls across discovery, extraction, and
targeted reading unless the coverage review justifies a narrower or broader budget.
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
    artifact_activity: dict[str, int | list[str]],
) -> Path:
    RUN_METRICS_DIR.mkdir(parents=True, exist_ok=True)
    main_usage = token_usage_by_agent.get(
        "react-agent",
        token_usage_by_agent.get("main-agent", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}),
    )
    payload = {
        "run_id": run_id,
        "thread_id": thread_id,
        "prompt": prompt,
        "saved_at": datetime.now(UTC).isoformat(),
        "main_agent_tokens": main_usage,
        "subagents_total_tokens": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "subagent_tokens_by_name": {},
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
        "context_engineering_artifacts": {
            "large_tool_results_file_count": _artifact_file_count(WORKSPACE_ROOT / "large_tool_results"),
            "conversation_history_file_count": _artifact_file_count(WORKSPACE_ROOT / "conversation_history"),
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
        "citation_self_checks": 0,
        "report_file_updates": 0,
        "review_file_updates": 0,
        "other_file_updates": 0,
        "report_write_count": 0,
        "report_edit_count": 0,
        "edit_file_failure_count": 0,
        "warnings": [],
    }

    print(_style(f"Thread: {thread_id}", BOLD, CYAN))
    print(_style("Prompt:", BOLD, CYAN), prompt)
    print(_style("-" * 40, DIM, GRAY))

    reset_runtime_diagnostics()
    async with build_react_research_agent() as agent:
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

            if root_run_id is None and event_type == "on_chain_start" and event.get("name") == "LangGraph":
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
                input_tokens, output_tokens, total_tokens = _extract_usage(getattr(output, "usage_metadata", None))
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
                    print()
                    print(_style(f"[call:{tool_name}]", BOLD, MAGENTA), _format_tool_args(data.get("input")))
                tool_input = data.get("input")
                if tool_name in {"write_file", "edit_file"} and isinstance(tool_input, dict):
                    file_path = tool_input.get("file_path")
                    if isinstance(file_path, str):
                        if file_path.startswith("/report/"):
                            artifact_activity["report_file_updates"] = int(artifact_activity["report_file_updates"]) + 1
                            if tool_name == "write_file":
                                artifact_activity["report_write_count"] = int(artifact_activity["report_write_count"]) + 1
                            elif tool_name == "edit_file":
                                artifact_activity["report_edit_count"] = int(artifact_activity["report_edit_count"]) + 1
                            if file_path.endswith(".md"):
                                report_file_candidates.append(_resolve_virtual_path(file_path))
                        elif file_path.startswith("/tmp/review/"):
                            artifact_activity["review_file_updates"] = int(artifact_activity["review_file_updates"]) + 1
                            if file_path.endswith("/citation_audit.md") or file_path.endswith("/citation_self_check.md"):
                                artifact_activity["citation_self_checks"] = int(artifact_activity["citation_self_checks"]) + 1
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
            output = str(data.get("output", ""))
            if _looks_like_tool_error(output):
                tool_error_counts[tool_name] += 1
                if tool_name == "edit_file":
                    artifact_activity["edit_file_failure_count"] = int(artifact_activity["edit_file_failure_count"]) + 1
            preview = _truncate(output)
            if preview:
                print()
                print(_style(f"[tool:{tool_name}]", BOLD, BLUE), preview)

        print()
        print()
        print(_style("-" * 40, DIM, GRAY))
        print(_style("Usage summary", BOLD, CYAN))
        main_usage = token_usage_by_agent.get(
            "react-agent",
            token_usage_by_agent.get("main-agent", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}),
        )
        print(
            f"main-agent tokens: input={main_usage['input_tokens']}, "
            f"output={main_usage['output_tokens']}, total={main_usage['total_tokens']}"
        )
        print("subagents total tokens: input=0, output=0, total=0")
        tool_total = sum(tool_call_counts.values())
        tool_error_total = sum(tool_error_counts.values())
        runtime_diagnostics = snapshot_runtime_diagnostics()
        print(f"Tool calls: total={tool_total}")
        for tool_name in sorted(tool_call_counts):
            print(f"  {tool_name}: {tool_call_counts[tool_name]}")
        print(f"Tool error-like results: total={tool_error_total}")
        for tool_name in sorted(tool_error_counts):
            print(f"  {tool_name}: {tool_error_counts[tool_name]}")

        ddgs_total = sum(ddgs_call_counts.values())
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
        if int(artifact_activity["report_file_updates"]) == 0:
            warnings.append("React agent did not write or edit /report/ during the run.")
        print(
            "artifact activity: "
            f"subagent_tasks=0, report_updates={artifact_activity['report_file_updates']}, "
            f"report_writes={artifact_activity['report_write_count']}, "
            f"report_edits={artifact_activity['report_edit_count']}, "
            f"edit_failures={artifact_activity['edit_file_failure_count']}, "
            f"review_updates={artifact_activity['review_file_updates']}, "
            f"citation_self_checks={artifact_activity['citation_self_checks']}"
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
            placeholder_issues = _report_placeholder_issues(article_text)
            if expected_report_path and placeholder_issues:
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
            for candidate in reversed(completed_model_text.get("react-agent", [])):
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
    parser = argparse.ArgumentParser(description="Stream a ReAct-agent run.")
    parser.add_argument("prompt", nargs="*", help="Prompt text.")
    parser.add_argument("--thread-id", default=DEFAULT_THREAD_ID, help="Checkpoint thread id.")
    args = parser.parse_args()
    prompt = " ".join(args.prompt).strip() or DEFAULT_PROMPT
    await _stream_run(prompt=prompt, thread_id=args.thread_id)


if __name__ == "__main__":
    asyncio.run(main())
