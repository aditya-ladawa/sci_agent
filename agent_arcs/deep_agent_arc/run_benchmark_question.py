from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
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

from agent_arcs.citation_integrity import citation_integrity_failure
from agent_arcs.costs import estimate_run_cost
from agent_arcs.diagnostic_metrics import research_breadth_metrics, source_metrics_from_text
from agent_arcs.langfuse_tracing import configure_langfuse


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
        "conversation_history": arch_root / "conversation_history",
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


def _metric_float(metrics: dict[str, Any], key: str) -> float:
    try:
        return float(metrics.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _total_tokens(usage: dict[str, Any]) -> int:
    return int(
        (usage.get("main_agent_tokens", {}) or {}).get("total_tokens", 0) or 0
    ) + int((usage.get("subagents_total_tokens", {}) or {}).get("total_tokens", 0) or 0)


def _langfuse_score_payload(
    *,
    race_metrics: dict[str, float],
    fact_metrics: dict[str, float],
    source_metrics: dict[str, Any],
    research_breadth: dict[str, Any],
    usage: dict[str, Any],
    cost_estimate: dict[str, Any],
    report_word_count: int,
    elapsed_seconds: float,
) -> dict[str, float | int]:
    scores: dict[str, float | int] = {
        "report_word_count": report_word_count,
        "elapsed_seconds": elapsed_seconds,
        "total_tokens": _total_tokens(usage),
        "ddgs_calls": int((usage.get("ddgs_tool_calls", {}) or {}).get("total", 0) or 0),
        "unique_source_count": int(source_metrics.get("unique_source_count", 0) or 0),
        "unique_domain_count": int(source_metrics.get("unique_domain_count", 0) or 0),
        "source_domain_entropy": _metric_float(research_breadth, "source_domain_entropy"),
        "valid_source_breadth": _metric_float(research_breadth, "valid_source_breadth"),
        "accurate_breadth_score": _metric_float(research_breadth, "accurate_breadth_score"),
        "summarization_count": int((usage.get("runtime_diagnostics", {}) or {}).get("summarization_count", 0) or 0),
        "large_tool_results_file_count": int(
            (usage.get("context_engineering_artifacts", {}) or {}).get("large_tool_results_file_count", 0) or 0
        ),
        "conversation_history_file_count": int(
            (usage.get("context_engineering_artifacts", {}) or {}).get("conversation_history_file_count", 0) or 0
        ),
    }

    if race_metrics:
        scores.update(
            {
                "race_overall": _metric_float(race_metrics, "Overall Score"),
                "race_comprehensiveness": _metric_float(race_metrics, "Comprehensiveness"),
                "race_insight": _metric_float(race_metrics, "Insight"),
                "race_instruction_following": _metric_float(race_metrics, "Instruction Following"),
                "race_readability": _metric_float(race_metrics, "Readability"),
            }
        )
    if fact_metrics:
        scores.update(
            {
                "fact_valid_rate": _metric_float(fact_metrics, "valid_rate"),
                "fact_total_citations": _metric_float(fact_metrics, "total_citations"),
                "fact_total_valid_citations": _metric_float(fact_metrics, "total_valid_citations"),
            }
        )

    llm_cost = cost_estimate.get("llm", {}) or {}
    ddgs_cost = cost_estimate.get("ddgs", {}) or {}
    scores.update(
        {
            "estimated_total_cost_usd": _metric_float(cost_estimate, "total_cost_usd"),
            "estimated_llm_cost_usd": _metric_float(llm_cost, "total_cost_usd"),
            "estimated_input_llm_cost_usd": _metric_float(llm_cost, "input_cost_usd"),
            "estimated_output_llm_cost_usd": _metric_float(llm_cost, "output_cost_usd"),
            "estimated_ddgs_cost_usd": _metric_float(ddgs_cost, "cost_usd"),
        }
    )
    return scores


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


def _clean_current_run_artifacts(*, paths: dict[str, Path], report_virtual_path: str) -> None:
    expected_paths = [
        paths["arch_root"] / report_virtual_path.removeprefix("/"),
        paths["review"] / "coverage_review.md",
        paths["review"] / "citation_self_check.md",
    ]
    for path in expected_paths:
        if path.exists():
            path.unlink()
    for path in [paths["large_tool_results"], paths["conversation_history"]]:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)


