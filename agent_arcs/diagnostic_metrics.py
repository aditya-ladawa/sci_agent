from __future__ import annotations

import asyncio
import math
import random
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from langchain.agents.middleware import ModelRetryMiddleware, SummarizationMiddleware, ToolRetryMiddleware
from deepagents.middleware.summarization import SummarizationMiddleware as DeepAgentsSummarizationMiddleware


URL_RE = re.compile(r"https?://[^\s)\]}>'\"]+")


@dataclass(slots=True)
class RuntimeDiagnostics:
    tool_retry_count: int = 0
    model_retry_count: int = 0
    failed_tool_call_count: int = 0
    failed_model_call_count: int = 0
    summarization_count: int = 0
    tool_retries_by_tool: Counter[str] = field(default_factory=Counter)
    model_retries_by_model: Counter[str] = field(default_factory=Counter)
    failed_tool_calls_by_tool: Counter[str] = field(default_factory=Counter)
    failed_model_calls_by_model: Counter[str] = field(default_factory=Counter)
    summarizations_by_agent: Counter[str] = field(default_factory=Counter)


_runtime_diagnostics = RuntimeDiagnostics()


def _calculate_retry_delay(
    retry_number: int,
    *,
    backoff_factor: float,
    initial_delay: float,
    max_delay: float,
    jitter: bool,
) -> float:
    if backoff_factor == 0.0:
        delay = initial_delay
    else:
        delay = initial_delay * (backoff_factor**retry_number)

    delay = min(delay, max_delay)

    if jitter and delay > 0:
        jitter_amount = delay * 0.25
        delay += random.uniform(-jitter_amount, jitter_amount)
        delay = max(0.0, delay)

    return delay


def _should_retry_exception(exc: Exception, retry_on: Any) -> bool:
    if callable(retry_on):
        return bool(retry_on(exc))
    return isinstance(exc, retry_on)


def reset_runtime_diagnostics() -> None:
    global _runtime_diagnostics
    _runtime_diagnostics = RuntimeDiagnostics()


def snapshot_runtime_diagnostics() -> dict[str, Any]:
    return {
        "tool_retry_count": _runtime_diagnostics.tool_retry_count,
        "model_retry_count": _runtime_diagnostics.model_retry_count,
        "failed_tool_call_count": _runtime_diagnostics.failed_tool_call_count,
        "failed_model_call_count": _runtime_diagnostics.failed_model_call_count,
        "summarization_count": _runtime_diagnostics.summarization_count,
        "tool_retries_by_tool": dict(sorted(_runtime_diagnostics.tool_retries_by_tool.items())),
        "model_retries_by_model": dict(sorted(_runtime_diagnostics.model_retries_by_model.items())),
        "failed_tool_calls_by_tool": dict(sorted(_runtime_diagnostics.failed_tool_calls_by_tool.items())),
        "failed_model_calls_by_model": dict(sorted(_runtime_diagnostics.failed_model_calls_by_model.items())),
        "summarizations_by_agent": dict(sorted(_runtime_diagnostics.summarizations_by_agent.items())),
    }


def record_summarization_event(diagnostic_label: str) -> None:
    _runtime_diagnostics.summarization_count += 1
    _runtime_diagnostics.summarizations_by_agent[diagnostic_label] += 1


