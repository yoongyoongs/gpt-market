from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.v3.application.register_expected_task import RegisterExpectedTaskService
from app.v3.domain.context import ContextLevel
from app.v3.domain.task import TaskProfile, TaskRunStatus


NOW = datetime(2026, 8, 31, 7, tzinfo=timezone.utc)


def _profile():
    return TaskProfile.build(
        task_profile_id=uuid4(), profile_code="POST_MARKET", version=1,
        schedule="0 16 * * 1-5", timezone="Asia/Shanghai",
        trading_calendar_source="exchange_calendars:XSHG",
        trading_calendar_version="2026.1", context_level=ContextLevel.NORMAL,
        comparison_first=True, candidate_limit=100, topk_limit=10,
        topk_context_level=ContextLevel.DEEP,
        output_schema={"type": "CandidateComparisonResult"},
        expected_group_count=11, grace_seconds=1800,
        strategy_version="strategy.v1",
    )


class _Registry:
    def __init__(self, profile):
        self.profile = profile
        self.expected = {}
        self.runs = {}

    async def get_profile_version(self, **kwargs): return self.profile
    async def publish_expected_run(self, value):
        created = value.expected_run_id not in self.expected
        self.expected[value.expected_run_id] = value
        return created
    async def create_task_run(self, value):
        created = value.task_run_id not in self.runs
        self.runs[value.task_run_id] = value
        return created


class _Uow:
    def __init__(self, registry): self.task_registry = registry
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    async def commit(self): return None


@pytest.mark.asyncio
async def test_registers_expected_schedule_fact_and_pending_task_idempotently():
    registry = _Registry(_profile())
    service = RegisterExpectedTaskService(
        lambda: _Uow(registry), clock=lambda: NOW
    )

    first = await service.execute(
        profile_code="POST_MARKET", profile_version=1, scheduled_for=NOW
    )
    replay = await service.execute(
        profile_code="POST_MARKET", profile_version=1, scheduled_for=NOW
    )

    assert replay == first
    assert len(registry.expected) == 1
    assert len(registry.runs) == 1
    expected = next(iter(registry.expected.values()))
    assert expected.window_end.timestamp() - expected.scheduled_for.timestamp() == 1800
    assert first.status is TaskRunStatus.PENDING_IMPORT
    assert first.counts.expected == 11
    assert first.counts.pending == 11
    assert not hasattr(expected, "ai_executed")


@pytest.mark.asyncio
async def test_rejects_naive_schedule_time():
    registry = _Registry(_profile())
    service = RegisterExpectedTaskService(lambda: _Uow(registry), clock=lambda: NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        await service.execute(
            profile_code="POST_MARKET", profile_version=1,
            scheduled_for=datetime(2026, 8, 31, 16),
        )