def _clean_eval_outputs(paths: dict[str, Path]) -> None:
    cleaned_data_dir = paths["race"] / "cleaned_data"
    if cleaned_data_dir.exists():
        shutil.rmtree(cleaned_data_dir)
    for output in [
        paths["race"] / "raw_results.jsonl",
        paths["race"] / "race_result.txt",
        paths["race"] / "race_command.log",
        paths["fact"] / "extracted.jsonl",
        paths["fact"] / "deduplicated.jsonl",
        paths["fact"] / "scraped.jsonl",
        paths["fact"] / "validated.jsonl",
        paths["fact"] / "fact_result.txt",
        paths["fact"] / "fact_step_1.log",
        paths["fact"] / "fact_step_2.log",
        paths["fact"] / "fact_step_3.log",
        paths["fact"] / "fact_step_4.log",
        paths["fact"] / "fact_step_5.log",
    ]:
        if output.exists():
            output.unlink()


def _require_metric_keys(metrics: dict[str, float], *, path: Path, keys: set[str]) -> None:
    missing = sorted(key for key in keys if key not in metrics)
    if missing:
        raise RuntimeError(f"Evaluation output {path} is missing required metric keys: {', '.join(missing)}")


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
    result_path = paths["race"] / "race_result.txt"
    metrics = _parse_key_value_file(result_path)
    _require_metric_keys(
        metrics,
        path=result_path,
        keys={"Overall Score", "Comprehensiveness", "Insight", "Instruction Following", "Readability"},
    )
    return metrics


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

    expected_outputs = [extracted_path, deduped_path, scraped_path, validated_path, result_path]
    for index, (command, expected_output) in enumerate(zip(commands, expected_outputs, strict=True), start=1):
        result = _run_command(command, cwd=BENCH_CODE_ROOT)
        _write_process_log(paths["fact"] / f"fact_step_{index}.log", command, result)
        if result.returncode != 0:
            raise RuntimeError(f"FACT step {index} failed; see {paths['fact'] / f'fact_step_{index}.log'}")
        if not expected_output.exists():
            raise RuntimeError(
                f"FACT step {index} completed but did not create {expected_output}; "
                f"see {paths['fact'] / f'fact_step_{index}.log'}"
            )

    metrics = _parse_key_value_file(result_path)
    _require_metric_keys(metrics, path=result_path, keys={"total_citations", "total_valid_citations", "valid_rate"})
    return metrics


