from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.container import container
from app.v3.application.execute_regression_case import ExecuteRegressionCaseService
from app.v3.application.release_resolver import ReleaseResolver
from app.v3.application.read_operations import ReadOperationsService
from app.v3.application.intraday_market_data import IntradayMarketDataService
from app.v3.application.intraday_structure_snapshot import (
    IntradayStructureSnapshotService,
)
from app.v3.application.read_entry_decision_context import (
    ReadEntryDecisionContextService,
)
from app.v3.application.read_position_context import ReadPositionContextService
from app.v3.application.read_position_decision_context import (
    ReadPositionDecisionContextService,
)
from app.v3.application.read_attention import ReadAttentionEventsService
from app.v3.application.market_intraday_status import MarketIntradayStatusService
from app.v3.application.pipeline_eod_latest import PipelineEodLatestService
from app.v3.infrastructure.providers.exchange_calendar import (
    ExchangeCalendarsAShareCalendar,
)
from app.v3.application.build_candidate_comparison import (
    BuildCandidateComparisonService,
    CandidateComparisonQuery,
)
from app.v3.application.build_context_pack import (
    BuildContextPackCommand,
    BuildContextPackService,
)
from app.v3.application.read_evidence import ReadEvidenceService
from app.v3.application.import_ai_results import (
    ConfirmAIResultImportService,
    PreviewAIResultImportService,
)
from app.v3.application.ocr import OcrUnavailableError, build_ocr_adapter
from app.v3.application.ocr_pipeline import (
    ImageUploadCommand,
    RecognizeImageService,
)
from app.v3.application.manage_portfolio import (
    DraftConfirmation,
    ImageDraftImport,
    PortfolioWriteService,
    DraftFieldCorrection,
)
from app.v3.application.manage_actions import ActionWriteService
from app.v3.application.manage_performance import (
    PerformanceService,
    PerformanceSummaryCommand,
    RecallMissSnapshotCommand,
)
from app.v3.application.manage_decisions import DecisionStateService
from app.v3.application.manage_strategy import StrategyStabilizationService
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.evidence import EvidenceReadQuery, EvidenceSourceType
from app.v3.domain.features import FeatureQuery, FeatureSortField
from app.v3.domain.ai_import import AIResultBundle, AIResultConfirmCommand
from app.v3.domain.action import ActionCandidateCreate, EntryAssessmentCreate
from app.v3.domain.performance import (
    PerformanceAttributionCreate,
    RegressionCaseCreate,
    ReplayRunCreate,
)
from app.v3.domain.decision import (
    DecisionCorrectionCommand,
    WatchlistState,
    WatchlistTransitionCommand,
)
from app.v3.domain.strategy import (
    CapacityEvaluationCreate,
    ExperimentEventCommand,
    GuardrailVersionCreate,
    OperationalHealthEventCreate,
    ShadowObservationCreate,
    StrategyActivationCommand,
    StrategyExperimentCreate,
    StrategyProposalCreate,
    StrategyRollbackCommand,
    StrategyVersionCreate,
)
from app.v3.domain.portfolio import (
    AccountCreate,
    OpeningPositionCreate,
    PortfolioAdjustmentCreate,
    PortfolioPreferenceCreate,
    ReconciliationCreate,
    TradeConfirm,
    TradeCorrectionCreate,
    TradeDraftCreate,
)
from app.v3.repositories.errors import (
    RepositoryConflictError,
    RepositoryNotFoundError,
)
from app.v3.application.deep_market_data import DeepMarketDataService
from app.v3.security import V3Principal, bind_v3_principal

router = APIRouter(prefix="/api/v3", tags=["V3"])


def _uow():
    if not container.v3.enabled:
        raise HTTPException(status_code=503, detail="V3 is not enabled")
    return container.v3.uow()


def _bind_principal(command, request: Request):
    principal = getattr(request.state, "v3_principal", None)
    if not isinstance(principal, V3Principal):
        raise HTTPException(status_code=401, detail="authenticated V3 principal is required")
    return bind_v3_principal(command, principal)


