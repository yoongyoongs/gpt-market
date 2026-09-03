from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from app.utils.time import SHANGHAI


def _scheduler_module():
    import scripts.v3_scheduler as module

    return module


def test_schedule_time_parses_local_clock_only() -> None:
    module = _scheduler_module()
    parser = module.build_parser()
    args = parser.parse_args(["--at", "18:45"])
    assert (args.at.hour, args.at.minute) == (18, 45)
    with pytest.raises(SystemExit):
        parser.parse_args(["--at", "18:45+08:00"])


def test_seconds_until_next_run_rolls_to_next_day() -> None:
    module = _scheduler_module()
    from datetime import time

    now = datetime(2026, 9, 2, 10, 0, tzinfo=SHANGHAI)
    seconds = module.seconds_until_next_run(now, time(18, 45))
    assert seconds == timedelta(hours=8, minutes=45).total_seconds()
    after_schedule = datetime(2026, 9, 2, 19, 0, tzinfo=SHANGHAI)
    seconds = module.seconds_until_next_run(after_schedule, time(18, 45))
    assert seconds == timedelta(hours=23, minutes=45).total_seconds()


def test_scheduler_job_graph_is_wired_in_dependency_order() -> None:
    module = _scheduler_module()
    main, maintenance, database = module.build_orchestrators(
        os.getenv("V3_TEST_DATABASE_URL", "postgresql+asyncpg://invalid")
    )
    assert main.execution_order() == (
        "market-data", "index-benchmarks", "features",
        "evidence-increment", "full-recall",
    )
    assert set(maintenance.execution_order()) == {
        "corporate-action-match", "projection-verify",
        "performance-mature", "recall-observation-mature",
    }


def test_evidence_failed_capabilities_reports_only_failed() -> None:
    """Evidence 增量失败策略：部分能力失败不阻断（如实上报），
    全部失败才让 Job FAILED——与 index-benchmarks 一致。"""
    from app.v3.application.run_evidence_registry import (
        CapabilityRunStatus,
        EvidenceCapabilityRun,
        EvidenceRegistryRun,
    )
    from app.v3.providers.evidence import EvidenceCapability

    module = _scheduler_module()

    def _capability(capability, status):
        return EvidenceCapabilityRun(capability=capability, status=status)

    report = EvidenceRegistryRun(capabilities=(
        _capability(EvidenceCapability.NEWS, CapabilityRunStatus.SUCCESS),
        _capability(EvidenceCapability.POLICY, CapabilityRunStatus.FAILED),
        _capability(EvidenceCapability.FINANCIAL, CapabilityRunStatus.UNAVAILABLE),
    ))
    assert module._evidence_failed_capabilities(report) == ["POLICY", "FINANCIAL"]
    assert module._evidence_failed_capabilities(
        EvidenceRegistryRun(capabilities=(
            _capability(EvidenceCapability.NEWS, CapabilityRunStatus.SUCCESS),
            _capability(EvidenceCapability.POLICY, CapabilityRunStatus.SUCCESS),
        ))
    ) == []


