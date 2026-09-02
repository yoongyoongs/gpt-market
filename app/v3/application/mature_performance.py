"""Performance Mature Engine（RC-06A / PF-001）。

正式生产绩效事实由本 Job 从系统事实自动计算，不接受调用方直接
"告诉服务器答案"作为正式生产来源（POST /performance/attributions 的
手工通道另行收口）：

- 输入：Decision（含 original EntryPlan 快照）、Trade Ledger、
  日 K Revision（QFQ）、指数基准 Revision、as_of/成熟日历；
- 指标：T+1/3/5/10/20 Return、Benchmark Excess Return、MFE/MAE、
  Target/Stop hit + first hit time、direction correctness、
  entry_triggered、actual_trade、holding sessions；
- 七类能力分开，本引擎产出 SELECTION（决策级）事实，禁止重新汇总
  为统一最终分；
- attribution_id 采用 uuid5 确定性派生（decision + horizon +
  calculation_version），同输入重跑内容寻址幂等；
- 输入缺失（基准不可用/无止损目标/无方向）显式置 None + reason，
  绝不伪造。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from app.v3.domain.performance import (
    PerformanceAbility,
    PerformanceAttributionCreate,
    content_hash,
)

CALCULATION_VERSION = "performance-mature-v1"
SOURCE = "system-mature-engine"
HORIZONS = (1, 3, 5, 10, 20)
BENCHMARK_CODE = "HS300"
# uuid5 命名空间（确定性 attribution_id，内容寻址幂等的组成部分）
_NAMESPACE = UUID("8f0e7a52-6d3b-4a5d-9d1a-2b6f0d3c5e01")


class MaturePerformanceService:
    def __init__(
        self,
        uow_factory: Callable,
        *,
        clock: Callable[[], datetime] | None = None,
        calculation_version: str = CALCULATION_VERSION,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._calculation_version = calculation_version

    async def execute(self, *, as_of: datetime | None = None) -> dict[str, Any]:
        as_of = as_of or self._clock()
        known_at = self._clock()
        matured = 0
        pending = 0
        skipped = 0
        async with self._uow_factory() as uow:
            candidates = await uow.performance.mature_decision_candidates(as_of)
            for decision in candidates:
                revisions = await uow.bars.latest_daily_revisions(
                    (decision["security_id"],), as_of=as_of
                )
                bars = [
                    bar for bar in revisions[0].bars
                    if not bar.provisional
                ] if revisions else []
                if not bars:
                    pending += len(HORIZONS)
                    continue
                baseline_index = max(
                    index for index, bar in enumerate(bars)
                    if bar.bar_time <= decision["as_of"]
                )
                baseline = bars[baseline_index]
                benchmark = await uow.index_benchmarks.latest(
                    BENCHMARK_CODE, as_of=as_of
                )
                trades = await uow.performance.decision_trades(
                    decision["decision_id"]
                )
                for horizon in HORIZONS:
                    window = bars[
                        baseline_index + 1 : baseline_index + 1 + horizon
                    ]
                    if len(window) < horizon:
                        pending += 1
                        continue
                    matures_at = window[-1].bar_time
                    if known_at < matures_at:
                        pending += 1
                        continue
                    attribution = self._build_attribution(
                        decision, baseline, window, horizon,
                        benchmark, trades, matures_at, known_at,
                    )
                    already_exists = (
                        await uow.performance.attribution_exists(
                            content_hash(attribution)
                        )
                        or await uow.performance.attribution_id_exists(
                            attribution.attribution_id
                        )
                    )
                    if already_exists:
                        skipped += 1
                        continue
                    await uow.performance.add_attribution(attribution)
                    matured += 1
            if matured:
                await uow.commit()
        return {
            "status": "COMPLETED",
            "known_at": known_at.isoformat(),
            "candidates": len(candidates) if candidates else 0,
            "matured_count": matured,
            "pending_count": pending,
            "skipped_count": skipped,
            "calculation_version": self._calculation_version,
        }

    def _build_attribution(
        self, decision, baseline, window, horizon: int,
        benchmark, trades, matures_at: datetime, known_at: datetime,
    ) -> PerformanceAttributionCreate:
        baseline_price = Decimal(str(baseline.close))
        future_price = Decimal(str(window[-1].close))
        raw_return = float(future_price / baseline_price - 1)
        highs = [float(bar.high) for bar in window]
        lows = [float(bar.low) for bar in window]
        mfe = max(highs) / float(baseline_price) - 1
        mae = min(lows) / float(baseline_price) - 1

        metrics: dict[str, Any] = {
            "calculation_version": self._calculation_version,
            "source": SOURCE,
            "baseline_price": float(baseline_price),
            "future_price": float(future_price),
            "baseline_time": baseline.bar_time.isoformat(),
            "window_end": window[-1].bar_time.isoformat(),
            "direction": decision["payload"].get("direction"),
        }

        # Benchmark Excess Return：同一时间窗的指数收益；缺失显式置 None
        if benchmark is None:
            excess_return = None
            metrics["excess_reason"] = "BENCHMARK_UNAVAILABLE"
        else:
            index_closes = [
                bar for bar in benchmark.bars
                if baseline.bar_time <= bar.bar_time <= window[-1].bar_time
            ]
            if index_closes:
                index_return = float(
                    Decimal(str(index_closes[-1].close))
                    / Decimal(str(index_closes[0].close))
                    - 1
                )
                excess_return = raw_return - index_return
                metrics["benchmark_code"] = benchmark.benchmark_code
                metrics["benchmark_return"] = index_return
            else:
                excess_return = None
                metrics["excess_reason"] = "BENCHMARK_INSUFFICIENT_HISTORY"

        # Target/Stop：来自 original EntryPlan 快照，缺失显式置 None
        plan = decision["original_entry_plan_snapshot"].get("plan", {})
        target = plan.get("take_profit")
        stop = plan.get("stop_loss")
        target_hit, first_target = self._first_hit(
            window, target, lambda bar, level: float(bar.high) >= float(level)
        )
        stop_hit, first_stop = self._first_hit(
            window, stop, lambda bar, level: float(bar.low) <= float(level)
        )
        if first_target is not None:
            metrics["first_target_hit_time"] = first_target.isoformat()
        if first_stop is not None:
            metrics["first_stop_hit_time"] = first_stop.isoformat()

        # 方向正确性：payload.direction 为 LONG/SHORT 时按收益方向判定，
        # 无方向声明时显式 None（不猜测）。
        direction = decision["payload"].get("direction")
        direction_correctness = (
            (raw_return > 0) if direction == "LONG"
            else (raw_return < 0) if direction == "SHORT"
            else None
        )
        metrics["direction_correctness"] = direction_correctness

        # 成交事实：actual_trade / entry_triggered / holding
        actual_trade = bool(trades)
        metrics["actual_trade"] = actual_trade
        entry_window_start = plan.get("entry_window_start")
        entry_window_end = plan.get("entry_window_end")
        if entry_window_start is not None and entry_window_end is not None:
            entry_triggered = any(
                datetime.fromisoformat(str(item["trade_time"]))
                <= datetime.fromisoformat(str(entry_window_end))
                and datetime.fromisoformat(str(item["trade_time"]))
                >= datetime.fromisoformat(str(entry_window_start))
                for item in trades
            )
            metrics["entry_trigger_source"] = "PLAN_ENTRY_WINDOW"
        else:
            entry_triggered = actual_trade
            metrics["entry_trigger_source"] = "ACTUAL_TRADE_FALLBACK"
        metrics["entry_triggered"] = entry_triggered
        if actual_trade:
            first_trade = min(
                datetime.fromisoformat(str(item["trade_time"])) for item in trades
            )
            metrics["holding_sessions"] = len([
                bar for bar in window if bar.bar_time >= first_trade
            ])
        else:
            metrics["holding_sessions"] = None

        attribution_id = uuid5(
            _NAMESPACE,
            f"{decision['decision_id']}:{horizon}:{self._calculation_version}",
        )
        return PerformanceAttributionCreate(
            attribution_id=attribution_id,
            ability=PerformanceAbility.SELECTION,
            subject_type="DECISION",
            subject_id=decision["decision_id"],
            strategy_version=str(
                decision["payload"].get("strategy_version") or "unversioned"
            ),
            decision_id=decision["decision_id"],
            original_entry_plan_id=decision["original_entry_plan_id"],
            horizon_sessions=horizon,
            as_of=decision["as_of"],
            matures_at=matures_at,
            known_at=known_at,
            raw_return=raw_return,
            excess_return=excess_return,
            mfe=mfe,
            mae=mae,
            target_hit=target_hit,
            stop_hit=stop_hit,
            metrics=metrics,
            explanation=(
                f"{SOURCE} 从日 K 事实自动计算 T+{horizon} 绩效"
                f"（calculation_version={self._calculation_version}）"
            ),
        )

    @staticmethod
    def _first_hit(window, level, predicate):
        if level is None:
            return None, None
        for bar in window:
            if predicate(bar, level):
                return True, bar.bar_time
        return False, None