def _request_id(request: Request) -> str | None:
    """NEW-AUD-001：把认证 principal 的 request_id 传入 audited service，
    落到 AuditEvent.request_id（匿名/未认证请求无 principal → None）。"""
    principal = getattr(request.state, "v3_principal", None)
    return principal.request_id if isinstance(principal, V3Principal) else None


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


class CandidateComparisonBuildRequest(BaseModel):
    """API-002：Comparison Pack build 的 POST body（build 会落库 publish）。"""

    candidate_set_id: UUID | None = None
    codes: list[str] = Field(default_factory=list)
    feature_run_id: UUID | None = None
    recall_run_id: UUID | None = None
    field_profile_version: str = Field(
        default="compact-fields.v1", min_length=1, max_length=64
    )
    as_of: datetime | None = None


class ContextPackBuildRequest(BaseModel):
    """API-002：Context Pack build 的 POST body（build 会落库 publish）。"""

    profile: str = Field(min_length=1, max_length=64)
    profile_version: int | None = Field(default=None, ge=1)
    market: str | None = Field(default=None, pattern=r"^(SH|SZ|BJ)$")
    as_of: datetime | None = None
    feature_run_id: UUID | None = None
    recall_run_id: UUID | None = None
    comparison_pack_id: UUID | None = None


