from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.v3.application.ingest_index_benchmarks import (
    BENCHMARK_CATALOG,
    IngestIndexBenchmarksService,
)


NOW = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)


class FakeIndexProvider:
    def __init__(self, fail_codes: set[str] = frozenset()):
        self.fail_codes = set(fail_codes)
        self.calls: list[tuple[str, str]] = []

    async def get_index_kline(self, code: str, market: str, period="day", limit=400):
        self.calls.append((code, market))
        if code in self.fail_codes:
            raise RuntimeError("provider down")
        bars = [
            type("Bar", (), {
                "timestamp": NOW - timedelta(days=60 - index),
                "close": 3800 + index * 5, "amount": 1e11 + index * 1e9,
            })()
            for index in range(60)
        ]
        return type("Result", (), {"klines": bars})()


class FakeIndexBenchmarkRepository:
    def __init__(self):
        self.published: list[object] = []
        self.return_values: list[bool] = []

    async def publish(self, revision) -> bool:
        duplicate = any(item.content_hash == revision.content_hash for item in self.published)
        if duplicate:
            return False
        self.published.append(revision)
        return True


class FakeUow:
    def __init__(self, repo):
        self.index_benchmarks = repo

    async def commit(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_service(provider, **kwargs) -> tuple[IngestIndexBenchmarksService, FakeIndexBenchmarkRepository]:
    repo = FakeIndexBenchmarkRepository()
    return (
        IngestIndexBenchmarksService(
            lambda: FakeUow(repo), provider, clock=lambda: NOW, **kwargs
        ),
        repo,
    )


@pytest.mark.asyncio
async def test_ingest_publishes_all_catalog_benchmarks() -> None:
    provider = FakeIndexProvider()
    service, repo = make_service(provider)
    report = await service.execute()
    assert report["status"] == "COMPLETED"
    assert set(report["benchmarks"]) == set(BENCHMARK_CATALOG)
    for code, item in report["benchmarks"].items():
        assert item["status"] == "PUBLISHED", code
        assert item["published"] == 1
        assert item["known_at"] == NOW
    assert len(provider.calls) == len(BENCHMARK_CATALOG)
    assert ("000300", "SH") in provider.calls
    assert repo.published[0].benchmark_code == "HS300"
    assert len(repo.published[0].bars) == 60


@pytest.mark.asyncio
async def test_ingest_isolates_provider_errors() -> None:
    service, _ = make_service(FakeIndexProvider(fail_codes={"000905"}))
    report = await service.execute(benchmarks=("HS300", "CSI500"))
    assert report["status"] == "PARTIAL"
    assert report["benchmarks"]["HS300"]["status"] == "PUBLISHED"
    assert report["benchmarks"]["CSI500"]["status"] == "FAILED"
    assert "RuntimeError" in report["benchmarks"]["CSI500"]["error"]


@pytest.mark.asyncio
async def test_ingest_same_content_is_unchanged_not_republished() -> None:
    service, repo = make_service(FakeIndexProvider())
    await service.execute(benchmarks=("HS300",))
    second = await service.execute(benchmarks=("HS300",))
    assert second["benchmarks"]["HS300"]["status"] == "UNCHANGED"
    assert second["benchmarks"]["HS300"]["published"] == 0
    assert len(repo.published) == 1


@pytest.mark.asyncio
async def test_fallback_publishes_with_honest_source() -> None:
    """RT §23.1：主源失败逐基准降级备用源，source 如实记录实际取数源。"""
    service, repo = make_service(
        FakeIndexProvider(fail_codes={"000300"}),
        fallback_provider=FakeIndexProvider(),
    )
    report = await service.execute(benchmarks=("HS300", "CSI500"))
    assert report["status"] == "COMPLETED"
    assert report["benchmarks"]["HS300"]["status"] == "PUBLISHED"
    assert report["benchmarks"]["HS300"]["source"] == "tencent"
    assert report["benchmarks"]["CSI500"]["source"] == "eastmoney"
    assert repo.published[0].source == "tencent"
    assert repo.published[0].upstream_source == "tencent"


@pytest.mark.asyncio
async def test_fallback_both_sources_fail_reports_error() -> None:
    service, _ = make_service(
        FakeIndexProvider(fail_codes={"000300"}),
        fallback_provider=FakeIndexProvider(fail_codes={"000300"}),
    )
    report = await service.execute(benchmarks=("HS300",))
    assert report["status"] == "PARTIAL"
    item = report["benchmarks"]["HS300"]
    assert item["status"] == "FAILED"
    assert "primary and fallback index sources both failed" in item["error"]
    assert "RuntimeError" in item["error"]


@pytest.mark.asyncio
async def test_fallback_source_label_is_configurable() -> None:
    """source 标签显式可配，不靠类名猜（生产诚实：审计可追）。"""
    service, repo = make_service(
        FakeIndexProvider(fail_codes={"000300"}),
        fallback_provider=FakeIndexProvider(),
        fallback_source="tencent-manual",
    )
    await service.execute(benchmarks=("HS300",))
    assert repo.published[0].source == "tencent-manual"
