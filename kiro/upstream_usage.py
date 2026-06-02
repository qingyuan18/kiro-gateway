# -*- coding: utf-8 -*-

"""
Upstream Kiro account usage query.

Provides a single function to query the Kiro account's credit usage and
subscription info via the undocumented `getUsageLimits` REST endpoint
(reverse-engineered from the Kiro IDE).

Endpoint: GET https://q.{region}.amazonaws.com/getUsageLimits
Auth: Bearer {access_token} from KiroAuthManager (kiro-cli SSO OIDC works)
Response: subscription tier, credit usage, overage config, reset date
"""

from typing import Any, Dict, Optional

import httpx
from loguru import logger

from kiro.auth import KiroAuthManager
from kiro.utils import get_kiro_headers


async def fetch_upstream_usage(
    auth_manager: KiroAuthManager,
    *,
    origin: str = "AI_EDITOR",
    resource_type: Optional[str] = "AGENTIC_REQUEST",
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """
    Fetch the upstream Kiro account's usage limits.

    Args:
        auth_manager: Authenticated KiroAuthManager instance
        origin: Request origin string (matches Kiro IDE: "AI_EDITOR")
        resource_type: Optional resource type filter (e.g. "AGENTIC_REQUEST")
        timeout: HTTP timeout in seconds

    Returns:
        Raw JSON response from upstream, including:
            - subscriptionInfo: subscription tier and management capabilities
            - usageBreakdownList: per-resource usage and limits
            - overageConfiguration: overage rate and limits
            - nextDateReset: epoch seconds for next usage reset
            - userInfo: user identifier
            - daysUntilReset: integer days until reset

    Raises:
        httpx.HTTPError: on transport-level failure
        httpx.HTTPStatusError: when upstream returns 4xx/5xx
    """
    token = await auth_manager.get_access_token()
    headers = get_kiro_headers(auth_manager, token)

    params: Dict[str, str] = {"origin": origin}
    if auth_manager.profile_arn:
        params["profileArn"] = auth_manager.profile_arn
    if resource_type:
        params["resourceType"] = resource_type

    url = f"{auth_manager.q_host}/getUsageLimits"
    logger.debug(f"Fetching upstream usage: GET {url} params={params}")

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()
