from __future__ import annotations

import asyncio
import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass

from app.cache import AsyncTTLCache
from app.fundamentals.base import FundamentalProvider
from app.models import FundamentalConflict, FundamentalField, FundamentalSnapshot
from app.utils.time import now_shanghai


logger = logging.getLogger("uvicorn.error")


@dataclass
class FundamentalProviderState:
    success_count: int = 0
    failure_count: int = 0
    last_error: str | None = None


def _numeric_conflict(left: object, right: object) -> bool:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return left != right
    scale = max(abs(float(left)), abs(float(right)), 1.0)
    return abs(float(left) - float(right)) / scale > 0.03


class FundamentalProviderManager:
    """Data-type scoped manager; scoring never imports concrete providers."""

    def __init__(
        self,
        primary: FundamentalProvider,
        fallbacks: list[FundamentalProvider] | None = None,
        *,
        cache: AsyncTTLCache | None = None,
        ttl_seconds: float = 21_600,
    ) -> None:
        self.primary = primary
        self.fallbacks = fallbacks or []
        self.cache = cache or AsyncTTLCache()
        self.ttl_seconds = ttl_seconds
        self.states = {provider.name: FundamentalProviderState() for provider in [primary, *self.fallbacks]}
        self._last_success: dict[str, FundamentalSnapshot] = {}

    async def _fetch(self, provider: FundamentalProvider, codes: list[str]) -> dict[str, FundamentalSnapshot]:
        try:
            result = await provider.get_many(codes)
            self.states[provider.name].success_count += len(result)
            return result
        except Exception as exc:
            state = self.states[provider.name]
            state.failure_count += len(codes)
            state.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("基本面源失败 provider=%s codes=%d error=%s", provider.name, len(codes), state.last_error)
            return {}

    @staticmethod
    def _merge(primary: FundamentalSnapshot, fallback: FundamentalSnapshot) -> FundamentalSnapshot:
        fields = dict(primary.fields)
        conflicts = list(primary.conflicts)
        for name, other in fallback.fields.items():
            current = fields.get(name)
            if current is None or not current.coverage:
                fields[name] = other
            elif other.coverage and _numeric_conflict(current.value, other.value):
                conflicts.append(
                    FundamentalConflict(
                        field=name,
                        selected_source=current.source,
                        selected_value=current.value,
                        conflicting_source=other.source,
                        conflicting_value=other.value,
                    )
                )
                fields[name] = current.model_copy(update={"conflicts": [*current.conflicts, conflicts[-1]]})
        return primary.model_copy(
            update={
                "fields": fields,
                "quarterly_trend": primary.quarterly_trend or fallback.quarterly_trend,
                "performance_forecast": primary.performance_forecast or fallback.performance_forecast,
                "performance_express": primary.performance_express or fallback.performance_express,
                "audit_opinion": primary.audit_opinion or fallback.audit_opinion,
                "upstream_sources": list(dict.fromkeys([*primary.upstream_sources, *fallback.upstream_sources])),
                "conflicts": conflicts,
            }
        ).with_coverage()

    async def get_many(self, codes: list[str]) -> dict[str, FundamentalSnapshot]:
        unique = list(dict.fromkeys(codes))
        cached: dict[str, FundamentalSnapshot] = {}
        missing: list[str] = []
        for code in unique:
            value = self.cache.peek(f"fundamental:{code}")
            if value is not None:
                cached[code] = value
            else:
                missing.append(code)
        if not missing:
            return self._add_industry_medians(cached)

        loaded = await self._fetch(self.primary, missing)
        incomplete = [code for code in missing if code not in loaded or loaded[code].coverage < 0.65]
        for provider in self.fallbacks:
            if not incomplete:
                break
            extra = await self._fetch(provider, incomplete)
            for code, snapshot in extra.items():
                loaded[code] = self._merge(loaded[code], snapshot) if code in loaded else snapshot
            incomplete = [code for code in incomplete if code not in loaded or loaded[code].coverage < 0.65]

        for code in missing:
            snapshot = loaded.get(code)
            if snapshot is not None and snapshot.coverage > 0:
                self._last_success[code] = snapshot
            elif code in self._last_success:
                previous = self._last_success[code]
                error = "; ".join(filter(None, (state.last_error for state in self.states.values()))) or "latest refresh returned no data"
                fields = {
                    name: field.model_copy(update={"stale": True, "error": error})
                    for name, field in previous.fields.items()
                }
                snapshot = previous.model_copy(update={"fields": fields, "stale": True, "error": error, "confidence": "LOW"})
            if snapshot is None:
                error = "; ".join(filter(None, (state.last_error for state in self.states.values()))) or "no fundamental data"
                field = FundamentalField(
                    value=None,
                    source="fundamental_manager",
                    upstream_source="none",
                    source_type="aggregator",
                    report_period=None,
                    fetch_time=now_shanghai(),
                    coverage=False,
                    stale=False,
                    confidence="LOW",
                    error=error,
                )
                snapshot = FundamentalSnapshot(
                    code=code,
                    fields={"unavailable": field},
                    fetch_time=now_shanghai(),
                    source="fundamental_manager",
                    upstream_sources=[],
                    error=error,
                )
            self.cache.set(
                f"fundamental:{code}",
                snapshot,
                self.ttl_seconds if snapshot.coverage > 0 and not snapshot.stale else min(300, self.ttl_seconds),
            )
            cached[code] = snapshot
        return self._add_industry_medians(cached)

    @staticmethod
    def _add_industry_medians(values: dict[str, FundamentalSnapshot]) -> dict[str, FundamentalSnapshot]:
        peers: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"pe": [], "pb": []})
        for snapshot in values.values():
            industry = snapshot.fields.get("industry")
            if not industry or not industry.coverage or not isinstance(industry.value, str):
                continue
            for key in ("pe", "pb"):
                field = snapshot.fields.get(key)
                if field and field.coverage and isinstance(field.value, (int, float)) and field.value > 0:
                    peers[industry.value][key].append(float(field.value))
        result: dict[str, FundamentalSnapshot] = {}
        for code, snapshot in values.items():
            fields = dict(snapshot.fields)
            industry = fields.get("industry")
            industry_name = industry.value if industry and isinstance(industry.value, str) else None
            for key in ("pe", "pb"):
                samples = peers[industry_name][key] if industry_name in peers else []
                value = statistics.median(samples) if len(samples) >= 3 else None
                fields[f"industry_{key}_median"] = FundamentalField(
                    value=value,
                    source="fundamental_manager_peer_median",
                    upstream_source="eastmoney",
                    source_type="aggregator",
                    report_period=None,
                    fetch_time=snapshot.fetch_time,
                    coverage=value is not None,
                    stale=snapshot.stale,
                    confidence="MEDIUM" if value is not None else "LOW",
                    error=None if value is not None else "fewer than 3 same-industry peers in candidate pool",
                )
            result[code] = snapshot.model_copy(update={"fields": fields})
        return result

    async def get(self, code: str) -> FundamentalSnapshot:
        return (await self.get_many([code]))[code]

    def health(self) -> dict[str, object]:
        return {name: vars(state) for name, state in self.states.items()}

    async def close(self) -> None:
        await asyncio.gather(*(provider.close() for provider in [self.primary, *self.fallbacks]))
