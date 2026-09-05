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
        "shadow-observation", "expected-run-registry",
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
    from datetime import timezone

    module = _scheduler_module()

    # 固定到周三交易日：run_once 用 module._utcnow 取时钟，不冻结的话
    # 周末/节假日跑套件时 trading_day=False → report 无 catchup 键（日期依赖）。
    monkeypatch.setattr(
        module, "_utcnow",
        lambda: datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc),  # 周三
    )

    class _FakeOrchestrator:
        async def execute(self, **kwargs):
            return {"status": "COMPLETED", "jobs": {}}

    class _FakeSession:
        # UoW 内的真实 repo 会执行 scalar 查询：桩定返回主链最近成功日
        async def scalar(self, stmt):
            return "2026-09-01"

        async def rollback(self):
            pass

        async def close(self):
            pass

    class _FakeDatabase:
        sessions = staticmethod(lambda: _FakeSession())

        async def close(self):
            pass

    def _fake_build(database_url, release=None, database=None):
        return _FakeOrchestrator(), _FakeOrchestrator(), _FakeDatabase()

    monkeypatch.setattr(module, "build_database", lambda url: _FakeDatabase())
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


def test_recall_strategy_version_prefers_release_configuration() -> None:
    """STR-002：Release configuration 声明了 recall_strategy_version 时真消费，
    未声明时退回缺省并如实标注 source=default。"""
    module = _scheduler_module()
    assert module._recall_strategy_version(None) == ("multi-recall-v1", "default")
    assert module._recall_strategy_version({}) == ("multi-recall-v1", "default")
    assert module._recall_strategy_version({"configuration": {}}) == (
        "multi-recall-v1", "default",
    )
    assert module._recall_strategy_version({
        "configuration": {"recall_strategy_version": "recall-v9"},
    }) == ("recall-v9", "release_configuration")


def _run_once_with_release(monkeypatch, tmp_path, *, effective_mode, reason):
    """STR-002/R3 主链 Gate 的通用测试装置：Release 解析结果由桩注入。

    R3-P0-001 验收要求：Resolver 必须**真消费 uow_factory**（模拟真实
    ReleaseResolver 查库），否则初始化顺序 bug 会被 stub 漏检。
    """
    import asyncio
    import contextlib
    import io
    from datetime import date, timezone as tz
    from datetime import datetime as dt

    module = _scheduler_module()
    seen: dict = {"orch_job_ids": [], "terminal_jobs": [], "resolver_database": None,
                  "build_orchestrator_database": None}

    class _FakeResolution:
        def __init__(self, uow_factory, v3_enabled):
            self._uow_factory = uow_factory

        async def resolve(self, environment):
            # 真实 ReleaseResolver.resolve() 会 async with uow_factory()
            async with self._uow_factory() as uow:
                await uow.orchestrator.latest_succeeded_idempotency_key("release")
            seen["resolver_database"] = id(self._uow_factory)
            return {
                "environment": environment, "resolved_at": dt.now(tz.utc),
                "mode": "V2", "effective_mode": effective_mode,
                "reason": reason, "strategy_version_id": None,
                "guardrail_version_id": None, "configuration": None,
                "row_version": None,
            }

    class _FakeCalendarMeta:
        source = "fixture"
        calendar_code = "XSHG"
        coverage_end = date(2026, 12, 31)

    class _FakeCalendar:
        metadata = _FakeCalendarMeta()

        def is_trading_day(self, value):
            return True

    class _FakeOrchRepo:
        async def latest_succeeded_idempotency_key(self, job_id):
            seen["terminal_jobs"].append(job_id)
            return None

    class _FakeUow:
        orchestrator = _FakeOrchRepo()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeOrchestrator:
        async def execute(self, **kwargs):
            seen["orch_job_ids"].append(kwargs.get("job_ids"))
            return {"status": "COMPLETED"}

    class _FakeDatabase:
        sessions = staticmethod(lambda: _FakeUow())

        async def close(self):
            pass

    def _fake_build_orchestrators(database_url, release=None, database=None):
        seen["build_orchestrator_database"] = database
        return _FakeOrchestrator(), _FakeOrchestrator(), _FakeDatabase()

    monkeypatch.setattr(module, "ExchangeCalendarsAShareCalendar", _FakeCalendar)
    monkeypatch.setattr(
        module, "latest_completed_session",
        lambda calendar, now: date(2026, 9, 2),
    )
    monkeypatch.setattr(module, "SQLAlchemyUnitOfWork", lambda sessions: _FakeUow())
    monkeypatch.setattr(module, "build_database", lambda url: _FakeDatabase())
    monkeypatch.setattr(
        module, "ReleaseResolver", _FakeResolution,
    )
    monkeypatch.setattr(
        module, "build_orchestrators", _fake_build_orchestrators,
    )
    monkeypatch.setenv("V3_DATABASE_URL", "postgresql+asyncpg://fake")
    monkeypatch.setenv("V3_ENABLED", "true")  # R3-P0-001 场景：Resolver 真查库
    output = tmp_path / "report.json"
    module.build_parser().parse_args(["--once", "--output", str(output)])
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        report = asyncio.run(module.run_once(output))
    report["_seen"] = seen
    return report


