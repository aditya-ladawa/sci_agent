from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from math import isfinite
from typing import Any


DEFAULT_LANGFUSE_BASE_URL = "http://localhost:3000"


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class LangfuseTraceConfig:
    enabled: bool
    base_url: str | None = None
    session_id: str | None = None
    trace_name: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    handler: Any | None = None
    client: Any | None = None
    error: str | None = None
    score_names: list[str] = field(default_factory=list)
    score_errors: list[str] = field(default_factory=list)
    flush_interval_seconds: float = 30.0
    last_flush_at: float = 0.0

    def message(self) -> str | None:
        if self.enabled:
            return f"Langfuse tracing enabled: session={self.session_id}, base_url={self.base_url}"
        if self.error:
            return f"Langfuse tracing disabled: {self.error}"
        return None

    def as_summary(self) -> dict[str, Any]:
        last_trace_id = getattr(self.handler, "last_trace_id", None) if self.handler is not None else None
        trace_url = None
        if self.client is not None and last_trace_id:
            try:
                trace_url = self.client.get_trace_url(trace_id=last_trace_id)
            except Exception:
                trace_url = None
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "session_id": self.session_id,
            "trace_name": self.trace_name,
            "last_trace_id": last_trace_id,
            "trace_url": trace_url,
            "tags": self.tags,
            "metadata": self.metadata,
            "scores_recorded": len(self.score_names),
            "score_names": self.score_names,
            "score_errors": self.score_errors,
            "error": self.error,
        }

    def update_langchain_config(self, config: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled or self.handler is None:
            return config

        updated = dict(config)
        callbacks = list(updated.get("callbacks") or [])
        callbacks.append(self.handler)
        updated["callbacks"] = callbacks
        updated["run_name"] = self.trace_name
        updated["tags"] = list(dict.fromkeys([*(updated.get("tags") or []), *self.tags]))
        updated["metadata"] = {
            **(updated.get("metadata") or {}),
            **self.metadata,
            "langfuse_session_id": self.session_id,
            "langfuse_trace_name": self.trace_name,
            "langfuse_tags": self.tags,
            "langfuse_user_id": "sci_agent_bm",
        }
        return updated

    def flush(self) -> None:
        if not self.enabled or self.client is None:
            return
        try:
            self.client.flush()
            self.last_flush_at = time.monotonic()
        except Exception:
            return

    def flush_if_due(self) -> None:
        if not self.enabled or self.client is None:
            return
        now = time.monotonic()
        if self.last_flush_at and now - self.last_flush_at < self.flush_interval_seconds:
            return
        self.flush()

    def record_scores(self, *, scores: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
        if not self.enabled or self.client is None:
            return

        trace_id = getattr(self.handler, "last_trace_id", None) if self.handler is not None else None
        if trace_id is None:
            try:
                trace_id = self.client.get_current_trace_id()
            except Exception:
                trace_id = None

        if trace_id is None and self.session_id is None:
            self.score_errors.append("No Langfuse trace_id or session_id available for score recording.")
            return

        score_metadata = {**self.metadata, **(metadata or {})}
        for name, value in sorted(scores.items()):
            if value is None or isinstance(value, bool):
                continue
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                continue
            if not isfinite(numeric_value):
                continue
            try:
                self.client.create_score(
                    name=name,
                    value=numeric_value,
                    trace_id=trace_id,
                    session_id=self.session_id,
                    data_type="NUMERIC",
                    metadata=score_metadata,
                )
                self.score_names.append(name)
            except Exception as exc:
                self.score_errors.append(f"{name}: {type(exc).__name__}: {exc}")

        self.flush()


def configure_langfuse(
    *,
    architecture: str,
    q_no: int,
    thread_id: str,
    enabled: bool = False,
    base_url: str | None = None,
) -> LangfuseTraceConfig:
    requested = enabled or _truthy(os.getenv("LANGFUSE_ENABLED"), default=False)
    resolved_base_url = base_url or os.getenv("LANGFUSE_BASE_URL") or DEFAULT_LANGFUSE_BASE_URL
    session_id = thread_id
    trace_name = f"q{q_no}-{architecture}-{thread_id}"
    tags = [
        "sci_agent_bm",
        "benchmark",
        f"architecture:{architecture}",
        f"question:q{q_no}",
    ]
    metadata: dict[str, Any] = {
        "benchmark": "deep_research_bench",
        "architecture": architecture,
        "question_id": str(q_no),
        "thread_id": thread_id,
    }

    if not requested:
        return LangfuseTraceConfig(
            enabled=False,
            base_url=resolved_base_url,
            session_id=session_id,
            trace_name=trace_name,
            tags=tags,
            metadata=metadata,
        )

    if base_url:
        os.environ["LANGFUSE_BASE_URL"] = base_url
        os.environ["LANGFUSE_HOST"] = base_url
    else:
        os.environ.setdefault("LANGFUSE_BASE_URL", resolved_base_url)
        os.environ.setdefault("LANGFUSE_HOST", resolved_base_url)

    missing = [name for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY") if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Langfuse tracing requested but missing environment variables: "
            + ", ".join(missing)
            + ". Create a Langfuse project and export its public/secret keys."
        )

    try:
        from langfuse import get_client
        from langfuse.langchain import CallbackHandler
    except ModuleNotFoundError as exc:
        raise RuntimeError("Langfuse tracing requested but package is missing. Install with `uv pip install langfuse`.") from exc

    client = get_client()
    handler = CallbackHandler()

    return LangfuseTraceConfig(
        enabled=True,
        base_url=resolved_base_url,
        session_id=session_id,
        trace_name=trace_name,
        tags=tags,
        metadata=metadata,
        handler=handler,
        client=client,
    )
