from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass
class CacheEntry:
    value: Any
    expires_at: float


class AsyncTTLCache:
    def __init__(self) -> None:
        self._items: dict[str, CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get_or_set(self, key: str, ttl: float, loader: Callable[[], Awaitable[T]]) -> T:
        now = time.monotonic()
        entry = self._items.get(key)
        if entry and entry.expires_at > now:
            return entry.value
        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._items.get(key)
            if entry and entry.expires_at > time.monotonic():
                return entry.value
            value = await loader()
            self._items[key] = CacheEntry(value=value, expires_at=time.monotonic() + ttl)
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        """Seed a canonical snapshot so every adapter can reuse the same fact."""
        self._items[key] = CacheEntry(value=value, expires_at=time.monotonic() + ttl)

    def set_many(self, values: dict[str, Any], ttl: float) -> None:
        expires_at = time.monotonic() + ttl
        self._items.update({key: CacheEntry(value=value, expires_at=expires_at) for key, value in values.items()})

    def peek(self, key: str) -> Any | None:
        entry = self._items.get(key)
        return entry.value if entry and entry.expires_at > time.monotonic() else None

    def clear(self) -> None:
        self._items.clear()
