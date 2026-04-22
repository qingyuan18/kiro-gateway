# -*- coding: utf-8 -*-
"""
Upstream Kiro credential pool.

Manages multiple KiroAuthManager instances and selects one per request
using configurable strategies (round-robin, least-used, random).
"""

import asyncio
import random
import time
from enum import Enum
from typing import Any, Dict, List, Optional

import aiosqlite
from loguru import logger
from pathlib import Path

from kiro.auth import KiroAuthManager


class PoolStrategy(str, Enum):
    ROUND_ROBIN = "round_robin"
    LEAST_USED = "least_used"
    RANDOM = "random"


POOL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS upstream_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    cred_type TEXT NOT NULL DEFAULT 'refresh_token',
    refresh_token TEXT,
    creds_file TEXT,
    sqlite_db TEXT,
    profile_arn TEXT,
    region TEXT DEFAULT 'us-east-1',
    enabled INTEGER DEFAULT 1,
    request_count INTEGER DEFAULT 0,
    last_used_at TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""


class CredentialPool:
    """Manages a pool of upstream Kiro credentials with load-balancing."""

    def __init__(
        self,
        db_path: str = "data/tenants.db",
        strategy: PoolStrategy = PoolStrategy.ROUND_ROBIN,
    ):
        self.db_path = db_path
        self.strategy = strategy
        self._db: Optional[aiosqlite.Connection] = None
        self._managers: Dict[int, KiroAuthManager] = {}
        self._robin_index: int = 0
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(POOL_SCHEMA_SQL)
        await self._db.commit()

        # Boot all enabled credentials
        cursor = await self._db.execute(
            "SELECT * FROM upstream_credentials WHERE enabled = 1"
        )
        rows = await cursor.fetchall()
        for row in rows:
            cred = dict(row)
            try:
                mgr = self._build_manager(cred)
                self._managers[cred["id"]] = mgr
                logger.info(f"Pool: loaded credential #{cred['id']} ({cred['name']})")
            except Exception as e:
                logger.warning(f"Pool: failed to load credential #{cred['id']}: {e}")

        logger.info(
            f"Credential pool ready: {len(self._managers)} active credentials, strategy={self.strategy.value}"
        )

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ---- Manager lifecycle ----

    def _build_manager(self, cred: Dict[str, Any]) -> KiroAuthManager:
        return KiroAuthManager(
            refresh_token=cred.get("refresh_token") or "",
            profile_arn=cred.get("profile_arn") or "",
            region=cred.get("region") or "us-east-1",
            creds_file=cred.get("creds_file") or None,
            sqlite_db=cred.get("sqlite_db") or None,
        )

    # ---- Selection ----

    async def get_manager(self) -> KiroAuthManager:
        """Select a KiroAuthManager from the pool based on configured strategy.

        Falls back to the default (non-pool) manager if the pool is empty.
        Raises ValueError if no managers are available.
        """
        async with self._lock:
            if not self._managers:
                raise ValueError("No upstream credentials available in pool")

            ids = list(self._managers.keys())

            if self.strategy == PoolStrategy.ROUND_ROBIN:
                chosen_id = ids[self._robin_index % len(ids)]
                self._robin_index += 1
            elif self.strategy == PoolStrategy.LEAST_USED:
                cursor = await self._db.execute(
                    "SELECT id FROM upstream_credentials WHERE enabled = 1 AND id IN ({}) ORDER BY request_count ASC LIMIT 1".format(
                        ",".join("?" for _ in ids)
                    ),
                    ids,
                )
                row = await cursor.fetchone()
                chosen_id = row["id"] if row else ids[0]
            else:  # RANDOM
                chosen_id = random.choice(ids)

            # Update stats
            await self._db.execute(
                "UPDATE upstream_credentials SET request_count = request_count + 1, last_used_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (chosen_id,),
            )
            await self._db.commit()

            return self._managers[chosen_id]

    # ---- CRUD ----

    async def add_credential(
        self,
        name: str,
        cred_type: str = "refresh_token",
        refresh_token: str = "",
        creds_file: str = "",
        sqlite_db: str = "",
        profile_arn: str = "",
        region: str = "us-east-1",
    ) -> Dict[str, Any]:
        await self._db.execute(
            """INSERT INTO upstream_credentials
               (name, cred_type, refresh_token, creds_file, sqlite_db, profile_arn, region)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, cred_type, refresh_token, creds_file, sqlite_db, profile_arn, region),
        )
        await self._db.commit()

        cursor = await self._db.execute(
            "SELECT * FROM upstream_credentials ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        cred = dict(row)

        try:
            mgr = self._build_manager(cred)
            self._managers[cred["id"]] = mgr
            logger.info(f"Pool: added and activated credential #{cred['id']} ({name})")
        except Exception as e:
            logger.warning(f"Pool: added credential #{cred['id']} but failed to activate: {e}")

        return self._sanitize(cred)

    async def list_credentials(self) -> List[Dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM upstream_credentials ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [self._sanitize(dict(r)) for r in rows]

    async def get_credential(self, cred_id: int) -> Optional[Dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM upstream_credentials WHERE id = ?", (cred_id,)
        )
        row = await cursor.fetchone()
        return self._sanitize(dict(row)) if row else None

    async def update_credential(self, cred_id: int, **fields) -> Optional[Dict[str, Any]]:
        allowed = {"name", "refresh_token", "creds_file", "sqlite_db", "profile_arn", "region", "enabled"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get_credential(cred_id)

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [cred_id]
        await self._db.execute(
            f"UPDATE upstream_credentials SET {set_clause} WHERE id = ?", values
        )
        await self._db.commit()

        # Reload manager
        cursor = await self._db.execute(
            "SELECT * FROM upstream_credentials WHERE id = ?", (cred_id,)
        )
        row = await cursor.fetchone()
        if row:
            cred = dict(row)
            if cred["enabled"]:
                try:
                    self._managers[cred_id] = self._build_manager(cred)
                except Exception as e:
                    logger.warning(f"Pool: failed to reload credential #{cred_id}: {e}")
                    self._managers.pop(cred_id, None)
            else:
                self._managers.pop(cred_id, None)
            return self._sanitize(cred)
        return None

    async def delete_credential(self, cred_id: int) -> bool:
        cursor = await self._db.execute(
            "DELETE FROM upstream_credentials WHERE id = ?", (cred_id,)
        )
        await self._db.commit()
        self._managers.pop(cred_id, None)
        return cursor.rowcount > 0

    def _sanitize(self, cred: Dict[str, Any]) -> Dict[str, Any]:
        """Mask sensitive fields for API responses."""
        result = dict(cred)
        if result.get("refresh_token"):
            result["refresh_token"] = result["refresh_token"][:8] + "..."
        return result

    @property
    def active_count(self) -> int:
        return len(self._managers)