def _write_benchmark_markdown(q_root: Path) -> None:
    summaries: list[dict[str, Any]] = []
    for summary_path in sorted(q_root.glob("*_agent_arc/summary.json")):
        summaries.append(json.loads(summary_path.read_text(encoding="utf-8")))

    lines = [
        f"# Question {q_root.name.removeprefix('q')} Benchmark",
        "",
        "| Architecture | Thread ID | RACE Overall | Comprehensiveness | Insight | Instruction Following | Readability | FACT Valid Rate | Total Tokens | DDGS Calls | Summarizations | Large Result Files | Conversation Files | Unique Sources | Unique Domains | Source Domain Entropy | Valid Source Breadth | Accurate Breadth Score | Est. Input LLM Cost | Est. Output LLM Cost | Est. LLM Cost | Est. DDGS Cost | Est. Total Cost | Scout Tasks | Research Tasks | Citation Self-Checks | Review Updates | Report Writes | Report Edits | Edit Failures | Report |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    for summary in summaries:
        race = summary.get("race", {}) or {}
        fact = summary.get("fact", {}) or {}
        usage = summary.get("usage", {}) or {}
        cost = summary.get("cost_estimate", {}) or {}
        source_metrics = summary.get("source_metrics", {}) or {}
        research_breadth = summary.get("research_breadth", {}) or {}
        llm_cost = (cost.get("llm", {}) or {}).get("total_cost_usd", 0.0)
        llm_input_cost = (cost.get("llm", {}) or {}).get("input_cost_usd", 0.0)
        llm_output_cost = (cost.get("llm", {}) or {}).get("output_cost_usd", 0.0)
        ddgs_cost = (cost.get("ddgs", {}) or {}).get("cost_usd", 0.0)
        total_cost = cost.get("total_cost_usd", 0.0)
        total_tokens = (
            usage.get("main_agent_tokens", {}).get("total_tokens", 0)
            + usage.get("subagents_total_tokens", {}).get("total_tokens", 0)
        )
        ddgs_calls = usage.get("ddgs_tool_calls", {}).get("total", 0)
        runtime_diagnostics = usage.get("runtime_diagnostics", {}) or {}
        context_artifacts = usage.get("context_engineering_artifacts", {}) or {}
        artifact_activity = usage.get("artifact_activity", {}) or {}
        scout_tasks = artifact_activity.get("scout_agent_task_calls", 0)
        research_tasks = artifact_activity.get("research_agent_task_calls", 0)
        self_checks = artifact_activity.get("citation_self_checks", 0)
        review_updates = artifact_activity.get("review_file_updates", 0)
        report_writes = artifact_activity.get("report_write_count", 0)
        report_edits = artifact_activity.get("report_edit_count", 0)
        edit_failures = artifact_activity.get("edit_file_failure_count", 0)
        report_path = summary.get("report_path") or ""
        lines.append(
            "| {arch} | `{thread}` | {overall:.4f} | {comp:.4f} | {insight:.4f} | {inst:.4f} | {read:.4f} | {valid:.4f} | {tokens} | {calls} | {summarizations} | {large_result_files} | {conversation_files} | {unique_sources} | {unique_domains} | {source_entropy:.4f} | {valid_source_breadth:.4f} | {accurate_breadth_score:.4f} | ${llm_input_cost:.4f} | ${llm_output_cost:.4f} | ${llm_cost:.4f} | ${ddgs_cost:.4f} | ${total_cost:.4f} | {scout_tasks} | {research_tasks} | {self_checks} | {review_updates} | {report_writes} | {report_edits} | {edit_failures} | `{report}` |".format(
                arch=summary.get("architecture", ""),
                thread=summary.get("thread_id", ""),
                overall=race.get("Overall Score", 0.0),
                comp=race.get("Comprehensiveness", 0.0),
                insight=race.get("Insight", 0.0),
                inst=race.get("Instruction Following", 0.0),
                read=race.get("Readability", 0.0),
                valid=fact.get("valid_rate", 0.0),
                tokens=total_tokens,
                calls=ddgs_calls,
                summarizations=runtime_diagnostics.get("summarization_count", 0),
                large_result_files=context_artifacts.get("large_tool_results_file_count", 0),
                conversation_files=context_artifacts.get("conversation_history_file_count", 0),
                unique_sources=source_metrics.get("unique_source_count", 0),
                unique_domains=source_metrics.get("unique_domain_count", 0),
                source_entropy=float(research_breadth.get("source_domain_entropy", 0.0) or 0.0),
                valid_source_breadth=float(research_breadth.get("valid_source_breadth", 0.0) or 0.0),
                accurate_breadth_score=float(research_breadth.get("accurate_breadth_score", 0.0) or 0.0),
                llm_input_cost=float(llm_input_cost or 0.0),
                llm_output_cost=float(llm_output_cost or 0.0),
                llm_cost=float(llm_cost or 0.0),
                ddgs_cost=float(ddgs_cost or 0.0),
                total_cost=float(total_cost or 0.0),
                scout_tasks=scout_tasks,
                research_tasks=research_tasks,
                self_checks=self_checks,
                review_updates=review_updates,
                report_writes=report_writes,
                report_edits=report_edits,
                edit_failures=edit_failures,
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
    research_breadth: dict[str, Any],
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
    if research_breadth:
        print(
            "Research breadth: "
            f"sources={research_breadth.get('unique_source_count', 0)}, "
            f"domains={research_breadth.get('unique_domain_count', 0)}, "
            f"entropy={float(research_breadth.get('source_domain_entropy', 0.0) or 0.0):.4f}, "
            f"valid_source_breadth={float(research_breadth.get('valid_source_breadth', 0.0) or 0.0):.4f}, "
            f"accurate_breadth={float(research_breadth.get('accurate_breadth_score', 0.0) or 0.0):.4f}"
        )
    if cost_estimate:
        print(
            "Estimated cost: "
            f"total=${float(cost_estimate.get('total_cost_usd', 0.0) or 0.0):.4f}, "
            f"llm=${float(((cost_estimate.get('llm', {}) or {}).get('total_cost_usd', 0.0)) or 0.0):.4f}, "
            f"ddgs=${float((((cost_estimate.get('ddgs', {}) or {}).get('cost_usd', 0.0))) or 0.0):.4f}"
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
        + "\n\nProduce the final polished Markdown report at exactly "
        + report_virtual_path
        + ". Use inline numeric citations like [1] and [1][3]. The final major section must be exactly ## References, must be present whenever inline citations appear, and must contain one uniquely numbered entry for every cited number with a title/source name and full http(s) URL."
        + " Every substantive factual paragraph, factual table row, quantitative value, date, source-position claim, and non-obvious interpretation needs nearby citation support attached to the exact sentence or clause it supports."
        + " Run coverage review and write a citation self-check; if the self-check finds blocking issues, repair them before finalizing."
        + " The runtime has prepared a clean virtual workspace for these expected artifacts: "
        + report_virtual_path
        + ", /tmp/review/coverage_review.md, and /tmp/review/citation_self_check.md."
        + " Never write the complete report in one filesystem call. Use write_file for the report path only to create the initial skeleton if it does not exist; that write_file must not contain the full report. After the report exists, all later report writing must use edit_file section-by-section after reading or searching the current file. Use section-sized edits for drafting and surgical edits only for local repairs. Never globally replace a bare citation marker such as [10]; citation repairs must be anchored to the surrounding sentence, table row, or reference entry. You may add, update, delete, move, or rewrite lines as needed, but do it with targeted edit_file calls rather than whole-report rewrites."
        + " Use write_file for review artifacts only when creating them for the first time; after a review artifact exists, read it and use edit_file for revisions."
        + " Do not write the report or review artifacts to any other directory."
        + " Research and deliverables are text-only: do not use image search, include images, embed Markdown images, collect visual assets, or use direct image URLs as report content."
        + " Every scout-agent or research-agent task prompt must include an explicit DDGS budget line. Use 1-2 calls for the initial scout, 4-8 calls for normal research-agent section work, 8-12 only for source-conflict or multi-source evidence packets, and never give one subagent 20+ calls. A DDGS call means any search_text, search_news, search_books, or extract_content call. Do not use search_images in this text-only workflow. The subagent must stop at the hard limit and return unresolved gaps."
        + " For non-trivial sourced research, first perform only a scout: your first todo list must contain exactly one item total, and that one item must be the in-progress scout. Do not include pending skeleton, research-batch, synthesis, review, citation-check, or finalization items in the first todo list. A first todo list with one scout item plus pending downstream items is invalid. After the scout handoff returns, write the report skeleton, then create the detailed plan, then launch follow-up research batches."
    )

    os.environ["DEEP_AGENT_WORKSPACE_ROOT"] = str(paths["arch_root"])
    langfuse_trace = configure_langfuse(
        architecture=ARCH_TYPE,
        q_no=args.q_no,
        thread_id=thread_id,
        enabled=args.langfuse,
        base_url=args.langfuse_base_url,
    )
    langfuse_message = langfuse_trace.message()
    if langfuse_message:
        print(langfuse_message)

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
        _clean_current_run_artifacts(paths=paths, report_virtual_path=report_virtual_path)
        from agent_arcs.deep_agent_arc.smoke_test import _stream_run

        result = None
        article_text = ""
        try:
            result = await _stream_run(
                prompt=prompt,
                thread_id=thread_id,
                expected_report_path=report_virtual_path,
                fail_on_incomplete_report=False,
                langfuse_trace=langfuse_trace,
            )
        finally:
            langfuse_trace.flush()
        if result.report_path is None and args.continue_if_missing_report:
            continuation_prompt = (
                f"Your previous run ended incorrectly: the stream finished, but the required report file at {report_virtual_path} was never created. "
                "Continue from the current checkpoint state. Fix that mistake now. Write the missing report file at the exact required path, "
                "then verify that the file exists by checking it with the filesystem tools before you stop. Stop only after the report exists and ends with a final ## References section."
            )
            print(
                f"report file not written at {report_virtual_path} on first attempt. "
                "continuing once on same thread..."
            )
            try:
                result = await _stream_run(
                    prompt=continuation_prompt,
                    thread_id=thread_id,
                    expected_report_path=report_virtual_path,
                    fail_on_incomplete_report=False,
                    langfuse_trace=langfuse_trace,
                )
            finally:
                langfuse_trace.flush()
        if result is None:
            raise RuntimeError("Agent did not run.")
        if result.report_path is None:
            raise RuntimeError(
                f"Report file was not written at {report_virtual_path}"
            )
        article_text = result.article_text
        report_path = result.report_path
        metrics_path = result.metrics_path
        run_id = result.run_id
        from agent_arcs.deep_agent_arc.smoke_test import _report_placeholder_issues

        placeholder_issues = _report_placeholder_issues(article_text)
        if placeholder_issues and not args.skip_eval:
            raise RuntimeError(f"Report at {report_path} is still a skeleton: {'; '.join(placeholder_issues)}")

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
    race_metrics: dict[str, float] = {}
    fact_metrics: dict[str, float] = {}
    if not args.skip_eval:
        if not args.skip_agent:
            _clean_eval_outputs(paths)
        print("\nRunning RACE evaluation...")
        race_metrics = _run_race(
            q_no=args.q_no,
            question=question,
            article_text=article_text,
            paths=paths,
            force=args.force_eval or not args.skip_agent,
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
    source_metrics = source_metrics_from_text(article_text)
    research_breadth = research_breadth_metrics(
        source_metrics=source_metrics,
        fact_metrics=fact_metrics,
        race_metrics=race_metrics,
    )
    report_word_count = _report_word_count(article_text)
    elapsed_seconds = round(time.monotonic() - start, 3)
    langfuse_trace.record_scores(
        scores=_langfuse_score_payload(
            race_metrics=race_metrics,
            fact_metrics=fact_metrics,
            source_metrics=source_metrics,
            research_breadth=research_breadth,
            usage=usage,
            cost_estimate=cost_estimate,
            report_word_count=report_word_count,
            elapsed_seconds=elapsed_seconds,
        ),
        metadata={
            "run_id": run_id,
            "report_path": str(report_path) if report_path else None,
            "metrics_path": str(metrics_path) if metrics_path else None,
        },
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
        "report_word_count": report_word_count,
        "metrics_path": str(metrics_path) if metrics_path else None,
        "race": race_metrics,
        "fact": fact_metrics,
        "usage": usage,
        "source_metrics": source_metrics,
        "research_breadth": research_breadth,
        "cost_estimate": cost_estimate,
        "langfuse": langfuse_trace.as_summary(),
        "elapsed_seconds": elapsed_seconds,
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
        research_breadth=research_breadth,
        cost_estimate=cost_estimate,
        summary_path=summary_path,
        benchmark_path=paths["q_root"] / f"q{args.q_no}_bm.md",
    )
    langfuse_trace.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one research question with the configured workflow.")
    parser.add_argument("--q-no", type=int, required=True, help="Question id from deep_research_bench/data/prompt_data/query.jsonl.")
    parser.add_argument("--thread-id", help="Optional explicit LangGraph thread id.")
    parser.add_argument("--skip-agent", action="store_true", help="Reuse an existing report instead of running the agent.")
    parser.add_argument("--skip-eval", action="store_true", help="Skip RACE and FACT evaluation.")
    parser.add_argument("--force-eval", action="store_true", help="Remove prior RACE/FACT outputs before evaluating.")
    parser.add_argument("--continue-if-missing-report", action="store_true", help="If a run finishes without writing the report file, continue once on the same checkpoint thread.")
    parser.add_argument("--langfuse", action="store_true", help="Enable optional Langfuse tracing for this run.")
    parser.add_argument("--langfuse-base-url", help="Langfuse base URL, default http://localhost:3000 or LANGFUSE_BASE_URL.")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