def test_resolve_feature_run_id_prefers_artifact_then_latest_run() -> None:
    """Full Recall 的 Feature Run 解析：同编排 artifacts 优先，
    追平/重跑退回最新 PUBLISHED run；都没有则报错。"""
    import asyncio
    from datetime import date, datetime, timezone

    from app.v3.jobs.orchestrator import JobContext

    module = _scheduler_module()

    class _FakeFeatureRun:
        feature_run_id = "11111111-2222-3333-4444-555555555555"

    class _FakeFeaturesRepo:
        def __init__(self, run):
            self._run = run

        async def latest_run(self):
            return self._run

    class _FakeUow:
        def __init__(self, run):
            self.features = _FakeFeaturesRepo(run)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def _context(artifacts, run):
        return JobContext(
            trade_date=date(2026, 9, 2),
            as_of=datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc),
            uow_factory=lambda: _FakeUow(run),
            artifacts=artifacts,
        )

    artifact_context = _context(
        {"features": {"feature_run_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}},
        run=None,
    )
    assert asyncio.run(module._resolve_feature_run_id(artifact_context)) == (
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )

    fallback_context = _context({}, run=_FakeFeatureRun())
    assert asyncio.run(module._resolve_feature_run_id(fallback_context)) == (
        "11111111-2222-3333-4444-555555555555"
    )

    with pytest.raises(RuntimeError, match="no published feature run"):
        asyncio.run(module._resolve_feature_run_id(_context({}, run=None)))


def test_run_once_report_is_json_serializable(tmp_path, monkeypatch) -> None:
    """生产缺陷回归：release_resolution.resolved_at 是 datetime，
    run_once 的 JSON 报表序列化绝不能崩（真实每日任务曾因此 FAILED）。"""
    import asyncio
    import json

    module = _scheduler_module()

    class _FakeOrchestrator:
        async def execute(self, **kwargs):
            return {"status": "COMPLETED", "jobs": {}}

    class _FakeSession:
        # UoW 内的真实 repo 会执行 scalar 查询：桩定返回主链最近成功日
        async def scalar(self, stmt):
            from datetime import date

            return date.today().isoformat()

        async def rollback(self):
            pass

        async def close(self):
            pass

    class _FakeDatabase:
        sessions = staticmethod(lambda: _FakeSession())

        async def close(self):
            pass

    def _fake_build(database_url):
        return _FakeOrchestrator(), _FakeOrchestrator(), _FakeDatabase()

    monkeypatch.setattr(module, "build_orchestrators", _fake_build)
    monkeypatch.setenv("V3_DATABASE_URL", "postgresql+asyncpg://fake")
    output = tmp_path / "report.json"
    args = module.build_parser().parse_args(
        ["--once", "--output", str(output)]
    )
    import contextlib
    import io
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = asyncio.run(module.run_scheduler(args))
    # stdout 摘要与落盘文件都必须序列化成功（print 路径曾是第二个崩溃点）
    printed = json.loads(stdout.getvalue())
    assert printed["status"] == "COMPLETED"
    assert exit_code == 0
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["status"] == "COMPLETED"
    assert "resolved_at" in loaded["release_resolution"]
    # RT-05：报表必须显式暴露 catch-up 结果（补跑了哪些交易日）
    assert "catchup" in loaded
    assert isinstance(loaded["main"], list)


def test_catchup_terminal_marker_uses_full_recall(monkeypatch) -> None:
    """NEW-OPS-002：追平完成标记必须取主链终端 Job（full-recall），
    features 成功而 evidence/full-recall 失败时不得误判已追平。"""
    import asyncio

    module = _scheduler_module()
    seen: dict = {}

    class _FakeOrchRepo:
        async def latest_succeeded_idempotency_key(self, job_id):
            seen["job_id"] = job_id
            return None

    class _FakeUow:
        orchestrator = _FakeOrchRepo()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeDatabase:
        sessions = staticmethod(lambda: _FakeUow())

        async def close(self):
            pass

    monkeypatch.setattr(module, "SQLAlchemyUnitOfWork", lambda sessions: _FakeUow())
    assert asyncio.run(module._latest_main_success_key(_FakeDatabase())) is None
    assert seen["job_id"] == "full-recall"


def test_annotate_catchup_runs_marks_historical_dates() -> None:
    """NEW-OPS-003：历史日期补跑显式标注 operational-catchup，
    当日运行标注 same-day；不改动 orchestrator 原始报告键。"""
    from datetime import date

    module = _scheduler_module()
    trade_date = date(2026, 9, 3)
    pending = [date(2026, 9, 1), date(2026, 9, 2), trade_date]
    runs = [{"status": "COMPLETED", "idempotency_key": d.isoformat()}
            for d in pending]
    annotated = module._annotate_catchup_runs(trade_date, pending, runs)
    assert [run["catchup_mode"] for run in annotated] == [
        "operational-catchup", "operational-catchup", "same-day",
    ]
    assert annotated[0]["idempotency_key"] == "2026-09-01"
    assert runs[0] == {"status": "COMPLETED", "idempotency_key": "2026-09-01"}
