from __future__ import annotations

import asyncio
import random
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.models import KlineResult, Quote, SectorRanking
from app.providers.base import (
    AllProvidersFailedError,
    MarketDataProvider,
    ProviderEmptyDataError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnsupportedError,
    provider_error_category,
)
from app.utils.time import now_shanghai


@dataclass
class ProviderHealth:
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0
    empty_data_count: int = 0
    total_latency_ms: float = 0.0
    consecutive_failures: int = 0
    last_success_time: datetime | None = None
    last_failure_time: datetime | None = None
    last_error_category: str | None = None
    last_error: str | None = None
    degraded_until_monotonic: float = 0.0

    def report(self) -> dict[str, Any]:
        value = asdict(self)
        value["avg_latency_ms"] = round(self.total_latency_ms / self.request_count, 3) if self.request_count else 0.0
        value["success_rate"] = round(self.success_count / self.request_count, 4) if self.request_count else 0.0
        value["status"] = "DEGRADED" if time.monotonic() < self.degraded_until_monotonic else "HEALTHY"
        value.pop("total_latency_ms")
        value.pop("degraded_until_monotonic")
        for key in ("last_success_time", "last_failure_time"):
            if value[key] is not None:
                value[key] = value[key].isoformat()
        return value


class ProviderManager(MarketDataProvider):
    """Primary/secondary selection, bounded retries and provider health."""

    def __init__(
        self,
        eastmoney: MarketDataProvider,
        tencent: MarketDataProvider,
        *,
        attempts_per_provider: int = 2,
        degrade_after: int = 5,
        degrade_seconds: float = 30.0,
    ) -> None:
        self.providers = {"eastmoney": eastmoney, "tencent": tencent}
        self.attempts_per_provider = attempts_per_provider
        self.degrade_after = degrade_after
        self.degrade_seconds = degrade_seconds
        self._health = {name: ProviderHealth() for name in self.providers}

    async def start(self) -> None:
        await asyncio.gather(*(provider.start() for provider in self.providers.values()))

    async def close(self) -> None:
        await asyncio.gather(*(provider.close() for provider in self.providers.values()))

    def health(self) -> dict[str, Any]:
        return {name: state.report() for name, state in self._health.items()}

    def _ordered_names(self, supported: tuple[str, ...] = ("eastmoney", "tencent")) -> list[str]:
        names = list(supported)
        east = self._health["eastmoney"]
        if "eastmoney" in names and "tencent" in names and time.monotonic() < east.degraded_until_monotonic:
            names.remove("tencent")
            names.insert(0, "tencent")
        return names

    def _record(self, name: str, started: float, error: Exception | None) -> None:
        state = self._health[name]
        state.request_count += 1
        state.total_latency_ms += (time.perf_counter() - started) * 1000
        if error is None:
            state.success_count += 1
            state.consecutive_failures = 0
            state.last_success_time = now_shanghai()
            return
        state.failure_count += 1
        state.consecutive_failures += 1
        state.last_failure_time = now_shanghai()
        state.last_error_category = provider_error_category(error)
        state.last_error = f"{type(error).__name__}: {str(error)[:500]}"
        if isinstance(error, (ProviderTimeoutError, TimeoutError, asyncio.TimeoutError)):
            state.timeout_count += 1
        if isinstance(error, ProviderEmptyDataError):
            state.empty_data_count += 1
        if state.consecutive_failures >= self.degrade_after:
            state.degraded_until_monotonic = time.monotonic() + self.degrade_seconds

    @staticmethod
    def _validate(value: Any) -> None:
        if value is None:
            raise ProviderEmptyDataError("provider returned null data")
        if isinstance(value, Quote) and (value.price is None or value.price <= 0):
            raise ProviderEmptyDataError("provider returned invalid quote price")
        if isinstance(value, KlineResult) and not value.klines:
            raise ProviderEmptyDataError("provider returned empty klines")

    async def _call(
        self,
        method: str,
        *args: Any,
        supported: tuple[str, ...] = ("eastmoney", "tencent"),
        **kwargs: Any,
    ) -> Any:
        errors: list[str] = []
        ordered = self._ordered_names(supported)
        for provider_index, name in enumerate(ordered):
            provider = self.providers[name]
            for attempt in range(self.attempts_per_provider):
                started = time.perf_counter()
                try:
                    value = await getattr(provider, method)(*args, **kwargs)
                    self._validate(value)
                    self._record(name, started, None)
                    return value
                except ProviderUnsupportedError as exc:
                    self._record(name, started, exc)
                    errors.append(f"{name}:UNSUPPORTED:{exc}")
                    break
                except Exception as exc:
                    self._record(name, started, exc)
                    errors.append(f"{name}:{provider_error_category(exc)}:{type(exc).__name__}:{exc}")
                    if attempt + 1 < self.attempts_per_provider:
                        low, high = ((0.2, 0.5) if attempt == 0 else (0.5, 1.0))
                        await asyncio.sleep(random.uniform(low, high))
            if provider_index + 1 < len(ordered):
                await asyncio.sleep(random.uniform(0.5, 1.0))
        raise AllProvidersFailedError(f"ALL_PROVIDER_FAILED {method}: {' | '.join(errors)}")

    async def get_quote(self, code: str) -> Quote:
        return await self._call("get_quote", code)

    async def get_index_quote(self, code: str, market: str) -> Quote:
        return await self._call("get_index_quote", code, market)

    async def get_quotes(self, codes: list[str]) -> list[Quote]:
        unique = list(dict.fromkeys(codes))
        collected: dict[str, Quote] = {}
        errors: list[str] = []
        ordered = self._ordered_names()
        for provider_index, name in enumerate(ordered):
            missing = [code for code in unique if code not in collected]
            if not missing:
                break
            for attempt in range(self.attempts_per_provider):
                started = time.perf_counter()
                try:
                    values = await self.providers[name].get_quotes(missing)
                    for value in values:
                        self._validate(value)
                        collected[value.code] = value
                    if not values:
                        raise ProviderEmptyDataError("provider returned no quotes")
                    self._record(name, started, None)
                    break
                except Exception as exc:
                    self._record(name, started, exc)
                    errors.append(f"{name}:{provider_error_category(exc)}:{type(exc).__name__}:{exc}")
                    if attempt + 1 < self.attempts_per_provider:
                        await asyncio.sleep(random.uniform(0.2, 0.5))
            if provider_index + 1 < len(ordered) and any(code not in collected for code in unique):
                await asyncio.sleep(random.uniform(0.5, 1.0))
        if not collected:
            raise AllProvidersFailedError(f"ALL_PROVIDER_FAILED get_quotes: {' | '.join(errors)}")
        return [collected[code] for code in unique if code in collected]

    async def get_kline(
        self, code: str, period: str, limit: int, adjust: str = "qfq", *, quote: Quote | None = None
    ) -> KlineResult:
        supported = ("eastmoney", "tencent") if period == "day" else ("eastmoney",)
        return await self._call("get_kline", code, period, limit, adjust, supported=supported)

    async def get_all_a_shares(self) -> tuple[int, list[Quote]]:
        return await self._call("get_all_a_shares", supported=("eastmoney",))

    async def get_sector_ranking(self, sector_type: str, limit: int) -> SectorRanking:
        return await self._call("get_sector_ranking", sector_type, limit, supported=("eastmoney",))
