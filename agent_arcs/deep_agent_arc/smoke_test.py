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

from agent_arcs.deep_agent_arc.deep_agent_arch import REPORTS_DIR, WORKSPACE_ROOT, build_deep_research_agent

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
        "contains unfinished-section placeholder": "will be completed after all research",
        "contains unfinished-references placeholder": "references will be populated",
        "contains generic placeholder marker": "*this section will be completed",
    }
    return [label for label, needle in checks.items() if needle in lowered]


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

    if "description" in parsed:
        return _truncate(str(parsed["description"]))
    if "reflection" in parsed:
        return _truncate(str(parsed["reflection"]))
    if "query" in parsed:
        return _truncate(str(parsed["query"]))
    if "urls" in parsed:
        return _truncate(json.dumps(parsed, ensure_ascii=True))
    return _truncate(json.dumps(parsed, ensure_ascii=True))


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


def _prompt_with_run_metadata(
    *,
    prompt: str,
    thread_id: str,
    expected_report_path: str | None,
) -> str:
    report_path = expected_report_path or "/report/final_report.md"
    metadata = f"""\
<run_metadata>
LangGraph thread_id: {thread_id}
Physical workspace root: {WORKSPACE_ROOT}
Virtual workspace root for agent tools: /
Virtual large tool result directory: /large_tool_results/
Virtual draft directory: /tmp/drafts/
Virtual review directory: /tmp/review/
Virtual final report path: {report_path}

Use virtual absolute paths with filesystem tools. Do not use the physical workspace root in
tool calls. When delegating to subagents, include the thread_id and the expected direct handoff
format in the task description. Subagents should return findings directly; use /tmp/drafts/ only
for unusually large supplementary appendices. Use /tmp/review/ for coverage and citation audits. If a tool result is automatically offloaded to
/large_tool_results/<tool_call_id>, read_file or grep that path before relying on the result.
</run_metadata>
"""
    return metadata + "\n" + prompt


def _save_run_metrics(
    *,
    run_id: str,
    thread_id: str,
    prompt: str,
    token_usage_by_agent: dict[str, dict[str, int]],
    tavily_call_counts: dict[str, int],
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
        "tavily_tool_calls": {
            "total": sum(tavily_call_counts.values()),
            "by_tool": dict(sorted(tavily_call_counts.items())),
        },
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
    tavily_call_counts: dict[str, int] = defaultdict(int)

    print(_style(f"Thread: {thread_id}", BOLD, CYAN))
    print(_style("Prompt:", BOLD, CYAN), prompt)
    print(_style("-" * 40, DIM, GRAY))

    async with build_deep_research_agent() as agent:
        async for event in agent.astream_events(
            {"messages": [{"role": "user", "content": agent_prompt}]},
            config={
                "configurable": {"thread_id": thread_id},
                "recursion_limit": 10_000,
            },
            version="v2",
        ):
            event_type = event.get("event")
            metadata = event.get("metadata", {}) or {}
            data = event.get("data", {}) or {}
            agent_name = _normalize_agent_name(metadata.get("lc_agent_name"))

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
                key = (tool_name, run_id)
                if key not in shown_tool_calls:
                    shown_tool_calls.add(key)
                    arg_preview = _format_tool_args(data.get("input"))
                    print()
                    print(_style(f"[call:{tool_name}]", BOLD, MAGENTA), arg_preview)
                tool_input = data.get("input")
                if (
                    agent_name == "main-agent"
                    and tool_name == "write_file"
                    and isinstance(tool_input, dict)
                ):
                    file_path = tool_input.get("file_path")
                    if isinstance(file_path, str) and file_path.startswith("/report/") and file_path.endswith(".md"):
                        report_file_candidates.append(_resolve_virtual_path(file_path))
                if tool_name.startswith("tavily_"):
                    tavily_call_counts[tool_name] += 1
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

            preview = _truncate(str(data.get("output", "")))
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

        tavily_total = sum(tavily_call_counts.values())
        print(f"Tavily tool calls: total={tavily_total}")
        for tool_name in sorted(tavily_call_counts):
            print(f"  {tool_name}: {tavily_call_counts[tool_name]}")

        metrics_run_id = root_run_id or f"{thread_id}-{int(datetime.now(UTC).timestamp())}"
        metrics_path = _save_run_metrics(
            run_id=metrics_run_id,
            thread_id=thread_id,
            prompt=agent_prompt,
            token_usage_by_agent=dict(token_usage_by_agent),
            tavily_call_counts=dict(tavily_call_counts),
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
        for candidate in reversed(completed_model_text.get("main-agent", [])):
            if not article_text and candidate.strip():
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
