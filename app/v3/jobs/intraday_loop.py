"""RT §21 盘中触发循环（部署裁决：现有 worker 容器内轻量循环，不新增容器）。

RT-01~03 的盘中能力此前只有 API/MCP 按需拉取——没人访问就没数据，
AttentionEvent 的盘中评估没有触发点。本循环补上确定性触发：

- 交易时段（XSHG 交易日 + 09:30-11:30 / 13:00-15:00 CST）内按任务
  独立 due-time 评估（R5-P1-007/§65 禁止单一 300s 控制所有任务）：
    * Entry/Stop/Target Trigger：默认 45s（30–60s）；
    * 全市场 Quote/Overlay/Scanner（Fast Lane）：默认 90s（30–120s）；
    * Intraday Evidence：默认 600s（5–15min，任务本体由 R5-08 接线）；
- 每轮：各 decision 最新 plan 的 stop/target × 实时 quote →
  AttentionEngineService.evaluate_entry_plan_levels（去抖由 engine 负责）；
- 行情失败/quote 缺失：跳过该计划并如实计数，绝不伪造价格；
- 不产生 Trade、不改 Decision——只落 AttentionEvent（engine append-only）；
- R4-P2-008：失败绝不静默——heartbeat 记录成功/错误时间线；
- R5-P1-007/§65：heartbeat 不再只活在本进程内存——可选 health_sink
  把心跳（含 per-lane 连续错误、quote 覆盖、池/候选/Deep 计数）节流
  持久化到 operational_health_events，API/Dashboard 进程可读。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, time, timezone
from typing import Any, Protocol

from app.utils.time import SHANGHAI


# A 股连续竞价时段（不含集合竞价，保守起点）
TRADING_SESSIONS: tuple[tuple[time, time], ...] = (
    (time(9, 30), time(11, 30)),
    (time(13, 0), time(15, 0)),
)
IDLE_POLL_SECONDS = 60.0
# R5-P1-007/§65：各任务独立 cadence（§65 冻结区间中值；禁单一 300s）
DEFAULT_TRIGGER_INTERVAL = 45.0    # 30–60s
DEFAULT_FASTLANE_INTERVAL = 90.0   # 30–120s
DEFAULT_EVIDENCE_INTERVAL = 600.0  # 5–15min
# heartbeat 持久化节流：同状态最多每 30s 落一条（错误强制落）
HEARTBEAT_PERSIST_SECONDS = 30.0
# Fast Lane 这些结局按"该轮失败"计（异常同样计）——部分市场可用不算失败
_FASTLANE_FAILED_STATUSES = {"QUOTE_FAILED", "UNAVAILABLE_FOR_FULL_MARKET_SCAN"}
# §65 验收阈值：连续失败达到 3 次，HTTP 状态接口必须可见 degraded
DEGRADED_ERROR_THRESHOLD = 3


def in_trading_session(
    local_time: time, sessions: tuple[tuple[time, time], ...] = TRADING_SESSIONS,
) -> bool:
    return any(start <= local_time <= end for start, end in sessions)


class _PlansRepo(Protocol):
    async def active_price_trigger_plans(self) -> tuple[dict, ...]: ...


class _Uow(Protocol):
    ai_imports: _PlansRepo

    async def __aenter__(self) -> "_Uow": ...

    async def __aexit__(self, *args) -> None: ...


class _AttentionEngine(Protocol):
    async def evaluate_entry_plan_levels(self, **kwargs) -> Any: ...


class _QuoteService(Protocol):
    async def get_quote_snapshot(self, code: str, *, as_of: datetime) -> Any: ...


class IntradayTriggerLoop:
    def __init__(
        self,
        uow_factory: Callable[[], _Uow],
        quote_service: _QuoteService,
        engine: _AttentionEngine,
        is_trading_day: Callable[..., bool],
        *,
        trigger_interval: float = DEFAULT_TRIGGER_INTERVAL,
        fast_lane_interval: float = DEFAULT_FASTLANE_INTERVAL,
        evidence_interval: float = DEFAULT_EVIDENCE_INTERVAL,
        clock: Callable[[], datetime] | None = None,
        fast_lane: Any = None,
        evidence_task: Callable[[], Any] | None = None,
        health_snapshot: Callable[[], dict] | None = None,
        health_sink: Callable[[dict], Any] | None = None,
    ) -> None:
        for name, value in (
            ("trigger_interval", trigger_interval),
            ("fast_lane_interval", fast_lane_interval),
            ("evidence_interval", evidence_interval),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self._uow_factory = uow_factory
        self._quote_service = quote_service
        self._engine = engine
        self._is_trading_day = is_trading_day
        self._trigger_interval = trigger_interval
        self._fast_lane_interval = fast_lane_interval
        self._evidence_interval = evidence_interval
        self._clock = clock or (
            lambda: datetime.now(datetime.now().astimezone().tzinfo)
        )
        # R4-P1-003：Intraday Fast Lane（Overlay/Scanner/Active Pool/Deep）
        # 与计划价格触发同一常驻循环；None 时退回纯 plan-trigger 模式。
        self._fast_lane = fast_lane
        self.last_fast_lane_summary: dict | None = None
        # R5-P1-007/§65：Intraday Evidence 任务槽（R5-08 接线本体）。
        self._evidence_task = evidence_task
        # R4-P2-008：循环失败绝不静默——heartbeat 记录成功/错误时间线，
        # 让"Worker 连续失败 2 小时但 Dashboard 看着在线"不可能再发生。
        # R5-P1-007/§65：per-lane 连续错误 + Fast Lane 全市场覆盖/池计数
        # 全部入 heartbeat；consecutive_errors = 各 lane 最大值。
        self.heartbeat: dict[str, Any] = {
            "last_success_at": None,
            "last_error_at": None,
            "last_error_type": None,
            "consecutive_errors": 0,
            "trigger_consecutive_errors": 0,
            "fast_lane_consecutive_errors": 0,
            "evidence_consecutive_errors": 0,
            "last_plan_count": None,
            "last_evaluated_count": None,
            "last_quote_failed": None,
            "last_engine_failed": None,
            "last_fast_lane_status": None,
            "last_fast_lane_error": None,
            "quote_expected": None,
            "quote_actual": None,
            "quote_coverage": None,
            "active_pool_size": None,
            "candidate_count": None,
            "deep_count": None,
            "provider_health": None,
        }
        self._health_snapshot = health_snapshot
        self._health_sink = health_sink
        self._last_health_persist: datetime | None = None

    # ---------- heartbeat 记账 ----------

    def _record_success(self, summary: dict) -> None:
        self.heartbeat.update({
            "last_success_at": self._clock().isoformat(),
            "last_plan_count": summary.get("plan_count"),
            "last_evaluated_count": summary.get("evaluated"),
            "last_quote_failed": summary.get("quote_failed"),
            "last_engine_failed": summary.get("engine_failed"),
            "trigger_consecutive_errors": 0,
        })
        if self._health_snapshot is not None:
            try:
                self.heartbeat["provider_health"] = self._health_snapshot()
            except Exception:  # noqa: BLE001 - 健康快照失败不阻断主循环
                pass
        self._sync_overall_errors()

    def _record_error(self, exc: BaseException, *, lane: str = "trigger") -> None:
        self.heartbeat.update({
            "last_error_at": self._clock().isoformat(),
            "last_error_type": type(exc).__name__,
            f"{lane}_consecutive_errors":
                self.heartbeat[f"{lane}_consecutive_errors"] + 1,
        })
        self._sync_overall_errors()

    def _sync_overall_errors(self) -> None:
        """§65：overall = 各 lane 连续错误最大值——单一 lane 连续失败
        不被另一 lane 的成功掩盖（连续 3 次 Fast Lane 失败必须可见）。"""
        self.heartbeat["consecutive_errors"] = max(
            self.heartbeat["trigger_consecutive_errors"],
            self.heartbeat["fast_lane_consecutive_errors"],
            self.heartbeat["evidence_consecutive_errors"],
        )

    def _record_fast_lane(self, summary: dict) -> None:
        """Fast Lane 一轮结果入 heartbeat（§65 必填字段全覆盖）。"""
        status = summary.get("status")
        self.heartbeat.update({
            "last_fast_lane_status": status,
            "last_fast_lane_error": summary.get("quote_error"),
            "quote_expected": summary.get("quote_expected"),
            "quote_actual": summary.get("quote_actual"),
            "quote_coverage": summary.get("quote_coverage"),
            "active_pool_size": summary.get("pool_size"),
            "candidate_count": summary.get("candidate_count"),
            "deep_count": len(summary.get("deep") or ()),
        })
        if status in _FASTLANE_FAILED_STATUSES:
            self.heartbeat["fast_lane_consecutive_errors"] += 1
            self.heartbeat["last_error_at"] = self._clock().isoformat()
            self.heartbeat["last_error_type"] = f"FAST_LANE_{status}"
        else:
            self.heartbeat["fast_lane_consecutive_errors"] = 0
        self._sync_overall_errors()

    def _record_evidence(self, summary: dict) -> None:
        self.heartbeat["last_evidence_status"] = summary.get("status")
        self.heartbeat["evidence_consecutive_errors"] = (
            0 if summary.get("status") == "AVAILABLE" else
            self.heartbeat["evidence_consecutive_errors"] + 1
        )
        self._sync_overall_errors()

    # ---------- heartbeat 持久化（跨进程可见，R5-P1-007/§65） ----------

    async def _persist_health(self, *, force: bool = False) -> None:
        """节流写心跳：同状态最多每 30s 一条；错误轮强制落库。
        落库失败绝不阻断主循环（心跳丢失好过 Worker 停摆）。"""
        if self._health_sink is None:
            return
        now = self._clock()
        if (
            not force and self._last_health_persist is not None
            and (now - self._last_health_persist).total_seconds()
            < HEARTBEAT_PERSIST_SECONDS
        ):
            return
        self._last_health_persist = now
        try:
            await self._health_sink(dict(self.heartbeat))
            self.heartbeat.pop("last_health_persist_error", None)
        except Exception as exc:  # noqa: BLE001 - 心跳落库失败不阻断主循环
            self.heartbeat["last_health_persist_error"] = (
                f"{type(exc).__name__}: {exc}"
            )

    # ---------- 单轮任务 ----------

    async def run_fast_lane_once(self) -> dict:
        """单轮 Fast Lane：全市场扫描 → 池 → Deep → Attention（§28 链）。"""
        if self._fast_lane is None:
            self.heartbeat["last_fast_lane_status"] = "NOT_WIRED"
            return {"status": "NOT_WIRED", "detail": "fast lane is not configured"}
        try:
            summary = await self._fast_lane.execute(as_of=self._clock())
        except Exception as exc:
            self.heartbeat["last_fast_lane_status"] = "ERROR"
            self.heartbeat["last_fast_lane_error"] = (
                f"{type(exc).__name__}: {exc}"
            )
            self._record_error(exc, lane="fast_lane")
            raise
        self.last_fast_lane_summary = summary
        self._record_fast_lane(summary)
        return summary

    async def run_evidence_once(self) -> dict:
        """R5-P1-007：Intraday Evidence 任务槽（R5-P1-008 接线本体）。"""
        if self._evidence_task is None:
            return {"status": "NOT_WIRED"}
        try:
            summary = await self._evidence_task()
        except Exception as exc:  # noqa: BLE001 - 单 lane 失败不终止循环
            self._record_error(exc, lane="evidence")
            return {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
        self._record_evidence(summary if isinstance(summary, dict) else {})
        return summary

    async def evaluate_once(self) -> dict:
        """单轮评估：读最新带 stop/target 的 plan，逐计划拉实时行情触发。"""
        as_of = self._clock()
        async with self._uow_factory() as uow:
            plans = await uow.ai_imports.active_price_trigger_plans()
        summary = {
            "plan_count": len(plans),
            "evaluated": 0,
            "quote_failed": 0,
            "engine_failed": 0,
            "created": 0,
            "skipped": 0,
            "as_of": as_of.isoformat(),
        }
        for item in plans:
            try:
                quote = await self._quote_service.get_quote_snapshot(
                    item["code"], as_of=as_of,
                )
            except Exception:  # noqa: BLE001 - 单计划行情失败不阻断其余
                summary["quote_failed"] += 1
                continue
            plan_levels = {
                "stop_loss": item["stop_loss"],
                "take_profit": item["take_profit"],
            }
            try:
                evaluation = await self._engine.evaluate_entry_plan_levels(
                    entry_plan_id=item["entry_plan_id"],
                    security_id=item["security_id"],
                    code=item["code"],
                    market=item["market"],
                    plan=plan_levels,
                    quote=quote,
                    as_of=as_of,
                )
            except Exception:  # noqa: BLE001 - 单计划引擎失败不阻断其余
                summary["engine_failed"] += 1
                continue
            summary["evaluated"] += 1
            summary["created"] += len(getattr(evaluation, "created", ()) or ())
            summary["skipped"] += getattr(evaluation, "skipped", 0) or 0
            # R5-P1-010/§33：后台常驻同样评估 typed Entry Trigger/Cancel——
            # WAIT_ENTRY 计划的价格/结构 Trigger 满足时自动产生
            # ENTRY_TRIGGER_MET（"等买点，到了系统提醒"），不再只靠
            # on-demand 上下文按需评估。
            plan_payload = item.get("plan")
            if isinstance(plan_payload, dict) and (
                plan_payload.get("triggers") or plan_payload.get("cancels")
            ):
                try:
                    trigger_eval = (
                        await self._engine.evaluate_entry_trigger_cancel(
                            entry_plan_id=item["entry_plan_id"],
                            security_id=item["security_id"],
                            code=item["code"],
                            market=item["market"],
                            plan=plan_payload,
                            quote=quote,
                            as_of=as_of,
                        )
                    )
                except Exception:  # noqa: BLE001 - 单计划引擎失败不阻断其余
                    summary["engine_failed"] += 1
                    continue
                summary["trigger_cancel_evaluated"] = (
                    summary.get("trigger_cancel_evaluated", 0) + 1
                )
                summary["created"] += len(
                    getattr(trigger_eval, "created", ()) or ()
                )
                summary["skipped"] += (
                    getattr(trigger_eval, "skipped", 0) or 0
                )
        return summary

    # ---------- 常驻循环 ----------

    async def run_forever(self) -> None:
        """常驻循环：非交易时段低频空转，时段内各任务按独立 due-time 跑。

        R4-P2-008：单轮失败不再静默吞掉——成功/失败都进 heartbeat，
        循环本身继续跑，绝不终止。
        R5-P1-007/§65：Trigger / Fast Lane / Evidence 各自独立 cadence，
        禁止单一 300s 控制所有任务；heartbeat 节流持久化（错误强制）。
        """
        due_trigger = 0.0
        due_fast_lane = 0.0
        due_evidence = 0.0
        while True:
            now_ts = self._clock().timestamp()
            local_now = self._clock().astimezone(SHANGHAI)
            try:
                trading_day = bool(self._is_trading_day(local_now.date()))
            except Exception:  # noqa: BLE001 - 日历失败按非交易时段空转
                trading_day = False
            if not trading_day or not in_trading_session(local_now.time()):
                # 空转且重置 due——下一时段开盘各 lane 立即跑一轮
                due_trigger = due_fast_lane = due_evidence = 0.0
                self._last_health_persist = None
                await asyncio.sleep(IDLE_POLL_SECONDS)
                continue
            force_persist = False
            if now_ts >= due_trigger:
                try:
                    summary = await self.evaluate_once()
                except Exception as exc:  # noqa: BLE001 - 单轮失败不终止循环
                    self._record_error(exc)
                    force_persist = True
                else:
                    self._record_success(summary)
                due_trigger = now_ts + self._trigger_interval
            if now_ts >= due_fast_lane:
                try:
                    await self.run_fast_lane_once()
                except Exception:  # noqa: BLE001 - Fast Lane 失败不阻断触发
                    force_persist = True
                due_fast_lane = now_ts + self._fast_lane_interval
            if now_ts >= due_evidence:
                await self.run_evidence_once()
                due_evidence = now_ts + self._evidence_interval
            if self.heartbeat["consecutive_errors"] > 0:
                force_persist = True
            await self._persist_health(force=force_persist)
            await asyncio.sleep(IDLE_POLL_SECONDS)


def build_health_sink(
    uow_factory: Callable[[], Any],
    *,
    component: str = "intraday-worker",
    capability: str = "intraday-trigger-loop",
    environment: str = "production",
    clock: Callable[[], datetime] | None = None,
) -> Callable[[dict], Any]:
    """R5-P1-007/§65：heartbeat → operational_health_events。

    Worker 内存里的 heartbeat 不跨进程；本 sink 把每轮心跳映射为
    OperationalHealthEventCreate 落库，API/Dashboard 进程经
    /operations/worker-heartbeat 读取。永不抛错（心跳失败不阻断循环）。
    """
    from app.v3.domain.strategy import OperationalHealthEventCreate

    now = clock or (lambda: datetime.now(timezone.utc))

    def _status(heartbeat: dict) -> str:
        # 不用 FAILED：策略仓库对 FAILED 有自动回滚旁路，worker 心跳
        # 只表达"降级/健康"，避免误触 release 回滚。
        if heartbeat.get("consecutive_errors", 0) >= DEGRADED_ERROR_THRESHOLD:
            return "DEGRADED"
        if heartbeat.get("consecutive_errors", 0) > 0:
            return "DEGRADED"
        return "HEALTHY"

    async def sink(heartbeat: dict) -> None:
        event = OperationalHealthEventCreate(
            environment=environment,
            component=component,
            capability=capability,
            status=_status(heartbeat),
            error_type=heartbeat.get("last_error_type"),
            circuit_state="CLOSED",
            observed_at=now(),
            metadata=dict(heartbeat),
        )
        async with uow_factory() as uow:
            await uow.strategies.add_health_event(event)
            await uow.commit()

    return sink
