from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.v3.application.run_evidence_registry import (
    CapabilityRunStatus,
    RunEvidenceRegistryService,
)
from app.v3.domain.evidence import (
    EvidenceFetchRun,
    EvidenceSource,
    EvidenceSourceType,
    FetchRunStatus,
)
from app.v3.infrastructure.providers.evidence import EvidenceProviderRegistry
from app.v3.providers.evidence import EvidenceCapability, EvidenceFetchBatch, ParsedEvidenceBundle


NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)


class StubProvider:
    def __init__(self, code: str, capability: EvidenceCapability, priority: int) -> None:
        self.source = EvidenceSource(
            code=code,
            source_type=EvidenceSourceType.OFFICIAL,
            capabilities={"types": [capability]},
            priority=priority,
            parser_version="stub-v1",
        )

    async def fetch(self, **_kwargs) -> EvidenceFetchBatch:
        raise AssertionError("registry runner test must use the fake run service")

    async def close(self) -> None:
        return None


class StubParser:
    code = "stub"
    version = "stub-v1"

    def parse(self, _raw, _source) -> ParsedEvidenceBundle:
        return ParsedEvidenceBundle(records=())


class FakeRunService:
    def __init__(self, outcomes) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def execute(self, **kwargs) -> EvidenceFetchRun:
        source = kwargs["provider"].source
        self.calls.append(source.code)
        outcome = self.outcomes[source.code]
        if isinstance(outcome, Exception):
            raise outcome
        terminal = outcome is not FetchRunStatus.RUNNING
        return EvidenceFetchRun(
            evidence_source_id=source.evidence_source_id,
            status=outcome,
            fetched_count=1 if outcome is not FetchRunStatus.FAILED else 0,
            raw_inserted_count=1 if outcome is not FetchRunStatus.FAILED else 0,
            parsed_count=1 if outcome in {FetchRunStatus.COMPLETED, FetchRunStatus.PARTIAL} else 0,
            evidence_count=1 if outcome in {FetchRunStatus.COMPLETED, FetchRunStatus.PARTIAL} else 0,
            failed_count=1 if outcome in {FetchRunStatus.FAILED, FetchRunStatus.PARTIAL} else 0,
            errors={"provider": "failed"} if outcome is FetchRunStatus.FAILED else {},
            started_at=NOW,
            completed_at=NOW if terminal else None,
        )


@pytest.mark.asyncio
async def test_registry_runner_falls_back_and_reports_unavailable_capability() -> None:
    registry = EvidenceProviderRegistry()
    primary = StubProvider("announcement-primary", EvidenceCapability.ANNOUNCEMENT, 10)
    fallback = StubProvider("announcement-fallback", EvidenceCapability.ANNOUNCEMENT, 20)
    parser = StubParser()
    registry.register(EvidenceCapability.ANNOUNCEMENT, primary, parser)
    registry.register(EvidenceCapability.ANNOUNCEMENT, fallback, parser)
    runs = FakeRunService(
        {
            primary.source.code: FetchRunStatus.FAILED,
            fallback.source.code: FetchRunStatus.COMPLETED,
        }
    )

    result = await RunEvidenceRegistryService(registry, runs).execute(
        capabilities=(EvidenceCapability.ANNOUNCEMENT, EvidenceCapability.FINANCIAL),
        window_start=NOW,
        window_end=NOW,
        max_batches=1,
    )

    announcement, financial = result.capabilities
    assert announcement.status is CapabilityRunStatus.SUCCESS
    assert announcement.selected_source == fallback.source.code
    assert [attempt.status for attempt in announcement.attempts] == [
        FetchRunStatus.FAILED,
        FetchRunStatus.COMPLETED,
    ]
    assert financial.status is CapabilityRunStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_registry_runner_isolates_exceptions_and_continues_other_capabilities() -> None:
    registry = EvidenceProviderRegistry()
    news = StubProvider("news-broken", EvidenceCapability.NEWS, 10)
    policy = StubProvider("policy-ok", EvidenceCapability.POLICY, 10)
    parser = StubParser()
    registry.register(EvidenceCapability.NEWS, news, parser)
    registry.register(EvidenceCapability.POLICY, policy, parser)
    runs = FakeRunService(
        {
            news.source.code: RuntimeError("network down"),
            policy.source.code: FetchRunStatus.COMPLETED,
        }
    )

    result = await RunEvidenceRegistryService(registry, runs).execute(
        capabilities=(EvidenceCapability.NEWS, EvidenceCapability.POLICY),
        window_start=NOW,
        window_end=NOW,
    )

    assert result.capabilities[0].status is CapabilityRunStatus.FAILED
    assert result.capabilities[0].attempts[0].errors == {
        "runner": "RuntimeError: network down"
    }
    assert result.capabilities[1].status is CapabilityRunStatus.SUCCESS
    assert runs.calls == [news.source.code, policy.source.code]


@pytest.mark.asyncio
async def test_registry_runner_tries_fallback_after_partial_primary() -> None:
    registry = EvidenceProviderRegistry()
    primary = StubProvider("partial-primary", EvidenceCapability.PERFORMANCE, 10)
    fallback = StubProvider("complete-fallback", EvidenceCapability.PERFORMANCE, 20)
    parser = StubParser()
    registry.register(EvidenceCapability.PERFORMANCE, primary, parser)
    registry.register(EvidenceCapability.PERFORMANCE, fallback, parser)
    runs = FakeRunService(
        {
            primary.source.code: FetchRunStatus.PARTIAL,
            fallback.source.code: FetchRunStatus.COMPLETED,
        }
    )

    result = await RunEvidenceRegistryService(registry, runs).execute(
        capabilities=(EvidenceCapability.PERFORMANCE,),
        window_start=NOW,
        window_end=NOW,
    )
    assert result.capabilities[0].status is CapabilityRunStatus.SUCCESS
    assert result.capabilities[0].selected_source == fallback.source.code


@pytest.mark.asyncio
async def test_registry_runner_collect_all_keeps_complementary_sources() -> None:
    registry = EvidenceProviderRegistry()
    forecast = StubProvider("forecast", EvidenceCapability.PERFORMANCE, 10)
    express = StubProvider("express", EvidenceCapability.PERFORMANCE, 20)
    parser = StubParser()
    registry.register(EvidenceCapability.PERFORMANCE, forecast, parser)
    registry.register(EvidenceCapability.PERFORMANCE, express, parser)
    runs = FakeRunService(
        {
            forecast.source.code: FetchRunStatus.COMPLETED,
            express.source.code: FetchRunStatus.COMPLETED,
        }
    )

    result = await RunEvidenceRegistryService(registry, runs).execute(
        capabilities=(EvidenceCapability.PERFORMANCE,),
        window_start=NOW,
        window_end=NOW,
        collect_all=True,
    )
    assert result.capabilities[0].status is CapabilityRunStatus.SUCCESS
    assert [attempt.source_code for attempt in result.capabilities[0].attempts] == [
        forecast.source.code,
        express.source.code,
    ]
