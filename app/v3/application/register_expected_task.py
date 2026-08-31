from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid5

from app.v3.domain.hashing import canonical_hash
from app.v3.domain.task import ExpectedRun, TaskGroupCounts, TaskRun
from app.v3.repositories.errors import RepositoryNotFoundError
from app.v3.repositories.protocols import UnitOfWork


EXPECTED_RUN_NAMESPACE = UUID("7935dcc2-2592-4073-bebd-bd50f21bbda9")
TASK_RUN_NAMESPACE = UUID("e80921dc-0a88-4ea7-9e79-33da44cab7f2")


class RegisterExpectedTaskService:
    """Registers schedule facts only; it never executes or claims an AI run."""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(
        self,
        *,
        profile_code: str,
        profile_version: int,
        scheduled_for: datetime,
    ) -> TaskRun:
        if scheduled_for.tzinfo is None or scheduled_for.utcoffset() is None:
            raise ValueError("scheduled_for must be timezone-aware")
        async with self._uow_factory() as uow:
            profile = await uow.task_registry.get_profile_version(
                profile_code=profile_code, version=profile_version
            )
        if profile is None or not profile.enabled:
            raise RepositoryNotFoundError("enabled task profile version does not exist")
        identity = canonical_hash({
            "task_profile_id": profile.task_profile_id,
            "task_profile_version": profile.version,
            "scheduled_for": scheduled_for,
        })
        expected_run_id = uuid5(EXPECTED_RUN_NAMESPACE, identity)
        expected = ExpectedRun.build(
            expected_run_id=expected_run_id,
            task_profile_id=profile.task_profile_id,
            task_profile_version=profile.version,
            scheduled_for=scheduled_for,
            window_end=scheduled_for + timedelta(seconds=profile.grace_seconds),
            known_at=self._clock(),
        )
        run = TaskRun(
            task_run_id=uuid5(TASK_RUN_NAMESPACE, str(expected_run_id)),
            expected_run_id=expected_run_id,
            task_profile_id=profile.task_profile_id,
            task_profile_version=profile.version,
            counts=TaskGroupCounts(
                expected=profile.expected_group_count,
                successful=0,
                failed=0,
                pending=profile.expected_group_count,
            ),
            row_version=1,
        )
        async with self._uow_factory() as uow:
            await uow.task_registry.publish_expected_run(expected)
            await uow.task_registry.create_task_run(run)
            await uow.commit()
        return run
