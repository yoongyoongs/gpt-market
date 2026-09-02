"""生产级 Recall Observation Outcome Provider（RT-10 / RC-06）。

从系统已落库的事实点时计算 outcome：

- future_price：maturity 收盘（日 K Revision 中 bar_time <= matures_at
  的最后一根非 provisional bar 的 close）；
- benchmark_return：同窗口（as_of → matures_at）指数基准收益，
  基准缺失或历史不足显式 None（不伪造）；
- 日 K 不足（maturity 前 5 个自然日内无 bar）显式 UNAVAILABLE。

点时安全：日 K / 基准 Revision 均以 known_at <= as_of 读取。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from app.v3.domain.recall import PerformanceObservation
from app.v3.providers.recall import ObservationOutcome

BENCHMARK_CODE = "HS300"
# maturity 之前的 bar 容忍窗口：交易日历节假日造成的自然日间隔
_MATURITY_TOLERANCE = timedelta(days=5)


class BarsOutcomeProvider:
    def __init__(self, uow_factory: Callable) -> None:
        self._uow_factory = uow_factory

    async def resolve(
        self,
        observations: tuple[PerformanceObservation, ...],
        *,
        as_of: datetime,
    ) -> tuple[ObservationOutcome, ...]:
        security_ids = tuple({item.security_id for item in observations})
        async with self._uow_factory() as uow:
            revisions = await uow.bars.latest_daily_revisions(
                security_ids,
                as_of=as_of,
            )
            benchmark = await uow.index_benchmarks.latest(
                BENCHMARK_CODE,
                as_of=as_of,
            )
        bars_by_security = {
            security_id: [bar for bar in revision.bars if not bar.provisional]
            for security_id, revision in zip(security_ids, revisions)
        }
        index_bars = list(benchmark.bars) if benchmark is not None else []
        return tuple(
            self._resolve_one(item, bars_by_security, index_bars)
            for item in observations
        )

    def _resolve_one(
        self,
        observation: PerformanceObservation,
        bars_by_security: dict,
        index_bars: list,
    ) -> ObservationOutcome:
        bars = bars_by_security.get(observation.security_id, [])
        maturity_bars = [bar for bar in bars if bar.bar_time <= observation.matures_at]
        if not maturity_bars:
            return ObservationOutcome(
                pending_observation_id=observation.observation_id,
                unavailable_reason="NO_BAR_AT_MATURITY",
            )
        future_bar = maturity_bars[-1]
        if observation.matures_at - future_bar.bar_time > _MATURITY_TOLERANCE:
            return ObservationOutcome(
                pending_observation_id=observation.observation_id,
                unavailable_reason="NO_BAR_AT_MATURITY",
            )
        benchmark_return = self._benchmark_return(
            observation,
            index_bars,
        )
        return ObservationOutcome(
            pending_observation_id=observation.observation_id,
            future_price=float(future_bar.close),
            benchmark_return=benchmark_return,
        )

    @staticmethod
    def _benchmark_return(observation: PerformanceObservation, index_bars: list):
        window = [
            bar
            for bar in index_bars
            if observation.as_of <= bar.bar_time <= observation.matures_at
        ]
        if len(window) < 2:
            return None
        return float(Decimal(str(window[-1].close)) / Decimal(str(window[0].close)) - 1)
