from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from langchain_core.outputs import ChatGeneration, LLMResult


DEFAULT_LANGFUSE_BASE_URL = "http://localhost:3000"
DEFAULT_LANGFUSE_MAX_FIELD_CHARS = 4_000
DEFAULT_LANGFUSE_MAX_COLLECTION_ITEMS = 20
DEFAULT_LANGFUSE_ATTACH_SCORES_TO_TRACE = False
DEFAULT_LANGFUSE_TRACE_NAMESPACE = "sci_agent_bm_final"


def _max_field_chars() -> int:
    try:
        return int(os.getenv("LANGFUSE_MAX_FIELD_CHARS", str(DEFAULT_LANGFUSE_MAX_FIELD_CHARS)))
    except ValueError:
        return DEFAULT_LANGFUSE_MAX_FIELD_CHARS


def _max_collection_items() -> int:
    try:
        return int(os.getenv("LANGFUSE_MAX_COLLECTION_ITEMS", str(DEFAULT_LANGFUSE_MAX_COLLECTION_ITEMS)))
    except ValueError:
        return DEFAULT_LANGFUSE_MAX_COLLECTION_ITEMS


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _attach_scores_to_trace() -> bool:
    return _truthy(os.getenv("LANGFUSE_ATTACH_SCORES_TO_TRACE"), default=DEFAULT_LANGFUSE_ATTACH_SCORES_TO_TRACE)


def _langfuse_trace_namespace() -> str:
    value = os.getenv("LANGFUSE_TRACE_NAMESPACE", DEFAULT_LANGFUSE_TRACE_NAMESPACE).strip()
    return value or DEFAULT_LANGFUSE_TRACE_NAMESPACE


def _langfuse_public_key() -> str | None:
    return os.getenv("LANGFUSE_TRACING_PUBLIC_KEY") or os.getenv("LANGFUSE_PUBLIC_KEY")


def _langfuse_secret_key() -> str | None:
    return os.getenv("LANGFUSE_TRACING_SECRET_KEY") or os.getenv("LANGFUSE_SECRET_KEY")


