from __future__ import annotations

import pytest

from app.v3.domain.market_data import AdjustType, BarPeriod, HistoricalBarFetchResult
from app.v3.infrastructure.providers.bars import (
    CircuitBreakingHistoricalBarProvider,
    HistoricalBarProviderCircuitOpen,
)
from tests.v3.test_ingest_daily_bars import result


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class FlakyProvider:
    code = "flaky"

    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = RuntimeError("down")

    async def fetch(self, code, period, adjust_type, limit) -> HistoricalBarFetchResult:
        self.calls += 1
        if self.error:
            raise self.error
        return result(self.code, adjust_type).model_copy(update={"code": code})


@pytest.mark.asyncio
async def test_circuit_opens_and_probes_again_after_cooldown() -> None:
    clock = Clock()
    provider = FlakyProvider()
    guarded = CircuitBreakingHistoricalBarProvider(
        provider,
        failure_threshold=2,
        cooldown_seconds=30,
        monotonic=clock,
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="down"):
            await guarded.fetch("600000", BarPeriod.DAY, AdjustType.QFQ, 300)
    with pytest.raises(HistoricalBarProviderCircuitOpen, match="circuit is open"):
        await guarded.fetch("600000", BarPeriod.DAY, AdjustType.QFQ, 300)
    assert provider.calls == 2

    clock.value += 31
    provider.error = None
    fetched = await guarded.fetch("600000", BarPeriod.DAY, AdjustType.QFQ, 300)

    assert fetched.code == "600000"
    assert provider.calls == 3
