"""Step 20：真实全市场扫描编排（设计 §3 主链路装配；P1-01 两阶段）。

数据源（与旧管线共用，不新建事实）：
- uow.universes.latest()：全市场 SecurityMember（ST/停牌/新股标记）
- uow.features.latest_run() + features_for_run：全市场特征行
  （close、bar_count、26 键特征含趋势状态）
- uow.scans.security_keys()：security_id → code 映射

流程（P1-01）：
assemble → run_to_machine（Safety→Recall→Pareto→Machine Top120）
→ 对 Machine selected 限并发抓 60m（Semaphore，只抓 Top120）
→ uow.scans.regime_snapshot(feature_run_id) PIT 取市场分
→ complete_deep（Deep→Final）→ scans.save_scan。

60m 为抓取时点事实：stale/UNTRUSTED 不当事实（§19.6）；provider
失败逐股降级 missing，不阻断扫描。mature 回填由 run_mature_backfill
在观察期后执行。调用方负责 UoW 提交（编排器内不 commit）。
R2.1-P1-02：同上海交易日同版本幂等（already_scanned，§10.5），
操作员强制重扫 force=True。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.v3.application.candidate_pipeline import CandidatePipeline
from app.v3.candidate_engine.market_regime_score import MarketRegimeScoreService
from app.v3.domain.candidate_engine import (
    DeepContext,
    ExpertFeatureView,
    ExpertInput,
    Minute60Fact,
    SafetyCandidateInput,
)
from app.v3.domain.market_data import SecurityMember

__all__ = ["UniverseScanOrchestrator"]

# P1-01 §19.4：Machine Top120 分钟K 抓取默认并发 4（run_once 资源治理
# 降峰，2026-09 SSH 失联整改——12 过高会与日K backfill 叠加打满 2G 宿主机），
# 环境变量 V3_SCAN_MINUTE60_CONCURRENCY 可覆盖
_DEFAULT_MINUTE60_CONCURRENCY = 4

# RecallFeatureView 顶层宽表字段（专家输入特征字典的补充来源）
_WIDE_COLUMNS = (
    "return_3d", "return_5d", "return_10d", "return_20d",
    "return_60d", "return_120d", "return_250d",
    "position_60d", "position_120d", "position_250d",
    "ma20_slope", "ma60_slope", "atr14", "atr_pct", "volatility20",
    "distance_60d_high", "distance_60d_low",
    "breakout_20d", "pullback_20d",
    "amount", "volume_ratio_5d", "volume_expansion",
    "relative_index_strength", "relative_industry_strength",
)


def _default_as_of() -> datetime:
    return datetime.now(timezone.utc)


class UniverseScanOrchestrator:
    def __init__(
        self,
        pipeline: CandidatePipeline | None = None,
        *,
        deep_service=None,
        minute60_concurrency: int | None = None,
    ) -> None:
        self._pipeline = pipeline or CandidatePipeline()
        self._deep_service = deep_service
        self._minute60_semaphore = asyncio.Semaphore(
            minute60_concurrency
            or int(os.getenv("V3_SCAN_MINUTE60_CONCURRENCY", _DEFAULT_MINUTE60_CONCURRENCY))
        )
        self._regime_score_service = MarketRegimeScoreService()

    async def execute(
        self,
        uow,
        *,
        as_of: datetime | None = None,
        feature_run_id=None,
        strategy_version: str = "v3",
        parameter_version: str = "v1",
        force: bool = False,
    ) -> dict:
        """feature_run_id 显式指定时跳过 latest_run（操作员决策通道，
        例如最新 run 头数据异常但特征行有效）。

        R2.1-P1-02（任务书 §10.5）幂等：同上海交易日同
        strategy/parameter 版本已有 PUBLISHED scan → 返回
        already_scanned（携带既有 scan_run_id），不静默创建重复
        Published Scan；操作员强制重扫走 force=True。
        """
        as_of = as_of or _default_as_of()

        if not force:
            existing = await uow.scans.published_run_on(
                as_of,
                strategy_version=strategy_version,
                parameter_version=parameter_version,
            )
            if existing is not None:
                return {
                    "status": "already_scanned",
                    "scan_run_id": str(existing.scan_run_id),
                    "strategy_version": strategy_version,
                    "parameter_version": parameter_version,
                }

        snapshot = await uow.universes.latest()
        if snapshot is None:
            return {"status": "no_universe"}
        if feature_run_id is not None:
            run_id = feature_run_id
        else:
            feature_run = await uow.features.latest_run()
            if feature_run is None:
                return {"status": "no_feature_run"}
            run_id = feature_run.feature_run_id
        views = await uow.features.features_for_run(run_id)
        key_map = await uow.scans.security_keys()

        candidates, stocks, skipped = self._assemble(
            snapshot.members, views, key_map, as_of,
        )
        if stocks:
            self._attach_evidence(stocks, await uow.evidence.for_securities(
                tuple(stocks.keys()), as_of=as_of,
            ))

        # P1-01 Phase A：Safety→Recall→Pareto→Machine Top120
        state = self._pipeline.run_to_machine(
            candidates, stocks, trade_date=as_of,
            strategy_version=strategy_version,
            parameter_version=parameter_version,
        )

        # Phase B 数据注入：60m（Top120 限并发）+ Regime（feature_run_id PIT）
        deep_context = await self._build_deep_context(
            uow, state, run_id, as_of,
        )
        result = self._pipeline.complete_deep(state, deep_context=deep_context)
        scan_run_id = await uow.scans.save_scan(result)

        funnel = {stage.stage: stage.output_count for stage in result.funnel.stages}
        return {
            "status": "ok",
            "scan_run_id": str(scan_run_id),
            "feature_run_id": str(run_id),
            "universe_snapshot_id": str(snapshot.snapshot_id),
            "members": len(snapshot.members),
            "feature_rows": len(views),
            "skipped_no_security": skipped,
            "candidates": len(candidates),
            "funnel": funnel,
            "final_count": len(result.final_entries),
            # P1-01/02 观测：60m 抓取覆盖与 regime 来源；
            # R2.1-P0-01：Gate 细分观测（§3.7/§15）
            "minute60_fetched": len(deep_context.minute_60_by_id),
            "minute60_usable": sum(
                1 for fact in deep_context.minute_60_by_id.values()
                if not fact.stale and fact.quality != "UNTRUSTED"
                and fact.state != "UNKNOWN"
            ),
            "m60_available": deep_context.m60_stats.get("available", 0),
            "m60_missing": deep_context.m60_stats.get("missing", 0),
            "m60_stale": deep_context.m60_stats.get("stale", 0),
            "m60_error": deep_context.m60_stats.get("error", 0),
            "m60_reasons": deep_context.m60_stats.get("reasons", {}),
            "market_regime_score": deep_context.market_regime_score,
            "market_regime_source": deep_context.market_regime_source,
        }

    async def _build_deep_context(
        self, uow, state, run_id, as_of: datetime,
    ) -> DeepContext:
        """Machine 之后才取数：60m 只抓 Machine selected（§19.2），
        Regime 按 feature_run_id PIT 读快照（§20.2）。"""
        selected_ids = [
            entry.security_id for entry in state.machine.entries if entry.selected
        ]
        key_map = state.stocks_by_id
        minute_60, m60_stats = await self._fetch_minute_60(selected_ids, key_map, as_of)

        regime_score = None
        regime_source = None
        regime = await uow.scans.regime_snapshot(run_id)
        if regime is not None and not regime.get("stale"):
            regime_score, detail = self._regime_score_service.compute(regime)
            regime_source = regime.get("regime_snapshot_id")

        return DeepContext(
            minute_60_by_id=minute_60,
            market_regime_score=regime_score,
            market_regime_source=regime_source,
            m60_stats=m60_stats,
        )

    @staticmethod
    def _m60_field(fact: Any, name: str):
        """R2.1-P0-01：structure 可能是 dict 或对象，按类型安全读取。"""
        if isinstance(fact, dict):
            return fact.get(name)
        return getattr(fact, name, None)

    async def _fetch_minute_60(
        self, security_ids: list, stocks_by_id: dict, as_of: datetime,
    ) -> tuple[dict, dict]:
        """§19.4：Semaphore 限并发；单股失败/无服务 → 降级 missing。

        R2.1-P0-01：60m 事实必须从 period["structure"] 读——此前错误读
        period 顶层（trend/support/resistance 恒 None），真实 60m 抓到
        也按 missing 运行。Gate（全过才注入 minute_60 事实）：

        - status == AVAILABLE
        - stale 非 truthy
        - quality 不属于 UNTRUSTED/UNAVAILABLE
        - structure.trend 有值且非 UNKNOWN

        拒收记 reason：M60_NOT_AVAILABLE/M60_STALE/M60_UNTRUSTED/
        M60_STRUCTURE_MISSING；单股异常记 error。不得默认给 60 分，
        不得把旧 K 线当当前事实。
        """
        deep_service = self._deep_service
        if deep_service is None:
            return {}, {}
        facts: dict = {}
        stats: dict = {"available": 0, "missing": 0, "stale": 0, "error": 0}
        reasons: dict[str, int] = {}

        def _reject(reason: str, bucket: str) -> None:
            stats[bucket] += 1
            if bucket != "error":
                reasons[reason] = reasons.get(reason, 0) + 1

        async def _one(security_id) -> None:
            stock = stocks_by_id.get(security_id)
            if stock is None:
                return
            code = stock.feature.code
            try:
                async with self._minute60_semaphore:
                    structure = await deep_service.get_intraday_structure(
                        code, as_of=as_of,
                    )
            except Exception:  # noqa: BLE001——单股失败不阻断扫描
                _reject("M60_ERROR", "error")
                return
            period = structure.periods.get("60m") or {}
            if period.get("status") != "AVAILABLE":
                _reject("M60_NOT_AVAILABLE", "missing")
                return
            if period.get("stale"):
                _reject("M60_STALE", "stale")
                return
            if period.get("quality") in ("UNTRUSTED", "UNAVAILABLE"):
                _reject("M60_UNTRUSTED", "missing")
                return
            structure_fact = period.get("structure") or {}
            trend = self._m60_field(structure_fact, "trend")
            if not trend or trend == "UNKNOWN":
                _reject("M60_STRUCTURE_MISSING", "missing")
                return
            facts[security_id] = Minute60Fact(
                state=trend,
                support=self._m60_field(structure_fact, "support"),
                resistance=self._m60_field(structure_fact, "resistance"),
                bar_count=int(period.get("bar_count") or 0),
                stale=bool(period.get("stale")),
                quality=period.get("quality"),
                known_at=period.get("known_at") or structure.known_at,
            )
            stats["available"] += 1

        await asyncio.gather(*(_one(security_id) for security_id in security_ids))
        stats["reasons"] = reasons
        return facts, stats

    def _assemble(
        self,
        members: tuple[SecurityMember, ...],
        views: tuple,
        key_map: dict,
        as_of: datetime,
    ) -> tuple[tuple[SafetyCandidateInput, ...], dict, int]:
        by_code: dict[str, SecurityMember] = {}
        for member in members:
            by_code[member.code] = member

        candidates: list[SafetyCandidateInput] = []
        stocks: dict = {}
        skipped = 0
        for view in views:
            code = key_map.get(view.security_id)
            member = by_code.get(code)
            if member is None:
                skipped += 1
                continue
            # 宽表主字段 + features JSONB（候选引擎 extras）合成专家输入；
            # JSONB 优先（extras 是同名主字段的更新语义不存在，setdefault 防覆盖）
            features = {k: v for k, v in view.features.items()}
            for key in _WIDE_COLUMNS:
                if features.get(key) is None:
                    features[key] = getattr(view, key, None)
            candidates.append(SafetyCandidateInput(
                security_id=view.security_id,
                member=member,
                close=view.close,
                bar_count=features.get("bar_count"),
            ))
            stocks[view.security_id] = ExpertInput(
                feature=ExpertFeatureView(
                    security_id=view.security_id,
                    code=code,
                    close=view.close,
                    as_of=as_of,
                    features=features,
                ),
            )
        return tuple(candidates), stocks, skipped

    @staticmethod
    def _attach_evidence(
        stocks: dict,
        evidence_rows: tuple,
    ) -> None:
        """把 PIT 证据按 security_id 注入 ExpertInput（FQ/CAT 数据链）。

        uow.evidence.for_securities 已做 known_at<=as_of / AVAILABLE /
        expire_at>=as_of 过滤且单次批量查询，这里只做分组绑定，
        不新建第二套 evidence 查询。"""
        evidence_by_security: dict[UUID, list] = {}
        for row in evidence_rows:
            evidence_by_security.setdefault(row.security_id, []).append(row)
        for security_id, rows in evidence_by_security.items():
            expert_input = stocks.get(security_id)
            if expert_input is None:
                continue
            stocks[security_id] = expert_input.model_copy(
                update={"evidence": tuple(rows)}
            )


async def run_full_scan() -> dict:
    """带 UoW 的便捷入口：扫描 + 当日 PENDING 回填一次完成。

    P1-01：与 fast_lane 同源 Provider 上挂 DeepMarketDataService，
    Machine Top120 60m 抓取真实生效；离线/测试构造 orchestrator 时
    不传 deep_service → 60m missing 降权。"""
    from app.container import container
    from app.v3.application.deep_market_data import DeepMarketDataService

    if not container.v3.enabled:
        return {"status": "v3_disabled"}
    deep_service = DeepMarketDataService(
        container.provider_manager, source="legacy-provider",
    )
    async with container.v3.uow() as uow:
        summary = await UniverseScanOrchestrator(deep_service=deep_service).execute(uow)
        if summary.get("status") == "ok":
            from app.v3.application.mature_scan_outcomes import (
                MatureScanOutcomesService,
            )

            backfill = await MatureScanOutcomesService().execute(
                uow.scans,
                scan_id=UUID(summary["scan_run_id"]),
            )
            summary["backfill"] = backfill
            await uow.commit()
    return summary
