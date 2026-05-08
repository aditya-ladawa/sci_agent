from __future__ import annotations

import os
from typing import Any

TOKENS_PER_MILLION = 1_000_000
MODEL_PRICING_USD_PER_M_TOKENS = {
    "xiaomi/mimo-v2.5-pro": {"input": 1.0, "output": 3.0},
    "xiaomi/mimo-v2-flash": {"input": 0.09, "output": 0.29},
}
DEFAULT_TAVILY_COST_USD_PER_CREDIT = 0.008
DEFAULT_TAVILY_CREDITS_PER_TOOL_CALL = 1.0


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

    input_cost = input_tokens / TOKENS_PER_MILLION * pricing["input"]
    output_cost = output_tokens / TOKENS_PER_MILLION * pricing["output"]
    return {
        "model": model_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_cost_per_million_tokens_usd": pricing["input"],
        "output_cost_per_million_tokens_usd": pricing["output"],
        "input_cost_usd": _round_cost(input_cost),
        "output_cost_usd": _round_cost(output_cost),
        "cost_usd": _round_cost(input_cost + output_cost),
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

    tavily_calls = _int_metric(usage.get("tavily_tool_calls", {}) or {}, "total")
    tavily_credits_per_call = _float_env("TAVILY_CREDITS_PER_TOOL_CALL", DEFAULT_TAVILY_CREDITS_PER_TOOL_CALL)
    tavily_cost_per_credit = _float_env("TAVILY_COST_USD_PER_CREDIT", DEFAULT_TAVILY_COST_USD_PER_CREDIT)
    tavily_estimated_credits = tavily_calls * tavily_credits_per_call
    tavily_cost = tavily_estimated_credits * tavily_cost_per_credit

    llm_cost = main_cost["cost_usd"] + subagent_cost["cost_usd"]
    total_cost = llm_cost + tavily_cost
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
            "total_cost_usd": _round_cost(llm_cost),
        },
        "tavily": {
            "tool_calls": tavily_calls,
            "estimated_credits": _round_cost(tavily_estimated_credits),
            "credits_per_tool_call_assumption": tavily_credits_per_call,
            "cost_per_credit_usd": tavily_cost_per_credit,
            "cost_usd": _round_cost(tavily_cost),
            "note": "Estimated from local Tavily tool-call counts; dashboard billing may differ if tools consume different credits.",
        },
        "total_cost_usd": _round_cost(total_cost),
        "warnings": warnings,
    }
