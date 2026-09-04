from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.contracts.agent import AIResultEnvelope
from app.v3.domain.ai_import import (
    AIResultAtomicGroup,
    AIResultBundle,
    AIResultImportPreview,
    GroupCommitStatus,
    ImportStatus,
)
from app.v3.domain.hashing import canonical_hash
from app.v3.domain.decision import (
    DecisionCorrectionCommand,
    WatchlistState,
    WatchlistTransitionCommand,
    validate_watchlist_transition,
)
from app.v3.domain.task import TaskGroupCounts, derive_task_run_status
from app.v3.infrastructure.db.models import (
    AIResultAtomicGroupModel,
    AIResultBundleModel,
    AIResultDependencyModel,
    AIResultEnvelopeModel,
    AIResultImportModel,
    AgentTaskModel,
    ContextPackModel,
    DecisionModel,
    DecisionCorrectionModel,
    EntryPlanModel,
    EvidenceRecordModel,
    MarketReviewModel,
    PositionProjectionModel,
    PositionReviewModel,
    ReviewModel,
    SecurityModel,
    TaskRunModel,
    WatchlistProposalModel,
    WatchlistEventModel,
    WatchlistModel,
)
from app.v3.repositories.errors import RepositoryConflictError, RepositoryNotFoundError

# R5-P1-005/§60：Resident Monitor 的 Entry 计划只接受这些现态。
# TRIGGERED/HOLDING 归持仓侧（POSITION），SLOW/DOWNGRADED/INVALIDATED/
# CLOSED 不再监控。
_ACTIVE_WATCHLIST_STATES = (
    WatchlistState.WATCHING.value,
    WatchlistState.WAIT_ENTRY.value,
    WatchlistState.ACTION_READY.value,
)


def _filter_active_plans(
    latest: dict[UUID, "EntryPlanModel"],
    decisions: "Iterable[DecisionModel]",
    security_by_id: dict[UUID, "SecurityModel"],
    active_watchlist: set[UUID],
    held_security_ids: set[UUID],
    now: datetime,
) -> list[dict]:
    """R5-P1-005/§60：Active Plan 冻结定义（纯函数，便于独立验收）。

    - 每个 security 只取当前 Decision（最新 as_of）的最新版本 Plan——
      历史 Decision 的 Plan 即使带 stop/target 也不进入 Resident Monitor；
    - 仅 ENTRY_WATCHLIST（Watchlist 现态 ∈ _ACTIVE_WATCHLIST_STATES）
      或 POSITION（持仓现态投影 quantity > 0）的 security 保留；
    - effective_from 未生效（未来计划）排除。
    """
    current_decision_id: dict[UUID, UUID] = {}  # security_id -> decision_id
    for decision in sorted(decisions, key=lambda d: d.as_of):
        current_decision_id[decision.security_id] = decision.decision_id
    result = []
    for security_id, decision_id in current_decision_id.items():
        security = security_by_id.get(security_id)
        if security is None:
            continue
        sources: list[str] = []
        if security_id in active_watchlist:
            sources.append("ENTRY_WATCHLIST")
        if security_id in held_security_ids:
            sources.append("POSITION")
        if not sources:
            continue
        plan = latest[decision_id]
        if plan.effective_from > now:
            continue  # 未来生效计划不进入 Resident Monitor
        payload = plan.plan or {}
        stop = payload.get("stop_loss")
        target = payload.get("take_profit")
        if stop is None and isinstance(payload.get("stop"), dict):
            stop = payload["stop"].get("price")
        if target is None:
            targets = payload.get("targets") or []
            if targets and isinstance(targets[0], dict):
                target = targets[0].get("price")
        if stop is None and target is None:
            continue
        result.append({
            "entry_plan_id": plan.entry_plan_id,
            "decision_id": decision_id,
            "security_id": security.security_id,
            "code": security.code,
            "market": security.market,
            "stop_loss": stop,
            "take_profit": target,
            "plan_source": "+".join(sources),
            "plan": payload,
        })
    return result
from app.v3.domain.entry_plan import EntryPlanPayload
from app.v3.domain.position_review import PositionReviewPayload


class SQLAlchemyAIResultImportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def validate_envelope(self, envelope: AIResultEnvelope) -> tuple[str, ...]:
        errors: list[str] = []
        task = await self._session.get(AgentTaskModel, envelope.task_id)
        if task is None:
            return (f"agent task {envelope.task_id} not found",)
        if task.task_run_id != envelope.task_run_id:
            errors.append("task_run_id does not match agent task")
        if task.expected_result_type != envelope.result_type:
            errors.append("result_type does not match agent task expectation")
        if task.context_pack_id != envelope.context_pack_id:
            errors.append("context_pack_id does not match agent task")
        if task.context_pack_hash != envelope.context_pack_hash:
            errors.append("context_pack_hash does not match agent task")
        pack = await self._session.get(ContextPackModel, envelope.context_pack_id)
        if pack is None or pack.content_hash != envelope.context_pack_hash:
            errors.append("referenced context pack or hash is invalid")
        if envelope.evidence_ids:
            known = set(
                (
                    await self._session.scalars(
                        select(EvidenceRecordModel.evidence_id).where(
                            EvidenceRecordModel.evidence_id.in_(envelope.evidence_ids),
                            EvidenceRecordModel.known_at <= envelope.as_of,
                        )
                    )
                ).all()
            )
            missing = set(envelope.evidence_ids) - known
            if missing:
                errors.append(
                    f"evidence unavailable point-in-time: {sorted(map(str, missing))}"
                )
        return tuple(errors)

    async def add_preview(self, preview: AIResultImportPreview) -> None:
        existing = await self._session.scalar(
            select(AIResultImportModel).where(
                AIResultImportModel.bundle_hash == preview.bundle.bundle_hash,
                AIResultImportModel.preview_revision == preview.preview_revision,
            )
        )
        if existing is not None:
            raise RepositoryConflictError("bundle revision has already been previewed")
        self._session.add(
            AIResultImportModel(
                import_id=preview.import_id,
                bundle_id=preview.bundle.bundle_id,
                schema_version=preview.bundle.schema_version,
                preview_revision=preview.preview_revision,
                bundle_hash=preview.bundle.bundle_hash,
                status=preview.status.value,
                preview_payload=preview.model_dump(mode="json"),
                created_at=preview.created_at,
            )
        )
        await self._session.flush()
        self._session.add(
            AIResultBundleModel(
                bundle_id=preview.bundle.bundle_id,
                import_id=preview.import_id,
                agent_identity=preview.bundle.agent.model_dump(mode="json"),
                task_run_ids=[str(item) for item in preview.bundle.task_run_ids],
                produced_at=preview.bundle.produced_at,
                bundle_hash=preview.bundle.bundle_hash,
            )
        )
        # 这三张表未声明 ORM relationship，SQLAlchemy 不保证跨表插入顺序；
        # 显式 flush 保证 imports -> bundles -> groups 的外键依赖次序。
        await self._session.flush()
        validity = {item.group_id: item.valid for item in preview.groups}
        now = datetime.now(timezone.utc)
        for group in preview.bundle.atomic_groups:
            self._session.add(
                AIResultAtomicGroupModel(
                    atomic_group_id=uuid4(),
                    bundle_id=preview.bundle.bundle_id,
                    group_key=group.group_id,
                    task_run_id=group.task_run_id,
                    subject=group.subject,
                    required=group.required,
                    group_hash=group.group_hash,
                    validation_status="VALID"
                    if validity[group.group_id]
                    else "INVALID",
                    commit_status=GroupCommitStatus.PENDING.value,
                    created_at=now,
                )
            )

    async def get_preview_payload(self, import_id: UUID) -> dict[str, Any]:
        row = await self._session.get(AIResultImportModel, import_id)
        if row is None:
            raise RepositoryNotFoundError("AI result import preview not found")
        return row.preview_payload

    async def claim_confirmation(
        self,
        import_id: UUID,
        preview_revision: int,
        bundle_hash: str,
        idempotency_key: str,
        confirmed_by: str,
    ) -> ImportStatus | None:
        row = await self._session.get(
            AIResultImportModel, import_id, with_for_update=True
        )
        if row is None:
            raise RepositoryNotFoundError("AI result import preview not found")
        if row.preview_revision != preview_revision or row.bundle_hash != bundle_hash:
            raise RepositoryConflictError("preview revision or bundle hash has changed")
        if row.idempotency_key is not None:
            if row.idempotency_key != idempotency_key:
                raise RepositoryConflictError("import has already been confirmed")
            return ImportStatus(row.status)
        row.idempotency_key = idempotency_key
        row.confirmed_by = confirmed_by
        row.confirmed_at = datetime.now(timezone.utc)
        return None

    async def group_row(self, bundle_id: UUID, group_key: str, *, lock: bool = False):
        statement = select(AIResultAtomicGroupModel).where(
            AIResultAtomicGroupModel.bundle_id == bundle_id,
            AIResultAtomicGroupModel.group_key == group_key,
        )
        if lock:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        if row is None:
            raise RepositoryNotFoundError("atomic group not found")
        return row

    async def commit_group(
        self,
        import_id: UUID,
        bundle: AIResultBundle,
        group: AIResultAtomicGroup,
    ) -> tuple[UUID, ...]:
        group_row = await self.group_row(bundle.bundle_id, group.group_id, lock=True)
        if group_row.validation_status != "VALID":
            raise RepositoryConflictError("invalid atomic group cannot be confirmed")
        if group_row.commit_status == GroupCommitStatus.COMMITTED.value:
            return tuple(item.result_id for item in group.results)
        created: list[UUID] = []
        ordered = sorted(
            group.results,
            key=lambda item: {
                "DecisionResult": 10,
                "MarketReview": 20,
                "WatchlistProposal": 30,
                "ReviewResult": 40,
                "EntryPlanResult": 50,
                "CandidateComparisonResult": 60,
                "PositionReviewResult": 70,
            }.get(item.result_type, 99),
        )
        envelope_rows: dict[UUID, AIResultEnvelopeModel] = {}
        for envelope in ordered:
            known_at = max(envelope.produced_at, datetime.now(timezone.utc))
            row = AIResultEnvelopeModel(
                result_id=envelope.result_id,
                import_id=import_id,
                bundle_id=bundle.bundle_id,
                atomic_group_id=group_row.atomic_group_id,
                task_id=envelope.task_id,
                task_run_id=envelope.task_run_id,
                schema_version=envelope.schema_version,
                result_type=envelope.result_type,
                agent_type=envelope.agent.agent_type.value,
                provider=envelope.agent.provider.value,
                model=envelope.agent.model,
                model_version=envelope.agent.model_version,
                context_pack_id=envelope.context_pack_id,
                context_pack_hash=envelope.context_pack_hash,
                prompt_version=envelope.prompt_version,
                strategy_version=envelope.strategy_version,
                produced_at=envelope.produced_at,
                as_of=envelope.as_of,
                known_at=known_at,
                evidence_ids=[str(item) for item in envelope.evidence_ids],
                payload=envelope.result,
                content_hash=envelope.content_hash,
            )
            self._session.add(row)
            envelope_rows[envelope.result_id] = row
        await self._session.flush()
        for envelope in ordered:
            created.extend(await self._materialize(envelope))
        for result_id, dependencies in group.dependencies.items():
            for dependency_id in dependencies:
                self._session.add(
                    AIResultDependencyModel(
                        result_id=result_id, depends_on_result_id=dependency_id
                    )
                )
        group_row.commit_status = GroupCommitStatus.COMMITTED.value
        group_row.committed_at = datetime.now(timezone.utc)
        group_row.error = None
        return tuple(created)

    async def fail_group(self, bundle_id: UUID, group_key: str, error: str) -> None:
        row = await self.group_row(bundle_id, group_key, lock=True)
        row.commit_status = GroupCommitStatus.FAILED.value
        row.error = error[:4000]

    async def _materialize(self, envelope: AIResultEnvelope) -> list[UUID]:
        payload = envelope.result
        agent = envelope.agent.model_dump(mode="json")
        evidence_ids = [str(item) for item in envelope.evidence_ids]
        if envelope.result_type == "DecisionResult":
            object_id = UUID(str(payload.get("decision_id", uuid4())))
            snapshot = payload.get("original_entry_plan", {})
            self._session.add(
                DecisionModel(
                    decision_id=object_id,
                    security_id=UUID(str(payload["security_id"])),
                    task_run_id=envelope.task_run_id,
                    context_pack_id=envelope.context_pack_id,
                    context_pack_hash=envelope.context_pack_hash,
                    source_result_id=envelope.result_id,
                    agent_identity=agent,
                    evidence_ids=evidence_ids,
                    original_entry_plan_id=UUID(str(payload["original_entry_plan_id"]))
                    if payload.get("original_entry_plan_id")
                    else None,
                    original_entry_plan_snapshot=snapshot,
                    original_entry_plan_hash=canonical_hash(snapshot)
                    if snapshot
                    else None,
                    as_of=envelope.as_of,
                    produced_at=envelope.produced_at,
                    payload=payload,
                    content_hash=canonical_hash(
                        {"source": envelope.content_hash, "payload": payload}
                    ),
                )
            )
            return [object_id]
        if envelope.result_type == "EntryPlanResult":
            # RT-09：Entry Schema 收口——plan 必须通过类型化校验才能落库
            plan_dict = payload.get("plan", payload)
            try:
                EntryPlanPayload.from_plan(plan_dict)
            except ValueError as exc:
                raise RepositoryConflictError(
                    f"invalid entry plan payload: {exc}"
                ) from exc
            object_id = UUID(str(payload.get("entry_plan_id", uuid4())))
            decision_id = UUID(str(payload["decision_id"]))
            version = int(payload.get("version", 1))
            supersedes = (
                UUID(str(payload["supersedes_entry_plan_id"]))
                if payload.get("supersedes_entry_plan_id")
                else None
            )
            creator_count = int(bool(payload.get("created_by_review_id"))) + int(
                bool(payload.get("created_by_position_review_id"))
            )
            if version == 1 and (supersedes is not None or creator_count):
                raise RepositoryConflictError(
                    "entry plan version 1 cannot supersede or be review-created"
                )
            if version > 1:
                previous = (
                    await self._session.get(EntryPlanModel, supersedes)
                    if supersedes
                    else None
                )
                if (
                    previous is None
                    or previous.decision_id != decision_id
                    or previous.version != version - 1
                    or creator_count != 1
                ):
                    raise RepositoryConflictError("entry plan version chain is invalid")
            self._session.add(
                EntryPlanModel(
                    entry_plan_id=object_id,
                    decision_id=decision_id,
                    version=version,
                    supersedes_entry_plan_id=supersedes,
                    created_by_review_id=UUID(str(payload["created_by_review_id"]))
                    if payload.get("created_by_review_id")
                    else None,
                    created_by_position_review_id=UUID(
                        str(payload["created_by_position_review_id"])
                    )
                    if payload.get("created_by_position_review_id")
                    else None,
                    source_result_id=envelope.result_id,
                    effective_from=envelope.produced_at,
                    expected_horizon=str(payload.get("expected_horizon", "SWING")),
                    plan=payload.get("plan", payload),
                    content_hash=canonical_hash(
                        {"source": envelope.content_hash, "payload": payload}
                    ),
                )
            )
            return [object_id]
        if envelope.result_type == "ReviewResult":
            object_id = UUID(str(payload.get("review_id", uuid4())))
            self._session.add(
                ReviewModel(
                    review_id=object_id,
                    decision_id=UUID(str(payload["decision_id"])),
                    previous_review_id=UUID(str(payload["previous_review_id"]))
                    if payload.get("previous_review_id")
                    else None,
                    task_run_id=envelope.task_run_id,
                    context_pack_id=envelope.context_pack_id,
                    context_pack_hash=envelope.context_pack_hash,
                    source_result_id=envelope.result_id,
                    agent_identity=agent,
                    evidence_ids=evidence_ids,
                    thesis_status=str(payload.get("thesis_status", "UNCHANGED")),
                    time_efficiency=str(payload.get("time_efficiency", "UNKNOWN")),
                    as_of=envelope.as_of,
                    payload=payload,
                    content_hash=canonical_hash(
                        {"source": envelope.content_hash, "payload": payload}
                    ),
                )
            )
            return [object_id]
        if envelope.result_type == "MarketReview":
            object_id = UUID(str(payload.get("market_review_id", uuid4())))
            self._session.add(
                MarketReviewModel(
                    market_review_id=object_id,
                    previous_market_review_id=UUID(
                        str(payload["previous_market_review_id"])
                    )
                    if payload.get("previous_market_review_id")
                    else None,
                    task_run_id=envelope.task_run_id,
                    market_regime_snapshot_id=UUID(
                        str(payload["market_regime_snapshot_id"])
                    )
                    if payload.get("market_regime_snapshot_id")
                    else None,
                    context_pack_id=envelope.context_pack_id,
                    context_pack_hash=envelope.context_pack_hash,
                    source_result_id=envelope.result_id,
                    agent_identity=agent,
                    evidence_ids=evidence_ids,
                    as_of=envelope.as_of,
                    produced_at=envelope.produced_at,
                    payload=payload,
                    content_hash=canonical_hash(
                        {"source": envelope.content_hash, "payload": payload}
                    ),
                )
            )
            return [object_id]
        if envelope.result_type == "WatchlistProposal":
            object_id = UUID(str(payload.get("proposal_id", uuid4())))
            self._session.add(
                WatchlistProposalModel(
                    proposal_id=object_id,
                    security_id=UUID(str(payload["security_id"])),
                    source_result_id=envelope.result_id,
                    proposed_state=str(payload.get("proposed_state", "OBSERVING")),
                    reason=str(payload.get("reason", "AI proposal")),
                    payload=payload,
                    content_hash=canonical_hash(
                        {"source": envelope.content_hash, "payload": payload}
                    ),
                    created_at=envelope.produced_at,
                )
            )
            return [object_id]
        if envelope.result_type == "PositionReviewResult":
            account_id = UUID(str(payload["account_id"]))
            security_id = UUID(str(payload["security_id"]))
            projection = await self._session.get(
                PositionProjectionModel, (account_id, security_id)
            )
            if projection is None or projection.quantity <= 0:
                raise RepositoryConflictError(
                    "position review requires a confirmed holding"
                )
            expected_hash = payload.get("position_projection_hash")
            if expected_hash and expected_hash != projection.input_hash:
                raise RepositoryConflictError(
                    "position projection changed after AI context"
                )
            # RT-07：动作枚举冻结 + SELL→EXIT 兼容映射；非法动作拒绝落库
            try:
                review_payload = PositionReviewPayload.from_payload(payload)
            except ValueError as exc:
                raise RepositoryConflictError(
                    f"invalid position review payload: {exc}"
                ) from exc
            object_id = UUID(str(payload.get("position_review_id", uuid4())))
            self._session.add(
                PositionReviewModel(
                    position_review_id=object_id,
                    account_id=account_id,
                    security_id=security_id,
                    portfolio_snapshot_id=UUID(str(payload["portfolio_snapshot_id"]))
                    if payload.get("portfolio_snapshot_id")
                    else None,
                    position_projection_hash=projection.input_hash,
                    decision_id=UUID(str(payload["decision_id"]))
                    if payload.get("decision_id")
                    else None,
                    entry_plan_id=UUID(str(payload["entry_plan_id"]))
                    if payload.get("entry_plan_id")
                    else None,
                    previous_position_review_id=UUID(
                        str(payload["previous_position_review_id"])
                    )
                    if payload.get("previous_position_review_id")
                    else None,
                    task_run_id=envelope.task_run_id,
                    context_pack_id=envelope.context_pack_id,
                    context_pack_hash=envelope.context_pack_hash,
                    source_result_id=envelope.result_id,
                    evidence_ids=evidence_ids,
                    agent_identity=agent,
                    as_of=envelope.as_of,
                    quantity_snapshot=projection.quantity,
                    average_cost_snapshot=projection.average_cost,
                    thesis_status=str(payload.get("thesis_status", "MAINTAINED")),
                    supporting_evidence=payload.get("supporting_evidence", {}),
                    contrary_evidence=payload.get("contrary_evidence", {}),
                    changed_facts=payload.get("changed_facts", {}),
                    new_risks=payload.get("new_risks", []),
                    time_efficiency=str(payload.get("time_efficiency", "UNKNOWN")),
                    recommended_action=str(review_payload.recommended_action),
                    reason=str(payload.get("reason", "No reason supplied")),
                    payload=payload,
                    content_hash=canonical_hash(
                        {
                            "source": envelope.content_hash,
                            "payload": payload,
                            "projection": projection.input_hash,
                        }
                    ),
                )
            )
            return [object_id]
        return []

    async def transition_watchlist(
        self,
        security_id: UUID,
        command: WatchlistTransitionCommand,
    ) -> dict[str, Any]:
        row = await self._session.scalar(
            select(WatchlistModel)
            .where(WatchlistModel.security_id == security_id)
            .with_for_update()
        )
        projection_quantity = await self._session.scalar(
            select(func.coalesce(func.sum(PositionProjectionModel.quantity), 0)).where(
                PositionProjectionModel.security_id == security_id
            )
        )
        target = command.target_state
        current = WatchlistState(row.state) if row is not None else None
        if row is None:
            if target is not WatchlistState.WATCHING:
                raise RepositoryConflictError(
                    "new watchlist item must start at WATCHING"
                )
            now = datetime.now(timezone.utc)
            row = WatchlistModel(
                watchlist_id=uuid4(),
                security_id=security_id,
                state=target.value,
                row_version=1,
                created_at=now,
                updated_at=now,
            )
            self._session.add(row)
        else:
            validate_watchlist_transition(
                current,
                target,
                confirmed_position_quantity=float(projection_quantity or 0),
            )
            row.state = target.value
            row.updated_at = datetime.now(timezone.utc)
            row.row_version += 1
        event_id = uuid4()
        event_payload = {
            "watchlist_id": str(row.watchlist_id),
            "from_state": current.value if current else None,
            "to_state": target.value,
            "reason": command.reason,
            "actor_id": command.actor_id,
        }
        self._session.add(
            WatchlistEventModel(
                event_id=event_id,
                watchlist_id=row.watchlist_id,
                from_state=current.value if current else None,
                to_state=target.value,
                reason=command.reason,
                event_time=datetime.now(timezone.utc),
                content_hash=canonical_hash(event_payload),
            )
        )
        row.latest_event_id = event_id
        return {
            "watchlist_id": row.watchlist_id,
            "event_id": event_id,
            "state": target.value,
            "row_version": row.row_version,
        }

    async def read_watchlist(self, state: str | None, limit: int):
        """当前 Watchlist 现态（R5-P2-013：Fast Lane 等 Active Pool 来源
        必须读现态表，不是最近 N 条变更事件）。join Security 带 back
        market/code，供无 security_id 上下文的调用方直接使用。"""
        statement = (
            select(WatchlistModel, SecurityModel)
            .join(
                SecurityModel,
                SecurityModel.security_id == WatchlistModel.security_id,
            )
        )
        if state:
            statement = statement.where(WatchlistModel.state == state)
        rows = (
            await self._session.execute(
                statement.order_by(WatchlistModel.updated_at.desc()).limit(limit)
            )
        ).all()
        return tuple(
            {
                **{
                    column.name: getattr(watchlist, column.name)
                    for column in watchlist.__table__.columns
                },
                "security_market": security.market,
                "security_code": security.code,
                "security_name": security.name,
            }
            for watchlist, security in rows
        )

    async def active_price_trigger_plans(self) -> tuple[dict, ...]:
        """RT §21 盘中循环 + R5-P1-005/§60：Resident Monitor 只监控
        当前有效 EntryPlan（Active Plan 定义见 _filter_active_plans）。

        - ENTRY_WATCHLIST：security 当前 Watchlist state ∈
          {WATCHING, WAIT_ENTRY, ACTION_READY}；
        - POSITION：security 持仓现态投影 quantity > 0；
        - 每个 security 只取当前 Decision（最新 as_of）的最新版本 Plan。

        历史 Decision / CLOSED / INVALIDATED Watchlist 不再产生
        STOP_HIT / TARGET_HIT / ENTRY_TRIGGER_MET。plan JSONB 为
        RT-06 类型化结构（stop.price / targets[].price），兼容历史
        stop_loss/take_profit 顶层键。
        """
        plans = (
            await self._session.scalars(
                select(EntryPlanModel).order_by(
                    EntryPlanModel.decision_id, EntryPlanModel.version,
                )
            )
        ).all()
        latest: dict[UUID, EntryPlanModel] = {}
        for row in plans:  # version 升序 → 同 decision 后者覆盖
            latest[row.decision_id] = row
        if not latest:
            return ()
        decisions = (
            await self._session.scalars(
                select(DecisionModel).where(
                    DecisionModel.decision_id.in_(latest)
                )
            )
        ).all()
        securities = (
            await self._session.scalars(
                select(SecurityModel).where(
                    SecurityModel.security_id.in_({
                        decision.security_id for decision in decisions
                    })
                )
            )
        ).all()
        security_by_id = {s.security_id: s for s in securities}
        active_watchlist = set(
            await self._session.scalars(
                select(WatchlistModel.security_id).where(
                    WatchlistModel.state.in_(_ACTIVE_WATCHLIST_STATES)
                )
            )
        )
        held_security_ids = set(
            await self._session.scalars(
                select(PositionProjectionModel.security_id).where(
                    PositionProjectionModel.quantity > 0
                )
            )
        )
        return tuple(_filter_active_plans(
            latest, decisions, security_by_id,
            active_watchlist, held_security_ids,
            datetime.now(timezone.utc),
        ))

    async def read_decision_state(self, security_id: UUID):
        decisions = (
            await self._session.scalars(
                select(DecisionModel)
                .where(DecisionModel.security_id == security_id)
                .order_by(DecisionModel.as_of.desc())
            )
        ).all()
        ids = [item.decision_id for item in decisions]
        plans = (
            (
                await self._session.scalars(
                    select(EntryPlanModel)
                    .where(EntryPlanModel.decision_id.in_(ids))
                    .order_by(EntryPlanModel.effective_from)
                )
            ).all()
            if ids
            else []
        )
        reviews = (
            (
                await self._session.scalars(
                    select(ReviewModel)
                    .where(ReviewModel.decision_id.in_(ids))
                    .order_by(ReviewModel.as_of)
                )
            ).all()
            if ids
            else []
        )

        def serialize(row):
            return {
                column.name: getattr(row, column.name)
                for column in row.__table__.columns
            }

        return {
            "decisions": tuple(serialize(item) for item in decisions),
            "entry_plan_versions": tuple(serialize(item) for item in plans),
            "reviews": tuple(serialize(item) for item in reviews),
        }

    async def add_decision_correction(
        self,
        decision_id: UUID,
        command: DecisionCorrectionCommand,
    ) -> UUID:
        if await self._session.get(DecisionModel, decision_id) is None:
            raise RepositoryNotFoundError("decision not found")
        object_id = uuid4()
        payload = {"decision_id": decision_id, **command.model_dump(mode="json")}
        self._session.add(
            DecisionCorrectionModel(
                correction_id=object_id,
                decision_id=decision_id,
                old_values=command.old_values,
                new_values=command.new_values,
                reason=command.reason,
                corrected_by=command.corrected_by,
                corrected_at=datetime.now(timezone.utc),
                content_hash=canonical_hash(payload),
            )
        )
        return object_id

    async def refresh_task_run(self, task_run_id: UUID) -> str:
        run = await self._session.get(TaskRunModel, task_run_id, with_for_update=True)
        if run is None:
            raise RepositoryNotFoundError("task run not found")
        counts = dict(
            (
                await self._session.execute(
                    select(AIResultAtomicGroupModel.commit_status, func.count())
                    .where(AIResultAtomicGroupModel.task_run_id == task_run_id)
                    .group_by(AIResultAtomicGroupModel.commit_status)
                )
            ).all()
        )
        successful = int(counts.get("COMMITTED", 0))
        failed = int(counts.get("FAILED", 0))
        pending = max(run.expected_group_count - successful - failed, 0)
        if successful + failed > run.expected_group_count:
            raise RepositoryConflictError("atomic group count exceeds task expectation")
        task_counts = TaskGroupCounts(
            expected=run.expected_group_count,
            successful=successful,
            failed=failed,
            pending=pending,
        )
        status = derive_task_run_status(task_counts)
        run.successful_group_count = successful
        run.failed_group_count = failed
        run.pending_group_count = pending
        run.status = status.value
        run.completed_at = datetime.now(timezone.utc) if pending == 0 else None
        run.row_version += 1
        return status.value

    async def finish_import(self, import_id: UUID) -> ImportStatus:
        row = await self._session.get(
            AIResultImportModel, import_id, with_for_update=True
        )
        if row is None:
            raise RepositoryNotFoundError("AI result import not found")
        counts = dict(
            (
                await self._session.execute(
                    select(AIResultAtomicGroupModel.commit_status, func.count())
                    .where(AIResultAtomicGroupModel.bundle_id == row.bundle_id)
                    .group_by(AIResultAtomicGroupModel.commit_status)
                )
            ).all()
        )
        committed = int(counts.get("COMMITTED", 0))
        failed = int(counts.get("FAILED", 0))
        pending = int(counts.get("PENDING", 0))
        if pending:
            status = ImportStatus.PREVIEWED
        elif committed and failed:
            status = ImportStatus.PARTIAL_COMPLETED
        elif committed:
            status = ImportStatus.CONFIRMED
        else:
            status = ImportStatus.FAILED
        row.status = status.value
        return status
