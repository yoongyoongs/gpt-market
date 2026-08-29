from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.contracts.agent import AgentTask
from app.v3.domain.audit import AuditEvent
from app.v3.domain.market_data import (
    AdjustmentFactorRevision,
    BarIngestionTarget,
    BarSeriesRevision,
    IngestionRunStatus,
    Market,
    MarketDataIngestionRun,
    SecurityMember,
    UniverseSnapshot,
)
from app.v3.infrastructure.db.models import (
    AgentTaskModel,
    AdjustmentFactorModel,
    AdjustmentFactorRevisionModel,
    AuditEventModel,
    BarSeriesRevisionModel,
    MarketBarModel,
    MarketDataIngestionRunModel,
    SecurityModel,
    UniverseDiffModel,
    UniverseMemberModel,
    UniverseSnapshotModel,
    UniverseSourceModel,
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
        self, security_id, *, minimum_bars: int, minimum_last_bar_date
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
            and last_bar_time.date() >= minimum_last_bar_date
        )


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
