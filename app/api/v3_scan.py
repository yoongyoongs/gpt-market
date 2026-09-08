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
from app.v3.domain.candidate_engine import TRACE_STAGES

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
        return {
            "scan_id": str(run.scan_run_id),
            "stage": stage,
            "rows": [
                {
                    "code": row.code,
                    "score": _opt(row.score),
                    "rank": row.rank,
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
        return {
            "scan_id": str(run.scan_run_id),
            "code": code,
            "trajectory": [
                {
                    "stage": row.stage,
                    "alive": row.alive,
                    "score": _opt(row.score),
                    "rank": row.rank,
                    "drop_reason": row.drop_reason,
                }
                for row in rows
            ],
        }


@router.get("/backtest/metrics")
async def backtest_metrics() -> dict:
    """Recall@K / Precision@K / NDCG@K（Step 17 计算落地后返回真实值）。"""
    async with _uow() as uow:
        run = await _resolve_run(None, uow)
        return {
            "scan_id": str(run.scan_run_id),
            "metrics": [],
            "note": "outcome labels pending Step 17 maturity window",
        }


@router.get("/backtest/misses")
async def backtest_misses(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    """False Negative / 漏选审计（Step 17 落地后返回真实值）。"""
    async with _uow() as uow:
        run = await _resolve_run(None, uow)
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
async def shadow_metrics() -> dict:
    """影子池对照指标（Step 18 落地后返回真实值）。"""
    async with _uow() as uow:
        run = await _resolve_run(None, uow)
        groups = await uow.scans.shadow_group_counts(run.scan_run_id)
        return {
            "scan_id": str(run.scan_run_id),
            "groups": groups,
            "note": "outcome labels pending Step 18",
        }


def _opt(value) -> float | None:
    return None if value is None else float(value)
