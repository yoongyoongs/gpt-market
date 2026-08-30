from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from app.container import container
from app.v3.domain.features import FeatureQuery, FeatureSortField
from app.v3.application.read_evidence import ReadEvidenceService
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.evidence import EvidenceReadQuery, EvidenceSourceType


router = APIRouter(prefix="/api/v3", tags=["V3"])


def _uow():
    if not container.v3.enabled:
        raise HTTPException(status_code=503, detail="V3 is not enabled")
    return container.v3.uow()


async def _feature_query(
    feature_run_id: str | None,
    market: str | None,
    stale: bool | None,
    sort_by: FeatureSortField,
    descending: bool,
    min_value: float | None,
    max_value: float | None,
    fields: str | None,
    limit: int,
    cursor: str | None,
):
    request = FeatureQuery(
        feature_run_id=feature_run_id,
        market=market,
        stale=stale,
        sort_by=sort_by,
        descending=descending,
        min_value=min_value,
        max_value=max_value,
        fields=tuple(value.strip() for value in fields.split(",") if value.strip()) if fields else (),
        limit=limit,
        cursor=cursor,
    )
    async with _uow() as uow:
        page = await uow.features.query(request)
    if page is None:
        raise HTTPException(status_code=404, detail="published feature run not found")
    return page


@router.get("/universe/features")
@router.get("/universe/query", include_in_schema=False)
async def universe_features(
    feature_run_id: str | None = None,
    market: str | None = Query(default=None, pattern=r"^(SH|SZ|BJ)$"),
    stale: bool | None = None,
    sort_by: FeatureSortField = FeatureSortField.CODE,
    descending: bool = False,
    min_value: float | None = None,
    max_value: float | None = None,
    fields: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
):
    return await _feature_query(
        feature_run_id, market, stale, sort_by, descending,
        min_value, max_value, fields, limit, cursor,
    )


@router.get("/market-regime")
async def market_regime():
    async with _uow() as uow:
        snapshot = await uow.features.latest_regime()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="published market regime not found")
    return snapshot


def _enum_values(value: str | None, enum_type):
    if not value:
        return ()
    try:
        return tuple(enum_type(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise HTTPException(status_code=422, detail=f"allowed values: {allowed}") from exc


@router.get("/evidence/{subject_type}/{subject_id}")
async def evidence_for_subject(
    subject_type: str,
    subject_id: str,
    as_of: datetime | None = None,
    evidence_types: str | None = None,
    source_types: str | None = None,
    min_effective_relevance: float = Query(default=0, ge=0, le=1),
    include_candidates: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
):
    query = EvidenceReadQuery(
        subject_type=subject_type.upper(),
        subject_id=subject_id,
        as_of=as_of or datetime.now(timezone.utc),
        evidence_types=_enum_values(evidence_types, EvidenceType),
        source_types=_enum_values(source_types, EvidenceSourceType),
        min_effective_relevance=min_effective_relevance,
        include_candidates=include_candidates,
        limit=limit,
    )
    return await ReadEvidenceService(_uow).execute(query)


@router.get("/recalls")
async def recall_results(
    recall_run_id: UUID | None = None,
    channel: str | None = Query(default=None, min_length=1, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
):
    try:
        async with _uow() as uow:
            page = await uow.recalls.read_results(
                recall_run_id=recall_run_id,
                channel_code=channel,
                limit=limit,
                cursor=cursor,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if page is None:
        raise HTTPException(status_code=404, detail="published recall run not found")
    return page


@router.get("/raw-opportunities")
async def raw_opportunities(
    recall_run_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
):
    try:
        async with _uow() as uow:
            page = await uow.recalls.read_raw(
                recall_run_id=recall_run_id,
                limit=limit,
                cursor=cursor,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if page is None:
        raise HTTPException(status_code=404, detail="published recall run not found")
    return page
