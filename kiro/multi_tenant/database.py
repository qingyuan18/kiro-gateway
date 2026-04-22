# -*- coding: utf-8 -*-
"""SQLite database for multi-tenant API key management and usage tracking."""

import aiosqlite
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    name TEXT,
    owner TEXT,
    budget_usd REAL,
    used_usd REAL DEFAULT 0,
    rate_limit_rpm INTEGER,
    rate_limit_tpm INTEGER,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    request_id TEXT,
    timestamp TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (api_key) REFERENCES api_keys(key)
);

CREATE INDEX IF NOT EXISTS idx_usage_api_key ON usage_logs(api_key);
CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_logs(timestamp);
"""


def generate_api_key() -> str:
    alphabet = string.ascii_letters + string.digits
    random_part = ''.join(secrets.choice(alphabet) for _ in range(32))
    return f"sk-kiro-{random_part}"


class TenantDatabase:
    def __init__(self, db_path: str = "data/tenants.db"):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info(f"Tenant database initialized: {self.db_path}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ---- API Key CRUD ----

    async def create_key(
        self,
        name: str,
        owner: str = "",
        budget_usd: Optional[float] = None,
        rate_limit_rpm: Optional[int] = None,
        rate_limit_tpm: Optional[int] = None,
    ) -> Dict[str, Any]:
        key = generate_api_key()
        await self._db.execute(
            """INSERT INTO api_keys (key, name, owner, budget_usd, rate_limit_rpm, rate_limit_tpm)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (key, name, owner, budget_usd, rate_limit_rpm, rate_limit_tpm),
        )
        await self._db.commit()
        return await self.get_key(key)

    async def get_key(self, key: str) -> Optional[Dict[str, Any]]:
        cursor = await self._db.execute("SELECT * FROM api_keys WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_key_by_id(self, key_id: int) -> Optional[Dict[str, Any]]:
        cursor = await self._db.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_keys(self) -> List[Dict[str, Any]]:
        cursor = await self._db.execute("SELECT * FROM api_keys ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_key(self, key: str, **fields) -> Optional[Dict[str, Any]]:
        allowed = {"name", "owner", "budget_usd", "rate_limit_rpm", "rate_limit_tpm", "enabled"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get_key(key)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [key]
        await self._db.execute(f"UPDATE api_keys SET {set_clause} WHERE key = ?", values)
        await self._db.commit()
        return await self.get_key(key)

    async def delete_key(self, key: str) -> bool:
        cursor = await self._db.execute("DELETE FROM api_keys WHERE key = ?", (key,))
        await self._db.commit()
        return cursor.rowcount > 0

    # ---- Validation ----

    async def validate_key(self, key: str) -> Optional[Dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM api_keys WHERE key = ? AND enabled = 1", (key,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        info = dict(row)
        if info["budget_usd"] is not None and info["used_usd"] >= info["budget_usd"]:
            return None
        return info

    # ---- Usage tracking ----

    async def record_usage(
        self,
        api_key: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost_usd: float = 0.0,
        request_id: str = "",
    ) -> None:
        await self._db.execute(
            """INSERT INTO usage_logs
               (api_key, model, input_tokens, output_tokens, cached_tokens, cache_write_tokens, cost_usd, request_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (api_key, model, input_tokens, output_tokens, cached_tokens, cache_write_tokens, cost_usd, request_id),
        )
        await self._db.execute(
            "UPDATE api_keys SET used_usd = used_usd + ?, last_used_at = ? WHERE key = ?",
            (cost_usd, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), api_key),
        )
        await self._db.commit()

    async def get_usage(
        self, api_key: str, start: Optional[str] = None, end: Optional[str] = None
    ) -> Dict[str, Any]:
        conditions = ["api_key = ?"]
        params: list = [api_key]
        if start:
            conditions.append("timestamp >= ?")
            params.append(start)
        if end:
            conditions.append("timestamp <= ?")
            params.append(end)
        where = " AND ".join(conditions)

        cursor = await self._db.execute(
            f"""SELECT
                    COUNT(*) as requests,
                    COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                    COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                    COALESCE(SUM(cached_tokens), 0) as total_cached_tokens,
                    COALESCE(SUM(cache_write_tokens), 0) as total_cache_write_tokens,
                    COALESCE(SUM(cost_usd), 0) as total_cost_usd
                FROM usage_logs WHERE {where}""",
            params,
        )
        row = await cursor.fetchone()
        return dict(row) if row else {}

    async def reset_monthly_usage(self) -> int:
        cursor = await self._db.execute("UPDATE api_keys SET used_usd = 0")
        await self._db.commit()
        return cursor.rowcount