def test_run_once_builds_database_before_release_resolution(monkeypatch, tmp_path) -> None:
    """R3-P0-001：V3_ENABLED=true + Resolver 真消费 uow_factory 时，
    database 必须先于 Release 解析创建（原实现引用未赋值局部变量，
    Resolver 查库即 NameError 崩溃），并以同一实例传给 build_orchestrators。"""
    report = _run_once_with_release(
        monkeypatch, tmp_path, effective_mode="V3", reason=None,
    )
    seen = report.pop("_seen")
    # 装置内 resolve() 真实 async with uow_factory()——能走到这里说明
    # database 在 Release 解析前已存在（旧顺序会 NameError）
    assert seen["resolver_database"] is not None
    assert seen["build_orchestrator_database"] is not None


def test_run_once_skips_v3_main_chain_when_effective_mode_is_v2(monkeypatch, tmp_path) -> None:
    """R3-P0-002：effective V2（紧急开关/无 Release/状态不完整）时
    Release Gate 只跳过策略链（full-recall）——数据事实链
    （market-data/index-benchmarks/features/evidence-increment）照常运行，
    否则 V2 期间 V3 数据冻结，无法"先观察再激活"；catch-up 终端标记
    退回数据链终端 evidence-increment。"""
    module = _scheduler_module()
    report = _run_once_with_release(
        monkeypatch, tmp_path,
        effective_mode="V2", reason="V3_DISABLED_FLAG",
    )
    seen = report.pop("_seen")
    gate = report["release_gate"]
    assert gate["data_chain"] == "EXECUTED"
    assert gate["strategy_chain"] == "SKIPPED"
    assert gate["reason"] == "V3_DISABLED_FLAG"
    # 数据链照常运行（每次 execute 只选 4 个数据 Job，full-recall 被排除）
    assert report["main"], "V2 期间数据链不得停止"
    assert all(run["status"] == "COMPLETED" for run in report["main"])
    assert seen["orch_job_ids"], "主链 Orchestrator 必须仍被调度"
    assert set(seen["orch_job_ids"][0]) == set(module.DATA_CHAIN_JOB_IDS)
    # catch-up 终端标记退回数据链终端
    assert seen["terminal_jobs"][-1] == "evidence-increment"
    # 维护链（数据运营作业）不受策略版本 Gate 影响，照常执行
    assert report["maintenance"]["status"] == "COMPLETED"
    assert report["status"] == "COMPLETED"


def test_run_once_executes_main_chain_when_effective_mode_is_v3(monkeypatch, tmp_path) -> None:
    report = _run_once_with_release(
        monkeypatch, tmp_path, effective_mode="V3", reason=None,
    )
    seen = report.pop("_seen")
    assert report["release_gate"]["data_chain"] == "EXECUTED"
    assert report["release_gate"]["strategy_chain"] == "EXECUTED"
    assert report["release_gate"]["reason"] is None
    # V3 生效：全主链（含 full-recall）执行，job_ids 不限选
    assert all(job_ids is None for job_ids in seen["orch_job_ids"])
    assert seen["terminal_jobs"][-1] == "full-recall"
    assert isinstance(report["main"], list) and report["main"]
    assert all(run["status"] == "COMPLETED" for run in report["main"])


# --- REMAIN-OPS-EXPECTED / R3-P1-005：Expected Run Registry Job ---


def _task_profile(
    code: str = "daily-review", version: int = 1, schedule: str | None = "0 16 * * 1-5",
):
    from app.v3.domain.context import ContextLevel
    from app.v3.domain.task import TaskProfile

    return TaskProfile.build(
        profile_code=code, version=version, schedule=schedule,
        timezone="Asia/Shanghai",
        trading_calendar_source="fixture", trading_calendar_version="v1",
        context_level=ContextLevel.NORMAL, comparison_first=False,
        output_schema={"type": "object"}, expected_group_count=2,
        grace_seconds=600, strategy_version="multi-recall-v1",
    )


