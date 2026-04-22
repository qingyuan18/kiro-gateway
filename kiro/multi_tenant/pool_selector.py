# -*- coding: utf-8 -*-
"""Select an auth manager — from the pool when enabled, otherwise the default."""

from fastapi import Request
from kiro.auth import KiroAuthManager


async def get_auth_manager(request: Request) -> KiroAuthManager:
    pool = getattr(request.app.state, "credential_pool", None)
    if pool is not None and pool.active_count > 0:
        return await pool.get_manager()
    return request.app.state.auth_manager
