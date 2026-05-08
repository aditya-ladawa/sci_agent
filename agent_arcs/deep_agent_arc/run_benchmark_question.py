from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DR_BENCH_ROOT = PROJECT_ROOT / "dr_bench"
BENCH_CODE_ROOT = PROJECT_ROOT / "deep_research_bench"
QUERY_FILE = BENCH_CODE_ROOT / "data" / "prompt_data" / "query.jsonl"
ARCH_DIR_NAME = "deep_agent_arc"
ARCH_TYPE = "deep"
MAX_COMPLETION_ATTEMPTS = int(os.getenv("DEEP_AGENT_MAX_COMPLETION_ATTEMPTS", "3"))

from agent_arcs.citation_integrity import citation_integrity_failure
from agent_arcs.costs import estimate_run_cost


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                data.append(json.loads(stripped))
    return data


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_question(q_no: int) -> dict[str, Any]:
    for row in _load_jsonl(QUERY_FILE):
        if int(row.get("id", -1)) == q_no:
            return row
    raise ValueError(f"Question id {q_no} not found in {QUERY_FILE}.")


def _ensure_workspace(q_no: int) -> dict[str, Path]:
    q_root = DR_BENCH_ROOT / f"q{q_no}"
    arch_root = q_root / ARCH_DIR_NAME
    paths = {
        "q_root": q_root,
        "arch_root": arch_root,
        "report": arch_root / "report",
        "tmp": arch_root / "tmp",
        "evidence": arch_root / "tmp" / "evidence",
        "drafts": arch_root / "tmp" / "drafts",
        "review": arch_root / "tmp" / "review",
        "run_metrics": arch_root / "run_metrics",
        "large_tool_results": arch_root / "large_tool_results",
        "race": arch_root / "race",
        "fact": arch_root / "fact",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _run_command(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _write_process_log(path: Path, command: list[str], result: subprocess.CompletedProcess[str]) -> None:
    path.write_text(
        "Command: " + " ".join(command) + "\n"
        f"Return code: {result.returncode}\n\n"
        + result.stdout,
        encoding="utf-8",
    )


def _parse_key_value_file(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    metrics: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        try:
            metrics[key.strip()] = float(value.strip())
        except ValueError:
            continue
    return metrics


def _read_run_metrics(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _report_word_count(article_text: str) -> int:
    return len(article_text.split())


def _activity_count(activity: dict[str, Any], key: str) -> int:
    try:
        return int(activity.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _review_artifact_failure(paths: dict[str, Path]) -> str | None:
    coverage_path = paths["review"] / "coverage_review.md"
    if not coverage_path.exists() or not coverage_path.read_text(encoding="utf-8").strip():
        return f"Coverage review is missing or empty: {coverage_path}"

    self_check_path = paths["review"] / "citation_self_check.md"
    if not self_check_path.exists():
        return f"Citation self-check is missing: {self_check_path}"
    self_check_text = self_check_path.read_text(encoding="utf-8")
    if not self_check_text.strip():
        return f"Citation self-check is empty: {self_check_path}"
    if "NEEDS_REPAIR" not in self_check_text:
        return None
    if "## Verdict" in self_check_text:
        verdict_section = self_check_text.split("## Verdict", 1)[1].split("##", 1)[0]
        if "NEEDS_REPAIR" in verdict_section:
            return f"Citation self-check still needs repair: {self_check_path}"
    first_lines = "\n".join(self_check_text.splitlines()[:30])
    if "NEEDS_REPAIR" in first_lines:
        return f"Citation self-check still needs repair: {self_check_path}"
    return None


def _current_run_artifact_failure(
    *,
    paths: dict[str, Path],
    report_path: Path | None,
    usage: dict[str, Any],
    run_started_at: float,
) -> str | None:
    artifact_activity = usage.get("artifact_activity", {}) or {}
    if not artifact_activity:
        return "Run metrics are missing artifact activity, so current-run report ownership cannot be verified."

    report_updates = _activity_count(artifact_activity, "report_file_updates")
    if report_updates == 0:
        return "Current Deep run did not write or edit /report/; refusing to evaluate a possibly stale report."

    artifact_paths = [path for path in [report_path] if path is not None]
    artifact_paths.extend(
        [
            paths["review"] / "coverage_review.md",
            paths["review"] / "citation_self_check.md",
        ]
    )
    stale_paths = [path for path in artifact_paths if path.exists() and path.stat().st_mtime < run_started_at]
    if stale_paths:
        formatted_paths = ", ".join(str(path) for path in stale_paths)
        return f"Current run appears to rely on stale artifact files from before this run: {formatted_paths}"
    return None


def _research_handoff_failure(*, usage: dict[str, Any], article_text: str) -> str | None:
    artifact_activity = usage.get("artifact_activity", {}) or {}
    if "research_agent_task_calls" not in artifact_activity:
        return None
    if _report_word_count(article_text) < 800:
        return None
    research_tasks = int(artifact_activity.get("research_agent_task_calls", 0) or 0)
    if research_tasks > 0:
        return None
    self_checks = int(artifact_activity.get("citation_self_checks", 0) or 0)
    return (
        "Deep Agent report was produced without any detected research-agent evidence handoff "
        f"(research tasks=0, citation self-checks={self_checks}). Use a fresh thread or continue the run "
        "so research-agent gathers a bounded evidence packet before evaluation."
    )


def _completion_repair_prompt(
    *,
    base_prompt: str,
    report_virtual_path: str,
    attempt: int,
) -> str:
    return (
        base_prompt
        + "\n\nThe previous attempt did not produce a complete final report. Continue this same run now. "
        + f"Read the current report at {report_virtual_path}. If the report file does not exist, create a skeleton/outline first. "
        + "If it is a skeleton or incomplete, use edit_file section-by-section to replace placeholders and complete sections. Do not rewrite the whole report in one pass. "
        + "Do not stop after saying you will write. "
        + "If the report file exists, your next report action must be edit_file, not another announcement. "
        + "Replace the Executive Summary placeholder first, then continue section-by-section with additional edit_file calls. "
        + "You must call write_file or edit_file to update the report before any ordinary message claiming writing progress. "
        + "Do not print the report body in chat; put report content only inside write_file/edit_file tool calls. "
        + "If prior checkpoint state says research is complete but the usable evidence is missing, rerun only the necessary research. "
        + "Subagents must return detailed findings directly; do not ask them to write evidence files. "
        + "Use the iterative cycle: read current draft, incorporate each research-agent handoff into /report/ with write_file/edit_file, update todos, then continue. "
        + "Make the report as complete as the question requires without padding. "
        + "Include methodology, calculations or comparisons where useful, assumptions, uncertainties, and numbered references. "
        + "Write /tmp/review/coverage_review.md. Write /tmp/review/citation_self_check.md with your citation/reference validation and repair any blocking issues before finalizing. "
        + f"Completion repair attempt: {attempt}."
    )


def _clean_eval_outputs(paths: dict[str, Path]) -> None:
    for output in [
        paths["race"] / "raw_results.jsonl",
        paths["race"] / "race_result.txt",
        paths["fact"] / "extracted.jsonl",
        paths["fact"] / "deduplicated.jsonl",
        paths["fact"] / "scraped.jsonl",
        paths["fact"] / "validated.jsonl",
        paths["fact"] / "fact_result.txt",
    ]:
        if output.exists():
            output.unlink()


def _run_race(
    *,
    q_no: int,
    question: dict[str, Any],
    article_text: str,
    paths: dict[str, Path],
    force: bool,
) -> dict[str, float]:
    model_name = f"q{q_no}_{ARCH_TYPE}"
    raw_data_dir = paths["race"] / "raw_data"
    cleaned_data_dir = paths["race"] / "cleaned_data"
    raw_data_path = raw_data_dir / f"{model_name}.jsonl"
    query_path = paths["race"] / "query.jsonl"

    _write_jsonl(query_path, [question])
    _write_jsonl(
        raw_data_path,
        [{"id": q_no, "prompt": question["prompt"], "article": article_text}],
    )

    command = [
        sys.executable,
        "deepresearch_bench_race.py",
        model_name,
        "--limit",
        "1",
        "--raw_data_dir",
        str(raw_data_dir),
        "--cleaned_data_dir",
        str(cleaned_data_dir),
        "--query_file",
        str(query_path),
        "--output_dir",
        str(paths["race"]),
    ]
    if question.get("language") == "en":
        command.append("--only_en")
    elif question.get("language") == "zh":
        command.append("--only_zh")
    if force:
        command.append("--force")

    result = _run_command(command, cwd=BENCH_CODE_ROOT)
    _write_process_log(paths["race"] / "race_command.log", command, result)
    if result.returncode != 0:
        raise RuntimeError(f"RACE failed; see {paths['race'] / 'race_command.log'}")
    return _parse_key_value_file(paths["race"] / "race_result.txt")


def _run_fact(
    *,
    q_no: int,
    question: dict[str, Any],
    article_text: str,
    paths: dict[str, Path],
) -> dict[str, float]:
    raw_data_path = paths["fact"] / "raw_data.jsonl"
    query_path = paths["fact"] / "query.jsonl"
    extracted_path = paths["fact"] / "extracted.jsonl"
    deduped_path = paths["fact"] / "deduplicated.jsonl"
    scraped_path = paths["fact"] / "scraped.jsonl"
    validated_path = paths["fact"] / "validated.jsonl"
    result_path = paths["fact"] / "fact_result.txt"

    _write_jsonl(query_path, [question])
    _write_jsonl(
        raw_data_path,
        [{"id": q_no, "prompt": question["prompt"], "article": article_text}],
    )

    commands = [
        [
            sys.executable,
            "-m",
            "utils.extract",
            "--output_path",
            str(extracted_path),
            "--raw_data_path",
            str(raw_data_path),
            "--query_data_path",
            str(query_path),
            "--n_total_process",
            "1",
        ],
        [
            sys.executable,
            "-m",
            "utils.deduplicate",
            "--output_path",
            str(deduped_path),
            "--raw_data_path",
            str(extracted_path),
            "--query_data_path",
            str(query_path),
            "--n_total_process",
            "1",
        ],
        [
            sys.executable,
            "-m",
            "utils.scrape",
            "--output_path",
            str(scraped_path),
            "--raw_data_path",
            str(deduped_path),
            "--n_total_process",
            "4",
        ],
        [
            sys.executable,
            "-m",
            "utils.validate",
            "--output_path",
            str(validated_path),
            "--raw_data_path",
            str(scraped_path),
            "--query_data_path",
            str(query_path),
            "--n_total_process",
            "4",
        ],
        [
            sys.executable,
            "-m",
            "utils.stat",
            "--input_path",
            str(validated_path),
            "--output_path",
            str(result_path),
        ],
    ]

    for index, command in enumerate(commands, start=1):
        result = _run_command(command, cwd=BENCH_CODE_ROOT)
        _write_process_log(paths["fact"] / f"fact_step_{index}.log", command, result)
        if result.returncode != 0:
            raise RuntimeError(f"FACT step {index} failed; see {paths['fact'] / f'fact_step_{index}.log'}")

    return _parse_key_value_file(result_path)


def _write_benchmark_markdown(q_root: Path) -> None:
    summaries: list[dict[str, Any]] = []
    for summary_path in sorted(q_root.glob("*_agent_arc/summary.json")):
        summaries.append(json.loads(summary_path.read_text(encoding="utf-8")))

    lines = [
        f"# Question {q_root.name.removeprefix('q')} Benchmark",
        "",
        "| Architecture | Thread ID | RACE Overall | Comprehensiveness | Insight | Instruction Following | Readability | FACT Valid Rate | Total Tokens | Tavily Calls | Est. LLM Cost | Est. Tavily Cost | Est. Total Cost | Research Tasks | Citation Self-Checks | Report Updates | Report |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    for summary in summaries:
        race = summary.get("race", {}) or {}
        fact = summary.get("fact", {}) or {}
        usage = summary.get("usage", {}) or {}
        cost = summary.get("cost_estimate", {}) or {}
        llm_cost = (cost.get("llm", {}) or {}).get("total_cost_usd", 0.0)
        tavily_cost = (cost.get("tavily", {}) or {}).get("cost_usd", 0.0)
        total_cost = cost.get("total_cost_usd", 0.0)
        total_tokens = (
            usage.get("main_agent_tokens", {}).get("total_tokens", 0)
            + usage.get("subagents_total_tokens", {}).get("total_tokens", 0)
        )
        tavily_calls = usage.get("tavily_tool_calls", {}).get("total", 0)
        artifact_activity = usage.get("artifact_activity", {}) or {}
        research_tasks = artifact_activity.get("research_agent_task_calls", 0)
        self_checks = artifact_activity.get("citation_self_checks", 0)
        report_updates = artifact_activity.get("report_file_updates", 0)
        report_path = summary.get("report_path") or ""
        lines.append(
            "| {arch} | `{thread}` | {overall:.4f} | {comp:.4f} | {insight:.4f} | {inst:.4f} | {read:.4f} | {valid:.4f} | {tokens} | {calls} | ${llm_cost:.4f} | ${tavily_cost:.4f} | ${total_cost:.4f} | {research_tasks} | {self_checks} | {updates} | `{report}` |".format(
                arch=summary.get("architecture", ""),
                thread=summary.get("thread_id", ""),
                overall=race.get("Overall Score", 0.0),
                comp=race.get("Comprehensiveness", 0.0),
                insight=race.get("Insight", 0.0),
                inst=race.get("Instruction Following", 0.0),
                read=race.get("Readability", 0.0),
                valid=fact.get("valid_rate", 0.0),
                tokens=total_tokens,
                calls=tavily_calls,
                llm_cost=float(llm_cost or 0.0),
                tavily_cost=float(tavily_cost or 0.0),
                total_cost=float(total_cost or 0.0),
                research_tasks=research_tasks,
                self_checks=self_checks,
                updates=report_updates,
                report=report_path,
            )
        )

    (q_root / f"{q_root.name}_bm.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _print_score_summary(
    *,
    q_no: int,
    report_path: Path | None,
    race_metrics: dict[str, float],
    fact_metrics: dict[str, float],
    cost_estimate: dict[str, Any],
    summary_path: Path,
    benchmark_path: Path,
) -> None:
    print("\nEvaluation complete.")
    print(f"Question: q{q_no}")
    if report_path is not None:
        print(f"Report: {report_path}")
    if race_metrics:
        print(
            "RACE: "
            f"overall={race_metrics.get('Overall Score', 0.0):.4f}, "
            f"comprehensiveness={race_metrics.get('Comprehensiveness', 0.0):.4f}, "
            f"insight={race_metrics.get('Insight', 0.0):.4f}, "
            f"instruction={race_metrics.get('Instruction Following', 0.0):.4f}, "
            f"readability={race_metrics.get('Readability', 0.0):.4f}"
        )
    if fact_metrics:
        print(
            "FACT: "
            f"valid_rate={fact_metrics.get('valid_rate', 0.0):.4f}, "
            f"valid={fact_metrics.get('total_valid_citations', 0.0):.0f}/"
            f"{fact_metrics.get('total_citations', 0.0):.0f}"
        )
    if cost_estimate:
        print(
            "Estimated cost: "
            f"total=${float(cost_estimate.get('total_cost_usd', 0.0) or 0.0):.4f}, "
            f"llm=${float(((cost_estimate.get('llm', {}) or {}).get('total_cost_usd', 0.0)) or 0.0):.4f}, "
            f"tavily=${float((((cost_estimate.get('tavily', {}) or {}).get('cost_usd', 0.0))) or 0.0):.4f}"
        )
    print(f"summary saved: {summary_path}")
    print(f"benchmark markdown saved: {benchmark_path}")


async def _run(args: argparse.Namespace) -> None:
    _load_env_file(PROJECT_ROOT / ".env")
    question = _read_question(args.q_no)
    paths = _ensure_workspace(args.q_no)
    if args.force_eval:
        _clean_eval_outputs(paths)

    thread_id = args.thread_id or f"q{args.q_no}_{ARCH_TYPE}_{uuid.uuid4().hex[:12]}"
    report_virtual_path = f"/report/q{args.q_no}_{ARCH_TYPE}_report.md"
    prompt = (
        question["prompt"].strip()
        + "\n\nWrite the final polished Markdown report to exactly "
        + report_virtual_path
        + ". Use numbered inline citations and a numbered References section with full URLs."
        + " Run coverage review and write a citation self-check; if the self-check finds blocking issues, repair them before finalizing."
        + " For non-trivial sourced research, first perform only a scout: your first todo list must contain exactly one in-progress scout item and no skeleton, research-batch, synthesis, review, citation-check, or finalization items. After the scout handoff returns, write the report skeleton, then create the detailed plan, then launch follow-up research batches."
    )

    os.environ["DEEP_AGENT_WORKSPACE_ROOT"] = str(paths["arch_root"])
    run_started_at = time.time()
    start = time.monotonic()
    if args.skip_agent:
        report_path = paths["arch_root"] / report_virtual_path.removeprefix("/")
        if not report_path.exists():
            raise FileNotFoundError(f"Expected existing report at {report_path}")
        article_text = report_path.read_text(encoding="utf-8")
        from agent_arcs.deep_agent_arc.smoke_test import _report_placeholder_issues

        placeholder_issues = _report_placeholder_issues(article_text)
        if placeholder_issues:
            issue_text = "; ".join(placeholder_issues)
            raise RuntimeError(f"Report at {report_path} is still a skeleton: {issue_text}")
        metrics_path = None
        run_id = None
    else:
        from agent_arcs.deep_agent_arc.smoke_test import _report_placeholder_issues, _stream_run

        result = None
        article_text = ""
        completion_attempts = MAX_COMPLETION_ATTEMPTS
        for attempt in range(1, completion_attempts + 1):
            run_prompt = prompt if attempt == 1 else _completion_repair_prompt(
                base_prompt=prompt,
                report_virtual_path=report_virtual_path,
                attempt=attempt,
            )
            result = await _stream_run(
                prompt=run_prompt,
                thread_id=thread_id,
                expected_report_path=report_virtual_path,
                fail_on_incomplete_report=False,
            )
            article_text = result.article_text
            if result.report_path is None:
                print(
                    f"report incomplete after attempt {attempt}/{completion_attempts}: "
                    f"expected report file was not written at {report_virtual_path}. continuing..."
                )
                continue
            placeholder_issues = _report_placeholder_issues(article_text)
            if article_text.strip() and not placeholder_issues:
                break
            issue_text = "; ".join(placeholder_issues) if placeholder_issues else "empty report"
            print(
                f"report incomplete after attempt {attempt}/{completion_attempts}: {issue_text}. "
                "continuing..."
            )
        if result is None:
            raise RuntimeError("Agent did not run.")
        if result.report_path is None:
            raise RuntimeError(
                f"Report file was not written after {completion_attempts} attempts: {report_virtual_path}"
            )
        placeholder_issues = _report_placeholder_issues(article_text)
        if placeholder_issues:
            issue_text = "; ".join(placeholder_issues)
            raise RuntimeError(f"Report is still incomplete after {completion_attempts} attempts: {issue_text}")
        report_path = result.report_path
        metrics_path = result.metrics_path
        run_id = result.run_id

    if not article_text.strip():
        raise RuntimeError("Agent did not produce article text for evaluation.")
    citation_failure = citation_integrity_failure(article_text)
    if citation_failure and not args.skip_eval:
        print(f"WARNING: citation integrity check found a non-blocking issue: {citation_failure}")
    usage = _read_run_metrics(metrics_path)
    if not args.skip_agent:
        current_run_failure = _current_run_artifact_failure(
            paths=paths,
            report_path=report_path,
            usage=usage,
            run_started_at=run_started_at,
        )
        if current_run_failure:
            if args.skip_eval:
                print(f"WARNING: {current_run_failure}")
            else:
                raise RuntimeError(current_run_failure)
    research_failure = _research_handoff_failure(usage=usage, article_text=article_text)
    if research_failure and not args.skip_eval:
        raise RuntimeError(research_failure)
    review_failure = _review_artifact_failure(paths)
    if review_failure and not args.skip_eval:
        raise RuntimeError(
            review_failure
            + "; use a fresh thread or continue the agent to complete review artifacts before eval."
        )

    race_metrics: dict[str, float] = {}
    fact_metrics: dict[str, float] = {}
    if not args.skip_eval:
        print("\nRunning RACE evaluation...")
        race_metrics = _run_race(
            q_no=args.q_no,
            question=question,
            article_text=article_text,
            paths=paths,
            force=args.force_eval,
        )
        print("Running FACT evaluation...")
        fact_metrics = _run_fact(
            q_no=args.q_no,
            question=question,
            article_text=article_text,
            paths=paths,
        )

    cost_estimate = estimate_run_cost(
        usage=usage,
        main_model=os.getenv("AI_MODEL"),
        subagent_model=os.getenv("SUB_MODEL"),
    )
    summary = {
        "question_id": args.q_no,
        "architecture": ARCH_DIR_NAME,
        "arch_type": ARCH_TYPE,
        "thread_id": thread_id,
        "run_id": run_id,
        "prompt": question["prompt"],
        "language": question.get("language"),
        "topic": question.get("topic"),
        "report_path": str(report_path) if report_path else None,
        "report_word_count": _report_word_count(article_text),
        "metrics_path": str(metrics_path) if metrics_path else None,
        "race": race_metrics,
        "fact": fact_metrics,
        "usage": usage,
        "cost_estimate": cost_estimate,
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "saved_at": datetime.now(UTC).isoformat(),
    }
    summary_path = paths["arch_root"] / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_benchmark_markdown(paths["q_root"])

    _print_score_summary(
        q_no=args.q_no,
        report_path=report_path,
        race_metrics=race_metrics,
        fact_metrics=fact_metrics,
        cost_estimate=cost_estimate,
        summary_path=summary_path,
        benchmark_path=paths["q_root"] / f"q{args.q_no}_bm.md",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one research question with the configured workflow.")
    parser.add_argument("--q-no", type=int, required=True, help="Question id from deep_research_bench/data/prompt_data/query.jsonl.")
    parser.add_argument("--thread-id", help="Optional explicit LangGraph thread id.")
    parser.add_argument("--skip-agent", action="store_true", help="Reuse an existing report instead of running the agent.")
    parser.add_argument("--skip-eval", action="store_true", help="Skip RACE and FACT evaluation.")
    parser.add_argument("--force-eval", action="store_true", help="Remove prior RACE/FACT outputs before evaluating.")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