def _model_label(model: Any) -> str:
    for attr in ("model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return type(model).__name__


class DiagnosticToolRetryMiddleware(ToolRetryMiddleware):
    def _record_retry(self, tool_name: str) -> None:
        _runtime_diagnostics.tool_retry_count += 1
        _runtime_diagnostics.tool_retries_by_tool[tool_name] += 1

    def _record_failure(self, tool_name: str) -> None:
        _runtime_diagnostics.failed_tool_call_count += 1
        _runtime_diagnostics.failed_tool_calls_by_tool[tool_name] += 1

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        tool_name = request.tool.name if request.tool else request.tool_call["name"]
        if not self._should_retry_tool(tool_name):
            return handler(request)

        tool_call_id = request.tool_call["id"]
        for attempt in range(self.max_retries + 1):
            try:
                return handler(request)
            except Exception as exc:
                attempts_made = attempt + 1
                if not _should_retry_exception(exc, self.retry_on):
                    self._record_failure(tool_name)
                    return self._handle_failure(tool_name, tool_call_id, exc, attempts_made)

                if attempt < self.max_retries:
                    self._record_retry(tool_name)
                    delay = _calculate_retry_delay(
                        attempt,
                        backoff_factor=self.backoff_factor,
                        initial_delay=self.initial_delay,
                        max_delay=self.max_delay,
                        jitter=self.jitter,
                    )
                    if delay > 0:
                        time.sleep(delay)
                else:
                    self._record_failure(tool_name)
                    return self._handle_failure(tool_name, tool_call_id, exc, attempts_made)

        raise RuntimeError("Unexpected: retry loop completed without returning")

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        tool_name = request.tool.name if request.tool else request.tool_call["name"]
        if not self._should_retry_tool(tool_name):
            return await handler(request)

        tool_call_id = request.tool_call["id"]
        for attempt in range(self.max_retries + 1):
            try:
                return await handler(request)
            except Exception as exc:
                attempts_made = attempt + 1
                if not _should_retry_exception(exc, self.retry_on):
                    self._record_failure(tool_name)
                    return self._handle_failure(tool_name, tool_call_id, exc, attempts_made)

                if attempt < self.max_retries:
                    self._record_retry(tool_name)
                    delay = _calculate_retry_delay(
                        attempt,
                        backoff_factor=self.backoff_factor,
                        initial_delay=self.initial_delay,
                        max_delay=self.max_delay,
                        jitter=self.jitter,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    self._record_failure(tool_name)
                    return self._handle_failure(tool_name, tool_call_id, exc, attempts_made)

        raise RuntimeError("Unexpected: retry loop completed without returning")


class DiagnosticModelRetryMiddleware(ModelRetryMiddleware):
    def _record_retry(self, model: Any) -> None:
        model_label = _model_label(model)
        _runtime_diagnostics.model_retry_count += 1
        _runtime_diagnostics.model_retries_by_model[model_label] += 1

    def _record_failure(self, model: Any) -> None:
        model_label = _model_label(model)
        _runtime_diagnostics.failed_model_call_count += 1
        _runtime_diagnostics.failed_model_calls_by_model[model_label] += 1

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        for attempt in range(self.max_retries + 1):
            try:
                return handler(request)
            except Exception as exc:
                attempts_made = attempt + 1
                if not _should_retry_exception(exc, self.retry_on):
                    self._record_failure(request.model)
                    return self._handle_failure(exc, attempts_made)

                if attempt < self.max_retries:
                    self._record_retry(request.model)
                    delay = _calculate_retry_delay(
                        attempt,
                        backoff_factor=self.backoff_factor,
                        initial_delay=self.initial_delay,
                        max_delay=self.max_delay,
                        jitter=self.jitter,
                    )
                    if delay > 0:
                        time.sleep(delay)
                else:
                    self._record_failure(request.model)
                    return self._handle_failure(exc, attempts_made)

        raise RuntimeError("Unexpected: retry loop completed without returning")

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        for attempt in range(self.max_retries + 1):
            try:
                return await handler(request)
            except Exception as exc:
                attempts_made = attempt + 1
                if not _should_retry_exception(exc, self.retry_on):
                    self._record_failure(request.model)
                    return self._handle_failure(exc, attempts_made)

                if attempt < self.max_retries:
                    self._record_retry(request.model)
                    delay = _calculate_retry_delay(
                        attempt,
                        backoff_factor=self.backoff_factor,
                        initial_delay=self.initial_delay,
                        max_delay=self.max_delay,
                        jitter=self.jitter,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    self._record_failure(request.model)
                    return self._handle_failure(exc, attempts_made)

        raise RuntimeError("Unexpected: retry loop completed without returning")


class DiagnosticSummarizationMiddleware(SummarizationMiddleware):
    def __init__(self, *args: Any, diagnostic_label: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.diagnostic_label = diagnostic_label

    def _record_summarization(self) -> None:
        record_summarization_event(self.diagnostic_label)

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        result = super().before_model(state, runtime)
        if result is not None:
            self._record_summarization()
        return result

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        result = await super().abefore_model(state, runtime)
        if result is not None:
            self._record_summarization()
        return result


class DiagnosticDeepSummarizationMiddleware(DeepAgentsSummarizationMiddleware):
    def __init__(self, *args: Any, diagnostic_label: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.diagnostic_label = diagnostic_label

    def _record_summarization(self) -> None:
        record_summarization_event(self.diagnostic_label)

    @staticmethod
    def _has_summarization_update(result: Any) -> bool:
        command = getattr(result, "command", None)
        update = getattr(command, "update", None)
        return isinstance(update, dict) and "_summarization_event" in update

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        result = super().wrap_model_call(request, handler)
        if self._has_summarization_update(result):
            self._record_summarization()
        return result

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        result = await super().awrap_model_call(request, handler)
        if self._has_summarization_update(result):
            self._record_summarization()
        return result

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        result = await super().abefore_model(state, runtime)
        if result is not None:
            self._record_summarization()
        return result


def estimate_text_tokens(text: str) -> int:
    return max(0, round(len(text) / 4))


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _domain(url: str) -> str | None:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None


def source_metrics_from_text(text: str) -> dict[str, Any]:
    all_urls = [match.group(0).rstrip(".,;:") for match in URL_RE.finditer(text)]
    unique_urls = list(dict.fromkeys(all_urls))
    domain_counts = Counter(domain for url in unique_urls if (domain := _domain(url)))
    domains = sorted(domain_counts)
    source_domain_entropy = 0.0
    if unique_urls and domain_counts:
        total_domains = sum(domain_counts.values())
        source_domain_entropy = -sum(
            (count / total_domains) * math.log2(count / total_domains)
            for count in domain_counts.values()
            if count > 0
        )
    return {
        "url_reference_count": len(all_urls),
        "unique_source_count": len(unique_urls),
        "duplicate_url_count": max(0, len(all_urls) - len(unique_urls)),
        "unique_domain_count": len(domains),
        "source_domain_entropy": round(source_domain_entropy, 4),
        "source_domain_distribution": dict(sorted(domain_counts.items())),
        "domains": domains,
    }


def _metric_float(metrics: dict[str, Any], key: str) -> float:
    try:
        return float(metrics.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def research_breadth_metrics(
    *,
    source_metrics: dict[str, Any],
    fact_metrics: dict[str, Any],
    race_metrics: dict[str, Any],
    domain_target: int = 25,
) -> dict[str, Any]:
    unique_source_count = int(source_metrics.get("unique_source_count", 0) or 0)
    unique_domain_count = int(source_metrics.get("unique_domain_count", 0) or 0)
    source_domain_entropy = _metric_float(source_metrics, "source_domain_entropy")
    fact_valid_rate = _metric_float(fact_metrics, "valid_rate")
    race_comprehensiveness = _metric_float(race_metrics, "Comprehensiveness")
    normalized_domain_breadth = min(unique_domain_count / domain_target, 1.0) if domain_target > 0 else 0.0

    return {
        "unique_source_count": unique_source_count,
        "unique_domain_count": unique_domain_count,
        "source_domain_entropy": round(source_domain_entropy, 4),
        "valid_source_breadth": round(unique_source_count * fact_valid_rate, 4),
        "accurate_breadth_score": round(fact_valid_rate * race_comprehensiveness * normalized_domain_breadth, 4),
        "formula": {
            "valid_source_breadth": "unique_source_count * FACT valid_rate",
            "accurate_breadth_score": "FACT valid_rate * RACE Comprehensiveness * min(unique_domain_count / 25, 1)",
            "source_domain_entropy": "Shannon entropy in bits over domains of unique cited source URLs",
        },
    }


def search_tool_efficiency(tool_call_counts: dict[str, int]) -> dict[str, Any]:
    search_calls = sum(count for name, count in tool_call_counts.items() if "search" in name.lower())
    extract_calls = sum(count for name, count in tool_call_counts.items() if "extract" in name.lower())
    return {
        "search_calls": search_calls,
        "extract_calls": extract_calls,
        "search_to_extract_ratio": None if extract_calls == 0 else round(search_calls / extract_calls, 4),
    }


def summarize_handoffs(handoffs: list[dict[str, Any]]) -> dict[str, Any]:
    if not handoffs:
        return {
            "count": 0,
            "total_estimated_tokens": 0,
            "min_estimated_tokens": 0,
            "max_estimated_tokens": 0,
            "avg_estimated_tokens": 0,
            "by_subagent": {},
            "handoffs": [],
        }

    token_counts = [int(item.get("estimated_tokens", 0) or 0) for item in handoffs]
    by_subagent: dict[str, list[int]] = defaultdict(list)
    for item in handoffs:
        by_subagent[str(item.get("subagent_type") or "unknown")].append(int(item.get("estimated_tokens", 0) or 0))

    return {
        "count": len(handoffs),
        "total_estimated_tokens": sum(token_counts),
        "min_estimated_tokens": min(token_counts),
        "max_estimated_tokens": max(token_counts),
        "avg_estimated_tokens": round(sum(token_counts) / len(token_counts), 2),
        "by_subagent": {
            name: {
                "count": len(values),
                "total_estimated_tokens": sum(values),
                "avg_estimated_tokens": round(sum(values) / len(values), 2),
            }
            for name, values in sorted(by_subagent.items())
        },
        "handoffs": handoffs,
    }


def source_overlap_between_handoffs(handoffs: list[dict[str, Any]]) -> dict[str, Any]:
    indexed: list[tuple[str, set[str]]] = []
    for index, item in enumerate(handoffs, start=1):
        urls = set(item.get("urls") or [])
        if urls:
            label = f"{item.get('subagent_type') or 'unknown'}#{index}"
            indexed.append((label, urls))

    overlap_pairs: list[dict[str, Any]] = []
    for left_index, (left_label, left_urls) in enumerate(indexed):
        for right_label, right_urls in indexed[left_index + 1 :]:
            overlap = sorted(left_urls & right_urls)
            if overlap:
                overlap_pairs.append(
                    {
                        "left": left_label,
                        "right": right_label,
                        "overlap_count": len(overlap),
                        "overlap_urls": overlap[:20],
                    }
                )

    return {
        "method": "URL overlap extracted from subagent task handoff text; semantic source overlap is not inferred.",
        "handoffs_with_urls": len(indexed),
        "overlap_pair_count": len(overlap_pairs),
        "max_overlap_count": max((pair["overlap_count"] for pair in overlap_pairs), default=0),
        "overlap_pairs": overlap_pairs[:50],
    }
