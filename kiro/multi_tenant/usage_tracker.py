# -*- coding: utf-8 -*-
"""Post-response usage tracking for multi-tenant."""

from typing import Any, Dict, Optional

from fastapi import Request
from loguru import logger

from kiro.multi_tenant.pricing import calculate_cost


async def track_request_usage(
    request: Request,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    request_id: str = "",
) -> None:
    """Record usage for the current tenant if multi-tenant is active."""
    tenant_info: Optional[Dict[str, Any]] = getattr(request.state, "tenant_key_info", None)
    if tenant_info is None:
        return

    tenant_db = getattr(request.app.state, "tenant_db", None)
    if tenant_db is None:
        return

    cost = calculate_cost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
    )

    try:
        await tenant_db.record_usage(
            api_key=tenant_info["key"],
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            cost_usd=cost,
            request_id=request_id,
        )
        logger.debug(f"Tracked usage for {tenant_info['key'][:12]}...: cost=${cost:.6f}")
    except Exception as e:
        logger.warning(f"Failed to track usage: {e}")
