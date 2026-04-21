"""
Passive local-only cost acceleration detector.

The detector is intentionally conservative:
- it only examines already-recorded local session data
- it never prompts, raises, or blocks user work
- it returns factual metrics only when a clear threshold trips
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

TRAILING_WINDOW = 5
MIN_PRIOR_CALLS = 3
MIN_LATEST_COST_USD = 0.002
MIN_COST_DELTA_USD = 0.001
COST_MULTIPLIER = 3.0
MIN_TOTAL_SESSION_COST_USD = 0.005
MIN_SPEND_RATE_USD_PER_MIN = 0.02
MIN_INPUT_GROWTH_RATIO = 2.0
MIN_INPUT_DELTA = 1500
MIN_MODEL_SWITCH_COST_USD = 0.004
MODEL_SWITCH_COST_MULTIPLIER = 2.0


def _record_cost(record: dict[str, Any]) -> float:
    return float(((record.get("cost") or {}).get("amount_usd")) or 0.0)


def _record_input_tokens(record: dict[str, Any]) -> int:
    return int(record.get("input_tokens") or 0)


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def detect_cost_spike(
    previous_records: list[dict[str, Any]],
    latest_record: dict[str, Any],
) -> dict[str, Any] | None:
    latest_cost = _record_cost(latest_record)
    if latest_cost <= 0 or latest_record.get("error"):
        return None

    prior_clean = [
        rec for rec in previous_records
        if not rec.get("error") and _record_cost(rec) > 0
    ]
    if len(prior_clean) < MIN_PRIOR_CALLS:
        return None

    window = prior_clean[-TRAILING_WINDOW:]
    trailing_avg_cost = sum(_record_cost(rec) for rec in window) / len(window)
    if trailing_avg_cost <= 0:
        return None

    latest_input_tokens = _record_input_tokens(latest_record)
    trailing_avg_input_tokens = (
        sum(_record_input_tokens(rec) for rec in window) / len(window)
    )
    input_growth_ratio = (
        latest_input_tokens / trailing_avg_input_tokens
        if trailing_avg_input_tokens > 0 else None
    )
    input_growth_delta = latest_input_tokens - trailing_avg_input_tokens

    clean_records = prior_clean + [latest_record]
    total_session_cost = sum(_record_cost(rec) for rec in clean_records)

    first_ts = _parse_timestamp(clean_records[0].get("timestamp"))
    last_ts = _parse_timestamp(latest_record.get("timestamp"))
    elapsed_seconds = 0.0
    if first_ts and last_ts:
        elapsed_seconds = max((last_ts - first_ts).total_seconds(), 0.0)
    spend_rate_usd_per_min = (
        total_session_cost / (elapsed_seconds / 60.0)
        if elapsed_seconds > 0 else None
    )

    prior_model = window[-1].get("model")
    latest_model = latest_record.get("model")
    model_switch = None
    if prior_model and latest_model and prior_model != latest_model:
        model_switch = {"from": prior_model, "to": latest_model}

    cost_jump = (
        latest_cost >= max(MIN_LATEST_COST_USD, trailing_avg_cost * COST_MULTIPLIER)
        and (latest_cost - trailing_avg_cost) >= MIN_COST_DELTA_USD
    )
    input_growth = (
        input_growth_ratio is not None
        and input_growth_ratio >= MIN_INPUT_GROWTH_RATIO
        and input_growth_delta >= MIN_INPUT_DELTA
    )
    spend_rate_high = (
        spend_rate_usd_per_min is not None
        and total_session_cost >= MIN_TOTAL_SESSION_COST_USD
        and spend_rate_usd_per_min >= MIN_SPEND_RATE_USD_PER_MIN
    )
    model_switch_signal = (
        model_switch is not None
        and latest_cost >= MIN_MODEL_SWITCH_COST_USD
        and latest_cost >= trailing_avg_cost * MODEL_SWITCH_COST_MULTIPLIER
    )

    if not cost_jump or not (input_growth or spend_rate_high or model_switch_signal):
        return None

    event = {
        "kind": "cost_spike",
        "timestamp": latest_record.get("timestamp"),
        "latest_cost_usd": round(latest_cost, 6),
        "trailing_avg_cost_usd": round(trailing_avg_cost, 6),
        "prior_calls_considered": len(window),
        "total_session_cost_usd": round(total_session_cost, 6),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "recent_spend_rate_usd_per_min": (
            round(spend_rate_usd_per_min, 6)
            if spend_rate_usd_per_min is not None else None
        ),
        "latest_input_tokens": latest_input_tokens,
        "trailing_avg_input_tokens": round(trailing_avg_input_tokens, 2),
        "input_growth_ratio": (
            round(input_growth_ratio, 2)
            if input_growth_ratio is not None else None
        ),
        "model_switch": model_switch,
    }
    event["message"] = format_spike_warning(
        latest_record.get("session_id", "?"),
        event,
    )
    return event


def format_spike_warning(session_id: str, event: dict[str, Any]) -> str:
    parts = [
        f"claude-act warning: session {session_id} cost acceleration detected",
        (
            f"latest ${event['latest_cost_usd']:.6f} vs prior avg "
            f"${event['trailing_avg_cost_usd']:.6f} over "
            f"{event['prior_calls_considered']} calls"
        ),
    ]

    if event.get("input_growth_ratio") is not None:
        parts.append(
            "input "
            f"{event['latest_input_tokens']} vs avg "
            f"{event['trailing_avg_input_tokens']:.0f} tokens"
        )

    if event.get("recent_spend_rate_usd_per_min") is not None:
        elapsed_minutes = event["elapsed_seconds"] / 60.0
        parts.append(
            f"session total ${event['total_session_cost_usd']:.6f} over "
            f"{elapsed_minutes:.2f} min "
            f"(${event['recent_spend_rate_usd_per_min']:.6f}/min)"
        )

    if event.get("model_switch"):
        model_switch = event["model_switch"]
        parts.append(
            f"model switch {model_switch['from']} -> {model_switch['to']}"
        )

    return "; ".join(parts)