async def _execute_comparison_build(
    *, candidate_set_id, codes, feature_run_id, recall_run_id,
    field_profile_version, as_of,
):
    try:
        query = CandidateComparisonQuery(
            candidate_set_id=candidate_set_id,
            codes=codes,
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


async def _execute_stock_context_build(
    *, code, profile, profile_version, market, as_of,
    feature_run_id, recall_run_id, comparison_pack_id,
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


@router.post("/candidates/comparison-pack")
async def build_candidate_comparison_pack(
    request: CandidateComparisonBuildRequest,
):
    """API-002：Comparison Pack 构建有落库副作用（publish ComparisonPack），
    正式入口为 POST；GET 仅作 deprecated 兼容。"""
    return await _execute_comparison_build(
        candidate_set_id=request.candidate_set_id,
        codes=tuple(item.strip() for item in request.codes if item.strip()),
        feature_run_id=request.feature_run_id,
        recall_run_id=request.recall_run_id,
        field_profile_version=request.field_profile_version,
        as_of=request.as_of,
    )


@router.get("/candidates/comparison-pack")
async def read_candidate_comparison_pack(
    comparison_pack_id: UUID | None = None,
    candidate_set_id: UUID | None = None,
    field_profile_version: str = Query(
        default="compact-fields.v1", min_length=1, max_length=64
    ),
    as_of: datetime | None = None,
):
    """API-002/R3-P0-003：GET 只读已有 pack，绝不触发 Build/Publish 落库
    （build 走同名 POST）。按 comparison_pack_id 精确读，或按
    candidate_set_id 读 as_of 前最近一次 pack。"""
    try:
        async with _uow() as uow:
            if comparison_pack_id is not None:
                pack = await uow.candidate_comparisons.get(comparison_pack_id)
            elif candidate_set_id is not None:
                pack = await uow.candidate_comparisons.latest_for_candidate_set(
                    candidate_set_id,
                    field_profile_version=field_profile_version,
                    as_of=as_of or datetime.now(timezone.utc),
                )
            else:
                raise ValueError(
                    "read requires comparison_pack_id or candidate_set_id; "
                    "building requires POST"
                )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if pack is None:
        raise HTTPException(status_code=404, detail="comparison pack not found")
    return pack


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


@router.post("/stocks/{code}/context-pack")
async def build_stock_context_pack(
    code: str, request: ContextPackBuildRequest,
):
    """API-002：Context Pack 构建有落库副作用（publish ContextPack），
    正式入口为 POST；GET 仅作 deprecated 兼容。"""
    return await _execute_stock_context_build(
        code=code,
        profile=request.profile,
        profile_version=request.profile_version,
        market=request.market,
        as_of=request.as_of,
        feature_run_id=request.feature_run_id,
        recall_run_id=request.recall_run_id,
        comparison_pack_id=request.comparison_pack_id,
    )


@router.get("/stocks/{code}/context-pack")
async def read_stock_context_pack(
    code: str,
    market: str | None = Query(default=None, pattern=r"^(SH|SZ|BJ)$"),
    as_of: datetime | None = None,
):
    """API-002/R3-P0-003：GET 只读该 SECURITY 主体最近一次已发布
    ContextPack，绝不触发 Build/Publish 落库（build 走同名 POST）。"""
    subject_id = f"{market}:{code}" if market else code
    async with _uow() as uow:
        pack = await uow.context_packs.latest_for_subject(
            subject_type="SECURITY",
            subject_id=subject_id,
            as_of=as_of or datetime.now(timezone.utc),
        )
    if pack is None:
        raise HTTPException(status_code=404, detail="context pack not found")
    return pack


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


@router.post("/ai-results/imports/preview")
async def preview_ai_result_import(bundle: AIResultBundle):
    try:
        return await PreviewAIResultImportService(_uow).execute(bundle)
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/ai-results/imports/{import_id}/confirm")
async def confirm_ai_result_import(
    import_id: UUID, command: AIResultConfirmCommand, request: Request,
):
    command = _bind_principal(command, request)
    try:
        return await ConfirmAIResultImportService(_uow).execute(
            import_id, command, request_id=_request_id(request),
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/portfolio/accounts")
async def create_portfolio_account(command: AccountCreate):
    return await PortfolioWriteService(_uow).create_account(command)


@router.post("/portfolio/images")
async def upload_portfolio_image(command: ImageUploadCommand, request: Request):
    """RC-09A：上传券商截图 → 服务端 OCR Adapter 识别 → Draft Preview。"""
    command = _bind_principal(command, request)
    import os

    service = RecognizeImageService(
        _uow, adapter=build_ocr_adapter(),
        store_dir=os.getenv("V3_IMAGE_STORE_DIR", "data/v3-images"),
    )
    try:
        return await service.execute(command)
    except OcrUnavailableError as exc:
        raise HTTPException(
            status_code=503, detail=f"OCR provider unavailable: {exc.reason}"
        ) from exc


@router.get("/portfolio/trade-drafts/{draft_id}")
async def read_trade_draft_preview(draft_id: UUID):
    try:
        return await PortfolioWriteService(_uow).trade_draft_preview(draft_id)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/portfolio/trade-drafts/{draft_id}/corrections")
async def correct_trade_draft_fields(
    draft_id: UUID, command: DraftFieldCorrection, request: Request,
):
    command = _bind_principal(command, request)
    try:
        return await PortfolioWriteService(_uow).correct_trade_draft(
            draft_id, command, request_id=_request_id(request),
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/portfolio/position-drafts/{draft_id}")
async def read_position_draft_preview(draft_id: UUID):
    try:
        return await PortfolioWriteService(_uow).position_draft_preview(draft_id)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/portfolio/position-drafts/{draft_id}/corrections")
async def correct_position_draft_fields(
    draft_id: UUID, command: DraftFieldCorrection, request: Request,
):
    command = _bind_principal(command, request)
    try:
        return await PortfolioWriteService(_uow).correct_position_draft(
            draft_id, command, request_id=_request_id(request),
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/portfolio/trade-drafts")
async def create_trade_draft(command: TradeDraftCreate):
    try:
        return await PortfolioWriteService(_uow).create_trade_draft(command)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/portfolio/image-imports")
async def import_portfolio_image(command: ImageDraftImport):
    try:
        return await PortfolioWriteService(_uow).import_image_drafts(command)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/portfolio/trade-drafts/{draft_id}/confirm")
async def confirm_portfolio_trade(
    draft_id: UUID, command: TradeConfirm, request: Request,
):
    command = _bind_principal(command, request)
    try:
        return await PortfolioWriteService(_uow).confirm_trade(
            draft_id, command, request_id=_request_id(request),
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/portfolio/opening-positions")
async def create_opening_position(command: OpeningPositionCreate, request: Request):
    command = _bind_principal(command, request)
    return await PortfolioWriteService(_uow).add_opening(
        command, request_id=_request_id(request),
    )


@router.post("/portfolio/position-drafts/{draft_id}/confirm")
async def confirm_position_snapshot_draft(
    draft_id: UUID, command: DraftConfirmation, request: Request,
):
    command = _bind_principal(command, request)
    try:
        return await PortfolioWriteService(_uow).confirm_position_draft(
            draft_id, command, request_id=_request_id(request),
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/portfolio/adjustments")
async def create_portfolio_adjustment(
    command: PortfolioAdjustmentCreate, request: Request,
):
    command = _bind_principal(command, request)
    return await PortfolioWriteService(_uow).add_adjustment(
        command, request_id=_request_id(request),
    )


@router.post("/portfolio/trade-corrections")
async def create_trade_correction(command: TradeCorrectionCreate, request: Request):
    command = _bind_principal(command, request)
    try:
        return await PortfolioWriteService(_uow).add_trade_correction(
            command, request_id=_request_id(request),
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/portfolio/reconciliations")
async def create_reconciliation(command: ReconciliationCreate, request: Request):
    command = _bind_principal(command, request)
    return await PortfolioWriteService(_uow).add_reconciliation(
        command, request_id=_request_id(request),
    )


@router.post("/portfolio/preferences")
async def create_portfolio_preference(command: PortfolioPreferenceCreate):
    return await PortfolioWriteService(_uow).add_preference(command)


@router.post("/portfolio/accounts/{account_id}/positions/{security_id}/rebuild")
async def rebuild_portfolio_position(account_id: UUID, security_id: UUID):
    try:
        return await PortfolioWriteService(_uow).rebuild_position(account_id, security_id)
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/portfolio/accounts/{account_id}/positions/{security_id}")
async def portfolio_position(account_id: UUID, security_id: UUID):
    position = await PortfolioWriteService(_uow).read_position(account_id, security_id)
    if position is None:
        raise HTTPException(status_code=404, detail="position not found")
    return position


@router.get("/stocks/{code}/decision-context")
async def entry_decision_context(
    code: str,
    market: str = Query(default="SZ"),
    as_of: datetime | None = Query(default=None),
):
    """RT-06：Entry Decision Context——回答"现在能买吗"。

    聚合最新 Decision/EntryPlan + 实时 Quote + 分钟结构；
    Trigger/Cancel 只做客观评估；缺关键实时数据绝不 READY。
    """
    if not container.v3.enabled:
        raise HTTPException(status_code=503, detail="V3 is not enabled")
    # R3-P1-006：实时主入口走 ProviderManager（东财/腾讯 fallback + 健康降级）
    bars_service = IntradayMarketDataService(container.provider_manager)
    service = ReadEntryDecisionContextService(
        _uow,
        bars_service,
        IntradayStructureSnapshotService(bars_service),
    )
    return await service.execute(
        code, market, as_of=as_of or datetime.now(timezone.utc),
    )


@router.get("/portfolio/intraday/{code}")
async def portfolio_intraday_structure(
    code: str,
    as_of: datetime | None = Query(default=None),
):
    """分钟级深度结构（RC-04D）：只服务持仓上下文，fetch-time 事实。"""
    service = DeepMarketDataService(
        container.provider_manager, source="legacy-provider",
    )
    return await service.get_intraday_structure(
        code, as_of=as_of or datetime.now(timezone.utc),
    )


@router.post("/actions")
async def create_action_candidate(command: ActionCandidateCreate):
    try:
        return await ActionWriteService(_uow).add_action(command)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/entries")
async def create_entry_assessment(command: EntryAssessmentCreate):
    try:
        return await ActionWriteService(_uow).add_entry(command)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/stocks/{security_id}/decision-pipeline")
async def decision_pipeline(
    security_id: UUID, limit: int = Query(default=50, ge=1, le=200),
):
    return await ActionWriteService(_uow).read_pipeline(security_id, limit)


@router.get("/portfolio/accounts/{account_id}/positions/{security_id}/reviews")
async def position_review_history(
    account_id: UUID, security_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
):
    return await ActionWriteService(_uow).read_position_reviews(
        account_id, security_id, limit
    )


def _position_context_service() -> ReadPositionContextService:
    """NEW-CTX-002：主路径绑定 Calendar + DeepMarketData + 实时 Quote，
    60m/15m/5m 与 holding_sessions 不再退化 UNKNOWN。"""
    return ReadPositionContextService(
        _uow,
        calendar=ExchangeCalendarsAShareCalendar(),
        deep_market_data=DeepMarketDataService(
            container.provider_manager, source="legacy-provider",
        ),
        quote_service=IntradayMarketDataService(container.provider_manager),
    )


@router.get("/portfolio/{code}/decision-context")
async def portfolio_position_decision_context(
    code: str, account_id: UUID,
    market: str | None = Query(default=None, pattern=r"^(SH|SZ|BJ)$"),
    as_of: datetime | None = Query(default=None),
):
    """RT-07：Position Decision Context——回答"现在卖不卖"。

    完整 Position Context + objective_sell_facts（stop/target 客观事实，
    只陈述、绝无建议）；NEW-CTX-001：stop/target 判断优先实时 Quote，
    FEATURE_LKG 仅作独立 EOD 事实并列返回。卖出决策由 AI/人做，
    成交必须走 Trade Draft。
    """
    if not container.v3.enabled:
        raise HTTPException(status_code=503, detail="V3 is not enabled")
    service = ReadPositionDecisionContextService(_position_context_service())
    return await service.execute(
        account_id, code, market, as_of=as_of or datetime.now(timezone.utc),
    )


@router.get("/portfolio/{code}/context")
async def portfolio_position_context(
    code: str, account_id: UUID,
    market: str | None = Query(default=None, pattern=r"^(SH|SZ|BJ)$"),
):
    try:
        return await _position_context_service().execute(account_id, code, market)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/performance/attributions")
async def create_performance_attribution(command: PerformanceAttributionCreate):
    try:
        return await PerformanceService(_uow).add_attribution(command)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/performance/summaries")
async def create_performance_summary(command: PerformanceSummaryCommand):
    try:
        return await PerformanceService(_uow).summarize(command)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/replays")
async def create_replay(command: ReplayRunCreate):
    return await PerformanceService(_uow).replay(command)


@router.post("/regression-cases")
async def create_regression_case(command: RegressionCaseCreate):
    try:
        return await PerformanceService(_uow).add_regression_case(command)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/regression-cases/{regression_case_id}/execute")
async def execute_regression_case(regression_case_id: UUID):
    """RC-06C：真执行 —— run replay → evaluate invariants → PASS/FAIL/BLOCKED → diff。"""
    try:
        return await ExecuteRegressionCaseService(_uow).execute(regression_case_id)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/release/resolution")
async def resolve_release():
    """RC-07B：唯一 Runtime Release Resolver —— 当次执行的策略/护栏/配置解析结果。"""
    if not container.v3.enabled:
        raise HTTPException(status_code=503, detail="V3 is not enabled")
    resolver = ReleaseResolver(
        _uow, v3_enabled=container.v3.enabled,
    )
    return await resolver.resolve()


@router.get("/portfolio")
async def read_portfolio(limit: int = 100):
    """RC-08B READ：账户与持仓投影（只读）。"""
    return await ReadOperationsService(_uow).portfolio_overview(limit)


@router.get("/portfolio/{code}/reviews")
async def read_position_reviews(code: str, limit: int = 100):
    """RC-08B READ：按证券代码查 Position Review 历史（只读）。"""
    return await ReadOperationsService(_uow).position_reviews_by_code(code, limit)


@router.get("/portfolio/{code}/adjustments")
async def read_portfolio_adjustments(code: str, limit: int = 100):
    """RC-08B READ：按证券代码查持仓调整事实（只读）。"""
    return await ReadOperationsService(_uow).adjustments_by_code(code, limit)


@router.get("/portfolio/preferences")
@router.get("/portfolio-preferences")
async def read_portfolio_preferences(limit: int = 100):
    """RC-08B READ：组合偏好版本（只读）。"""
    return await ReadOperationsService(_uow).preferences(limit)


@router.get("/entry-plans/{entry_plan_id}/versions")
async def read_entry_plan_versions(entry_plan_id: UUID):
    """RC-08B READ：EntryPlan 版本链（同 decision 全版本，只读）。"""
    return await ReadOperationsService(_uow).entry_plan_versions(entry_plan_id)


@router.get("/watchlist/changes")
async def read_watchlist_changes(limit: int = 100):
    """RC-08B READ：Watchlist 事件历史（append-only，只读）。"""
    return await ReadOperationsService(_uow).watchlist_changes(limit)


@router.get("/decisions")
async def read_decisions(limit: int = 100):
    """RC-08B READ：决策列表（immutable 投影，只读）。"""
    return await ReadOperationsService(_uow).decisions(limit)


@router.get("/reviews")
async def read_reviews(limit: int = 100):
    """RC-08B READ：决策 Review 历史（只读）。"""
    return await ReadOperationsService(_uow).reviews(limit)


@router.get("/market-reviews")
async def read_market_reviews(limit: int = 100):
    """RC-08B READ：市场 Review 历史（只读）。"""
    return await ReadOperationsService(_uow).market_reviews(limit)


@router.get("/performance")
async def read_performance(limit: int = 100):
    """RC-08B READ：绩效 attribution/summary 事实（只读）。"""
    return await ReadOperationsService(_uow).performance(limit)


@router.get("/intraday/attention")
async def read_attention_events(
    codes: str | None = Query(default=None),
    event_types: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    """RT-08：Attention 事件只读聚合（OPEN 状态，§18.4 点时字段）。"""
    if not container.v3.enabled:
        raise HTTPException(status_code=503, detail="V3 is not enabled")
    service = ReadAttentionEventsService(_uow, clock=lambda: datetime.now(timezone.utc))
    return await service.execute(
        codes=codes.split(",") if codes else None,
        event_types=event_types.split(",") if event_types else None,
        limit=limit,
    )


@router.get("/market/intraday-status")
async def read_market_intraday_status():
    """RT-08：市场盘中状态（交易时段判定，确定性规则）。"""
    if not container.v3.enabled:
        raise HTTPException(status_code=503, detail="V3 is not enabled")
    calendar = ExchangeCalendarsAShareCalendar()

    def _trading_day(value):
        try:
            return bool(calendar.is_trading_day(value))
        except Exception:
            return False

    service = MarketIntradayStatusService(
        clock=lambda: datetime.now(timezone.utc), is_trading_day=_trading_day,
    )
    return await service.execute()


@router.get("/pipeline/eod/latest")
async def read_pipeline_eod_latest(
    limit: int = Query(default=50, ge=1, le=200),
):
    """RT-08：EOD 流水线最近一次各 job 运行状态（按 job 聚合）。"""
    if not container.v3.enabled:
        raise HTTPException(status_code=503, detail="V3 is not enabled")
    service = PipelineEodLatestService(
        _uow, clock=lambda: datetime.now(timezone.utc),
    )
    return await service.execute(limit=limit)


@router.get("/health/data-quality")
async def read_data_quality():
    """RC-08B READ：数据质量事实（最新 feature run 覆盖率/状态 + 最新 bar revision known_at，只读）。"""
    return await ReadOperationsService(_uow).data_quality()


@router.post("/performance/recall-miss-runs")
async def create_recall_miss_snapshot(command: RecallMissSnapshotCommand):
    return await PerformanceService(_uow).snapshot_recall_misses(command)


@router.get("/watchlist")
async def read_watchlist(
    state: WatchlistState | None = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    return await DecisionStateService(_uow).read_watchlist(
        state.value if state else None, limit
    )


@router.post("/watchlist/{security_id}/transitions")
async def transition_watchlist(
    security_id: UUID, command: WatchlistTransitionCommand, request: Request,
):
    command = _bind_principal(command, request)
    try:
        return await DecisionStateService(_uow).transition_watchlist(
            security_id, command, request_id=_request_id(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/decisions/security/{security_id}")
async def read_security_decisions(security_id: UUID):
    return await DecisionStateService(_uow).read_decision_state(security_id)


@router.post("/decisions/{decision_id}/corrections")
async def create_decision_correction(
    decision_id: UUID, command: DecisionCorrectionCommand, request: Request,
):
    command = _bind_principal(command, request)
    try:
        return await DecisionStateService(_uow).add_correction(
            decision_id, command, request_id=_request_id(request),
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/strategies/versions")
async def create_strategy_version(command: StrategyVersionCreate, request: Request):
    command = _bind_principal(command, request)
    try:
        return await StrategyStabilizationService(_uow).add_strategy_version(
            command, request_id=_request_id(request),
        )
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/strategies")
async def read_strategy_catalog(limit: int = Query(default=50, ge=1, le=200)):
    return await StrategyStabilizationService(_uow).catalog(limit)


@router.post("/strategies/proposals")
async def create_strategy_proposal(command: StrategyProposalCreate, request: Request):
    command = _bind_principal(command, request)
    try:
        return await StrategyStabilizationService(_uow).add_proposal(
            command, request_id=_request_id(request),
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/strategies/guardrails")
async def create_guardrail_version(command: GuardrailVersionCreate, request: Request):
    command = _bind_principal(command, request)
    try:
        return await StrategyStabilizationService(_uow).add_guardrail(command)
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/strategies/experiments")
async def create_strategy_experiment(
    command: StrategyExperimentCreate, request: Request,
):
    command = _bind_principal(command, request)
    try:
        return await StrategyStabilizationService(_uow).add_experiment(
            command, request_id=_request_id(request),
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/strategies/experiments/{experiment_id}/events")
async def strategy_experiment_event(
    experiment_id: UUID, command: ExperimentEventCommand, request: Request,
):
    command = _bind_principal(command, request)
    try:
        return await StrategyStabilizationService(_uow).experiment_event(
            experiment_id, command, request_id=_request_id(request),
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/strategies/experiments/{experiment_id}")
async def read_strategy_experiment(experiment_id: UUID):
    try:
        return await StrategyStabilizationService(_uow).experiment_detail(
            experiment_id
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/strategies/experiments/{experiment_id}/assign")
async def assign_strategy_experiment(experiment_id: UUID, subject_key: str):
    try:
        return await StrategyStabilizationService(_uow).assign(
            experiment_id, subject_key
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/strategies/shadow-observations")
async def create_shadow_observation(command: ShadowObservationCreate):
    try:
        return await StrategyStabilizationService(_uow).shadow_observation(command)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/strategies/capacity-evaluations")
async def create_capacity_evaluation(command: CapacityEvaluationCreate):
    try:
        return await StrategyStabilizationService(_uow).evaluate_capacity(command)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/strategies/releases/{environment}/activate")
async def activate_strategy(
    environment: str, command: StrategyActivationCommand, request: Request,
):
    command = _bind_principal(command, request)
    try:
        return await StrategyStabilizationService(_uow).activate(
            environment, command, request_id=_request_id(request),
        )
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/strategies/releases/{environment}/rollback")
async def rollback_strategy(
    environment: str, command: StrategyRollbackCommand, request: Request,
):
    command = _bind_principal(command, request)
    try:
        return await StrategyStabilizationService(_uow).rollback(
            environment, command, request_id=_request_id(request),
        )
    except RepositoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/strategies/releases/{environment}")
async def strategy_release_dashboard(environment: str):
    return await StrategyStabilizationService(_uow).dashboard(environment)


@router.post("/operations/health-events")
async def create_operational_health_event(command: OperationalHealthEventCreate):
    return await StrategyStabilizationService(_uow).add_health_event(command)