class _FakeTaskRegistry:
    def __init__(self, profiles, known_versions):
        self._profiles = profiles
        self._known = known_versions
        self.published = []
        self.created = []

    async def enabled_profiles(self):
        return tuple(self._profiles)

    async def get_profile_version(self, *, profile_code, version):
        return self._known.get((profile_code, version))

    async def publish_expected_run(self, expected):
        self.published.append(expected)
        return True

    async def create_task_run(self, run):
        self.created.append(run)
        return True


class _RegistryFakeUow:
    def __init__(self, registry):
        self.task_registry = registry

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None


def _expected_run_context(registry, *, as_of):
    from app.v3.jobs.orchestrator import JobContext

    return JobContext(
        trade_date=as_of.date(),
        as_of=as_of,
        uow_factory=lambda: _RegistryFakeUow(registry),
        artifacts={},
    )


def _expected_run_handler(monkeypatch, *, trading_day=True):
    module = _scheduler_module()

    class _FakeCalendar:
        def is_trading_day(self, value):
            return trading_day

    monkeypatch.setattr(module, "ExchangeCalendarsAShareCalendar", _FakeCalendar)
    return module


def _maintenance_handler(module, job_id):
    """从 build_orchestrators 取维护链 handler（闭包内函数，不连真库）。"""
    main, maintenance, _ = module.build_orchestrators(
        "postgresql+asyncpg://invalid"
    )
    return maintenance._jobs[job_id].handler


def test_profile_schedule_slots_contract() -> None:
    """R3-P1-005：schedule × timezone → 当日 slot 的显式契约——
    cron 固定时刻 / 简式多时刻 / 缺失 = NO_AUTO_SCHEDULE / 其余显式拒绝。"""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    module = _scheduler_module()
    tz = ZoneInfo("Asia/Shanghai")
    wednesday = date(2026, 9, 2)
    saturday = date(2026, 9, 5)

    def _profile(schedule):
        return _task_profile(schedule=schedule)

    # 5 段 cron 固定时刻 + day-of-week 过滤（1-5 = Mon-Fri，cron 1=Monday）
    assert module.profile_schedule_slots(_profile("0 16 * * 1-5"), wednesday) == [
        datetime(2026, 9, 2, 16, 0, tzinfo=tz)
    ]
    assert module.profile_schedule_slots(_profile("0 16 * * 1-5"), saturday) == []
    assert module.profile_schedule_slots(_profile("0 16 * * 0-6"), saturday) == [
        datetime(2026, 9, 5, 16, 0, tzinfo=tz)
    ]
    # 简式多时刻
    assert module.profile_schedule_slots(_profile("10:00,14:30"), wednesday) == [
        datetime(2026, 9, 2, 10, 0, tzinfo=tz),
        datetime(2026, 9, 2, 14, 30, tzinfo=tz),
    ]
    # 缺失 → NO_AUTO_SCHEDULE（不猜 00:00）
    assert module.profile_schedule_slots(_profile(None), wednesday) == []
    assert module.profile_schedule_slots(_profile("   "), wednesday) == []
    # 显式拒绝：限定日 cron / 步进 cron / 不可解析格式
    with pytest.raises(ValueError, match="day-of-month"):
        module.profile_schedule_slots(_profile("0 9 1 * *"), wednesday)
    with pytest.raises(ValueError, match="unsupported"):
        module.profile_schedule_slots(_profile("*/15 * * * *"), wednesday)
    with pytest.raises(ValueError, match="unsupported"):
        module.profile_schedule_slots(_profile("at noon"), wednesday)


