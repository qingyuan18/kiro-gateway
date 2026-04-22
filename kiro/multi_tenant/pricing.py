# -*- coding: utf-8 -*-
"""Cost calculation based on model pricing."""

from typing import Dict

MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "claude-sonnet-4.5": {
        "input": 3.00 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "cached": 0.30 / 1_000_000,
        "cache_write": 3.75 / 1_000_000,
    },
    "claude-sonnet-4": {
        "input": 3.00 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "cached": 0.30 / 1_000_000,
        "cache_write": 3.75 / 1_000_000,
    },
    "claude-haiku-4.5": {
        "input": 0.80 / 1_000_000,
        "output": 4.00 / 1_000_000,
        "cached": 0.08 / 1_000_000,
        "cache_write": 1.00 / 1_000_000,
    },
    "claude-opus-4.5": {
        "input": 15.00 / 1_000_000,
        "output": 75.00 / 1_000_000,
        "cached": 1.50 / 1_000_000,
        "cache_write": 18.75 / 1_000_000,
    },
    "claude-3.7-sonnet": {
        "input": 3.00 / 1_000_000,
        "output": 15.00 / 1_000_000,
        "cached": 0.30 / 1_000_000,
        "cache_write": 3.75 / 1_000_000,
    },
}

DEFAULT_PRICING = MODEL_PRICING["claude-sonnet-4.5"]


def calculate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    normalized = model.lower().replace("-", "-")
    price = DEFAULT_PRICING
    for key, val in MODEL_PRICING.items():
        if key in normalized:
            price = val
            break

    return (
        input_tokens * price["input"]
        + output_tokens * price["output"]
        + cached_tokens * price["cached"]
        + cache_write_tokens * price["cache_write"]
    )
