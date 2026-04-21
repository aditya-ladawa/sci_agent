from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_arcs.deep_agent_arc.deep_agent_arch import WORKSPACE_ROOT
from agent_arcs.deep_agent_arc.smoke_test import _load_env_file, _stream_run

BENCHMARK_ROOT = PROJECT_ROOT / "deep_research_bench"
DEFAULT_QUERY_FILE = BENCHMARK_ROOT / "data" / "prompt_data" / "query.jsonl"
BENCHMARK_RUNS_DIR = WORKSPACE_ROOT / "benchmark_runs"
DEFAULT_BENCHMARK_MODEL_NAME = "deep-agent-openrouter"


def _build_prompt_with_report_requirement(prompt: str, report_virtual_path: str) -> str:
    return (
        f"{prompt}\n\n"
        "Operational requirements:\n"
        f"- Write exactly one final polished markdown report to `{report_virtual_path}`.\n"
        "- Put the complete final deliverable in that file.\n"
        "- Do not create any other polished final report markdown files for this task.\n"
        "- Use at most 5 top-level todos.\n"
        "- Prefer a small number of high-signal research tasks over broad exploration.\n"
        "- Prioritize official Japanese government sources, IPSS, UN, and a few high-quality market sources.\n"
        "- Stop researching once you have enough evidence for a solid report; do not keep searching for minor incremental improvements.\n"
        "- Aim for one concise but complete report rather than exhaustive notes.\n"
        "- Before concluding, verify that the file exists and contains the final report.\n"
        "- In your final response, state the exact report path.\n"
    )


def _generate_thread_id(query_id: int) -> str:
    suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    return f"deep-research-bench-q{query_id}-{suffix}"


def _load_query_record(query_file: Path, query_id: int) -> dict[str, Any]:
    for raw_line in query_file.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        record = json.loads(raw_line)
        if int(record.get("id")) == query_id:
            return record
    raise ValueError(f"Query id {query_id} not found in {query_file}")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _stream_subprocess(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    printable = " ".join(cmd)
    print(f"\n[benchmark] {printable}\n")
    process = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
    return_code = process.wait()
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, cmd)


