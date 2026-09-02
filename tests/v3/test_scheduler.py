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
    )
    assert maintenance.execution_order() == (
        "corporate-action-match",
        "projection-verify",
    )


def test_run_once_report_is_json_serializable(tmp_path, monkeypatch) -> None:
    """生产缺陷回归：release_resolution.resolved_at 是 datetime，
    run_once 的 JSON 报表序列化绝不能崩（真实每日任务曾因此 FAILED）。"""
    import asyncio
    import json

    module = _scheduler_module()

    class _FakeOrchestrator:
        async def execute(self, **kwargs):
            return {"status": "COMPLETED", "jobs": {}}

    class _FakeDatabase:
        sessions = None

        async def close(self):
            pass

    def _fake_build(database_url):
        return _FakeOrchestrator(), _FakeOrchestrator(), _FakeDatabase()

    monkeypatch.setattr(module, "build_orchestrators", _fake_build)
    monkeypatch.setenv("V3_DATABASE_URL", "postgresql+asyncpg://fake")
    output = tmp_path / "report.json"
    report = asyncio.run(module.run_once(output))
    assert report["status"] == "COMPLETED"
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["status"] == "COMPLETED"
    assert "resolved_at" in loaded["release_resolution"]
