from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from uuid import UUID, uuid4


class CyclicDependencyError(ValueError):
    pass


class UnknownDependencyError(ValueError):
    pass


@dataclass(frozen=True)
class JobContext:
    """传给每个 Job handler 的上下文：UoW 工厂、交易日、as_of、上游产物。"""

    trade_date: date
    as_of: datetime
    uow_factory: Callable
    artifacts: dict[str, dict] = field(default_factory=dict)

    def artifact(self, job_id: str) -> dict:
        return self.artifacts.get(job_id, {})


JobHandler = Callable[[JobContext], dict]


@dataclass(frozen=True)
class JobDefinition:
    job_id: str
    handler: JobHandler
    depends_on: tuple[str, ...] = ()


@dataclass
class JobRunReport:
    job_id: str
    status: str
    attempt: int
    job_run_id: UUID | None = None
    error_type: str | None = None
    error_summary: str | None = None
    metrics: dict = field(default_factory=dict)


class Orchestrator:
    """正式 V3 Production Pipeline Orchestrator（RC-03 / OPS-001）。

    职责：按依赖顺序执行 Job；每个 Job 落库记录 Run（status/as_of/
    known_at/attempt/error/metrics，content_hash 去重）；按
    (job_id, idempotency_key) 幂等，已成功 Job 重复执行时跳过并保留
    其产物；Job 失败时下游 SKIPPED、独立分支继续；可选全局
    advisory lock 防止同一幂等键并发重复执行。
    Orchestrator 不实现业务逻辑，业务在 handler（通常是 Application Service）。
    """

    def __init__(
        self,
        uow_factory: Callable,
        jobs: tuple[JobDefinition, ...],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        advisory_lock_key: str | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._jobs = {job.job_id: job for job in jobs}
        self._clock = clock
        if len(self._jobs) != len(jobs):
            raise ValueError("duplicate job_id in orchestrator jobs")
        self._validate_graph()
        self._advisory_lock_key = advisory_lock_key

    def _validate_graph(self) -> None:
        for job in self._jobs.values():
            for dependency in job.depends_on:
                if dependency not in self._jobs:
                    raise UnknownDependencyError(
                        f"job {job.job_id} depends on unknown job {dependency}"
                    )
        order = self.execution_order()
        if len(order) != len(self._jobs):
            raise CyclicDependencyError("orchestrator job graph contains a cycle")

    def execution_order(self) -> tuple[str, ...]:
        ordered: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(job_id: str) -> None:
            if job_id in visited:
                return
            if job_id in visiting:
                raise CyclicDependencyError(
                    "orchestrator job graph contains a cycle"
                )
            visiting.add(job_id)
            for dependency in self._jobs[job_id].depends_on:
                visit(dependency)
            visiting.discard(job_id)
            visited.add(job_id)
            ordered.append(job_id)

        for job_id in sorted(self._jobs):
            visit(job_id)
        return tuple(ordered)

    async def execute(
        self,
        *,
        trade_date: date,
        as_of: datetime | None = None,
        job_ids: tuple[str, ...] | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        if as_of is None:
            as_of = self._clock()
        key = idempotency_key or trade_date.isoformat()
        async with self._uow_factory() as uow:
            if self._advisory_lock_key is not None:
                acquired = await uow.orchestrator.try_advisory_lock(
                    f"v3-orchestrator:{self._advisory_lock_key}"
                )
                if not acquired:
                    return {
                        "orchestrator_run_id": None,
                        "idempotency_key": key,
                        "trade_date": trade_date.isoformat(),
                        "status": "LOCKED",
                        "jobs": [],
                    }
            try:
                return await self._run_jobs(
                    trade_date=trade_date, as_of=as_of, key=key,
                    job_ids=job_ids,
                )
            finally:
                if self._advisory_lock_key is not None:
                    try:
                        await uow.orchestrator.advisory_unlock(
                            f"v3-orchestrator:{self._advisory_lock_key}"
                        )
                    finally:
                        await uow.rollback()

    async def _run_jobs(
        self,
        *,
        trade_date: date,
        as_of: datetime,
        key: str,
        job_ids: tuple[str, ...] | None,
    ) -> dict:
        orchestrator_run_id = uuid4()
        known_at = self._clock()
        selected = self._select_jobs(job_ids)
        artifacts: dict[str, dict] = {}
        for job_id in sorted(selected):
            metrics = await self._latest_succeeded(job_id, key)
            if metrics is not None:
                artifacts[job_id] = dict(metrics)
        reports: list[JobRunReport] = []
        for job_id in self.execution_order():
            if job_id not in selected:
                continue
            job = self._jobs[job_id]
            if await self._has_succeeded(job.job_id, key):
                reports.append(JobRunReport(
                    job_id=job.job_id, status="SKIPPED", attempt=1,
                    error_type="ALREADY_SUCCEEDED",
                    error_summary=(
                        "a successful run with the same idempotency key exists"
                    ),
                ))
                continue
            dependencies_ok = all([
                await self._dependency_met(dep, key, reports)
                for dep in job.depends_on
            ])
            if not dependencies_ok:
                reports.append(await self._record(
                    orchestrator_run_id, job.job_id, key,
                    status="SKIPPED", known_at=known_at, as_of=None,
                    error_type="DEPENDENCY_FAILED",
                    error_summary="skipped because an upstream job did not succeed",
                ))
                continue
            context = JobContext(
                trade_date=trade_date, as_of=as_of,
                uow_factory=self._uow_factory, artifacts=dict(artifacts),
            )
            started_at = self._clock()
            try:
                metrics = await job.handler(context)
            except Exception as exc:  # noqa: BLE001 - 失败必须落库为 Run 记录
                reports.append(await self._record(
                    orchestrator_run_id, job.job_id, key,
                    status="FAILED", known_at=known_at, as_of=as_of,
                    started_at=started_at,
                    error_type=type(exc).__name__,
                    error_summary=str(exc)[:1000],
                ))
                continue
            artifacts[job.job_id] = dict(metrics or {})
            reports.append(await self._record(
                orchestrator_run_id, job.job_id, key,
                status="SUCCEEDED", known_at=known_at, as_of=as_of,
                started_at=started_at, metrics=dict(metrics or {}),
            ))
        statuses = {report.status for report in reports}
        if "FAILED" in statuses:
            overall = "PARTIAL" if "SUCCEEDED" in statuses else "FAILED"
        else:
            overall = "COMPLETED"
        return {
            "orchestrator_run_id": str(orchestrator_run_id),
            "idempotency_key": key,
            "trade_date": trade_date.isoformat(),
            "status": overall,
            "jobs": [
                {
                    "job_id": report.job_id, "status": report.status,
                    "attempt": report.attempt,
                    "job_run_id": (
                        str(report.job_run_id) if report.job_run_id else None
                    ),
                    "error_type": report.error_type,
                    "error_summary": report.error_summary,
                    "metrics": report.metrics,
                }
                for report in reports
            ],
        }

    def _select_jobs(self, job_ids: tuple[str, ...] | None) -> set[str]:
        if job_ids is None:
            return set(self._jobs)
        unknown = sorted(set(job_ids) - set(self._jobs))
        if unknown:
            raise UnknownDependencyError(f"unknown jobs: {', '.join(unknown)}")
        selected = set(job_ids)
        for job_id in job_ids:
            selected.update(self._collect_dependencies(job_id))
        return selected

    def _collect_dependencies(self, job_id: str) -> set[str]:
        collected: set[str] = set()
        for dependency in self._jobs[job_id].depends_on:
            collected.add(dependency)
            collected.update(self._collect_dependencies(dependency))
        return collected

    async def _dependency_met(
        self, dependency: str, key: str, reports: list[JobRunReport]
    ) -> bool:
        this_run = next(
            (report for report in reports if report.job_id == dependency), None
        )
        if this_run is not None:
            return this_run.status == "SUCCEEDED"
        # 依赖 Job 本次未执行：历史幂等成功视为已满足
        return await self._has_succeeded(dependency, key)

    async def _latest_succeeded(self, job_id: str, key: str) -> dict | None:
        async with self._uow_factory() as uow:
            return await uow.orchestrator.latest_succeeded_metrics(job_id, key)

    async def _has_succeeded(self, job_id: str, key: str) -> bool:
        async with self._uow_factory() as uow:
            return await uow.orchestrator.has_succeeded(job_id, key)

    async def _record(
        self,
        orchestrator_run_id: UUID,
        job_id: str,
        idempotency_key: str,
        *,
        status: str,
        known_at: datetime,
        as_of: datetime | None,
        started_at: datetime | None = None,
        error_type: str | None = None,
        error_summary: str | None = None,
        metrics: dict | None = None,
    ) -> JobRunReport:
        metrics = metrics or {}
        async with self._uow_factory() as uow:
            # 同 Job 写入互斥：事务级 advisory lock（按 job_id 派生）
            await uow.orchestrator.job_lock(job_id)
            attempt = await uow.orchestrator.next_attempt(job_id, idempotency_key)
            job_run_id = await uow.orchestrator.record(
                orchestrator_run_id=orchestrator_run_id,
                job_id=job_id, idempotency_key=idempotency_key,
                attempt=attempt, status=status,
                known_at=known_at, as_of=as_of,
                started_at=started_at,
                completed_at=self._clock(),
                error_type=error_type, error_summary=error_summary,
                metrics=metrics,
            )
            await uow.commit()
        return JobRunReport(
            job_id=job_id, status=status, attempt=attempt,
            job_run_id=job_run_id, error_type=error_type,
            error_summary=error_summary, metrics=metrics,
        )

