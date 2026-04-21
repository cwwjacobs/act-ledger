"""
Pricing table for Anthropic models.
Version-stamped so every cost calculation is attributable.
Update rates here when Anthropic changes pricing.
"""

from __future__ import annotations

PRICING_VERSION = "2026-04-14"

# Cost in USD per 1M tokens
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-6":          {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6":        {"input":  3.00, "output": 15.00},
    "claude-haiku-4-5":         {"input":  0.80, "output":  4.00},
    # fallback for unknown models — use Sonnet rates, flag it
    "_unknown":                 {"input":  3.00, "output": 15.00},
}


def get_rates(model: str) -> tuple[dict[str, float], bool]:
    """
    Returns (rates_dict, is_exact_match).
    is_exact_match=False means we fell back to _unknown rates.
    Caller should record this in the cost record.
    """
    # guard against None
    if not model:
        return PRICING["_unknown"], False
    # exact match first
    if model in PRICING:
        return PRICING[model], True
    # prefix match — handles version suffixes like claude-sonnet-4-6-20251001
    for key in PRICING:
        if key.startswith("_"):
            continue
        if model.startswith(key) or key in model:
            return PRICING[key], True
    return PRICING["_unknown"], False


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> dict:
    """
    Returns a cost record with full provenance:
    - amount_usd
    - pricing_version
    - rates_used
    - exact_match (False = fell back to _unknown)
    """
    rates, exact = get_rates(model)
    cost = (input_tokens / 1_000_000 * rates["input"]) + \
           (output_tokens / 1_000_000 * rates["output"])
    return {
        "amount_usd": round(cost, 8),
        "pricing_version": PRICING_VERSION,
        "rates_used": rates,
        "exact_match": exact,
    }
