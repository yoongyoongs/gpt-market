from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.time import SHANGHAI
from app.v3.contracts.agent import AgentTask
from app.v3.contracts.evidence import EvidenceType
from app.v3.domain.audit import AuditEvent
from app.v3.domain.market_data import (
    AdjustmentFactorRevision,
    AdjustType,
    BarIngestionTarget,
    BarPeriod,
    BarSeriesRevision,
    CorporateAction,
    IngestionRunStatus,
    Market,
    MarketDataIngestionRun,
    SecurityMember,
    UniverseSnapshot,
    MarketBar,
    BarSeriesRevisionContent,
    PointInTimePrecision,
)
from app.v3.domain.features import (
    FeaturePage, FeatureQuery, FeatureRun, FeatureRunStatus, FeatureSortField,
    MarketRegimeSnapshot, SecurityFeature,
)
from app.v3.domain.evidence import (
    DecayModel,
    EntityLink,
    EvidenceConflict,
    EvidenceAvailability,
    EvidenceFetchRun,
    EvidenceMatchType,
    EvidenceReadQuery,
    EvidenceRepositoryPage,
    EvidenceRepositoryView,
    EvidenceRelation,
    EvidenceSource,
    EvidenceSourceType,
    FetchRunStatus,
    NormalizedEvidence,
    ParseAttempt,
    RawDocument,
    SecurityEvidenceView,
)
from app.v3.domain.recall import (
    PerformanceObservation,
    RawOpportunity,
    RawOpportunityReadItem,
    RawOpportunityReadPage,
    RecallChannel,
    RecallFeatureView,
    RecallReadItem,
    RecallReadPage,
    RecallResult,
    RecallRun,
    RecallRunStatus,
)
from app.v3.domain.hashing import canonical_hash, canonical_json
from app.v3.infrastructure.db.models import (
    AgentTaskModel,
    AdjustmentFactorModel,
    AdjustmentFactorRevisionModel,
    AuditEventModel,
    BarSeriesRevisionModel,
    CorporateActionModel,
    MarketBarModel,
    MarketDataIngestionRunModel,
    SecurityModel,
    UniverseDiffModel,
    UniverseMemberModel,
    UniverseSnapshotModel,
    UniverseSourceModel,
    FeatureRunModel,
    SecurityFeatureModel,
    MarketRegimeSnapshotModel,
    RecallChannelModel,
    RecallRunModel,
    RecallResultModel,
    RawOpportunityModel,
    PerformanceObservationModel,
    EvidenceSourceModel,
    RawDocumentModel,
    EvidenceRecordModel,
    RawDocumentParseAttemptModel,
    EvidenceEntityLinkModel,
    EvidenceFetchRunModel,
    EvidenceRelationModel,
    EvidenceConflictModel,
    EvidenceConflictMemberModel,
)


class SQLAlchemyAgentTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, task: AgentTask) -> bool:
        serialized_task = task.model_dump(mode="json")
        statement = (
            insert(AgentTaskModel)
            .values(
                task_id=task.task_id,
                task_run_id=task.task_run_id,
                task_type=task.task_type,
                subject=serialized_task["subject"],
                task_profile=task.task_profile,
                trigger_type=task.trigger_type,
                as_of=task.as_of,
                context_pack_id=task.context_pack_id,
                context_pack_hash=task.context_pack_hash,
                expected_result_type=task.expected_result_type,
                constraints=serialized_task["constraints"],
                content_hash=task.computed_content_hash(),
            )
            .on_conflict_do_nothing(index_elements=[AgentTaskModel.content_hash])
            .returning(AgentTaskModel.task_id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None


class SQLAlchemyAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: AuditEvent) -> None:
        self._session.add(
            AuditEventModel(
                audit_id=event.audit_id,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                action=event.action,
                object_type=event.object_type,
                object_id=event.object_id,
                request_id=event.request_id,
                before_hash=event.before_hash,
                after_hash=event.after_hash,
                result=event.result,
                event_time=event.event_time,
                metadata_payload=event.metadata,
            )
        )


class SQLAlchemyEvidenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_source(self, source: EvidenceSource) -> UUID:
        statement = (
            insert(EvidenceSourceModel)
            .values(
                evidence_source_id=source.evidence_source_id,
                code=source.code,
                source_type=source.source_type.value,
                upstream_source=source.upstream_source,
                capabilities=source.capabilities,
                priority=source.priority,
                rate_limit_per_minute=source.rate_limit_per_minute,
                parser_version=source.parser_version,
                reliability=source.reliability,
                enabled=source.enabled,
            )
            .on_conflict_do_update(
                index_elements=[EvidenceSourceModel.code],
                set_={
                    "source_type": source.source_type.value,
                    "upstream_source": source.upstream_source,
                    "capabilities": source.capabilities,
                    "priority": source.priority,
                    "rate_limit_per_minute": source.rate_limit_per_minute,
                    "parser_version": source.parser_version,
                    "reliability": source.reliability,
                    "enabled": source.enabled,
                },
            )
            .returning(EvidenceSourceModel.evidence_source_id)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def add_fetch_run(self, run: EvidenceFetchRun) -> None:
        self._session.add(EvidenceFetchRunModel(
            fetch_run_id=run.fetch_run_id,
            evidence_source_id=run.evidence_source_id,
            status=run.status.value,
            window_start=run.window_start,
            window_end=run.window_end,
            cursor=run.cursor,
            expected_count=run.expected_count,
            fetched_count=run.fetched_count,
            raw_inserted_count=run.raw_inserted_count,
            duplicate_count=run.duplicate_count,
            parsed_count=run.parsed_count,
            evidence_count=run.evidence_count,
            failed_count=run.failed_count,
            errors=run.errors,
            started_at=run.started_at,
            completed_at=run.completed_at,
            row_version=run.row_version,
        ))

    async def get_fetch_run(self, fetch_run_id: UUID) -> EvidenceFetchRun | None:
        model = await self._session.get(EvidenceFetchRunModel, fetch_run_id)
        return None if model is None else self._fetch_run(model)

    async def save_fetch_run(
        self, run: EvidenceFetchRun, *, expected_version: int
    ) -> bool:
        result = await self._session.execute(
            update(EvidenceFetchRunModel)
            .where(
                EvidenceFetchRunModel.fetch_run_id == run.fetch_run_id,
                EvidenceFetchRunModel.row_version == expected_version,
            )
            .values(
                status=run.status.value,
                cursor=run.cursor,
                expected_count=run.expected_count,
                fetched_count=run.fetched_count,
                raw_inserted_count=run.raw_inserted_count,
                duplicate_count=run.duplicate_count,
                parsed_count=run.parsed_count,
                evidence_count=run.evidence_count,
                failed_count=run.failed_count,
                errors=run.errors,
                completed_at=run.completed_at,
                row_version=run.row_version,
            )
        )
        return result.rowcount == 1

    async def add_raw_if_absent(self, document: RawDocument) -> bool:
        inserted = (
            await self._session.execute(
                insert(RawDocumentModel)
                .values(
                    raw_document_id=document.raw_document_id,
                    evidence_source_id=document.evidence_source_id,
                    document_key=document.document_key,
                    raw_reference=document.raw_reference,
                    normalized_reference=document.normalized_reference,
                    storage_path=document.storage_path,
                    mime_type=document.mime_type,
                    payload_text=document.payload_text,
                    payload_size=document.payload_size,
                    encoding=document.encoding,
                    response_metadata=document.response_metadata,
                    untrusted=document.untrusted,
                    fetch_time=document.fetch_time,
                    known_at=document.known_at,
                    content_hash=document.content_hash,
                )
                .on_conflict_do_nothing(
                    constraint="uq_raw_documents_source_document_content"
                )
                .returning(RawDocumentModel.raw_document_id)
            )
        ).scalar_one_or_none()
        return inserted is not None

    async def get_raw(self, raw_document_id: UUID) -> RawDocument | None:
        model = await self._session.get(RawDocumentModel, raw_document_id)
        return None if model is None else self._raw(model)

    async def find_raw(
        self, *, evidence_source_id: UUID, document_key: str, content_hash: str
    ) -> RawDocument | None:
        model = (
            await self._session.execute(
                select(RawDocumentModel).where(
                    RawDocumentModel.evidence_source_id == evidence_source_id,
                    RawDocumentModel.document_key == document_key,
                    RawDocumentModel.content_hash == content_hash,
                )
            )
        ).scalar_one_or_none()
        return None if model is None else self._raw(model)

    async def publish_parse(
        self,
        attempt: ParseAttempt,
        records: tuple[NormalizedEvidence, ...],
        links: tuple[EntityLink, ...],
        relations: tuple[EvidenceRelation, ...] = (),
        conflicts: tuple[EvidenceConflict, ...] = (),
    ) -> bool:
        attempt_inserted = (
            await self._session.execute(
                insert(RawDocumentParseAttemptModel)
                .values(
                    parse_attempt_id=attempt.parse_attempt_id,
                    raw_document_id=attempt.raw_document_id,
                    parser_code=attempt.parser_code,
                    parser_version=attempt.parser_version,
                    status=attempt.status.value,
                    output_count=attempt.output_count,
                    error=attempt.error,
                    started_at=attempt.started_at,
                    completed_at=attempt.completed_at,
                    content_hash=attempt.content_hash,
                )
                .on_conflict_do_nothing(
                    constraint="uq_raw_document_parse_attempts_document_parser"
                )
                .returning(RawDocumentParseAttemptModel.parse_attempt_id)
            )
        ).scalar_one_or_none()
        if attempt_inserted is None:
            return False
        for record in records:
            inserted = (
                await self._session.execute(
                    insert(EvidenceRecordModel)
                    .values(**self._record_values(record))
                    .on_conflict_do_nothing(
                        constraint="uq_evidence_records_raw_parser_content"
                    )
                    .returning(EvidenceRecordModel.evidence_id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                raise RuntimeError("new parse attempt produced an existing evidence identity")
        for link in links:
            inserted = (
                await self._session.execute(
                    insert(EvidenceEntityLinkModel)
                    .values(
                        entity_link_id=link.entity_link_id,
                        evidence_id=link.evidence_id,
                        entity_type=link.entity_type,
                        entity_id=link.entity_id,
                        match_basis=link.match_basis,
                        confidence=link.confidence,
                        status=link.status.value,
                        content_hash=link.content_hash,
                    )
                    .on_conflict_do_nothing(
                        constraint="uq_evidence_entity_links_evidence_entity"
                    )
                    .returning(EvidenceEntityLinkModel.entity_link_id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                raise RuntimeError("new parse attempt produced a duplicate entity link")
        for relation in relations:
            await self._session.execute(
                insert(EvidenceRelationModel)
                .values(
                    relation_id=relation.relation_id,
                    from_evidence_id=relation.from_evidence_id,
                    to_evidence_id=relation.to_evidence_id,
                    relation_type=relation.relation_type.value,
                    similarity=relation.similarity,
                    reason=relation.reason,
                    content_hash=relation.content_hash,
                )
                .on_conflict_do_nothing(
                    constraint="uq_evidence_relations_pair_type"
                )
            )
        for conflict in conflicts:
            inserted_conflict = (
                await self._session.execute(
                    insert(EvidenceConflictModel)
                    .values(
                        conflict_id=conflict.conflict_id,
                        subject_type=conflict.subject_type,
                        subject_id=conflict.subject_id,
                        claim_key=conflict.claim_key,
                        status=conflict.status.value,
                        selected_evidence_id=conflict.selected_evidence_id,
                        resolution=conflict.resolution,
                        content_hash=conflict.content_hash,
                    )
                    .on_conflict_do_nothing(
                        constraint="uq_evidence_conflicts_claim_content"
                    )
                    .returning(EvidenceConflictModel.conflict_id)
                )
            ).scalar_one_or_none()
            if inserted_conflict is None:
                continue
            member_models = (
                await self._session.execute(
                    select(EvidenceRecordModel).where(
                        EvidenceRecordModel.evidence_id.in_(conflict.member_ids)
                    )
                )
            ).scalars().all()
            if len(member_models) != len(conflict.member_ids):
                raise RuntimeError("conflict references unavailable evidence")
            for member in member_models:
                self._session.add(EvidenceConflictMemberModel(
                    conflict_id=conflict.conflict_id,
                    evidence_id=member.evidence_id,
                    value_hash=canonical_hash(member.normalized_payload),
                    source_priority=member.source_priority,
                    confidence=member.confidence,
                ))
        return True

    async def records_for_claim(
        self, *, subject_type: str, subject_id: str, claim_key: str, as_of: datetime
    ) -> tuple[NormalizedEvidence, ...]:
        models = (
            await self._session.execute(
                select(EvidenceRecordModel)
                .where(
                    EvidenceRecordModel.subject_type == subject_type,
                    EvidenceRecordModel.subject_id == subject_id,
                    EvidenceRecordModel.claim_key == claim_key,
                    EvidenceRecordModel.known_at <= as_of,
                )
                .order_by(EvidenceRecordModel.known_at.desc(), EvidenceRecordModel.evidence_id)
            )
        ).scalars().all()
        return tuple(self._record(model) for model in models)

    async def retrieve(
        self, *, subject_type: str, subject_id: str, as_of: datetime, limit: int
    ) -> tuple[NormalizedEvidence, ...]:
        models = (
            await self._session.execute(
                select(EvidenceRecordModel)
                .where(
                    EvidenceRecordModel.subject_type == subject_type,
                    EvidenceRecordModel.subject_id == subject_id,
                    EvidenceRecordModel.known_at <= as_of,
                    EvidenceRecordModel.availability == EvidenceAvailability.AVAILABLE.value,
                    or_(EvidenceRecordModel.expire_at.is_(None), EvidenceRecordModel.expire_at >= as_of),
                )
                .order_by(
                    EvidenceRecordModel.relevance.desc(),
                    EvidenceRecordModel.confidence.desc(),
                    EvidenceRecordModel.known_at.desc(),
                    EvidenceRecordModel.evidence_id,
                )
                .limit(limit)
            )
        ).scalars().all()
        return tuple(self._record(model) for model in models)

    async def retrieve_view(
        self, *, query: EvidenceReadQuery
    ) -> EvidenceRepositoryPage:
        confirmed_link = exists().where(
            EvidenceEntityLinkModel.evidence_id == EvidenceRecordModel.evidence_id,
            EvidenceEntityLinkModel.entity_type == query.subject_type,
            EvidenceEntityLinkModel.entity_id == query.subject_id,
            EvidenceEntityLinkModel.status == "CONFIRMED",
        )
        candidate_link = exists().where(
            EvidenceEntityLinkModel.evidence_id == EvidenceRecordModel.evidence_id,
            EvidenceEntityLinkModel.entity_type == query.subject_type,
            EvidenceEntityLinkModel.entity_id == query.subject_id,
            EvidenceEntityLinkModel.status == "CANDIDATE",
        )
        direct = and_(
            EvidenceRecordModel.subject_type == query.subject_type,
            EvidenceRecordModel.subject_id == query.subject_id,
        )
        conflict_status = (
            select(EvidenceConflictModel.status)
            .join(
                EvidenceConflictMemberModel,
                EvidenceConflictMemberModel.conflict_id == EvidenceConflictModel.conflict_id,
            )
            .where(EvidenceConflictMemberModel.evidence_id == EvidenceRecordModel.evidence_id)
            .order_by(EvidenceConflictModel.created_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        match_type = (
            select(EvidenceEntityLinkModel.status)
            .where(
                EvidenceEntityLinkModel.evidence_id == EvidenceRecordModel.evidence_id,
                EvidenceEntityLinkModel.entity_type == query.subject_type,
                EvidenceEntityLinkModel.entity_id == query.subject_id,
                EvidenceEntityLinkModel.status.in_(("CONFIRMED", "CANDIDATE")),
            )
            .order_by(EvidenceEntityLinkModel.confidence.desc())
            .limit(1)
            .scalar_subquery()
        )
        relevance_age_days = func.greatest(
            0.0,
            func.extract(
                "epoch",
                query.as_of - func.coalesce(
                    EvidenceRecordModel.event_time,
                    EvidenceRecordModel.publish_time,
                    EvidenceRecordModel.known_at,
                ),
            ) / 86400.0,
        )
        effective_relevance = case(
            (
                EvidenceRecordModel.decay_model == DecayModel.LINEAR.value,
                func.greatest(
                    0.0,
                    EvidenceRecordModel.relevance * (
                        1 - func.coalesce(EvidenceRecordModel.decay_rate, 0) * relevance_age_days
                    ),
                ),
            ),
            (
                EvidenceRecordModel.decay_model == DecayModel.EXPONENTIAL.value,
                EvidenceRecordModel.relevance * func.exp(
                    -func.coalesce(EvidenceRecordModel.decay_rate, 0) * relevance_age_days
                ),
            ),
            else_=EvidenceRecordModel.relevance,
        )
        filters = [
            or_(direct, confirmed_link, candidate_link if query.include_candidates else False),
            EvidenceRecordModel.known_at <= query.as_of,
            EvidenceRecordModel.availability == EvidenceAvailability.AVAILABLE.value,
            or_(EvidenceRecordModel.expire_at.is_(None), EvidenceRecordModel.expire_at >= query.as_of),
            effective_relevance > 0,
            effective_relevance >= query.min_effective_relevance,
        ]
        if query.evidence_types:
            filters.append(EvidenceRecordModel.evidence_type.in_(tuple(query.evidence_types)))
        if query.source_types:
            filters.append(EvidenceRecordModel.source_type.in_(tuple(query.source_types)))
        rows = (
            await self._session.execute(
                select(EvidenceRecordModel, match_type, conflict_status)
                .where(*filters)
                .order_by(
                    effective_relevance.desc(),
                    EvidenceRecordModel.source_priority,
                    EvidenceRecordModel.known_at.desc(),
                    EvidenceRecordModel.evidence_id,
                )
                .limit(query.limit)
            )
        ).all()
        result = []
        for model, link_status, current_conflict_status in rows:
            if model.subject_type == query.subject_type and model.subject_id == query.subject_id:
                matched_by = EvidenceMatchType.DIRECT
            elif link_status == "CONFIRMED":
                matched_by = EvidenceMatchType.CONFIRMED_LINK
            else:
                matched_by = EvidenceMatchType.CANDIDATE_LINK
            result.append(EvidenceRepositoryView(
                record=self._record(model),
                match_type=matched_by,
                conflict_status=current_conflict_status or "NONE",
            ))
        coverage_rows = (
            await self._session.execute(
                select(EvidenceRecordModel.evidence_type, func.count())
                .where(*filters)
                .group_by(EvidenceRecordModel.evidence_type)
            )
        ).all()
        return EvidenceRepositoryPage(
            views=tuple(result),
            coverage_counts={EvidenceType(kind): count for kind, count in coverage_rows},
        )

    async def for_securities(
        self, security_ids: tuple[UUID, ...], *, as_of: datetime
    ) -> tuple[SecurityEvidenceView, ...]:
        if not security_ids:
            return ()
        securities = (
            await self._session.execute(
                select(SecurityModel.security_id, SecurityModel.market, SecurityModel.code)
                .where(SecurityModel.security_id.in_(security_ids))
            )
        ).all()
        subject_to_security = {
            f"{market}:{code}": security_id for security_id, market, code in securities
        }
        subjects = tuple(subject_to_security)
        if not subjects:
            return ()
        base_filters = (
            EvidenceRecordModel.known_at <= as_of,
            EvidenceRecordModel.availability == EvidenceAvailability.AVAILABLE.value,
            or_(EvidenceRecordModel.expire_at.is_(None), EvidenceRecordModel.expire_at >= as_of),
        )
        direct = (
            await self._session.execute(
                select(EvidenceRecordModel)
                .where(
                    EvidenceRecordModel.subject_type == "SECURITY",
                    EvidenceRecordModel.subject_id.in_(subjects),
                    *base_filters,
                )
            )
        ).scalars().all()
        linked = (
            await self._session.execute(
                select(EvidenceEntityLinkModel.entity_id, EvidenceRecordModel)
                .join(
                    EvidenceRecordModel,
                    EvidenceRecordModel.evidence_id == EvidenceEntityLinkModel.evidence_id,
                )
                .where(
                    EvidenceEntityLinkModel.entity_type == "SECURITY",
                    EvidenceEntityLinkModel.entity_id.in_(subjects),
                    EvidenceEntityLinkModel.status == "CONFIRMED",
                    *base_filters,
                )
            )
        ).all()
        found = {}
        for model in direct:
            security_id = subject_to_security[model.subject_id]
            found[(security_id, model.evidence_id)] = self._record(model)
        for subject_id, model in linked:
            security_id = subject_to_security[subject_id]
            found[(security_id, model.evidence_id)] = self._record(model)
        return tuple(
            SecurityEvidenceView(
                security_id=security_id,
                record=record,
                effective_relevance=record.effective_relevance(as_of),
            )
            for (security_id, _), record in sorted(
                found.items(), key=lambda item: (str(item[0][0]), item[1].known_at, str(item[0][1]))
            )
            if record.effective_relevance(as_of) > 0
        )

    @staticmethod
    def _raw(model: RawDocumentModel) -> RawDocument:
        return RawDocument(
            raw_document_id=model.raw_document_id,
            evidence_source_id=model.evidence_source_id,
            document_key=model.document_key,
            raw_reference=model.raw_reference,
            normalized_reference=model.normalized_reference,
            storage_path=model.storage_path,
            mime_type=model.mime_type,
            payload_text=model.payload_text,
            payload_size=model.payload_size,
            encoding=model.encoding,
            response_metadata=model.response_metadata,
            untrusted=model.untrusted,
            fetch_time=model.fetch_time,
            known_at=model.known_at,
            content_hash=model.content_hash,
        )

    @staticmethod
    def _fetch_run(model: EvidenceFetchRunModel) -> EvidenceFetchRun:
        return EvidenceFetchRun(
            fetch_run_id=model.fetch_run_id,
            evidence_source_id=model.evidence_source_id,
            status=FetchRunStatus(model.status),
            window_start=model.window_start,
            window_end=model.window_end,
            cursor=model.cursor,
            expected_count=model.expected_count,
            fetched_count=model.fetched_count,
            raw_inserted_count=model.raw_inserted_count,
            duplicate_count=model.duplicate_count,
            parsed_count=model.parsed_count,
            evidence_count=model.evidence_count,
            failed_count=model.failed_count,
            errors=model.errors,
            started_at=model.started_at,
            completed_at=model.completed_at,
            row_version=model.row_version,
        )

    @staticmethod
    def _record_values(record: NormalizedEvidence) -> dict[str, object]:
        return {
            "evidence_id": record.evidence_id,
            "raw_document_id": record.raw_document_id,
            "evidence_type": record.evidence_type.value,
            "source_type": record.source_type.value,
            "source_priority": record.source_priority,
            "subject_type": record.subject_type,
            "subject_id": record.subject_id,
            "claim_key": record.claim_key,
            "source": record.source,
            "upstream_source": record.upstream_source,
            "payload": record.payload,
            "normalized_payload": record.normalized_payload,
            "event_time": record.event_time,
            "publish_time": record.publish_time,
            "fetch_time": record.fetch_time,
            "known_at": record.known_at,
            "confidence": record.confidence,
            "relevance": record.relevance,
            "expire_at": record.expire_at,
            "decay_model": record.decay_model.value,
            "decay_rate": record.decay_rate,
            "availability": record.availability.value,
            "untrusted": record.untrusted,
            "conflict_state": record.conflict_state,
            "parser_version": record.parser_version,
            "supersedes_evidence_id": record.supersedes_evidence_id,
            "content_hash": record.content_hash,
        }

    @staticmethod
    def _record(model: EvidenceRecordModel) -> NormalizedEvidence:
        return NormalizedEvidence(
            evidence_id=model.evidence_id,
            raw_document_id=model.raw_document_id,
            evidence_type=EvidenceType(model.evidence_type),
            source_type=EvidenceSourceType(model.source_type),
            source_priority=model.source_priority,
            subject_type=model.subject_type,
            subject_id=model.subject_id,
            claim_key=model.claim_key,
            source=model.source,
            upstream_source=model.upstream_source,
            payload=model.payload,
            normalized_payload=model.normalized_payload,
            event_time=model.event_time,
            publish_time=model.publish_time,
            fetch_time=model.fetch_time,
            known_at=model.known_at,
            confidence=float(model.confidence),
            relevance=float(model.relevance),
            expire_at=model.expire_at,
            decay_model=DecayModel(model.decay_model),
            decay_rate=None if model.decay_rate is None else float(model.decay_rate),
            availability=EvidenceAvailability(model.availability),
            untrusted=model.untrusted,
            conflict_state=model.conflict_state,
            parser_version=model.parser_version,
            supersedes_evidence_id=model.supersedes_evidence_id,
            content_hash=model.content_hash,
        )


class SQLAlchemyUniverseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest(self) -> UniverseSnapshot | None:
        row = (
            await self._session.execute(
                select(UniverseSnapshotModel, UniverseSourceModel.code)
                .join(UniverseSourceModel, UniverseSourceModel.source_id == UniverseSnapshotModel.source_id)
                .order_by(UniverseSnapshotModel.known_at.desc(), UniverseSnapshotModel.created_at.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        snapshot, source_code = row
        member_rows = (
            await self._session.execute(
                select(UniverseMemberModel, SecurityModel)
                .join(SecurityModel, SecurityModel.security_id == UniverseMemberModel.security_id)
                .where(UniverseMemberModel.snapshot_id == snapshot.snapshot_id)
                .order_by(SecurityModel.market, SecurityModel.code)
            )
        ).all()
        return UniverseSnapshot(
            snapshot_id=snapshot.snapshot_id,
            source_code=source_code,
            status=snapshot.status,
            as_of=snapshot.as_of,
            fetch_time=snapshot.fetch_time,
            known_at=snapshot.known_at,
            coverage=float(snapshot.coverage),
            stale=snapshot.stale,
            previous_snapshot_id=snapshot.previous_snapshot_id,
            members=tuple(
                SecurityMember(
                    code=security.code,
                    market=Market(security.market),
                    name=member.name,
                    trading_status=member.trading_status,
                    is_st=member.is_st,
                    suspended=member.suspended,
                    is_new_listing=member.is_new_listing,
                    delisting_risk=member.delisting_risk,
                    raw_reference=member.raw_reference,
                )
                for member, security in member_rows
            ),
            content_hash=snapshot.content_hash,
        )

    async def publish(self, snapshot: UniverseSnapshot) -> bool:
        source_id = (
            await self._session.execute(
                insert(UniverseSourceModel)
                .values(
                    code=snapshot.source_code,
                    source_type="MARKET_UNIVERSE",
                    priority={"PRIMARY": 1, "SECONDARY": 2, "LKG": 3}[snapshot.status.value],
                    capability_version="v3-phase2",
                )
                .on_conflict_do_update(
                    index_elements=[UniverseSourceModel.code],
                    set_={"enabled": True},
                )
                .returning(UniverseSourceModel.source_id)
            )
        ).scalar_one()
        inserted = (
            await self._session.execute(
                insert(UniverseSnapshotModel)
                .values(
                    snapshot_id=snapshot.snapshot_id,
                    source_id=source_id,
                    as_of=snapshot.as_of,
                    fetch_time=snapshot.fetch_time,
                    known_at=snapshot.known_at,
                    coverage=snapshot.coverage,
                    stale=snapshot.stale,
                    content_hash=snapshot.content_hash,
                    previous_snapshot_id=snapshot.previous_snapshot_id,
                    status=snapshot.status.value,
                )
                .on_conflict_do_nothing(index_elements=[UniverseSnapshotModel.content_hash])
                .returning(UniverseSnapshotModel.snapshot_id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            return False

        previous = await self._members_by_key(snapshot.previous_snapshot_id)
        current: dict[tuple[str, str], tuple[SecurityMember, UUID]] = {}
        for member in snapshot.members:
            security_id = (
                await self._session.execute(
                    insert(SecurityModel)
                    .values(code=member.code, market=member.market.value, name=member.name)
                    .on_conflict_do_update(
                        index_elements=[SecurityModel.market, SecurityModel.code],
                        set_={"name": member.name},
                    )
                    .returning(SecurityModel.security_id)
                )
            ).scalar_one()
            current[(member.market.value, member.code)] = (member, security_id)
            self._session.add(
                UniverseMemberModel(
                    snapshot_id=snapshot.snapshot_id,
                    security_id=security_id,
                    **self._member_values(member),
                )
            )

        for key in sorted(previous.keys() | current.keys()):
            before = previous.get(key)
            after = current.get(key)
            before_payload = self._member_values(before[0]) if before else None
            after_payload = self._member_values(after[0]) if after else None
            if before_payload == after_payload:
                continue
            change_type = "ADDED" if before is None else "REMOVED" if after is None else "CHANGED"
            security_id = (after or before)[1]
            self._session.add(
                UniverseDiffModel(
                    snapshot_id=snapshot.snapshot_id,
                    previous_snapshot_id=snapshot.previous_snapshot_id,
                    security_id=security_id,
                    change_type=change_type,
                    before_value=before_payload,
                    after_value=after_payload,
                    reason="snapshot membership comparison",
                )
            )
        return True

    async def targets(self, snapshot_id) -> tuple[BarIngestionTarget, ...]:
        rows = (
            await self._session.execute(
                select(UniverseMemberModel, SecurityModel)
                .join(SecurityModel, SecurityModel.security_id == UniverseMemberModel.security_id)
                .where(UniverseMemberModel.snapshot_id == snapshot_id)
                .order_by(SecurityModel.market, SecurityModel.code)
            )
        ).all()
        return tuple(
            BarIngestionTarget(
                security_id=security.security_id,
                code=security.code,
                market=Market(security.market),
                suspended=member.suspended,
                is_new_listing=member.is_new_listing,
            )
            for member, security in rows
        )

    async def _members_by_key(
        self, snapshot_id
    ) -> dict[tuple[str, str], tuple[SecurityMember, UUID]]:
        if snapshot_id is None:
            return {}
        rows = (
            await self._session.execute(
                select(UniverseMemberModel, SecurityModel)
                .join(SecurityModel, SecurityModel.security_id == UniverseMemberModel.security_id)
                .where(UniverseMemberModel.snapshot_id == snapshot_id)
            )
        ).all()
        return {
            (security.market, security.code): (
                SecurityMember(
                    code=security.code,
                    market=Market(security.market),
                    **self._member_values_from_model(member),
                ),
                security.security_id,
            )
            for member, security in rows
        }

    @staticmethod
    def _member_values(member: SecurityMember) -> dict:
        return member.model_dump(mode="json", exclude={"code", "market"})

    @staticmethod
    def _member_values_from_model(member: UniverseMemberModel) -> dict:
        return {
            "name": member.name,
            "trading_status": member.trading_status,
            "is_st": member.is_st,
            "suspended": member.suspended,
            "is_new_listing": member.is_new_listing,
            "delisting_risk": member.delisting_risk,
            "raw_reference": member.raw_reference,
        }


class SQLAlchemyBarRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_factor_revision_id(self, security_id: UUID) -> UUID | None:
        return (
            await self._session.execute(
                select(AdjustmentFactorRevisionModel.factor_revision_id)
                .where(AdjustmentFactorRevisionModel.security_id == security_id)
                .order_by(
                    AdjustmentFactorRevisionModel.known_at.desc(),
                    AdjustmentFactorRevisionModel.factor_revision_id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    async def latest_series_revision_ids(
        self, security_id: UUID
    ) -> dict[tuple[BarPeriod, AdjustType], UUID]:
        ranked = (
            select(
                BarSeriesRevisionModel.revision_id,
                BarSeriesRevisionModel.period,
                BarSeriesRevisionModel.adjust_type,
                func.row_number()
                .over(
                    partition_by=(
                        BarSeriesRevisionModel.period,
                        BarSeriesRevisionModel.adjust_type,
                    ),
                    order_by=(
                        BarSeriesRevisionModel.known_at.desc(),
                        BarSeriesRevisionModel.revision_id.desc(),
                    ),
                )
                .label("revision_rank"),
            )
            .where(
                BarSeriesRevisionModel.security_id == security_id,
                BarSeriesRevisionModel.status == "PUBLISHED",
            )
            .subquery()
        )
        rows = (
            await self._session.execute(select(ranked).where(ranked.c.revision_rank == 1))
        ).mappings()
        return {
            (BarPeriod(row["period"]), AdjustType(row["adjust_type"])): row[
                "revision_id"
            ]
            for row in rows
        }

    async def publish_factor_revision(self, revision: AdjustmentFactorRevision) -> bool:
        inserted = (
            await self._session.execute(
                insert(AdjustmentFactorRevisionModel)
                .values(
                    factor_revision_id=revision.factor_revision_id,
                    security_id=revision.security_id,
                    source=revision.source,
                    upstream_source=revision.upstream_source,
                    derivation_method=revision.derivation_method,
                    fetch_time=revision.fetch_time,
                    known_at=revision.known_at,
                    content_hash=revision.content_hash,
                    supersedes_revision_id=revision.supersedes_revision_id,
                )
                .on_conflict_do_nothing(index_elements=[AdjustmentFactorRevisionModel.content_hash])
                .returning(AdjustmentFactorRevisionModel.factor_revision_id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            return False
        self._session.add_all(
            AdjustmentFactorModel(
                factor_revision_id=revision.factor_revision_id,
                trading_time=item.trading_time,
                factor=item.factor,
            )
            for item in revision.factors
        )
        return True

    async def publish_series_revision(self, revision: BarSeriesRevision) -> bool:
        inserted = (
            await self._session.execute(
                insert(BarSeriesRevisionModel)
                .values(
                    revision_id=revision.revision_id,
                    security_id=revision.security_id,
                    period=revision.period.value,
                    adjust_type=revision.adjust_type.value,
                    source=revision.source,
                    upstream_source=revision.upstream_source,
                    raw_bar_available=revision.raw_bar_available,
                    factor_revision_id=revision.factor_revision_id,
                    point_in_time_precision=revision.point_in_time_precision.value,
                    precision_reason=revision.precision_reason,
                    known_at=revision.known_at,
                    content_hash=revision.content_hash,
                    supersedes_revision_id=revision.supersedes_revision_id,
                    status="PUBLISHED",
                )
                .on_conflict_do_nothing(index_elements=[BarSeriesRevisionModel.content_hash])
                .returning(BarSeriesRevisionModel.revision_id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            return False
        self._session.add_all(
            MarketBarModel(
                revision_id=revision.revision_id,
                bar_time=item.bar_time,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=item.volume,
                amount=item.amount,
                provisional=item.provisional,
                event_time=item.bar_time,
                fetch_time=item.fetch_time,
            )
            for item in revision.bars
        )
        return True

    async def has_daily_coverage(
        self, security_id: UUID, *, minimum_bars: int, minimum_last_bar_date: date
    ) -> bool:
        row = (
            await self._session.execute(
                select(
                    BarSeriesRevisionModel.raw_bar_available,
                    func.count(MarketBarModel.bar_time),
                    func.max(MarketBarModel.bar_time),
                )
                .join(MarketBarModel, MarketBarModel.revision_id == BarSeriesRevisionModel.revision_id)
                .where(
                    BarSeriesRevisionModel.security_id == security_id,
                    BarSeriesRevisionModel.period == "DAY",
                    BarSeriesRevisionModel.adjust_type == "QFQ",
                    BarSeriesRevisionModel.status == "PUBLISHED",
                )
                .group_by(
                    BarSeriesRevisionModel.revision_id,
                    BarSeriesRevisionModel.raw_bar_available,
                    BarSeriesRevisionModel.known_at,
                )
                .order_by(BarSeriesRevisionModel.known_at.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return False
        raw_available, count, last_bar_time = row
        return bool(
            raw_available
            and count >= minimum_bars
            and last_bar_time.astimezone(SHANGHAI).date() >= minimum_last_bar_date
        )

    async def covered_daily_security_ids(
        self,
        targets: tuple[BarIngestionTarget, ...],
        *,
        minimum_bars: int,
        minimum_last_bar_date: date,
    ) -> set[UUID]:
        if not targets:
            return set()
        security_ids = [target.security_id for target in targets]
        ranked = (
            select(
                BarSeriesRevisionModel.revision_id,
                BarSeriesRevisionModel.security_id,
                BarSeriesRevisionModel.adjust_type,
                BarSeriesRevisionModel.raw_bar_available,
                func.row_number()
                .over(
                    partition_by=(
                        BarSeriesRevisionModel.security_id,
                        BarSeriesRevisionModel.adjust_type,
                    ),
                    order_by=(
                        BarSeriesRevisionModel.known_at.desc(),
                        BarSeriesRevisionModel.revision_id.desc(),
                    ),
                )
                .label("revision_rank"),
            )
            .where(
                BarSeriesRevisionModel.security_id.in_(security_ids),
                BarSeriesRevisionModel.period == "DAY",
                BarSeriesRevisionModel.adjust_type.in_(("QFQ", "HFQ")),
                BarSeriesRevisionModel.status == "PUBLISHED",
            )
            .subquery()
        )
        rows = (
            await self._session.execute(
                select(
                    ranked.c.security_id,
                    ranked.c.adjust_type,
                    ranked.c.raw_bar_available,
                    func.count(MarketBarModel.bar_time),
                    func.max(MarketBarModel.bar_time),
                )
                .join(MarketBarModel, MarketBarModel.revision_id == ranked.c.revision_id)
                .where(ranked.c.revision_rank == 1)
                .group_by(
                    ranked.c.security_id,
                    ranked.c.adjust_type,
                    ranked.c.raw_bar_available,
                )
            )
        ).all()
        required_dates = {
            target.security_id: (
                date(1900, 1, 1) if target.suspended else minimum_last_bar_date
            )
            for target in targets
        }
        valid_adjustments: dict[UUID, set[str]] = {}
        for security_id, adjust_type, raw_available, count, last_bar_time in rows:
            if (
                raw_available
                and count >= minimum_bars
                and last_bar_time.astimezone(SHANGHAI).date()
                >= required_dates[security_id]
            ):
                valid_adjustments.setdefault(security_id, set()).add(adjust_type)
        return {
            security_id
            for security_id, adjustments in valid_adjustments.items()
            if adjustments == {"QFQ", "HFQ"}
        }

    async def latest_daily_revisions(
        self, security_ids: tuple[UUID, ...], *, as_of: datetime
    ) -> tuple[BarSeriesRevision, ...]:
        if not security_ids:
            return ()
        ranked = (
            select(
                BarSeriesRevisionModel.revision_id,
                func.row_number().over(
                    partition_by=BarSeriesRevisionModel.security_id,
                    order_by=(BarSeriesRevisionModel.known_at.desc(), BarSeriesRevisionModel.revision_id.desc()),
                ).label("revision_rank"),
            )
            .where(
                BarSeriesRevisionModel.security_id.in_(security_ids),
                BarSeriesRevisionModel.period == "DAY",
                BarSeriesRevisionModel.adjust_type == "QFQ",
                BarSeriesRevisionModel.status == "PUBLISHED",
                BarSeriesRevisionModel.known_at <= as_of,
            )
            .subquery()
        )
        revision_ids = select(ranked.c.revision_id).where(ranked.c.revision_rank == 1)
        rows = (
            await self._session.execute(
                select(BarSeriesRevisionModel, MarketBarModel)
                .join(MarketBarModel, MarketBarModel.revision_id == BarSeriesRevisionModel.revision_id)
                .where(
                    BarSeriesRevisionModel.revision_id.in_(revision_ids),
                    MarketBarModel.bar_time <= as_of,
                )
                .order_by(BarSeriesRevisionModel.security_id, MarketBarModel.bar_time)
            )
        ).all()
        grouped: dict[UUID, tuple[BarSeriesRevisionModel, list[MarketBar]]] = {}
        for revision, bar in rows:
            if revision.revision_id not in grouped:
                grouped[revision.revision_id] = (revision, [])
            grouped[revision.revision_id][1].append(MarketBar(
                bar_time=bar.bar_time, open=float(bar.open), high=float(bar.high),
                low=float(bar.low), close=float(bar.close), volume=bar.volume,
                amount=None if bar.amount is None else float(bar.amount),
                provisional=bar.provisional, fetch_time=bar.fetch_time,
            ))
        return tuple(
            BarSeriesRevision.build(BarSeriesRevisionContent(
                revision_id=model.revision_id, security_id=model.security_id,
                period=BarPeriod(model.period), adjust_type=AdjustType(model.adjust_type),
                source=model.source, upstream_source=model.upstream_source,
                raw_bar_available=model.raw_bar_available,
                factor_revision_id=model.factor_revision_id,
                point_in_time_precision=PointInTimePrecision(model.point_in_time_precision),
                precision_reason=model.precision_reason, known_at=model.known_at,
                supersedes_revision_id=model.supersedes_revision_id, bars=tuple(bars),
            ))
            for model, bars in grouped.values()
        )


class SQLAlchemyFeatureRepository:
    FIELD_COLUMNS = {
        "code": SecurityModel.code,
        "market": SecurityModel.market,
        "name": SecurityModel.name,
        "close": SecurityFeatureModel.close,
        "return_3d": SecurityFeatureModel.return_3d,
        "return_5d": SecurityFeatureModel.return_5d,
        "return_10d": SecurityFeatureModel.return_10d,
        "return_20d": SecurityFeatureModel.return_20d,
        "return_60d": SecurityFeatureModel.return_60d,
        "return_120d": SecurityFeatureModel.return_120d,
        "return_250d": SecurityFeatureModel.return_250d,
        "position_60d": SecurityFeatureModel.position_60d,
        "position_120d": SecurityFeatureModel.position_120d,
        "position_250d": SecurityFeatureModel.position_250d,
        "ma5": SecurityFeatureModel.ma5,
        "ma10": SecurityFeatureModel.ma10,
        "ma20": SecurityFeatureModel.ma20,
        "ma60": SecurityFeatureModel.ma60,
        "atr_pct": SecurityFeatureModel.atr_pct,
        "volatility20": SecurityFeatureModel.volatility20,
        "amount": SecurityFeatureModel.amount,
        "volume_ratio_5d": SecurityFeatureModel.volume_ratio_5d,
        "coverage": SecurityFeatureModel.coverage,
        "stale": SecurityFeatureModel.stale,
        "missing_fields": SecurityFeatureModel.missing_fields,
        "quality": SecurityFeatureModel.quality,
        "features": SecurityFeatureModel.features,
    }
    DEFAULT_FIELDS = ("code", "market", "name", "close", "return_20d", "position_60d", "amount", "coverage", "stale")

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def publish(
        self,
        run: FeatureRun,
        features: tuple[SecurityFeature, ...],
        regime: MarketRegimeSnapshot,
    ) -> bool:
        if run.status.value != "PUBLISHED":
            raise ValueError("only complete PUBLISHED feature runs can be persisted")
        if len(features) != run.successful_count:
            raise ValueError("feature count does not match run successful_count")
        inserted = (
            await self._session.execute(
                insert(FeatureRunModel).values(**run.model_dump(mode="python"))
                .on_conflict_do_nothing(index_elements=[FeatureRunModel.content_hash])
                .returning(FeatureRunModel.feature_run_id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            return False
        feature_models = []
        for item in features:
            payload = item.model_dump(mode="json")
            payload.update({
                "feature_run_id": item.feature_run_id,
                "security_id": item.security_id,
                "series_revision_id": item.series_revision_id,
                "factor_revision_id": item.factor_revision_id,
                "as_of": item.as_of,
            })
            feature_models.append(SecurityFeatureModel(**payload))
        self._session.add_all(feature_models)
        regime_payload = regime.model_dump(mode="python")
        regime_payload["domestic_risk_evidence_ids"] = [
            str(value) for value in regime.domestic_risk_evidence_ids
        ]
        regime_payload["global_risk_evidence_ids"] = [
            str(value) for value in regime.global_risk_evidence_ids
        ]
        self._session.add(MarketRegimeSnapshotModel(**regime_payload))
        return True

    async def query(self, query: FeatureQuery) -> FeaturePage | None:
        run_statement = select(FeatureRunModel).where(FeatureRunModel.status == "PUBLISHED")
        if query.feature_run_id is not None:
            run_statement = run_statement.where(FeatureRunModel.feature_run_id == query.feature_run_id)
        run = (
            await self._session.execute(
                run_statement.order_by(FeatureRunModel.as_of.desc(), FeatureRunModel.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if run is None:
            return None
        fields = query.fields or self.DEFAULT_FIELDS
        invalid = sorted(set(fields) - self.FIELD_COLUMNS.keys())
        if invalid:
            raise ValueError(f"unsupported feature fields: {', '.join(invalid)}")
        sort_column = self.FIELD_COLUMNS[query.sort_by.value]
        filters = [SecurityFeatureModel.feature_run_id == run.feature_run_id]
        if query.market:
            filters.append(SecurityModel.market == query.market)
        if query.stale is not None:
            filters.append(SecurityFeatureModel.stale == query.stale)
        if query.min_value is not None:
            filters.append(sort_column >= query.min_value)
        if query.max_value is not None:
            filters.append(sort_column <= query.max_value)
        count_filters = tuple(filters)
        cursor_value, cursor_security_id = self._decode_cursor(query.cursor, query.sort_by)
        if cursor_security_id is not None:
            if cursor_value is None:
                filters.append(and_(sort_column.is_(None), SecurityFeatureModel.security_id > cursor_security_id))
            else:
                comparison = sort_column < cursor_value if query.descending else sort_column > cursor_value
                filters.append(or_(
                    comparison,
                    and_(sort_column == cursor_value, SecurityFeatureModel.security_id > cursor_security_id),
                    sort_column.is_(None),
                ))
        selected = [self.FIELD_COLUMNS[field].label(field) for field in fields]
        statement = (
            select(SecurityFeatureModel.security_id, *selected, sort_column.label("_sort_value"))
            .join(SecurityModel, SecurityModel.security_id == SecurityFeatureModel.security_id)
            .where(*filters)
        )
        ordering = sort_column.desc().nullslast() if query.descending else sort_column.asc().nullslast()
        rows = (await self._session.execute(
            statement.order_by(ordering, SecurityFeatureModel.security_id.asc()).limit(query.limit + 1)
        )).mappings().all()
        count = await self._session.scalar(
            select(func.count()).select_from(SecurityFeatureModel)
            .join(SecurityModel, SecurityModel.security_id == SecurityFeatureModel.security_id)
            .where(*count_filters)
        )
        has_more = len(rows) > query.limit
        rows = rows[:query.limit]
        items = tuple({key: self._json_scalar(value) for key, value in row.items() if key not in {"security_id", "_sort_value"}} for row in rows)
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = self._encode_cursor(query.sort_by, last["_sort_value"], last["security_id"])
        return FeaturePage(
            feature_run_id=run.feature_run_id, as_of=run.as_of,
            feature_version=run.feature_version, total_count=int(count or 0), items=items,
            next_cursor=next_cursor,
            quality_summary={"coverage": float(run.coverage), "successful_count": run.successful_count, "failed_count": run.failed_count, "errors": run.error_summary},
        )

    async def latest_regime(self) -> MarketRegimeSnapshot | None:
        model = (
            await self._session.execute(
                select(MarketRegimeSnapshotModel)
                .join(FeatureRunModel, FeatureRunModel.feature_run_id == MarketRegimeSnapshotModel.feature_run_id)
                .where(FeatureRunModel.status == "PUBLISHED")
                .order_by(
                    MarketRegimeSnapshotModel.as_of.desc(),
                    MarketRegimeSnapshotModel.created_at.desc(),
                    MarketRegimeSnapshotModel.regime_snapshot_id.desc(),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if model is None:
            return None
        return MarketRegimeSnapshot(
            regime_snapshot_id=model.regime_snapshot_id, feature_run_id=model.feature_run_id,
            as_of=model.as_of, known_at=model.known_at, index_states=model.index_states,
            breadth=model.breadth, turnover=model.turnover, limit_structure=model.limit_structure,
            size_style=model.size_style, growth_value_style=model.growth_value_style,
            industry_rotation=model.industry_rotation, risk_appetite_facts=model.risk_appetite_facts,
            domestic_risk_evidence_ids=tuple(model.domestic_risk_evidence_ids),
            global_risk_evidence_ids=tuple(model.global_risk_evidence_ids),
            coverage=float(model.coverage), confidence=float(model.confidence), stale=model.stale,
            content_hash=model.content_hash,
        )

    async def get_run_by_content_hash(self, content_hash: str) -> FeatureRun | None:
        model = (
            await self._session.execute(
                select(FeatureRunModel).where(FeatureRunModel.content_hash == content_hash)
            )
        ).scalar_one_or_none()
        return None if model is None else self._run(model)

    async def get_run(self, feature_run_id: UUID) -> FeatureRun | None:
        model = (
            await self._session.execute(
                select(FeatureRunModel).where(
                    FeatureRunModel.feature_run_id == feature_run_id,
                    FeatureRunModel.status == FeatureRunStatus.PUBLISHED.value,
                )
            )
        ).scalar_one_or_none()
        return None if model is None else self._run(model)

    async def latest_run(self) -> FeatureRun | None:
        model = (
            await self._session.execute(
                select(FeatureRunModel)
                .where(FeatureRunModel.status == FeatureRunStatus.PUBLISHED.value)
                .order_by(
                    FeatureRunModel.as_of.desc(),
                    FeatureRunModel.created_at.desc(),
                    FeatureRunModel.feature_run_id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if model is None else self._run(model)

    async def features_for_run(self, feature_run_id: UUID) -> tuple[RecallFeatureView, ...]:
        models = (
            await self._session.execute(
                select(SecurityFeatureModel)
                .where(SecurityFeatureModel.feature_run_id == feature_run_id)
                .order_by(SecurityFeatureModel.security_id)
            )
        ).scalars().all()
        return tuple(self._feature(model) for model in models)

    @staticmethod
    def _run(model: FeatureRunModel) -> FeatureRun:
        return FeatureRun(
            feature_run_id=model.feature_run_id, as_of=model.as_of,
            universe_snapshot_id=model.universe_snapshot_id,
            feature_version=model.feature_version, status=FeatureRunStatus(model.status),
            expected_count=model.expected_count, successful_count=model.successful_count,
            failed_count=model.failed_count, coverage=float(model.coverage),
            bar_revision_set_hash=model.bar_revision_set_hash,
            input_manifest=model.input_manifest, error_summary=model.error_summary,
            started_at=model.started_at, completed_at=model.completed_at,
            content_hash=model.content_hash,
        )

    @staticmethod
    def _feature(model: SecurityFeatureModel) -> RecallFeatureView:
        def number(value):
            return None if value is None else float(value)

        return RecallFeatureView(
            feature_run_id=model.feature_run_id,
            security_id=model.security_id,
            as_of=model.as_of,
            close=float(model.close),
            return_3d=number(model.return_3d),
            return_5d=number(model.return_5d),
            return_20d=number(model.return_20d),
            position_60d=number(model.position_60d),
            ma20_slope=number(model.ma20_slope),
            breakout_20d=model.breakout_20d,
            pullback_20d=model.pullback_20d,
            volume_ratio_5d=number(model.volume_ratio_5d),
            volume_expansion=model.volume_expansion,
            relative_index_strength=number(model.relative_index_strength),
            relative_industry_strength=number(model.relative_industry_strength),
            coverage=float(model.coverage),
            stale=model.stale,
            features=model.features,
            source_content_hash=model.content_hash,
        )

    @staticmethod
    def _json_scalar(value):
        from decimal import Decimal
        return float(value) if isinstance(value, Decimal) else value

    @staticmethod
    def _encode_cursor(sort_by: FeatureSortField, value, security_id: UUID) -> str:
        import base64
        payload = canonical_json({
            "sort_by": sort_by.value,
            "value": SQLAlchemyFeatureRepository._json_scalar(value),
            "security_id": security_id,
        })
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None, sort_by: FeatureSortField):
        import base64
        import json
        if cursor is None:
            return None, None
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload = json.loads(raw)
            if payload.get("sort_by") != sort_by.value:
                raise ValueError("cursor sort field does not match query")
            return payload.get("value"), UUID(payload["security_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid feature query cursor") from exc


class SQLAlchemyRecallRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_channels(
        self, channels: tuple[RecallChannel, ...]
    ) -> dict[str, UUID]:
        result = {}
        for channel in channels:
            channel_id = (
                await self._session.execute(
                    insert(RecallChannelModel)
                    .values(**channel.model_dump(mode="python"))
                    .on_conflict_do_nothing(index_elements=[RecallChannelModel.content_hash])
                    .returning(RecallChannelModel.channel_id)
                )
            ).scalar_one_or_none()
            if channel_id is None:
                channel_id = await self._session.scalar(
                    select(RecallChannelModel.channel_id).where(
                        RecallChannelModel.content_hash == channel.content_hash
                    )
                )
            if channel_id is None:
                raise RuntimeError("recall channel conflict did not resolve")
            result[channel.content_hash] = channel_id
        return result

    async def publish(
        self,
        run: RecallRun,
        results: tuple[RecallResult, ...],
        raw_opportunities: tuple[RawOpportunity, ...],
        observations: tuple[PerformanceObservation, ...],
    ) -> bool:
        inserted = (
            await self._session.execute(
                insert(RecallRunModel)
                .values(**run.model_dump(mode="python"))
                .on_conflict_do_nothing(index_elements=[RecallRunModel.content_hash])
                .returning(RecallRunModel.recall_run_id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            return False
        self._session.add_all(RecallResultModel(
            **item.model_dump(mode="python", exclude={"reasons"}),
            reasons=list(item.reasons),
        ) for item in results)
        self._session.add_all(RawOpportunityModel(
            **item.model_dump(
                mode="python",
                exclude={"recall_result_ids", "channel_codes", "reason_summary"},
            ),
            recall_result_ids=[str(value) for value in item.recall_result_ids],
            channel_codes=list(item.channel_codes),
            reason_summary={key: list(value) for key, value in item.reason_summary.items()},
        ) for item in raw_opportunities)
        self._session.add_all(PerformanceObservationModel(
            **item.model_dump(mode="python")
        ) for item in observations)
        return True

    async def get_run_by_content_hash(self, content_hash: str) -> RecallRun | None:
        model = await self._session.scalar(
            select(RecallRunModel).where(RecallRunModel.content_hash == content_hash)
        )
        if model is None:
            return None
        return self._run(model)

    async def read_results(
        self,
        *,
        recall_run_id: UUID | None,
        channel_code: str | None,
        limit: int,
        cursor: str | None,
    ) -> RecallReadPage | None:
        run_model = await self._read_run(recall_run_id)
        if run_model is None:
            return None
        filters = [RecallResultModel.recall_run_id == run_model.recall_run_id]
        if channel_code:
            filters.append(RecallChannelModel.code == channel_code)
        cursor_values = self._decode_read_cursor(cursor, "recall")
        if cursor_values:
            last_channel, last_rank, last_id = cursor_values
            filters.append(or_(
                RecallChannelModel.code > last_channel,
                and_(
                    RecallChannelModel.code == last_channel,
                    RecallResultModel.channel_rank > int(last_rank),
                ),
                and_(
                    RecallChannelModel.code == last_channel,
                    RecallResultModel.channel_rank == int(last_rank),
                    RecallResultModel.recall_result_id > UUID(last_id),
                ),
            ))
        rows = (
            await self._session.execute(
                select(RecallResultModel, RecallChannelModel, SecurityModel)
                .join(RecallChannelModel, RecallChannelModel.channel_id == RecallResultModel.channel_id)
                .join(SecurityModel, SecurityModel.security_id == RecallResultModel.security_id)
                .where(*filters)
                .order_by(
                    RecallChannelModel.code,
                    RecallResultModel.channel_rank,
                    RecallResultModel.recall_result_id,
                )
                .limit(limit + 1)
            )
        ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = tuple(RecallReadItem(
            recall_result_id=result.recall_result_id,
            security_id=result.security_id,
            market=security.market,
            code=security.code,
            name=security.name,
            channel_code=channel.code,
            channel_version=channel.version,
            channel_rank=result.channel_rank,
            strength=float(result.strength),
            reasons=tuple(result.reasons),
            matched_features=result.matched_features,
            coverage=float(result.coverage),
        ) for result, channel, security in rows)
        next_cursor = None
        if has_more and rows:
            result, channel, _ = rows[-1]
            next_cursor = self._encode_read_cursor(
                "recall", (channel.code, result.channel_rank, str(result.recall_result_id))
            )
        return RecallReadPage(run=self._run(run_model), items=items, next_cursor=next_cursor)

    async def read_raw(
        self,
        *,
        recall_run_id: UUID | None,
        limit: int,
        cursor: str | None,
    ) -> RawOpportunityReadPage | None:
        run_model = await self._read_run(recall_run_id)
        if run_model is None:
            return None
        filters = [RawOpportunityModel.recall_run_id == run_model.recall_run_id]
        cursor_values = self._decode_read_cursor(cursor, "raw")
        if cursor_values:
            last_market, last_code, last_security_id = cursor_values
            filters.append(or_(
                SecurityModel.market > last_market,
                and_(SecurityModel.market == last_market, SecurityModel.code > last_code),
                and_(
                    SecurityModel.market == last_market,
                    SecurityModel.code == last_code,
                    SecurityModel.security_id > UUID(last_security_id),
                ),
            ))
        rows = (
            await self._session.execute(
                select(RawOpportunityModel, SecurityModel)
                .join(SecurityModel, SecurityModel.security_id == RawOpportunityModel.security_id)
                .where(*filters)
                .order_by(SecurityModel.market, SecurityModel.code, SecurityModel.security_id)
                .limit(limit + 1)
            )
        ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = tuple(RawOpportunityReadItem(
            raw_opportunity_id=raw.raw_opportunity_id,
            security_id=raw.security_id,
            market=security.market,
            code=security.code,
            name=security.name,
            as_of=raw.as_of,
            known_at=raw.known_at,
            recall_result_ids=tuple(UUID(value) for value in raw.recall_result_ids),
            channel_codes=tuple(raw.channel_codes),
            reason_summary={key: tuple(value) for key, value in raw.reason_summary.items()},
        ) for raw, security in rows)
        next_cursor = None
        if has_more and rows:
            _, security = rows[-1]
            next_cursor = self._encode_read_cursor(
                "raw", (security.market, security.code, str(security.security_id))
            )
        return RawOpportunityReadPage(
            run=self._run(run_model), items=items, next_cursor=next_cursor
        )

    async def _read_run(self, recall_run_id: UUID | None) -> RecallRunModel | None:
        statement = select(RecallRunModel)
        if recall_run_id is not None:
            statement = statement.where(RecallRunModel.recall_run_id == recall_run_id)
        else:
            statement = statement.where(RecallRunModel.status == RecallRunStatus.PUBLISHED.value)
        return (
            await self._session.execute(
                statement.order_by(
                    RecallRunModel.as_of.desc(),
                    RecallRunModel.created_at.desc(),
                    RecallRunModel.recall_run_id.desc(),
                ).limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    def _run(model: RecallRunModel) -> RecallRun:
        return RecallRun(
            recall_run_id=model.recall_run_id,
            feature_run_id=model.feature_run_id,
            regime_snapshot_id=model.regime_snapshot_id,
            strategy_version=model.strategy_version,
            channel_set_hash=model.channel_set_hash,
            as_of=model.as_of,
            known_at=model.known_at,
            status=RecallRunStatus(model.status),
            expected_channel_count=model.expected_channel_count,
            successful_channel_count=model.successful_channel_count,
            failed_channel_count=model.failed_channel_count,
            security_count=model.security_count,
            hit_security_count=model.hit_security_count,
            coverage=float(model.coverage),
            errors=model.errors,
            content_hash=model.content_hash,
        )

    @staticmethod
    def _encode_read_cursor(kind: str, values: tuple[object, ...]) -> str:
        import base64
        payload = canonical_json({"kind": kind, "values": values})
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @staticmethod
    def _decode_read_cursor(cursor: str | None, kind: str) -> tuple | None:
        import base64
        import json
        if cursor is None:
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(
                cursor + "=" * (-len(cursor) % 4)
            ))
            if payload.get("kind") != kind or not isinstance(payload.get("values"), list):
                raise ValueError
            return tuple(payload["values"])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid recall cursor") from exc


class SQLAlchemyCorporateActionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_by_source_references(
        self, source: str, references: tuple[str, ...]
    ) -> dict[str, CorporateAction]:
        if not references:
            return {}
        ranked = (
            select(
                CorporateActionModel,
                func.row_number()
                .over(
                    partition_by=CorporateActionModel.source_reference,
                    order_by=(
                        CorporateActionModel.known_at.desc(),
                        CorporateActionModel.corporate_action_id.desc(),
                    ),
                )
                .label("revision_rank"),
            )
            .where(
                CorporateActionModel.source == source,
                CorporateActionModel.source_reference.in_(references),
            )
            .subquery()
        )
        rows = (
            await self._session.execute(select(ranked).where(ranked.c.revision_rank == 1))
        ).mappings()
        return {
            row["source_reference"]: CorporateAction(
                corporate_action_id=row["corporate_action_id"],
                security_id=row["security_id"],
                action_type=row["action_type"],
                announcement_time=row["announcement_time"],
                record_time=row["record_time"],
                effective_time=row["effective_time"],
                payload=row["payload"],
                source=row["source"],
                source_reference=row["source_reference"],
                evidence_id=row["evidence_id"],
                fetch_time=row["fetch_time"],
                known_at=row["known_at"],
                supersedes_action_id=row["supersedes_action_id"],
                content_hash=row["content_hash"],
            )
            for row in rows
        }

    async def publish(self, action: CorporateAction) -> bool:
        inserted = (
            await self._session.execute(
                insert(CorporateActionModel)
                .values(
                    corporate_action_id=action.corporate_action_id,
                    security_id=action.security_id,
                    action_type=action.action_type.value,
                    announcement_time=action.announcement_time,
                    record_time=action.record_time,
                    effective_time=action.effective_time,
                    payload=action.payload,
                    source=action.source,
                    source_reference=action.source_reference,
                    evidence_id=action.evidence_id,
                    fetch_time=action.fetch_time,
                    known_at=action.known_at,
                    content_hash=action.content_hash,
                    supersedes_action_id=action.supersedes_action_id,
                )
                .on_conflict_do_nothing(index_elements=[CorporateActionModel.content_hash])
                .returning(CorporateActionModel.corporate_action_id)
            )
        ).scalar_one_or_none()
        return inserted is not None


class SQLAlchemyIngestionRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: MarketDataIngestionRun) -> None:
        self._session.add(
            MarketDataIngestionRunModel(
                run_id=run.run_id,
                run_type=run.run_type,
                universe_snapshot_id=run.universe_snapshot_id,
                status=run.status.value,
                cursor=run.cursor,
                expected_count=run.expected_count,
                processed_count=run.processed_count,
                successful_count=run.successful_count,
                failed_count=run.failed_count,
                errors=list(run.errors),
                started_at=run.started_at,
                completed_at=run.completed_at,
                row_version=run.row_version,
            )
        )

    async def get(self, run_id) -> MarketDataIngestionRun | None:
        model = await self._session.get(MarketDataIngestionRunModel, run_id)
        if model is None:
            return None
        return MarketDataIngestionRun(
            run_id=model.run_id,
            run_type=model.run_type,
            universe_snapshot_id=model.universe_snapshot_id,
            status=IngestionRunStatus(model.status),
            cursor=model.cursor,
            expected_count=model.expected_count,
            processed_count=model.processed_count,
            successful_count=model.successful_count,
            failed_count=model.failed_count,
            errors=tuple(model.errors),
            started_at=model.started_at,
            completed_at=model.completed_at,
            row_version=model.row_version,
        )

    async def save(self, run: MarketDataIngestionRun, *, expected_version: int) -> bool:
        result = await self._session.execute(
            update(MarketDataIngestionRunModel)
            .where(
                MarketDataIngestionRunModel.run_id == run.run_id,
                MarketDataIngestionRunModel.row_version == expected_version,
            )
            .values(
                status=run.status.value,
                cursor=run.cursor,
                expected_count=run.expected_count,
                processed_count=run.processed_count,
                successful_count=run.successful_count,
                failed_count=run.failed_count,
                errors=list(run.errors),
                started_at=run.started_at,
                completed_at=run.completed_at,
                row_version=expected_version + 1,
            )
        )
        return result.rowcount == 1
