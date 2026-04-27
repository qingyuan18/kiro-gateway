# -*- coding: utf-8 -*-
"""
Pool-aware auth manager selection with transparent failover.

When the credential pool is enabled, requests that hit 401/403 from Kiro API
are automatically retried with a different credential — the caller never
needs to know about the retry.
"""

from typing import Callable, Optional, Set, Tuple

from fastapi import Request
from loguru import logger

from kiro.auth import KiroAuthManager

FAILOVER_STATUS_CODES = {401, 403}


async def get_auth_manager(request: Request) -> KiroAuthManager:
    """Pick an auth manager. Returns the pool choice or the default."""
    pool = getattr(request.app.state, "credential_pool", None)
    if pool is not None and pool.active_count > 0:
        manager, _ = await pool.get_manager()
        return manager
    return request.app.state.auth_manager


async def get_auth_manager_with_failover(
    request: Request,
) -> Tuple[KiroAuthManager, Optional["_FailoverContext"]]:
    """Return (manager, failover_ctx).

    If the pool is active, failover_ctx is a helper that the route can call
    when it receives a 401/403 to transparently get a new manager.
    If the pool is not active, failover_ctx is None — no failover possible.
    """
    pool = getattr(request.app.state, "credential_pool", None)
    if pool is not None and pool.active_count > 0:
        manager, cred_id = await pool.get_manager()
        ctx = _FailoverContext(pool=pool, first_cred_id=cred_id)
        return manager, ctx
    return request.app.state.auth_manager, None


class _FailoverContext:
    """Tracks which credentials have been tried and provides next_manager()."""

    def __init__(self, pool, first_cred_id: int):
        self._pool = pool
        self._tried: Set[int] = {first_cred_id}
        self._max_attempts = min(3, pool.active_count)
        self._last_cred_id = first_cred_id

    @property
    def can_retry(self) -> bool:
        return len(self._tried) < self._max_attempts

    async def mark_failed_and_get_next(self) -> Optional[KiroAuthManager]:
        """Mark the last credential as unhealthy, return next healthy one or None."""
        await self._pool.mark_unhealthy(self._last_cred_id)
        logger.warning(
            f"Failover: credential #{self._last_cred_id} failed, "
            f"tried {len(self._tried)}/{self._max_attempts}"
        )

        if not self.can_retry:
            return None

        try:
            manager, cred_id = await self._pool.get_manager(exclude=self._tried)
            self._tried.add(cred_id)
            self._last_cred_id = cred_id
            logger.info(f"Failover: switching to credential #{cred_id}")
            return manager
        except ValueError:
            return None


def should_failover(status_code: int) -> bool:
    """Return True if this Kiro API status code warrants a credential failover."""
    return status_code in FAILOVER_STATUS_CODES
