from __future__ import annotations

import os
from typing import Any

TOKENS_PER_MILLION = 1_000_000
MODEL_PRICING_USD_PER_M_TOKENS = {
    "xiaomi/mimo-v2.5-pro": {"input": 1.0, "output": 3.0},
    "xiaomi/mimo-v2-flash": {"input": 0.09, "output": 0.29},
}
DEFAULT_DDGS_QUERY_COST_USD = 0.0


def _int_metric(metrics: dict[str, Any], key: str) -> int:
    try:
        return int(metrics.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _round_cost(value: float) -> float:
    return round(value, 6)


def _cost_from_tokens(tokens: int, price_per_million: float) -> float:
    return tokens * price_per_million / TOKENS_PER_MILLION


def _model_pricing(model_name: str | None) -> dict[str, float] | None:
    if not model_name:
        return None
    return MODEL_PRICING_USD_PER_M_TOKENS.get(model_name.lower())


def _estimate_llm_cost(tokens: dict[str, Any], model_name: str | None) -> dict[str, Any]:
    input_tokens = _int_metric(tokens, "input_tokens")
    output_tokens = _int_metric(tokens, "output_tokens")
    total_tokens = _int_metric(tokens, "total_tokens") or input_tokens + output_tokens
    pricing = _model_pricing(model_name)

    if pricing is None:
        return {
            "model": model_name or "unknown",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "input_cost_usd": 0.0,
            "output_cost_usd": 0.0,
            "cost_usd": 0.0,
            "pricing_found": False,
        }

    input_cost = _cost_from_tokens(input_tokens, pricing["input"])
    output_cost = _cost_from_tokens(output_tokens, pricing["output"])
    total_cost = input_cost + output_cost
    return {
        "model": model_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "formula": "input_tokens * input_price_per_million / 1e6 + output_tokens * output_price_per_million / 1e6",
        "input_cost_per_million_tokens_usd": pricing["input"],
        "output_cost_per_million_tokens_usd": pricing["output"],
        "input_cost_usd": _round_cost(input_cost),
        "output_cost_usd": _round_cost(output_cost),
        "cost_usd": _round_cost(total_cost),
        "unrounded_cost_usd": total_cost,
        "pricing_found": True,
    }


def estimate_run_cost(
    *,
    usage: dict[str, Any],
    main_model: str | None,
    subagent_model: str | None = None,
) -> dict[str, Any]:
    main_cost = _estimate_llm_cost(usage.get("main_agent_tokens", {}) or {}, main_model)
    subagent_tokens = usage.get("subagents_total_tokens", {}) or {}
    subagent_cost = _estimate_llm_cost(subagent_tokens, subagent_model or main_model)
    subagent_has_tokens = _int_metric(subagent_tokens, "total_tokens") > 0
    if not subagent_has_tokens:
        subagent_cost["model"] = subagent_model or main_model or "none"
        subagent_cost["pricing_found"] = True

    ddgs_tool_calls = usage.get("ddgs_tool_calls", {}) or {}
    ddgs_calls = _int_metric(ddgs_tool_calls, "total")
    ddgs_query_cost = _float_env("DDGS_QUERY_COST_USD", DEFAULT_DDGS_QUERY_COST_USD)
    ddgs_cost = ddgs_calls * ddgs_query_cost

    main_input_cost = _cost_from_tokens(
        _int_metric(usage.get("main_agent_tokens", {}) or {}, "input_tokens"),
        float(main_cost.get("input_cost_per_million_tokens_usd", 0.0) or 0.0),
    )
    main_output_cost = _cost_from_tokens(
        _int_metric(usage.get("main_agent_tokens", {}) or {}, "output_tokens"),
        float(main_cost.get("output_cost_per_million_tokens_usd", 0.0) or 0.0),
    )
    subagent_input_cost = _cost_from_tokens(
        _int_metric(subagent_tokens, "input_tokens"),
        float(subagent_cost.get("input_cost_per_million_tokens_usd", 0.0) or 0.0),
    )
    subagent_output_cost = _cost_from_tokens(
        _int_metric(subagent_tokens, "output_tokens"),
        float(subagent_cost.get("output_cost_per_million_tokens_usd", 0.0) or 0.0),
    )
    llm_input_cost = main_input_cost + subagent_input_cost
    llm_output_cost = main_output_cost + subagent_output_cost
    llm_cost = llm_input_cost + llm_output_cost
    total_cost = llm_cost + ddgs_cost
    warnings: list[str] = []
    if not main_cost.get("pricing_found") and main_cost["total_tokens"]:
        warnings.append(f"No LLM pricing configured for main model: {main_model}")
    if subagent_has_tokens and not subagent_cost.get("pricing_found"):
        warnings.append(f"No LLM pricing configured for subagent model: {subagent_model or main_model}")

    return {
        "currency": "USD",
        "llm": {
            "main_agent": main_cost,
            "subagents": subagent_cost,
            "total_input_tokens": _int_metric(usage.get("main_agent_tokens", {}) or {}, "input_tokens")
            + _int_metric(subagent_tokens, "input_tokens"),
            "total_output_tokens": _int_metric(usage.get("main_agent_tokens", {}) or {}, "output_tokens")
            + _int_metric(subagent_tokens, "output_tokens"),
            "total_tokens": _int_metric(usage.get("main_agent_tokens", {}) or {}, "total_tokens")
            + _int_metric(subagent_tokens, "total_tokens"),
            "input_cost_usd": _round_cost(llm_input_cost),
            "output_cost_usd": _round_cost(llm_output_cost),
            "total_cost_usd": _round_cost(llm_cost),
        },
        "ddgs": {
            "tool_calls": ddgs_calls,
            "query_cost_usd": ddgs_query_cost,
            "cost_usd": _round_cost(ddgs_cost),
            "note": "Estimated from local DDGS MCP tool-call counts. DDGS is local/open-source by default, so the default per-call cost is $0. Override with DDGS_QUERY_COST_USD if needed.",
        },
        "total_cost_usd": _round_cost(total_cost),
        "warnings": warnings,
    }
