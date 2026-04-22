# -*- coding: utf-8 -*-
"""Tenant authentication helpers used by route handlers."""

from typing import Any, Dict, Optional

from fastapi import HTTPException, Request
from loguru import logger

from kiro.multi_tenant.rate_limiter import RateLimiter


async def resolve_tenant(
    request: Request,
    api_key_value: str,
) -> Optional[Dict[str, Any]]:
    """Validate an incoming API key against the tenant database.

    Returns the key info dict if multi-tenant is enabled and the key is valid.
    Returns None when multi-tenant is disabled (fall back to PROXY_API_KEY check).
    Raises HTTPException when multi-tenant is enabled but key is invalid.
    """
    tenant_db = getattr(request.app.state, "tenant_db", None)
    if tenant_db is None:
        return None

    key_info = await tenant_db.validate_key(api_key_value)
    if key_info is None:
        logger.warning(f"Tenant auth failed for key: {api_key_value[:12]}...")
        raise HTTPException(status_code=401, detail="Invalid, disabled, or over-budget API key")

    rate_limiter: RateLimiter = request.app.state.rate_limiter
    allowed, retry_after = rate_limiter.check(
        key_info["key"],
        key_info.get("rate_limit_rpm"),
        key_info.get("rate_limit_tpm"),
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Retry after {retry_after:.1f}s",
        )

    return key_info
