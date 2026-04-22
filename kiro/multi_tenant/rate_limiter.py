# -*- coding: utf-8 -*-
"""Token-bucket rate limiter per API key."""

import time
from typing import Dict, Optional, Tuple


class _Bucket:
    __slots__ = ("capacity", "tokens", "refill_rate", "last_refill")

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = capacity / 60.0  # refill full bucket over 60 seconds
        self.last_refill = time.monotonic()

    def allow(self, cost: int = 1) -> Tuple[bool, float]:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0
        deficit = cost - self.tokens
        retry_after = deficit / self.refill_rate
        return False, retry_after


class RateLimiter:
    def __init__(self):
        self._rpm_buckets: Dict[str, _Bucket] = {}
        self._tpm_buckets: Dict[str, _Bucket] = {}

    def check(
        self,
        key: str,
        rpm_limit: Optional[int],
        tpm_limit: Optional[int],
        token_cost: int = 0,
    ) -> Tuple[bool, float]:
        if rpm_limit:
            bucket = self._rpm_buckets.get(key)
            if bucket is None or bucket.capacity != rpm_limit:
                bucket = _Bucket(rpm_limit)
                self._rpm_buckets[key] = bucket
            allowed, retry = bucket.allow(1)
            if not allowed:
                return False, retry

        if tpm_limit and token_cost > 0:
            bucket = self._tpm_buckets.get(key)
            if bucket is None or bucket.capacity != tpm_limit:
                bucket = _Bucket(tpm_limit)
                self._tpm_buckets[key] = bucket
            allowed, retry = bucket.allow(token_cost)
            if not allowed:
                return False, retry

        return True, 0.0

    def evict(self, key: str) -> None:
        self._rpm_buckets.pop(key, None)
        self._tpm_buckets.pop(key, None)