def _truncate_text(value: str, *, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    omitted = len(value) - max_chars
    return value[:max_chars] + f"\n\n[Langfuse payload truncated: omitted {omitted} chars]"


def _sanitize_langfuse_payload(value: Any, *, max_chars: int, max_items: int, depth: int = 0) -> Any:
    if depth > 8:
        return "[Langfuse payload truncated: max depth reached]"
    if isinstance(value, str):
        return _truncate_text(value, max_chars=max_chars)
    if isinstance(value, bytes):
        return f"[Langfuse payload omitted: {len(value)} bytes]"
    if isinstance(value, dict):
        items = list(value.items())
        truncated: dict[Any, Any] = {
            key: _sanitize_langfuse_payload(item_value, max_chars=max_chars, max_items=max_items, depth=depth + 1)
            for key, item_value in items[:max_items]
        }
        if len(items) > max_items:
            truncated["__langfuse_truncated_items__"] = len(items) - max_items
        return truncated
    if isinstance(value, (list, tuple)):
        sanitized = [
            _sanitize_langfuse_payload(item, max_chars=max_chars, max_items=max_items, depth=depth + 1)
            for item in value[:max_items]
        ]
        return tuple(sanitized) if isinstance(value, tuple) else sanitized
    if hasattr(value, "model_copy"):
        update: dict[str, Any] = {}
        for attr in ("content", "text", "message", "generations", "llm_output"):
            if hasattr(value, attr):
                update[attr] = _sanitize_langfuse_payload(
                    getattr(value, attr),
                    max_chars=max_chars,
                    max_items=max_items,
                    depth=depth + 1,
                )
        if hasattr(value, "artifact"):
            artifact = getattr(value, "artifact")
            artifact_text = str(artifact)
            update["artifact"] = (
                artifact
                if len(artifact_text) <= max_chars
                else f"[Langfuse tool artifact omitted: {len(artifact_text)} chars]"
            )
        try:
            return value.model_copy(update=update) if update else value
        except Exception:
            return _truncate_text(str(value), max_chars=max_chars)
    return value


class SanitizingLangfuseCallbackHandler:
    """Forward LangChain callbacks to Langfuse after trimming oversized payloads."""

    def __init__(self, handler: Any, *, max_chars: int, max_items: int) -> None:
        self._handler = handler
        self._max_chars = max_chars
        self._max_items = max_items

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handler, name)

    def _sanitize_args(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
        return (
            tuple(
                _sanitize_langfuse_payload(arg, max_chars=self._max_chars, max_items=self._max_items)
                for arg in args
            ),
            {
                key: _sanitize_langfuse_payload(value, max_chars=self._max_chars, max_items=self._max_items)
                for key, value in kwargs.items()
            },
        )

    def _forward(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        sanitized_args, sanitized_kwargs = self._sanitize_args(args, kwargs)
        return getattr(self._handler, method_name)(*sanitized_args, **sanitized_kwargs)

    def on_agent_action(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_agent_action", *args, **kwargs)

    def on_agent_finish(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_agent_finish", *args, **kwargs)

    def on_chain_end(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_chain_end", *args, **kwargs)

    def on_chain_error(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_chain_error", *args, **kwargs)

    def on_chain_start(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_chain_start", *args, **kwargs)

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_chat_model_start", *args, **kwargs)

    def on_custom_event(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_custom_event", *args, **kwargs)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        from langfuse.langchain.CallbackHandler import _extract_raw_response, _parse_model, _parse_usage

        try:
            self._handler._log_debug_event(
                "on_llm_end", run_id, parent_run_id, response=response, kwargs=kwargs
            )
            response_generation = response.generations[-1][-1]
            extracted_response = (
                self._handler._convert_message_to_dict(response_generation.message)
                if isinstance(response_generation, ChatGeneration)
                else _extract_raw_response(response_generation)
            )

            llm_usage = _parse_usage(response)
            model = _parse_model(response)
            generation = self._handler._detach_observation(run_id)

            if generation is not None:
                update_kwargs: dict[str, Any] = {
                    "output": _sanitize_langfuse_payload(
                        extracted_response, max_chars=self._max_chars, max_items=self._max_items
                    ),
                    "usage": llm_usage,
                    "usage_details": llm_usage,
                    "input": _sanitize_langfuse_payload(
                        kwargs.get("inputs"), max_chars=self._max_chars, max_items=self._max_items
                    ),
                    "model": model,
                }
                cost_details = _parse_openrouter_cost_details(response)
                if cost_details is not None:
                    update_kwargs["cost_details"] = cost_details
                generation.update(**update_kwargs).end()
        except Exception:
            return self._forward("on_llm_end", response, run_id=run_id, parent_run_id=parent_run_id, **kwargs)
        finally:
            self._handler._updated_completion_start_time_memo.discard(run_id)
            if parent_run_id is None:
                self._handler._clear_root_run_resume_key(run_id)
                self._handler._reset(run_id)

    def on_llm_error(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_llm_error", *args, **kwargs)

    def on_llm_new_token(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_llm_new_token", *args, **kwargs)

    def on_llm_start(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_llm_start", *args, **kwargs)

    def on_retriever_end(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_retriever_end", *args, **kwargs)

    def on_retriever_error(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_retriever_error", *args, **kwargs)

    def on_retriever_start(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_retriever_start", *args, **kwargs)

    def on_retry(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_retry", *args, **kwargs)

    def on_text(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_text", *args, **kwargs)

    def on_tool_end(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_tool_end", *args, **kwargs)

    def on_tool_error(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_tool_error", *args, **kwargs)

    def on_tool_start(self, *args: Any, **kwargs: Any) -> Any:
        return self._forward("on_tool_start", *args, **kwargs)


def _response_token_usage(response: LLMResult) -> dict[str, Any] | None:
    llm_output = response.llm_output or {}
    token_usage = llm_output.get("token_usage") if isinstance(llm_output, dict) else None
    if isinstance(token_usage, dict):
        return token_usage

    for generation in getattr(response, "generations", []) or []:
        for generation_chunk in generation:
            message_chunk = getattr(generation_chunk, "message", None)
            response_metadata = getattr(message_chunk, "response_metadata", None)
            if isinstance(response_metadata, dict):
                nested = response_metadata.get("token_usage")
                if isinstance(nested, dict):
                    return nested
    return None


def _parse_openrouter_cost_details(response: LLMResult) -> dict[str, float] | None:
    token_usage = _response_token_usage(response)
    if not token_usage:
        return None

    raw_cost_details = token_usage.get("cost_details") if isinstance(token_usage.get("cost_details"), dict) else {}
    cost_details: dict[str, float] = {}

    prompt_cost = raw_cost_details.get("upstream_inference_prompt_cost")
    if isinstance(prompt_cost, (int, float)) and isfinite(float(prompt_cost)):
        cost_details["input"] = float(prompt_cost)

    completion_cost = raw_cost_details.get("upstream_inference_completions_cost")
    if isinstance(completion_cost, (int, float)) and isfinite(float(completion_cost)):
        cost_details["output"] = float(completion_cost)

    total_cost = token_usage.get("cost")
    if isinstance(total_cost, (int, float)) and isfinite(float(total_cost)):
        cost_details["total"] = float(total_cost)
    else:
        upstream_total = raw_cost_details.get("upstream_inference_cost")
        if isinstance(upstream_total, (int, float)) and isfinite(float(upstream_total)):
            cost_details["total"] = float(upstream_total)

    return cost_details or None


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

        trace_id = None
        if _attach_scores_to_trace():
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
                score_kwargs: dict[str, Any] = {
                    "name": name,
                    "value": numeric_value,
                    "session_id": self.session_id,
                    "data_type": "NUMERIC",
                    "metadata": score_metadata,
                }
                if trace_id is not None:
                    score_kwargs["trace_id"] = trace_id
                self.client.create_score(**score_kwargs)
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
    trace_namespace = _langfuse_trace_namespace()
    tags = [
        trace_namespace,
        "benchmark",
        f"architecture:{architecture}",
        f"question:q{q_no}",
    ]
    metadata: dict[str, Any] = {
        "benchmark": "deep_research_bench",
        "architecture": architecture,
        "question_id": str(q_no),
        "thread_id": thread_id,
        "trace_namespace": trace_namespace,
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

    public_key = _langfuse_public_key()
    secret_key = _langfuse_secret_key()
    missing = [
        name
        for name, value in (("LANGFUSE_TRACING_PUBLIC_KEY or LANGFUSE_PUBLIC_KEY", public_key), ("LANGFUSE_TRACING_SECRET_KEY or LANGFUSE_SECRET_KEY", secret_key))
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Langfuse tracing requested but missing environment variables: "
            + ", ".join(missing)
            + ". Create a Langfuse project and export its public/secret keys."
        )

    os.environ["LANGFUSE_PUBLIC_KEY"] = public_key or ""
    os.environ["LANGFUSE_SECRET_KEY"] = secret_key or ""

    try:
        from langfuse import get_client
        from langfuse.langchain import CallbackHandler
    except ModuleNotFoundError as exc:
        raise RuntimeError("Langfuse tracing requested but package is missing. Install with `uv pip install langfuse`.") from exc

    client = get_client()
    raw_handler = CallbackHandler()
    handler = SanitizingLangfuseCallbackHandler(
        raw_handler,
        max_chars=_max_field_chars(),
        max_items=_max_collection_items(),
    )

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