def _parse_race_metrics(race_dir: Path) -> dict[str, float]:
    raw_results_path = race_dir / "raw_results.jsonl"
    if raw_results_path.exists():
        rows = [
            json.loads(line)
            for line in raw_results_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        successful_rows = [row for row in rows if "error" not in row]
        if successful_rows:
            row = successful_rows[0]
            return {
                "comprehensiveness": float(row.get("comprehensiveness", 0.0)),
                "insight": float(row.get("insight", 0.0)),
                "instruction_following": float(row.get("instruction_following", 0.0)),
                "readability": float(row.get("readability", 0.0)),
                "overall_score": float(row.get("overall_score", 0.0)),
            }

    result_path = race_dir / "race_result.txt"
    metrics: dict[str, float] = {}
    for raw_line in result_path.read_text(encoding="utf-8").splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        normalized = key.strip().lower().replace(" ", "_")
        metrics[normalized] = float(value.strip())
    return {
        "comprehensiveness": metrics.get("comprehensiveness", 0.0),
        "insight": metrics.get("insight", 0.0),
        "instruction_following": metrics.get("instruction_following", 0.0),
        "readability": metrics.get("readability", 0.0),
        "overall_score": metrics.get("overall_score", 0.0),
    }


def _parse_fact_metrics(fact_result_path: Path) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for raw_line in fact_result_path.read_text(encoding="utf-8").splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        metrics[key.strip()] = float(value.strip())
    return {
        "total_citations": metrics.get("total_citations", 0.0),
        "total_valid_citations": metrics.get("total_valid_citations", 0.0),
        "valid_rate": metrics.get("valid_rate", 0.0),
    }


def _compute_fact_metrics_from_validated(validated_path: Path) -> dict[str, float]:
    total_citations = 0
    total_valid_citations = 0
    total_num = 0

    for raw_line in validated_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        record = json.loads(raw_line)
        if not record.get("citations"):
            continue

        for citation in record.get("citations_deduped", {}).values():
            if citation.get("validate_error") is not None:
                continue
            for result in citation.get("validate_res", []):
                if result.get("result") != "unknown":
                    total_citations += 1
                    if result.get("result") == "supported":
                        total_valid_citations += 1

        total_num += 1

    if total_num == 0:
        return {
            "total_citations": 0.0,
            "total_valid_citations": 0.0,
            "valid_rate": 0.0,
        }

    return {
        "total_citations": total_citations / total_num,
        "total_valid_citations": total_valid_citations / total_num,
        "valid_rate": (total_valid_citations / total_citations) if total_citations else 0.0,
    }


def _write_fact_result(path: Path, metrics: dict[str, float]) -> None:
    path.write_text(
        "\n".join(
            [
                f"total_citations: {metrics['total_citations']}",
                f"total_valid_citations: {metrics['total_valid_citations']}",
                f"valid_rate: {metrics['valid_rate']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _build_summary(
    *,
    query_id: int,
    prompt: str,
    thread_id: str,
    run_id: str,
    article_path: Path,
    agent_run_metrics_path: Path,
    race_dir: Path,
    fact_dir: Path,
) -> dict[str, Any]:
    agent_metrics = json.loads(agent_run_metrics_path.read_text(encoding="utf-8"))
    race_metrics = _parse_race_metrics(race_dir)
    fact_result_path = fact_dir / "fact_result.txt"
    fact_metrics = _parse_fact_metrics(fact_result_path)

    return {
        "thread_id": thread_id,
        "run_id": run_id,
        "query_id": query_id,
        "prompt": prompt,
        "article_path": str(article_path),
        "agent_run_metrics_path": str(agent_run_metrics_path),
        "main_agent_tokens": agent_metrics.get("main_agent_tokens", {}),
        "subagents_total_tokens": agent_metrics.get("subagents_total_tokens", {}),
        "subagent_tokens_by_name": agent_metrics.get("subagent_tokens_by_name", {}),
        "tavily_tool_calls": agent_metrics.get("tavily_tool_calls", {}),
        "race_metrics": race_metrics,
        "fact_metrics": fact_metrics,
        "paths": {
            "race_dir": str(race_dir),
            "fact_dir": str(fact_dir),
            "race_result_path": str(race_dir / "race_result.txt"),
            "fact_result_path": str(fact_result_path),
        },
    }


def _validate_agent_metrics(path: Path, *, expected_thread_id: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required_keys = {
        "run_id",
        "thread_id",
        "prompt",
        "main_agent_tokens",
        "subagents_total_tokens",
        "subagent_tokens_by_name",
        "tavily_tool_calls",
    }
    missing = sorted(required_keys - payload.keys())
    if missing:
        raise RuntimeError(f"Metrics file missing required keys: {missing}")
    if payload.get("thread_id") != expected_thread_id:
        raise RuntimeError(
            f"Metrics thread_id mismatch: expected {expected_thread_id}, got {payload.get('thread_id')}"
        )
    return payload


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run one DeepResearch Bench question through the deep agent.")
    parser.add_argument("--query-id", type=int, required=True, help="Benchmark query id to run.")
    parser.add_argument(
        "--thread-id",
        type=str,
        default=None,
        help="Optional LangGraph thread id. Defaults to a fresh id per run.",
    )
    parser.add_argument(
        "--benchmark-model-name",
        type=str,
        default=DEFAULT_BENCHMARK_MODEL_NAME,
        help="Model name used for benchmark raw_data output files.",
    )
    parser.add_argument(
        "--query-file",
        type=Path,
        default=DEFAULT_QUERY_FILE,
        help="Path to the DeepResearch Bench query.jsonl file.",
    )
    args = parser.parse_args()

    _load_env_file(PROJECT_ROOT / ".env")

    thread_id = args.thread_id or _generate_thread_id(args.query_id)
    query_record = _load_query_record(args.query_file, args.query_id)
    prompt = str(query_record["prompt"])

    run_dir = BENCHMARK_RUNS_DIR / thread_id
    raw_data_dir = run_dir / "raw_data"
    cleaned_data_dir = run_dir / "cleaned_data"
    race_dir = run_dir / "race"
    fact_dir = run_dir / "fact"
    query_output_path = run_dir / "query.jsonl"
    raw_data_path = raw_data_dir / f"{args.benchmark_model_name}.jsonl"
    summary_path = run_dir / "summary.json"
    report_virtual_path = f"/reports/deep_research_bench_q{args.query_id}_{thread_id}.md"

    run_dir.mkdir(parents=True, exist_ok=True)
    raw_data_dir.mkdir(parents=True, exist_ok=True)
    cleaned_data_dir.mkdir(parents=True, exist_ok=True)
    race_dir.mkdir(parents=True, exist_ok=True)
    fact_dir.mkdir(parents=True, exist_ok=True)

    _write_jsonl(query_output_path, [query_record])

    benchmark_prompt = _build_prompt_with_report_requirement(prompt, report_virtual_path)
    stream_result = await _stream_run(
        prompt=benchmark_prompt,
        thread_id=thread_id,
        expected_report_path=report_virtual_path,
    )
    if not stream_result.article_text.strip():
        raise RuntimeError("Could not capture a final main-agent article from the streamed run.")
    if stream_result.report_path is None or not stream_result.report_path.exists():
        raise RuntimeError("The agent did not write the required final markdown report.")
    _validate_agent_metrics(stream_result.metrics_path, expected_thread_id=thread_id)
    _write_jsonl(
        raw_data_path,
        [
            {
                "id": query_record["id"],
                "prompt": prompt,
                "article": stream_result.article_text,
            }
        ],
    )

    env = os.environ.copy()

    race_cmd = [
        sys.executable,
        "-u",
        "deepresearch_bench_race.py",
        args.benchmark_model_name,
        "--raw_data_dir",
        str(raw_data_dir),
        "--cleaned_data_dir",
        str(cleaned_data_dir),
        "--max_workers",
        "1",
        "--limit",
        "1",
        "--force",
        "--query_file",
        str(query_output_path),
        "--output_dir",
        str(race_dir),
    ]
    _stream_subprocess(race_cmd, cwd=BENCHMARK_ROOT, env=env)

    extracted_path = fact_dir / "extracted.jsonl"
    deduplicated_path = fact_dir / "deduplicated.jsonl"
    scraped_path = fact_dir / "scraped.jsonl"
    validated_path = fact_dir / "validated.jsonl"
    fact_result_path = fact_dir / "fact_result.txt"

    fact_commands = [
        [
            sys.executable,
            "-u",
            "-m",
            "utils.extract",
            "--raw_data_path",
            str(raw_data_path),
            "--output_path",
            str(extracted_path),
            "--query_data_path",
            str(query_output_path),
            "--n_total_process",
            "1",
        ],
        [
            sys.executable,
            "-u",
            "-m",
            "utils.deduplicate",
            "--raw_data_path",
            str(extracted_path),
            "--output_path",
            str(deduplicated_path),
            "--query_data_path",
            str(query_output_path),
            "--n_total_process",
            "1",
        ],
        [
            sys.executable,
            "-u",
            "-m",
            "utils.scrape",
            "--raw_data_path",
            str(deduplicated_path),
            "--output_path",
            str(scraped_path),
            "--n_total_process",
            "1",
        ],
        [
            sys.executable,
            "-u",
            "-m",
            "utils.validate",
            "--raw_data_path",
            str(scraped_path),
            "--output_path",
            str(validated_path),
            "--query_data_path",
            str(query_output_path),
            "--n_total_process",
            "1",
        ],
    ]
    for cmd in fact_commands:
        _stream_subprocess(cmd, cwd=BENCHMARK_ROOT, env=env)

    stat_cmd = [
        sys.executable,
        "-u",
        "-m",
        "utils.stat",
        "--input_path",
        str(validated_path),
        "--output_path",
        str(fact_result_path),
    ]
    try:
        _stream_subprocess(stat_cmd, cwd=BENCHMARK_ROOT, env=env)
    except subprocess.CalledProcessError:
        fallback_metrics = _compute_fact_metrics_from_validated(validated_path)
        _write_fact_result(fact_result_path, fallback_metrics)
        print(f"[benchmark] wrote FACT fallback metrics to {fact_result_path}")

    summary = _build_summary(
        query_id=args.query_id,
        prompt=prompt,
        thread_id=stream_result.thread_id,
        run_id=stream_result.run_id,
        article_path=stream_result.report_path,
        agent_run_metrics_path=stream_result.metrics_path,
        race_dir=race_dir,
        fact_dir=fact_dir,
    )
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\nsummary saved: {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())