def test_expected_run_registry_registers_enabled_profiles(monkeypatch) -> None:
    """REMAIN-OPS-EXPECTED：启用 Profile 按其 schedule（cron 16:00）确定性
    登记 Expected Run + PENDING Task Run；uuid5 identity 同 slot 重放零新增。"""
    import asyncio
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    module = _expected_run_handler(monkeypatch)
    profile = _task_profile("daily-review", 3, schedule="0 16 * * 1-5")
    registry = _FakeTaskRegistry(
        [profile], {("daily-review", 3): profile},
    )
    # 2026-09-02 是周三（Asia/Shanghai）→ 命中 1-5
    as_of = datetime(2026, 9, 2, 10, 45, tzinfo=timezone.utc)
    handler = _maintenance_handler(module, "expected-run-registry")
    result = asyncio.run(handler(_expected_run_context(registry, as_of=as_of)))
    assert result["trading_day"] is True
    assert result["profile_count"] == 1
    assert result["registered_count"] == 1
    assert result["skipped_no_schedule"] == 0
    assert result["error_count"] == 0
    # scheduled_for = Profile 时区 cron 时刻（确定性 → 幂等 identity）
    assert len(registry.published) == 1 and len(registry.created) == 1
    expected = registry.published[0]
    assert expected.scheduled_for == datetime(
        2026, 9, 2, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert expected.task_profile_id == profile.task_profile_id
    assert expected.task_profile_version == 3


def test_expected_run_registry_simple_schedule_multi_slot(monkeypatch) -> None:
    import asyncio
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    module = _expected_run_handler(monkeypatch)
    profile = _task_profile("intraday", 1, schedule="10:00,14:30")
    registry = _FakeTaskRegistry([profile], {("intraday", 1): profile})
    as_of = datetime(2026, 9, 2, 3, 0, tzinfo=timezone.utc)
    handler = _maintenance_handler(module, "expected-run-registry")
    result = asyncio.run(handler(_expected_run_context(registry, as_of=as_of)))
    assert result["registered_count"] == 2
    assert [run.scheduled_for for run in registry.published] == [
        datetime(2026, 9, 2, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        datetime(2026, 9, 2, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    ]


def test_expected_run_registry_skips_no_schedule(monkeypatch) -> None:
    """R3-P1-005：schedule 缺失 → 显式 NO_AUTO_SCHEDULE 跳过（不登记、
    不报错、绝不猜 00:00）。"""
    import asyncio
    from datetime import datetime, timezone

    module = _expected_run_handler(monkeypatch)
    profile = _task_profile("manual-only", 1, schedule=None)
    registry = _FakeTaskRegistry([profile], {("manual-only", 1): profile})
    as_of = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    handler = _maintenance_handler(module, "expected-run-registry")
    result = asyncio.run(handler(_expected_run_context(registry, as_of=as_of)))
    assert result["registered_count"] == 0
    assert result["skipped_no_schedule"] == 1
    assert result["error_count"] == 0
    assert registry.published == [] and registry.created == []


def test_expected_run_registry_unsupported_schedule_isolated(monkeypatch) -> None:
    """R3-P1-005：schedule 无法解释 → UNSUPPORTED_SCHEDULE 记 errors，
    绝不伪造 slot；同批其它 Profile 不受阻断。"""
    import asyncio
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    module = _expected_run_handler(monkeypatch)
    good = _task_profile("good-profile", 1, schedule="0 16 * * 1-5")
    bad = _task_profile("bad-profile", 2, schedule="at noon")
    registry = _FakeTaskRegistry(
        [good, bad],
        {("good-profile", 1): good, ("bad-profile", 2): bad},
    )
    as_of = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    handler = _maintenance_handler(module, "expected-run-registry")
    result = asyncio.run(handler(_expected_run_context(registry, as_of=as_of)))
    assert result["profile_count"] == 2
    assert result["registered_count"] == 1
    assert result["error_count"] == 1
    assert result["errors"][0]["profile_code"] == "bad-profile"
    assert "unsupported" in result["errors"][0]["error"]
    # 只有 good-profile 的 16:00 slot 被登记
    assert registry.published[0].scheduled_for == datetime(
        2026, 9, 2, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )


def test_expected_run_registry_skips_non_trading_day(monkeypatch) -> None:
    import asyncio
    from datetime import datetime, timezone

    module = _expected_run_handler(monkeypatch, trading_day=False)
    registry = _FakeTaskRegistry([], {})
    handler = _maintenance_handler(module, "expected-run-registry")
    result = asyncio.run(handler(_expected_run_context(
        registry, as_of=datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc),
    )))
    assert result["trading_day"] is False
    assert result["registered_count"] == 0
    assert result["skipped_no_schedule"] == 0
    assert registry.published == [] and registry.created == []


def test_expected_run_registry_isolates_profile_errors(monkeypatch) -> None:
    """单 Profile 失败（版本消失/禁用）隔离记 errors，不阻断其它 Profile。"""
    import asyncio
    from datetime import datetime, timezone

    module = _expected_run_handler(monkeypatch)
    good = _task_profile("good-profile", 1)
    registry = _FakeTaskRegistry(
        [good, _task_profile("ghost-profile", 9)],
        {("good-profile", 1): good, ("ghost-profile", 9): None},
    )
    handler = _maintenance_handler(module, "expected-run-registry")
    result = asyncio.run(handler(_expected_run_context(
        registry, as_of=datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc),
    )))
    assert result["profile_count"] == 2
    assert result["registered_count"] == 1
    assert result["error_count"] == 1
    assert result["errors"][0]["profile_code"] == "ghost-profile"


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
