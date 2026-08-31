from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from app.container import container
from app.v3.application.build_candidate_comparison import (
    BuildCandidateComparisonService,
    CandidateComparisonQuery,
)
from app.v3.application.build_context_pack import (
    BuildContextPackCommand,
    BuildContextPackService,
)
from app.v3.application.read_evidence import ReadEvidenceService
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.evidence import EvidenceReadQuery, EvidenceSourceType
from app.v3.domain.features import FeatureQuery, FeatureSortField
from app.v3.repositories.errors import (
    RepositoryConflictError,
    RepositoryNotFoundError,
)

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


@router.get("/market-overview")
async def market_overview():
    return await market_regime()


@router.get("/candidates/comparison-pack")
async def candidate_comparison_pack(
    candidate_set_id: UUID | None = None,
    codes: str | None = Query(
        default=None,
        description="20-100 comma-separated CODE or MARKET:CODE candidates",
    ),
    feature_run_id: UUID | None = None,
    recall_run_id: UUID | None = None,
    field_profile_version: str = Query(
        default="compact-fields.v1", min_length=1, max_length=64
    ),
    as_of: datetime | None = None,
):
    try:
        query = CandidateComparisonQuery(
            candidate_set_id=candidate_set_id,
            codes=tuple(item.strip() for item in codes.split(",") if item.strip())
            if codes
            else (),
            feature_run_id=feature_run_id,
            recall_run_id=recall_run_id,
            field_profile_version=field_profile_version,
            as_of=as_of or datetime.now(timezone.utc),
        )
        return await BuildCandidateComparisonService(_uow).execute(query)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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


@router.get("/stocks/{code}/evidence")
async def stock_evidence(
    code: str,
    market: str | None = Query(default=None, pattern=r"^(SH|SZ|BJ)$"),
    as_of: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    subject_id = f"{market}:{code}" if market else code
    return await evidence_for_subject(
        "SECURITY", subject_id, as_of=as_of, limit=limit
    )


@router.get("/stocks/{code}/context-pack")
async def stock_context_pack(
    code: str,
    profile: str = Query(min_length=1, max_length=64),
    profile_version: int | None = Query(default=None, ge=1),
    market: str | None = Query(default=None, pattern=r"^(SH|SZ|BJ)$"),
    as_of: datetime | None = None,
    feature_run_id: UUID | None = None,
    recall_run_id: UUID | None = None,
    comparison_pack_id: UUID | None = None,
):
    try:
        async with _uow() as uow:
            task_profile = (
                await uow.task_registry.latest_profile(profile)
                if profile_version is None
                else await uow.task_registry.get_profile_version(
                    profile_code=profile, version=profile_version
                )
            )
        if task_profile is None or not task_profile.enabled:
            raise RepositoryNotFoundError("enabled task profile not found")
        return await BuildContextPackService(_uow).execute(
            BuildContextPackCommand(
                context_level=task_profile.context_level,
                subject_type="SECURITY",
                subject_id=f"{market}:{code}" if market else code,
                task_profile_id=task_profile.task_profile_id,
                task_profile_version=task_profile.version,
                as_of=as_of or datetime.now(timezone.utc),
                feature_run_id=feature_run_id,
                recall_run_id=recall_run_id,
                comparison_pack_id=comparison_pack_id,
            )
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/context-packs/{context_pack_id}")
async def context_pack_by_id(context_pack_id: UUID):
    async with _uow() as uow:
        pack = await uow.context_packs.get(context_pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="context pack not found")
    return pack


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


@router.get("/recalls/misses")
async def recall_misses(
    threshold_version: str | None = Query(default=None, min_length=1, max_length=64),
    only_misses: bool = True,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
):
    try:
        async with _uow() as uow:
            return await uow.recalls.read_misses(
                threshold_version=threshold_version,
                only_misses=only_misses,
                limit=limit,
                cursor=cursor,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/recalls/{run_id}")
async def recall_run_results(
    run_id: UUID,
    channel: str | None = Query(default=None, min_length=1, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
):
    return await recall_results(run_id, channel, limit, cursor)


@router.get("/task-context/{profile}")
async def task_context(profile: str):
    async with _uow() as uow:
        context = await uow.task_registry.latest_task_context(profile)
    if context is None:
        raise HTTPException(status_code=404, detail="task profile not found")
    task_profile, expected_run, task_run = context
    return {
        "profile": task_profile,
        "expected_run": expected_run,
        "task_run": task_run,
        "semantics": {
            "expected_run": "scheduled ChatGPT task expectation; not server AI execution"
        },
    }


@router.get("/task-runs")
async def task_runs(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
):
    try:
        async with _uow() as uow:
            return await uow.task_registry.read_task_runs(limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/task-runs/{task_run_id}")
async def task_run_by_id(task_run_id: UUID):
    async with _uow() as uow:
        run = await uow.task_registry.get_task_run(task_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="task run not found")
    return run
