"""Step 20：真实全市场扫描编排（设计 §3 主链路装配）。

数据源（与旧管线共用，不新建事实）：
- uow.universes.latest()：全市场 SecurityMember（ST/停牌/新股标记）
- uow.features.latest_run() + features_for_run：全市场特征行
  （close、bar_count、26 键特征含趋势状态）
- uow.scans.security_keys()：security_id → code 映射

流程：assemble → CandidatePipeline.execute → scans.save_scan。
mature 回填（outcome/miss/shadow）由 run_mature_backfill 在观察期后执行；
当日扫描尾步可立即调用（全 PENDING 行先落）。

调用方负责 UoW 提交（编排器内不 commit）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from app.v3.application.candidate_pipeline import CandidatePipeline
from app.v3.domain.candidate_engine import (
    ExpertFeatureView,
    ExpertInput,
    SafetyCandidateInput,
)
from app.v3.domain.market_data import SecurityMember

__all__ = ["UniverseScanOrchestrator"]

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
    def __init__(self, pipeline: CandidatePipeline | None = None) -> None:
        self._pipeline = pipeline or CandidatePipeline()

    async def execute(
        self,
        uow,
        *,
        as_of: datetime | None = None,
        feature_run_id=None,
    ) -> dict:
        """feature_run_id 显式指定时跳过 latest_run（操作员决策通道，
        例如最新 run 头数据异常但特征行有效）。"""
        as_of = as_of or _default_as_of()

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
        result = self._pipeline.execute(candidates, stocks, trade_date=as_of)
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
        }

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
    """带 UoW 的便捷入口：扫描 + 当日 PENDING 回填一次完成。"""
    from app.container import container

    if not container.v3.enabled:
        return {"status": "v3_disabled"}
    async with container.v3.uow() as uow:
        summary = await UniverseScanOrchestrator().execute(uow)
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
