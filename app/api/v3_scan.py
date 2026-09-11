"""V3 候选扫描 API（设计 §36，Step 15）。

9 端点：scan latest/funnel/experts/pareto/top、stock scan-trace、
backtest metrics/misses、shadow metrics。全部只读 GET；
新公开端点同步登记 app/v3/security.py 白名单。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from app.api.v3 import _uow
from app.v3.application.backtest_metrics import BacktestMetricsService
from app.v3.domain.candidate_engine import (
    GOOD_LABELS,
    MATURED_LABELS,
    OutcomeLabelResult,
    TRACE_STAGES,
    status_from_label,
)
from app.v3.presentation.zh_cn_labels import (
    DROP_REASON_LABELS,
    STAGE_LABELS,
    SUPPORT_NOTE_LABEL,
)

router = APIRouter(prefix="/api/v3", tags=["V3 Scan"])


async def _resolve_run(scan_id: UUID | None, uow):
    run = (
        await uow.scans.run_by_id(scan_id)
        if scan_id is not None
        else await uow.scans.latest_run()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="no scan run found")
    return run


def _run_payload(run) -> dict:
    return {
        "scan_id": str(run.scan_run_id),
        "scan_time": run.scan_time.isoformat(),
        "market_date": run.market_date.isoformat(),
        "strategy_version": run.strategy_version,
        "parameter_version": run.parameter_version,
        "universe_count": run.universe_count,
        "eligible_count": run.eligible_count,
        "recall_count": run.recall_count,
        "pareto_count": run.pareto_count,
        "machine_count": run.machine_count,
        "deep_count": run.deep_count,
        "final_count": run.final_count,
        "duration_ms": run.duration_ms,
        "status": run.status,
    }


@router.get("/scan/latest")
async def scan_latest() -> dict:
    async with _uow() as uow:
        run = await _resolve_run(None, uow)
        return _run_payload(run)


@router.get("/scan/{scan_id}/funnel")
async def scan_funnel(scan_id: UUID) -> dict:
    async with _uow() as uow:
        run = await _resolve_run(scan_id, uow)
        return {
            "scan_id": str(run.scan_run_id),
            "market_date": run.market_date.isoformat(),
            "funnel": [
                {"stage": "UNIVERSE", "count": run.universe_count},
                {"stage": "SAFETY", "count": run.eligible_count},
                {"stage": "RECALL", "count": run.recall_count},
                {"stage": "PARETO", "count": run.pareto_count},
                {"stage": "MACHINE", "count": run.machine_count},
                {"stage": "DEEP", "count": run.deep_count},
                {"stage": "FINAL", "count": run.final_count},
            ],
        }


@router.get("/scan/{scan_id}/experts")
async def scan_experts(scan_id: UUID, expert: str | None = Query(default=None)) -> dict:
    async with _uow() as uow:
        run = await _resolve_run(scan_id, uow)
        rows = await uow.scans.expert_rows(run.scan_run_id, expert=expert)
        by_expert: dict[str, int] = {}
        items = [
            {
                "code": row.code,
                "expert": row.expert,
                "score": float(row.score),
                "rank": row.rank,
                "reasons": row.reason_json.get("reasons", []),
                "confidence": row.reason_json.get("confidence"),
            }
            for row in rows
        ]
        for item in items:
            by_expert[item["expert"]] = by_expert.get(item["expert"], 0) + 1
        return {
            "scan_id": str(run.scan_run_id),
            "expert_counts": by_expert,
            "rows": items,
        }


@router.get("/scan/{scan_id}/pareto")
async def scan_pareto(scan_id: UUID, front: int | None = Query(default=None)) -> dict:
    async with _uow() as uow:
        run = await _resolve_run(scan_id, uow)
        rows = await uow.scans.pareto_rows(run.scan_run_id)
        if front is not None:
            rows = [row for row in rows if row.front == front]
        return {
            "scan_id": str(run.scan_run_id),
            "rows": [
                {
                    "code": row.code,
                    "front": row.front,
                    "crowding_distance": float(row.crowding_distance),
                    "position": _opt(row.p_position),
                    "transition": _opt(row.p_transition),
                    "accumulation": _opt(row.p_accumulation),
                    "quality_catalyst": _opt(row.p_quality_catalyst),
                    "risk_reward": _opt(row.p_risk_reward),
                }
                for row in rows
            ],
        }


@router.get("/scan/{scan_id}/top")
async def scan_top(
    scan_id: UUID,
    stage: str = Query(default="FINAL"),
    limit: int = Query(default=30, ge=1, le=500),
) -> dict:
    if stage not in TRACE_STAGES:
        raise HTTPException(status_code=422, detail=f"stage must be one of {TRACE_STAGES}")
    async with _uow() as uow:
        run = await _resolve_run(scan_id, uow)
        rows = await uow.scans.snapshots(
            run.scan_run_id, stage=stage, alive_only=True, limit=limit,
        )
        # 设计 §83：附 market/name/stage_label 展示字段（§85 raw 保留英文）；
        # 名称批量一次 IN 查询，禁止逐行 N+1。
        ids = [row.security_id for row in rows if row.security_id is not None]
        names = await uow.scans.security_names(ids) if ids else {}
        return {
            "scan_id": str(run.scan_run_id),
            "stage": stage,
            "stage_label": STAGE_LABELS.get(stage, stage),
            "rows": [
                {
                    "code": row.code,
                    "score": _opt(row.score),
                    "rank": row.rank,
                    "security_id": str(row.security_id) if row.security_id else None,
                    "name": (names.get(row.security_id) or {}).get("name"),
                    "market": (names.get(row.security_id) or {}).get("market"),
                    "stage_label": STAGE_LABELS.get(stage, stage),
                }
                for row in rows
            ],
        }


@router.get("/stock/{code}/scan-trace")
async def stock_scan_trace(
    code: str,
    scan_id: UUID | None = Query(default=None),
) -> dict:
    async with _uow() as uow:
        run = await _resolve_run(scan_id, uow)
        rows = await uow.scans.snapshots(run.scan_run_id, code=code, limit=64)
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"code {code} not in scan {run.scan_run_id}",
            )
        # 设计 §82：轨迹附 stage_label/drop_reason_label 展示字段，
        # 原始 stage/drop_reason 保留英文（§85 DB/API 不写中文）。
        trajectory = [
            {
                "stage": row.stage,
                "alive": row.alive,
                "score": _opt(row.score),
                "rank": row.rank,
                "drop_reason": row.drop_reason,
                "stage_label": STAGE_LABELS.get(row.stage, row.stage),
                "drop_reason_label": (
                    SUPPORT_NOTE_LABEL
                    if str(row.drop_reason) == "support_not_broken"
                    else DROP_REASON_LABELS.get(row.drop_reason, row.drop_reason)
                ) if row.drop_reason else None,
            }
            for row in rows
        ]
        return {
            "scan_id": str(run.scan_run_id),
            "code": code,
            "final_alive": any(
                str(row["stage"]) == "FINAL" and row["alive"] for row in trajectory
            ),
            "trajectory": trajectory,
        }


@router.get("/backtest/metrics")
async def backtest_metrics(scan_id: UUID | None = Query(default=None)) -> dict:
    """Recall@K / Precision@K / NDCG@K（§27-§29；任务书 §14 Pool 语义）。"""
    async with _uow() as uow:
        run = await _resolve_run(scan_id, uow)

        async def _pool(stage: str, *, alive_only: bool = True) -> list[tuple[str, int]]:
            rows = await uow.scans.snapshots(
                run.scan_run_id, stage=stage, alive_only=alive_only, limit=1_000_000,
            )
            return [(row.code, row.rank) for row in rows if row.rank is not None]

        # P0-12：Machine 全量 ranking（不限 alive），Top300/Top120 才真正可分
        machine_ranking = await _pool("MACHINE", alive_only=False)
        deep_ranking = await _pool("DEEP")
        final_ranking = await _pool("FINAL")
        eligible_rows = await uow.scans.snapshots(
            run.scan_run_id, stage="SAFETY", alive_only=True, limit=1_000_000,
        )
        pools = {
            "recall_pool": await _pool("RECALL"),
            "pareto_pool": await _pool("PARETO"),
        }
        label_rows = await uow.scans.outcome_labels(run.scan_run_id)
        # R2.1-P0-03：DB 行 → status（label NULL=PENDING；A/B/C/NONE=MATURED）
        labels = [
            OutcomeLabelResult(
                code=row.code, label=row.label,
                status=status_from_label(row.label),
            )
            for row in label_rows
        ]
        result = BacktestMetricsService().compute(
            labels, pools,
            rankings={
                "machine": machine_ranking,
                "deep": deep_ranking,
                "final": final_ranking,
            },
            scan_id=run.scan_run_id,
            eligible_codes={row.code for row in eligible_rows},
        )
        # R2.1-P0-04：顶层三态由 matured_count 驱动（禁"label rows 存在=OK"）
        return {
            "scan_id": str(run.scan_run_id),
            "good_count": result.good_count,
            "labeled_count": result.labeled_count,
            "status": result.status,
            "matured_count": result.matured_count,
            "pending_count": result.pending_count,
            "metrics": [
                {
                    "metric": entry.metric,
                    "pool": entry.pool,
                    "ranking_source": entry.ranking_source,
                    "k": entry.k,
                    "value": entry.value,
                    "numerator": entry.numerator,
                    "denominator": entry.denominator,
                    "status": entry.status,
                    "reason": entry.reason,
                }
                for entry in result.entries
            ],
            "note": None if result.status == "OK" else "outcome labels pending maturity window",
        }


@router.get("/backtest/misses")
async def backtest_misses(
    scan_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    """False Negative / 漏选审计（§30）。"""
    async with _uow() as uow:
        run = await _resolve_run(scan_id, uow)
        rows = await uow.scans.miss_rows(run.scan_run_id, limit=limit)
        return {
            "scan_id": str(run.scan_run_id),
            "misses": [
                {
                    "code": row.code,
                    "future_label": row.future_label,
                    "last_alive_stage": row.last_alive_stage,
                    "drop_stage": row.drop_stage,
                    "drop_reason": row.drop_reason,
                    "audit": row.audit_json,
                }
                for row in rows
            ],
        }


@router.get("/shadow/metrics")
async def shadow_metrics(scan_id: UUID | None = Query(default=None)) -> dict:
    """影子池对照（§31.2/§32）：各组 GOOD rate + 淘汰原因分布，对照 Final 组。

    R2.1-P0-05：分母只取 matured_count；未成熟不得显示假 0%——
    matured=0 → status=PENDING / good_rate=null。
    """
    async with _uow() as uow:
        run = await _resolve_run(scan_id, uow)
        rows = await uow.scans.shadow_rows(run.scan_run_id)
        labels = await uow.scans.labels_by_code(run.scan_run_id)
        final_rows = await uow.scans.snapshots(
            run.scan_run_id, stage="FINAL", alive_only=True, limit=100,
        )
        final_labels = [labels.get(row.code) for row in final_rows]
        final_matured = [
            label for label in final_labels if label in MATURED_LABELS
        ]
        final_good = sum(1 for label in final_matured if label in GOOD_LABELS)

        def _summary(count: int, good: int, matured: int) -> dict:
            pending = count - matured
            return {
                "sample_count": count,
                "matured_count": matured,
                "pending_count": pending,
                "good_count": good,
                "status": "PENDING" if matured == 0 else (
                    "PARTIAL" if pending else "OK"
                ),
                "good_rate": None if matured == 0 else good / matured,
            }

        groups: dict[str, dict] = {}
        for row in rows:
            bucket = groups.setdefault(
                row.sample_group,
                {"count": 0, "good": 0, "matured": 0, "drop_reasons": {}},
            )
            bucket["count"] += 1
            label = row.outcome_label or labels.get(row.code)
            if label in MATURED_LABELS:  # A/B/C/NONE=成熟；None=PENDING 不进分母
                bucket["matured"] += 1
                if label in GOOD_LABELS:
                    bucket["good"] += 1
            bucket["drop_reasons"][row.drop_reason] = (
                bucket["drop_reasons"].get(row.drop_reason, 0) + 1
            )
        return {
            "scan_id": str(run.scan_run_id),
            "groups": {
                name: _summary(bucket["count"], bucket["good"], bucket["matured"])
                | {"drop_reasons": bucket["drop_reasons"]}
                for name, bucket in groups.items()
            },
            "final_group": _summary(len(final_labels), final_good, len(final_matured)),
            "note": None if rows else "shadow pool pending mature backfill",
        }


def _opt(value) -> float | None:
    return None if value is None else float(value)
