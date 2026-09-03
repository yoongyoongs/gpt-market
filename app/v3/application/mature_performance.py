"""Performance Mature Engine（RC-06A / PF-001）。

正式生产绩效事实由本 Job 从系统事实自动计算，不接受调用方直接
"告诉服务器答案"作为正式生产来源（POST /performance/attributions 的
手工通道另行收口）：

- 输入：Decision（含 original EntryPlan 快照）、Trade Ledger、
  日 K Revision（QFQ）、指数基准 Revision、as_of/成熟日历；
- 指标：T+1/3/5/10/20 Return、Benchmark Excess Return、MFE/MAE、
  Target/Stop hit + first hit time、direction correctness、
  entry_triggered、actual_trade、holding sessions；
- 七类能力分开，本引擎自动产出其中四类（禁止重新汇总为统一最终分）：
  SELECTION（决策级）、INITIAL_ENTRY（入场执行一致性，Trade + EntryPlan）、
  USER_EXECUTION（逐笔成交质量，subject=TRADE）、RISK_CONTROL（止损响应，
  计划有止损且实际有成交时才有意义）；ADD/REDUCE/FINAL_EXIT 依赖仓位
  调整语义，暂不自动产出（诚实缺失，绝不伪造凑数）；
- attribution_id 采用 uuid5 确定性派生（ability + subject + horizon +
  calculation_version），同输入重跑内容寻址幂等；
- 输入缺失（基准不可用/无止损目标/无方向）显式置 None + reason，
  绝不伪造；
- NEW-PF-001：单候选隔离 —— 坏候选（无 baseline bar / 脏数据）计入
  issues 并跳过，绝不拖垮整批 Job。
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
        by_ability: dict[str, int] = {}
        issues: dict[str, int] = {}
        async with self._uow_factory() as uow:
            candidates = await uow.performance.mature_decision_candidates(as_of)
            for decision in candidates:
                # NEW-PF-001：单候选隔离 —— 一个异常候选绝不拖垮整批 Job
                try:
                    written, waited, dropped, reasons = await self._process_candidate(
                        uow, decision, as_of, known_at, by_ability,
                    )
                except Exception as exc:  # noqa: BLE001
                    written, waited, dropped = 0, 0, len(HORIZONS)
                    key = f"CANDIDATE_ERROR:{type(exc).__name__}"
                    reasons = {key: 1}
                matured += written
                pending += waited
                skipped += dropped
                for reason, count in reasons.items():
                    issues[reason] = issues.get(reason, 0) + count
            if matured:
                await uow.commit()
        return {
            "status": "COMPLETED",
            "known_at": known_at.isoformat(),
            "candidates": len(candidates) if candidates else 0,
            "matured_count": matured,
            "pending_count": pending,
            "skipped_count": skipped,
            "by_ability": by_ability,
            "issues": issues,
            "calculation_version": self._calculation_version,
        }

    async def _process_candidate(
        self, uow, decision, as_of: datetime, known_at: datetime,
        by_ability: dict[str, int],
    ) -> tuple[int, int, int, dict[str, int]]:
        """处理单个候选决策，返回 (matured, pending, skipped, issues)。

        产出四类 Attribution：SELECTION 决策级 + INITIAL_ENTRY /
        USER_EXECUTION / RISK_CONTROL（Trade + Plan 锚定）。"""
        matured = pending = skipped = 0
        issues: dict[str, int] = {}
        revisions = await uow.bars.latest_daily_revisions(
            (decision["security_id"],), as_of=as_of
        )
        bars = [
            bar for bar in revisions[0].bars
            if not bar.provisional
        ] if revisions else []
        if not bars:
            return 0, len(HORIZONS), 0, issues
        # NEW-PF-001：无 baseline bar 不再抛 ValueError，跳过并记录 reason
        eligible = [
            index for index, bar in enumerate(bars)
            if bar.bar_time <= decision["as_of"]
        ]
        if not eligible:
            return 0, 0, len(HORIZONS), {"NO_BASELINE_BAR": 1}
        baseline_index = eligible[-1]
        baseline = bars[baseline_index]
        benchmark = await uow.index_benchmarks.latest(
            BENCHMARK_CODE, as_of=as_of
        )
        trades = await uow.performance.decision_trades(
            decision["decision_id"]
        )
        plan = decision["original_entry_plan_snapshot"].get("plan", {})
        stop = plan.get("stop_loss")
        buys = [
            item for item in trades
            if str(item.get("side", "")).upper() == "BUY"
        ]
        sells = [
            item for item in trades
            if str(item.get("side", "")).upper() == "SELL"
        ]

        async def persist(attribution) -> None:
            nonlocal matured, skipped
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
                return
            await uow.performance.add_attribution(attribution)
            matured += 1
            ability = attribution.ability.value
            by_ability[ability] = by_ability.get(ability, 0) + 1

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
            await persist(self._build_selection(
                decision, plan, baseline, window, horizon,
                benchmark, trades, matures_at, known_at,
            ))
            if buys:
                await persist(self._build_initial_entry(
                    decision, plan, baseline, horizon,
                    buys, matures_at, known_at,
                ))
            for trade in trades:
                await persist(self._build_user_execution(
                    decision, trade, bars, horizon, matures_at, known_at,
                ))
            # 计划有止损且实际有成交（持仓存在）时，止损响应才有事实可评
            if stop is not None and trades:
                await persist(self._build_risk_control(
                    decision, plan, baseline, window, horizon,
                    sells, matures_at, known_at,
                ))
        return matured, pending, skipped, issues

    def _build_selection(
        self, decision, plan, baseline, window, horizon: int,
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

    def _build_initial_entry(
        self, decision, plan, baseline, horizon: int,
        buys, matures_at: datetime, known_at: datetime,
    ) -> PerformanceAttributionCreate:
        """INITIAL_ENTRY：首次买入执行 vs 计划入场窗口/价格区间的一致性。"""
        entry = min(
            buys, key=lambda item: datetime.fromisoformat(str(item["trade_time"]))
        )
        entry_price = Decimal(str(entry["price"]))
        baseline_price = Decimal(str(baseline.close))
        metrics: dict[str, Any] = {
            "calculation_version": self._calculation_version,
            "source": SOURCE,
            "entry_price": float(entry_price),
            "entry_time": datetime.fromisoformat(str(entry["trade_time"])).isoformat(),
            "entry_price_vs_baseline_close": float(entry_price / baseline_price - 1),
        }
        window_start = plan.get("entry_window_start")
        window_end = plan.get("entry_window_end")
        if window_start is not None and window_end is not None:
            metrics["entry_within_window"] = (
                datetime.fromisoformat(str(window_start))
                <= datetime.fromisoformat(str(entry["trade_time"]))
                <= datetime.fromisoformat(str(window_end))
            )
        else:
            metrics["entry_within_window"] = None
            metrics["entry_window_reason"] = "PLAN_HAS_NO_ENTRY_WINDOW"
        price_low = plan.get("entry_price_low")
        price_high = plan.get("entry_price_high")
        if price_low is not None and price_high is not None:
            metrics["entry_price_in_range"] = (
                Decimal(str(price_low)) <= entry_price <= Decimal(str(price_high))
            )
        else:
            metrics["entry_price_in_range"] = None
            metrics["entry_price_range_reason"] = "PLAN_HAS_NO_ENTRY_PRICE_RANGE"
        trade_plan_id = entry.get("entry_plan_id")
        if trade_plan_id is None:
            plan_binding = "UNSPECIFIED"
        elif str(trade_plan_id) == str(decision["original_entry_plan_id"]):
            plan_binding = "BOUND"
        else:
            plan_binding = "UNBOUND"
        metrics["plan_binding"] = plan_binding
        attribution_id = uuid5(
            _NAMESPACE,
            f"INITIAL_ENTRY:{decision['decision_id']}:{horizon}:{self._calculation_version}",
        )
        return PerformanceAttributionCreate(
            attribution_id=attribution_id,
            ability=PerformanceAbility.INITIAL_ENTRY,
            subject_type="DECISION",
            subject_id=decision["decision_id"],
            strategy_version=str(
                decision["payload"].get("strategy_version") or "unversioned"
            ),
            decision_id=decision["decision_id"],
            original_entry_plan_id=decision["original_entry_plan_id"],
            trade_id=entry["trade_id"],
            trade_bound_entry_plan_id=(
                trade_plan_id if plan_binding == "BOUND" else None
            ),
            horizon_sessions=horizon,
            as_of=decision["as_of"],
            matures_at=matures_at,
            known_at=known_at,
            metrics=metrics,
            explanation=(
                f"{SOURCE} 从 Trade/EntryPlan 事实自动评估 INITIAL_ENTRY"
                f" 执行一致性（T+{horizon} 窗口）"
            ),
        )

    def _build_user_execution(
        self, decision, trade, bars, horizon: int,
        matures_at: datetime, known_at: datetime,
    ) -> PerformanceAttributionCreate:
        """USER_EXECUTION：逐笔成交质量（subject=TRADE，要求 trade_id）。"""
        trade_id = trade["trade_id"]
        price = Decimal(str(trade["price"]))
        trade_time = datetime.fromisoformat(str(trade["trade_time"]))
        metrics: dict[str, Any] = {
            "calculation_version": self._calculation_version,
            "source": SOURCE,
            "trade_side": str(trade.get("side", "")).upper(),
            "executed_price": float(price),
            "executed_quantity": float(Decimal(str(trade["quantity"]))),
        }
        day_bar = next(
            (bar for bar in bars if bar.bar_time.date() == trade_time.date()),
            None,
        )
        if day_bar is None:
            metrics["trade_day_reason"] = "NO_BAR_ON_TRADE_DAY"
            metrics["in_day_range"] = None
            metrics["price_vs_open_pct"] = None
        else:
            day_open = Decimal(str(day_bar.open))
            metrics["trade_day_open"] = float(day_open)
            metrics["trade_day_high"] = float(day_bar.high)
            metrics["trade_day_low"] = float(day_bar.low)
            metrics["in_day_range"] = (
                Decimal(str(day_bar.low)) <= price <= Decimal(str(day_bar.high))
            )
            metrics["price_vs_open_pct"] = float(price / day_open - 1)
        attribution_id = uuid5(
            _NAMESPACE,
            f"USER_EXECUTION:{trade_id}:{horizon}:{self._calculation_version}",
        )
        return PerformanceAttributionCreate(
            attribution_id=attribution_id,
            ability=PerformanceAbility.USER_EXECUTION,
            subject_type="TRADE",
            subject_id=trade_id,
            strategy_version=str(
                decision["payload"].get("strategy_version") or "unversioned"
            ),
            decision_id=decision["decision_id"],
            trade_id=trade_id,
            trade_bound_entry_plan_id=trade.get("entry_plan_id"),
            horizon_sessions=horizon,
            as_of=decision["as_of"],
            matures_at=matures_at,
            known_at=known_at,
            metrics=metrics,
            explanation=(
                f"{SOURCE} 从 Trade Ledger 事实自动评估 USER_EXECUTION"
                f" 成交质量（T+{horizon} 窗口）"
            ),
        )

    def _build_risk_control(
        self, decision, plan, baseline, window, horizon: int,
        sells, matures_at: datetime, known_at: datetime,
    ) -> PerformanceAttributionCreate:
        """RISK_CONTROL：止损触发事实 + 是否实际退出（绝不产生建议）。"""
        stop = plan.get("stop_loss")
        baseline_price = Decimal(str(baseline.close))
        stop_hit, first_stop = self._first_hit(
            window, stop, lambda bar, level: float(bar.low) <= float(level)
        )
        metrics: dict[str, Any] = {
            "calculation_version": self._calculation_version,
            "source": SOURCE,
            "stop_loss": float(Decimal(str(stop))),
            "stop_hit": stop_hit,
        }
        if first_stop is not None:
            metrics["first_stop_hit_time"] = first_stop.isoformat()
        if stop_hit:
            exit_times = [
                datetime.fromisoformat(str(item["trade_time"])) for item in sells
            ]
            exited = bool(exit_times) and min(exit_times) >= first_stop
            metrics["exited_after_stop"] = exited
            metrics["risk_outcome"] = (
                "EXITED_AFTER_STOP" if exited else "NO_EXIT_AFTER_STOP"
            )
        mae = min(float(bar.low) for bar in window) / float(baseline_price) - 1
        attribution_id = uuid5(
            _NAMESPACE,
            f"RISK_CONTROL:{decision['decision_id']}:{horizon}:{self._calculation_version}",
        )
        return PerformanceAttributionCreate(
            attribution_id=attribution_id,
            ability=PerformanceAbility.RISK_CONTROL,
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
            mae=mae,
            stop_hit=stop_hit,
            metrics=metrics,
            explanation=(
                f"{SOURCE} 从日 K + Trade 事实自动评估 RISK_CONTROL"
                f" 止损响应（T+{horizon} 窗口）"
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
