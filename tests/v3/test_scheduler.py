from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta

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
    """§8-12：四组编排——15:35 data-prep / 18:20 evidence / 18:45 eod-scan /
    20:30 maintenance，各自独立 advisory lock（§100 组间不互斥）。"""
    module = _scheduler_module()
    bundle = module.build_orchestrators(
        os.getenv("V3_TEST_DATABASE_URL", "postgresql+asyncpg://invalid")
    )
    assert bundle.data_prep.execution_order() == ("market-data", "index-benchmarks")
    assert bundle.evidence.execution_order() == ("evidence-increment",)
    # §9：evidence 解除对 features 的运行时人工依赖（handler 自验 Universe）
    # §10：eod-scan 组内 features → full-recall/candidate-scan
    # （拓扑排序以字母序入栈，两个策略 Job 的相对次序不构成约束）
    eod_order = bundle.eod_scan.execution_order()
    assert set(eod_order) == {"features", "full-recall", "candidate-scan"}
    assert eod_order.index("features") == 0
    jobs = bundle.eod_scan._jobs
    assert jobs["features"].depends_on == ()
    assert jobs["full-recall"].depends_on == ("features",)
    assert jobs["candidate-scan"].depends_on == ("features",)
    assert set(bundle.maintenance.execution_order()) == {
        "corporate-action-match", "projection-verify",
        "performance-mature", "recall-observation-mature",
        "shadow-observation", "expected-run-registry",
        "candidate-outcome-mature",
    }
    # §100：四组独立 advisory lock——resident 跑 eod-scan 时
    # --once --group data-prep 不得被无关锁挡住
    assert bundle.data_prep._advisory_lock_key == "v3-scheduler-data-prep"
    assert bundle.evidence._advisory_lock_key == "v3-scheduler-evidence"
    assert bundle.eod_scan._advisory_lock_key == "v3-scheduler-eod-scan"
    assert bundle.maintenance._advisory_lock_key == "v3-scheduler-maintenance"


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
        # P1-03：run_once 经 SchedulerBundle 收口（closeables 留空）
        return module.SchedulerBundle(
            data_prep=_FakeOrchestrator(), evidence=_FakeOrchestrator(),
            eod_scan=_FakeOrchestrator(), maintenance=_FakeOrchestrator(),
            database=_FakeDatabase(),
        )

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
    # §6/§13：报表按四组组织，catch-up 交易日跨组汇总暴露
    assert set(loaded["groups"]) == set(module.GROUP_ORDER)
    assert "catchup" in loaded
    for name in module.GROUP_ORDER:
        part = loaded["groups"][name]
        if name == "maintenance":
            # 维护链 = 原始 orchestrator 报告形状（status/jobs）
            assert "status" in part
        else:
            assert isinstance(part["runs"], list)


def test_eod_scan_completed_requires_all_required_jobs(monkeypatch) -> None:
    """§16/§99：EOD 完成判断不再用单一 terminal Job——策略链启用时
    features+full-recall+candidate-scan 全 SUCCEEDED 才算完成；
    candidate-scan FAILED 而 full-recall SUCCEEDED 绝不误判已追平。
    V2 无 Shadow（策略链被 Gate 排除）时只看 features。"""
    import asyncio

    module = _scheduler_module()
    succeeded: set[tuple[str, str]] = set()

    class _FakeOrchRepo:
        async def has_succeeded(self, job_id, idempotency_key):
            return (job_id, idempotency_key) in succeeded

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
    key = "2026-09-02"

    # 全部成功 → 完成
    succeeded = {("features", key), ("full-recall", key), ("candidate-scan", key)}
    assert asyncio.run(module.eod_scan_completed(
        _FakeDatabase(), date(2026, 9, 2), strategy_chain_active=True,
    )) is True

    # candidate-scan 未成功（FAILED/未跑）→ 未完成（§99 核心场景）
    succeeded = {("features", key), ("full-recall", key)}
    assert asyncio.run(module.eod_scan_completed(
        _FakeDatabase(), date(2026, 9, 2), strategy_chain_active=True,
    )) is False

    # features 未成功 → 未完成
    succeeded = {("full-recall", key), ("candidate-scan", key)}
    assert asyncio.run(module.eod_scan_completed(
        _FakeDatabase(), date(2026, 9, 2), strategy_chain_active=True,
    )) is False

    # V2 无 Shadow：策略链被 Gate 排除，features 成功即完成
    succeeded = {("features", key)}
    assert asyncio.run(module.eod_scan_completed(
        _FakeDatabase(), date(2026, 9, 2), strategy_chain_active=False,
    )) is True
    assert asyncio.run(module.eod_scan_completed(
        _FakeDatabase(), date(2026, 9, 2), strategy_chain_active=True,
    )) is False


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


def _run_once_with_release(
    monkeypatch, tmp_path, *, effective_mode, reason,
    trading_day=True, weekday_calendar=False, latest_success=None,
    eod_execute_result=None, group="all", prereq_ready=True,
):
    """STR-002/R3 Gate 的通用测试装置：Release 解析结果由桩注入。

    R3-P0-001 验收要求：Resolver 必须**真消费 uow_factory**（模拟真实
    ReleaseResolver 查库），否则初始化顺序 bug 会被 stub 漏检。
    latest_success：按 job_id 注入"最近成功幂等键"（catch-up 追平场景）。
    prereq_ready：§11 EOD 前置检查是否满足（False → 当日无 evidence 运行）。
    """
    import asyncio
    import contextlib
    import io
    from datetime import date, timezone as tz
    from datetime import datetime as dt

    module = _scheduler_module()
    seen: dict = {
        "executes": {},         # 组名 → [execute kwargs]
        "required_queried": [], # catch-up 追平查询过的 required Job
        "resolver_database": None,
        "build_orchestrator_database": None,
    }
    latest_success = dict(latest_success or {})

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
            if weekday_calendar:
                return value.weekday() < 5
            return trading_day

    class _FakeOrchRepo:
        async def latest_succeeded_idempotency_key(self, job_id):
            seen["required_queried"].append(job_id)
            return latest_success.get(job_id)

        async def has_succeeded(self, job_id, idempotency_key):
            return True

        async def has_run(self, job_id, idempotency_key):
            return prereq_ready

    class _FakeUow:
        orchestrator = _FakeOrchRepo()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeOrchestrator:
        def __init__(self, label):
            self._label = label

        async def execute(self, **kwargs):
            seen["executes"].setdefault(self._label, []).append(kwargs)
            if self._label == "eod-scan" and eod_execute_result is not None:
                return dict(eod_execute_result)
            return {"status": "COMPLETED"}

    class _FakeDatabase:
        sessions = staticmethod(lambda: _FakeUow())

        async def close(self):
            pass

    def _fake_build_orchestrators(database_url, release=None, database=None):
        seen["build_orchestrator_database"] = database
        # P1-03：run_once 通过 SchedulerBundle 收口 Provider/DB 生命周期，
        # fake 同样返回 bundle（closeables 留空——fake 无真连接）
        return module.SchedulerBundle(
            data_prep=_FakeOrchestrator("data-prep"),
            evidence=_FakeOrchestrator("evidence"),
            eod_scan=_FakeOrchestrator("eod-scan"),
            maintenance=_FakeOrchestrator("maintenance"),
            database=_FakeDatabase(),
        )

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
    # 冻结时钟：fixture 假定 today = 2026-09-02（周三）——真实时钟在周末
    # 运行会把 trading_day 判成 False，NON_TRADING_DAY 分支吃掉 pending
    monkeypatch.setattr(
        module, "_utcnow",
        lambda: dt(2026, 9, 2, 10, 0, tzinfo=tz.utc),
    )
    monkeypatch.setenv("V3_DATABASE_URL", "postgresql+asyncpg://fake")
    monkeypatch.setenv("V3_ENABLED", "true")  # R3-P0-001 场景：Resolver 真查库
    output = tmp_path / "report.json"
    module.build_parser().parse_args(["--once", "--output", str(output)])
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        report = asyncio.run(module.run_once(output, group=group))
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
    Release Gate 只把策略链（full-recall/candidate-scan）排除出 eod-scan
    组——数据事实组（data-prep/evidence/features catch-up）照常运行，
    否则 V2 期间 V3 数据冻结，无法"先观察再激活"。"""
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
    groups = report["groups"]
    assert set(groups) == set(module.GROUP_ORDER)
    # 数据组照常 catch-up
    assert all(
        run["status"] == "COMPLETED"
        for name in ("data-prep", "evidence") for run in groups[name]["runs"]
    )
    # eod-scan 组每次 execute 限定数据 Job（features），策略链被排除
    eod_executes = seen["executes"]["eod-scan"]
    assert eod_executes and all(
        kwargs["job_ids"] == module.EOD_DATA_JOB_IDS for kwargs in eod_executes
    )
    # 追平查询以 eod 组唯一 required（features）收尾
    assert seen["required_queried"][-1] == "features"
    # 维护链（数据运营作业）不受策略版本 Gate 影响，照常执行
    assert groups["maintenance"]["status"] == "COMPLETED"
    assert report["status"] == "COMPLETED"


def test_run_once_executes_main_chain_when_effective_mode_is_v3(monkeypatch, tmp_path) -> None:
    module = _scheduler_module()
    report = _run_once_with_release(
        monkeypatch, tmp_path, effective_mode="V3", reason=None,
    )
    seen = report.pop("_seen")
    assert report["release_gate"]["data_chain"] == "EXECUTED"
    assert report["release_gate"]["strategy_chain"] == "EXECUTED"
    assert report["release_gate"]["reason"] is None
    # V3 生效：eod-scan 组 execute 不限选 job_ids（full-recall/candidate-scan 照常）
    eod_executes = seen["executes"]["eod-scan"]
    assert eod_executes and all(kwargs["job_ids"] is None for kwargs in eod_executes)
    # 追平查询以策略链三 Job 收尾
    assert seen["required_queried"][-3:] == [
        "features", "full-recall", "candidate-scan",
    ]
    part = report["groups"]["eod-scan"]
    assert part["required_jobs"] == ["features", *module.STRATEGY_CHAIN_JOB_IDS]
    assert all(
        run["status"] == "COMPLETED"
        for name in ("data-prep", "evidence", "eod-scan")
        for run in report["groups"][name]["runs"]
    )


def test_run_once_research_shadow_runs_full_recall_under_v2(monkeypatch, tmp_path) -> None:
    """F6-09/Case J/§17：V2 Live + V3_RESEARCH_SHADOW_ENABLED=true →
    eod-scan 组照常包含 full-recall/candidate-scan（V3 每日实际跑低位
    埋伏候选，产出 Recall/Raw Opportunity 数据事实供观察），但正式
    Release 解析不变（effective_mode 仍 V2）、release_gate.strategy_chain
    如实标注 SHADOW_RESEARCH_EXECUTED（绝不假装正式 V3 激活）、无 Trade
    路径。env 关闭时回到纯 Gate 行为（SKIPPED + 只补 features）。"""
    module = _scheduler_module()
    report = _run_once_with_release(
        monkeypatch, tmp_path, effective_mode="V2", reason="V3_DISABLED_FLAG",
    )
    assert report["release_gate"]["strategy_chain"] == "SKIPPED"
    assert report["release_gate"].get("research_shadow") is False
    assert all(
        kwargs["job_ids"] == module.EOD_DATA_JOB_IDS
        for kwargs in report["_seen"]["executes"]["eod-scan"]
    )
    # --- 开启 Research Shadow ---
    monkeypatch.setenv("V3_RESEARCH_SHADOW_ENABLED", "true")
    module = _scheduler_module()
    report = _run_once_with_release(
        monkeypatch, tmp_path, effective_mode="V2", reason="V3_DISABLED_FLAG",
    )
    seen = report.pop("_seen")
    gate = report["release_gate"]
    # 正式 Release 解析未被影子模式篡改
    assert report["release_resolution"]["effective_mode"] == "V2"
    assert gate["data_chain"] == "EXECUTED"
    assert gate["strategy_chain"] == "SHADOW_RESEARCH_EXECUTED"
    assert gate["research_shadow"] is True
    assert gate["reason"] == "V3_DISABLED_FLAG"
    # eod-scan 组不限选 job_ids——full-recall 照常执行（Recall 数据事实刷新）
    eod_executes = seen["executes"]["eod-scan"]
    assert eod_executes and all(kwargs["job_ids"] is None for kwargs in eod_executes)
    assert seen["required_queried"][-3:] == [
        "features", "full-recall", "candidate-scan",
    ]
    assert all(
        run["status"] == "COMPLETED"
        for run in report["groups"]["eod-scan"]["runs"]
    )
    assert report["status"] == "COMPLETED"


# --- §96：四时点日程（env fallback / 单主循环时点解析 / CLI --group） ---


def _clear_slot_env(monkeypatch) -> None:
    for name in (
        "V3_DATA_PREP_AT", "V3_EVIDENCE_AT", "V3_EOD_SCAN_AT",
        "V3_MAINTENANCE_AT", "V3_SCHEDULE_AT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_daily_slots_env_fallback_chain(monkeypatch) -> None:
    """§13/§15：新四 env 各自生效；V3_EOD_SCAN_AT 未配置时读
    V3_SCHEDULE_AT（Deprecated）再 fallback 18:45——老部署不坏；
    非法 env 显式报错而不是静默吃掉。"""
    module = _scheduler_module()
    _clear_slot_env(monkeypatch)
    assert module.daily_slots() == {
        "data-prep": time(15, 35), "evidence": time(18, 20),
        "eod-scan": time(18, 45), "maintenance": time(20, 30),
    }
    # Deprecated V3_SCHEDULE_AT 仍被读取（老部署不坏）
    monkeypatch.setenv("V3_SCHEDULE_AT", "19:00")
    assert module.daily_slots()["eod-scan"] == time(19, 0)
    # 新 env 优先于 Deprecated
    monkeypatch.setenv("V3_EOD_SCAN_AT", "18:50")
    assert module.daily_slots()["eod-scan"] == time(18, 50)
    # --at 显式传入仅覆盖 eod-scan 时点
    slots = module.daily_slots(eod_override=time(21, 0))
    assert slots["eod-scan"] == time(21, 0)
    assert slots["data-prep"] == time(15, 35)
    # 非法 env 显式 ValueError
    monkeypatch.setenv("V3_DATA_PREP_AT", "not-a-time")
    with pytest.raises(ValueError, match="V3_DATA_PREP_AT"):
        module.daily_slots()


def test_resolve_next_slot_picks_nearest_group_in_stable_order(monkeypatch) -> None:
    """§13：单一主循环 resolve_next_slot——四时点各自命中；全部过期时
    滚到次日；同刻并列按 GROUP_ORDER 稳定顺序。"""
    module = _scheduler_module()
    _clear_slot_env(monkeypatch)
    slots = module.daily_slots()
    # 10:00 → 下一个时点 15:35 data-prep
    now = datetime(2026, 9, 10, 10, 0, tzinfo=SHANGHAI)
    name, seconds = module.resolve_next_slot(now, slots)
    assert name == "data-prep"
    assert seconds == module.seconds_until_next_run(now, time(15, 35))
    # 18:20:30 → eod-scan（18:45 早于 20:30 maintenance）
    now = datetime(2026, 9, 10, 18, 20, 30, tzinfo=SHANGHAI)
    assert module.resolve_next_slot(now, slots)[0] == "eod-scan"
    # 21:00 → 全部过期 → 次日 15:35 data-prep，且秒数必须为正
    now = datetime(2026, 9, 10, 21, 0, tzinfo=SHANGHAI)
    name, seconds = module.resolve_next_slot(now, slots)
    assert name == "data-prep"
    assert seconds > 0
    # 同刻并列 → GROUP_ORDER 首组
    tie = {name_: time(12, 0) for name_ in module.GROUP_ORDER}
    name, _ = module.resolve_next_slot(
        datetime(2026, 9, 10, 8, 0, tzinfo=SHANGHAI), tie,
    )
    assert name == "data-prep"


def test_cli_group_flag_parses_and_validates() -> None:
    """§14：CLI --group 合法值 = all + 四组名；非法值 argparse 报错。"""
    module = _scheduler_module()
    parser = module.build_parser()
    assert parser.parse_args(["--once"]).group == "all"
    assert parser.parse_args(["--once", "--group", "eod-scan"]).group == "eod-scan"
    assert parser.parse_args(["--once", "--group", "data-prep"]).group == "data-prep"
    with pytest.raises(SystemExit):
        parser.parse_args(["--once", "--group", "nope"])


# --- §97：非交易日只跑 maintenance ---


def test_run_once_non_trading_day_skips_data_groups_runs_maintenance(
    monkeypatch, tmp_path,
) -> None:
    """§97：非交易日 data-prep/evidence/eod-scan 显式 SKIPPED
    （NON_TRADING_DAY、无 runs、绝不 execute），maintenance 自然日继续；
    整体状态不因非交易日 skip 误判 PARTIAL。"""
    report = _run_once_with_release(
        monkeypatch, tmp_path, effective_mode="V3", reason=None,
        trading_day=False,
    )
    seen = report.pop("_seen")
    groups = report["groups"]
    assert not (set(seen["executes"]) & {"data-prep", "evidence", "eod-scan"}), \
        "非交易日数据组绝不能执行"
    assert "maintenance" in seen["executes"], "维护链自然日继续"
    for name in ("data-prep", "evidence", "eod-scan"):
        assert groups[name]["status"] == "SKIPPED"
        assert groups[name]["reason"] == "NON_TRADING_DAY"
        assert groups[name]["runs"] == []
    assert groups["maintenance"]["status"] == "COMPLETED"
    assert report["catchup"] == []
    assert report["status"] == "COMPLETED"


# --- §98：catch-up 按组追平 ---


def test_run_once_catchup_fills_gap_from_group_last_success(
    monkeypatch, tmp_path,
) -> None:
    """§98：catch-up 按组独立追平——组内 required Job 最近**全部**成功
    交易日之后的每个交易日补齐；已追平组不重复执行（Orchestrator 幂等
    是第二道保险，但 catch-up 计算本身就不该产生多余 execute）。
    fixture：交易日历 = 周一~周五，today = 2026-09-02（周三）。"""
    report = _run_once_with_release(
        monkeypatch, tmp_path, effective_mode="V3", reason=None,
        weekday_calendar=True,
        latest_success={
            "market-data": "2026-09-02", "index-benchmarks": "2026-09-02",
            "evidence-increment": "2026-08-31",
            "features": "2026-08-31", "full-recall": "2026-09-01",
            "candidate-scan": "2026-08-29",
        },
    )
    seen = report.pop("_seen")
    groups = report["groups"]
    # data-prep：min(09-02, 09-02) → 已追平，无 execute
    assert groups["data-prep"]["pending"] == []
    assert seen["executes"].get("data-prep") is None
    # evidence：08-31（周一）之后 → 09-01 / 09-02
    assert groups["evidence"]["pending"] == ["2026-09-01", "2026-09-02"]
    assert [k["trade_date"].isoformat() for k in seen["executes"]["evidence"]] == [
        "2026-09-01", "2026-09-02",
    ]
    # 补跑历史日标注 operational-catchup，当日为 same-day
    assert [r["catchup_mode"] for r in groups["evidence"]["runs"]] == [
        "operational-catchup", "same-day",
    ]
    # eod-scan：组内 min(features 08-31, full-recall 09-01, candidate-scan
    # 08-29) = 08-29（周六非交易日）→ 08-31 / 09-01 / 09-02
    assert groups["eod-scan"]["pending"] == [
        "2026-08-31", "2026-09-01", "2026-09-02",
    ]
    # 跨组 pending 并集进 report["catchup"]
    assert report["catchup"] == ["2026-08-31", "2026-09-01", "2026-09-02"]
    assert report["catchup_mode"] == "operational"
    assert report["status"] == "COMPLETED"


# --- §99：candidate-scan 失败不被 full-recall 掩盖 ---


def test_run_once_candidate_scan_failure_not_masked_by_full_recall(
    monkeypatch, tmp_path,
) -> None:
    """§99：candidate-scan 最近成功落后于 full-recall 时，eod-scan 组
    追平以组内最落后者（min）为准——次日仍补跑 candidate-scan
    （Orchestrator 幂等对 FAILED 重跑），绝不因 full-recall 成功而
    误判 eod 已完成。"""
    report = _run_once_with_release(
        monkeypatch, tmp_path, effective_mode="V3", reason=None,
        weekday_calendar=True,
        latest_success={
            "market-data": "2026-09-02", "index-benchmarks": "2026-09-02",
            "evidence-increment": "2026-09-02", "features": "2026-09-02",
            "full-recall": "2026-09-02", "candidate-scan": "2026-09-01",
        },
    )
    seen = report.pop("_seen")
    groups = report["groups"]
    assert groups["data-prep"]["pending"] == []
    assert groups["evidence"]["pending"] == []
    # eod-scan：min(09-02 ×3, candidate-scan 09-01) = 09-01 → 只补 09-02
    assert groups["eod-scan"]["pending"] == ["2026-09-02"]
    assert len(seen["executes"]["eod-scan"]) == 1
    assert seen["executes"]["eod-scan"][0]["trade_date"].isoformat() == "2026-09-02"


# --- §11：EOD 前置完整性检查 ---


def test_ensure_eod_prerequisites_requires_market_index_and_evidence() -> None:
    """§11：market-data / index-benchmarks 必须 SUCCEEDED、evidence 必须
    存在当日运行记录；不满足 → EOD_PREREQUISITE_NOT_READY，绝不拿旧数据
    生成假 Final30。"""
    import asyncio

    module = _scheduler_module()

    class _Repo:
        def __init__(self, succeeded, ran):
            self._succeeded = succeeded
            self._ran = ran

        async def has_succeeded(self, job_id, key):
            return key in self._succeeded.get(job_id, ())

        async def has_run(self, job_id, key):
            return key in self._ran

    class _Uow:
        def __init__(self, repo):
            self.orchestrator = repo

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def factory(repo):
        return lambda: _Uow(repo)

    key = "2026-09-02"
    full = {"market-data": {key}, "index-benchmarks": {key}}
    result = asyncio.run(module.ensure_eod_prerequisites(
        factory(_Repo(full, {key})), date(2026, 9, 2),
    ))
    assert result.ready
    assert result.error is None
    assert result.checks == {
        "market-data": True, "index-benchmarks": True, "evidence-increment": True,
    }
    # index-benchmarks 未成功 → 不就绪
    missing = asyncio.run(module.ensure_eod_prerequisites(
        factory(_Repo({"market-data": {key}}, {key})), date(2026, 9, 2),
    ))
    assert not missing.ready
    assert missing.error == "EOD_PREREQUISITE_NOT_READY"
    assert missing.checks["index-benchmarks"] is False
    # evidence 当日完全没跑过 → 不就绪
    no_evidence = asyncio.run(module.ensure_eod_prerequisites(
        factory(_Repo(full, set())), date(2026, 9, 2),
    ))
    assert not no_evidence.ready
    assert no_evidence.checks["evidence-increment"] is False


def test_run_once_eod_prerequisite_not_ready_skips_eod_day(
    monkeypatch, tmp_path,
) -> None:
    """§11：eod-scan 组待补交易日前置不满足 → 该日记
    SKIPPED / EOD_PREREQUISITE_NOT_READY（checks 逐项暴露），绝不
    execute 生成假 Final30；整体状态 PARTIAL 而非 COMPLETED。"""
    report = _run_once_with_release(
        monkeypatch, tmp_path, effective_mode="V3", reason=None,
        group="eod-scan", prereq_ready=False,
    )
    seen = report.pop("_seen")
    part = report["groups"]["eod-scan"]
    assert part["runs"], "至少一个待补交易日"
    assert all(
        run["status"] == "SKIPPED"
        and run["error_type"] == "EOD_PREREQUISITE_NOT_READY"
        and set(run["checks"]) == {"market-data", "index-benchmarks", "evidence-increment"}
        for run in part["runs"]
    )
    assert seen["executes"] == {}, "前置不满足绝不能 execute"
    assert report["status"] == "PARTIAL"


# --- §100：advisory lock LOCKED → 如实 PARTIAL ---


def test_run_once_locked_run_marks_report_partial(monkeypatch, tmp_path) -> None:
    """§100：eod-scan 组被其它进程 advisory lock 占用（execute 返回
    LOCKED）→ 该运行如实记入 runs，整体状态 PARTIAL（绝不谎报
    COMPLETED）；单组 --group 路径不影响其它组。"""
    report = _run_once_with_release(
        monkeypatch, tmp_path, effective_mode="V3", reason=None,
        group="eod-scan",
        latest_success={
            "market-data": "2026-09-02", "index-benchmarks": "2026-09-02",
            "evidence-increment": "2026-09-02", "features": "2026-09-02",
            "full-recall": "2026-09-02", "candidate-scan": "2026-09-01",
        },
        eod_execute_result={"status": "LOCKED", "jobs": []},
    )
    seen = report.pop("_seen")
    part = report["groups"]["eod-scan"]
    assert part["pending"] == ["2026-09-02"]
    assert [run["status"] for run in part["runs"]] == ["LOCKED"]
    assert len(seen["executes"]["eod-scan"]) == 1
    assert report["status"] == "PARTIAL"


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
    bundle = module.build_orchestrators("postgresql+asyncpg://invalid")
    return bundle.maintenance._jobs[job_id].handler


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


# --- R2.1-P1-02/P1-03：candidate-scan / candidate-outcome-mature Job ---


def _commit_uow_factory(calls: dict | None = None):
    """带 commit 计数的 fake UoW 工厂（handler 测试用）。"""
    seen = calls if calls is not None else {}

    class _Uow:
        async def __aenter__(self):
            seen["entered"] = seen.get("entered", 0) + 1
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            seen["commits"] = seen.get("commits", 0) + 1

    def factory():
        return _Uow()

    return factory, seen


def _job_context(uow_factory, *, artifacts=None):
    from datetime import timezone

    from app.v3.jobs.orchestrator import JobContext

    return JobContext(
        trade_date=datetime(2026, 9, 2).date(),
        as_of=datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc),
        uow_factory=uow_factory,
        artifacts=artifacts or {},
    )


def test_candidate_scan_handler_runs_scan_with_pit_feature_run(monkeypatch) -> None:
    """Test A（§12）：candidate-scan Job 接入主链——handler 用同一编排
    features Job 的 feature_run_id（PIT 一致），扫描成功后提交 UoW。"""
    import asyncio
    from datetime import timezone

    module = _scheduler_module()
    factory, seen = _commit_uow_factory()
    calls: dict = {}

    class _FakeScanOrchestrator:
        def __init__(self, *, deep_service=None):
            calls["deep_service"] = deep_service

        async def execute(self, uow, *, as_of=None, feature_run_id=None, **kwargs):
            calls["as_of"] = as_of
            calls["feature_run_id"] = feature_run_id
            return {
                "status": "ok",
                "scan_run_id": "cccccccc-dddd-eeee-ffff-000000000001",
                "feature_run_id": str(feature_run_id),
                "final_count": 30,
                "minute60_fetched": 120,
                "minute60_usable": 100,
                "m60_available": 100,
                "m60_stale": 3,
                "m60_error": 1,
                "market_regime_score": 62.5,
            }

    monkeypatch.setattr(module, "UniverseScanOrchestrator", _FakeScanOrchestrator)
    artifacts = {"features": {"feature_run_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}}
    context = _job_context(factory, artifacts=artifacts)
    assert context.as_of.tzinfo is timezone.utc
    handler = module.build_orchestrators(
        "postgresql+asyncpg://invalid"
    ).eod_scan._jobs["candidate-scan"].handler
    metrics = asyncio.run(handler(context))

    # PIT 一致：显式传同编排 features 产物
    assert str(calls["feature_run_id"]) == artifacts["features"]["feature_run_id"]
    assert calls["as_of"] == context.as_of
    assert calls["deep_service"] is not None  # 60m 抓取真实接线
    assert metrics["status"] == "ok"
    assert metrics["scan_run_id"] == "cccccccc-dddd-eeee-ffff-000000000001"
    assert seen.get("commits") == 1  # 扫描成功必须提交


def test_candidate_scan_skipped_when_features_failed() -> None:
    """Test B（§12）：features 失败 → candidate-scan=SKIPPED
    （DEPENDENCY_FAILED），不拿旧 Feature Run 偷跑。"""
    import asyncio

    from app.v3.jobs.orchestrator import JobDefinition, Orchestrator
    from tests.v3.test_orchestrator_retry_fallback import _FakeOrchRepo, _FakeUow

    async def features_handler(context):
        raise RuntimeError("features upstream broken")

    async def ok_handler(context):
        return {"status": "ok"}

    candidate_calls: list = []

    async def candidate_handler(context):
        candidate_calls.append(context)
        return {"status": "ok"}

    orchestrator = Orchestrator(
        lambda: _FakeUow(_FakeOrchRepo()),
        (
            JobDefinition(job_id="features", handler=features_handler),
            JobDefinition(
                job_id="evidence-increment", handler=ok_handler,
                depends_on=("features",),
            ),
            JobDefinition(
                job_id="candidate-scan", handler=candidate_handler,
                depends_on=("evidence-increment", "features"),
            ),
        ),
    )
    from datetime import date as _date

    report = asyncio.run(orchestrator.execute(
        trade_date=_date(2026, 9, 2),
    ))
    by_job = {job["job_id"]: job for job in report["jobs"]}
    assert by_job["features"]["status"] == "FAILED"
    assert by_job["candidate-scan"]["status"] == "SKIPPED"
    assert by_job["candidate-scan"]["error_type"] == "DEPENDENCY_FAILED"
    assert not candidate_calls, "features 失败时 candidate-scan 绝不偷跑"


def test_candidate_scan_v2_research_shadow_no_trade_side_effects(monkeypatch) -> None:
    """Test C（§12）：mode=V2 + Research Shadow 时 candidate-scan 可执行，
    仅产出扫描数据事实——绝不触碰 Trade 相关存储。"""
    import asyncio

    module = _scheduler_module()
    touched: set[str] = set()

    class _ProbeUow:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

        @property
        def scans(self):
            touched.add("scans")
            return _ProbeScans()

        @property
        def trades(self):  # pragma: no cover —— 触碰即失败
            touched.add("trades")
            raise AssertionError("candidate-scan 不得产生 Trade side effects")

        @property
        def trade_drafts(self):  # pragma: no cover
            touched.add("trade_drafts")
            raise AssertionError("candidate-scan 不得产生 TradeDraft")

    class _ProbeScans:
        async def published_run_on(self, as_of, **kwargs):
            return None

    class _FakeScanOrchestrator:
        def __init__(self, *, deep_service=None):
            pass

        async def execute(self, uow, **kwargs):
            uow.scans  # 触碰 scans（真实扫描会读写），Trade 探针在 property 里
            return {"status": "ok", "scan_run_id": "s1"}

    monkeypatch.setattr(module, "UniverseScanOrchestrator", _FakeScanOrchestrator)
    monkeypatch.setattr(module, "SQLAlchemyUnitOfWork", lambda sessions: _ProbeUow())
    handler = module.build_orchestrators(
        "postgresql+asyncpg://invalid"
    ).eod_scan._jobs["candidate-scan"].handler
    # artifacts 提供 feature_run_id → _resolve_feature_run_id 不触 features repo
    context = _job_context(
        _ProbeUow,
        artifacts={"features": {"feature_run_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}},
    )
    metrics = asyncio.run(handler(context))
    assert metrics["status"] == "ok"
    assert "trades" not in touched and "trade_drafts" not in touched
    assert touched == {"scans"}


def test_candidate_outcome_mature_processes_multiple_pending_scans(monkeypatch) -> None:
    """Test D（§12）：mature Job 处理多个历史 PENDING scan——绝不只
    成熟 latest；此前失败的历史 scan 今日补跑。"""
    import asyncio
    from uuid import uuid4

    module = _scheduler_module()
    id_a, id_b = uuid4(), uuid4()
    service_calls: list = []

    class _FakeMatureService:
        async def execute_pending(self, uow_factory, *, as_of=None, batch_limit=None):
            service_calls.append({
                "as_of": as_of, "batch_limit": batch_limit,
                "uow_factory": uow_factory,
            })
            return {
                "status": "ok",
                "candidate_count": 2,
                "processed_count": 2,
                "matured_scans": 1,
                "pending_count": 1,
                "matured_labels": 5,
                "pending_labels": 3,
                "error_count": 0,
                "errors": [],
                "_scan_ids": [id_a, id_b],
            }

    monkeypatch.setattr(module, "MatureScanOutcomesService", _FakeMatureService)
    factory, seen = _commit_uow_factory()
    handler = module.build_orchestrators(
        "postgresql+asyncpg://invalid"
    ).maintenance._jobs["candidate-outcome-mature"].handler
    metrics = asyncio.run(handler(_job_context(factory)))

    assert metrics["status"] == "ok"
    assert metrics["processed_count"] == 2  # 多个历史 scan 都被处理
    assert metrics["matured_scans"] == 1 and metrics["pending_count"] == 1
    assert service_calls[0]["as_of"] is not None
    assert service_calls[0]["uow_factory"] is factory


def test_candidate_outcome_mature_single_scan_failure_isolated(monkeypatch) -> None:
    """Test D 补充（§11）：批量补跑单 scan 失败隔离，其余 scan 照常
    成熟（此前失败的历史 scan 今日必须能补上）。"""
    import asyncio
    from uuid import uuid4

    from app.v3.application.mature_scan_outcomes import MatureScanOutcomesService

    id_bad, id_good = uuid4(), uuid4()
    executed: list = []

    async def fake_execute(self, scans, *, scan_id=None):
        executed.append(scan_id)
        if scan_id == id_bad:
            raise RuntimeError("bars unavailable")
        return {"status": "ok", "matured": 5, "pending": 0}

    class _FakeScans:
        async def pending_mature_scan_ids(self, *, older_than, limit):
            return [id_bad, id_good]

    class _Uow:
        scans = _FakeScans()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    service = MatureScanOutcomesService()
    original = MatureScanOutcomesService.execute
    MatureScanOutcomesService.execute = fake_execute
    try:
        from datetime import timezone as _tz

        report = asyncio.run(service.execute_pending(
            lambda: _Uow(),
            as_of=datetime(2026, 9, 2, 10, 0, tzinfo=_tz.utc),
        ))
    finally:
        MatureScanOutcomesService.execute = original

    assert executed == [id_bad, id_good]  # 失败不阻断后续 scan
    assert report["processed_count"] == 1
    assert report["matured_scans"] == 1
    assert report["error_count"] == 1
    assert report["errors"][0]["scan_run_id"] == str(id_bad)


def test_candidate_outcome_mature_batch_limit_and_pending_window(monkeypatch) -> None:
    """Test E（§12）：batch_limit 分批生效；候选窗口 = as_of−45 自然日
    （20 未来交易日保守上界）——窗口内观察窗不足的 scan 保持 PENDING
    （pending_count 如实计数，绝不伪装成熟）。"""
    import asyncio
    from datetime import timedelta
    from datetime import timezone as tz
    from uuid import uuid4

    from app.v3.application.mature_scan_outcomes import MatureScanOutcomesService

    as_of = datetime(2026, 9, 2, 10, 0, tzinfo=tz.utc)
    seen_windows: list = []

    class _FakeScans:
        async def pending_mature_scan_ids(self, *, older_than, limit):
            seen_windows.append({"older_than": older_than, "limit": limit})
            return [uuid4()]

    class _Uow:
        scans = _FakeScans()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    async def fake_execute(self, scans, *, scan_id=None):
        # 观察窗不足 20 交易日 → 该 scan 保持 PENDING（label=None）
        return {"status": "ok", "matured": 0, "pending": 5}

    service = MatureScanOutcomesService()
    original = MatureScanOutcomesService.execute
    MatureScanOutcomesService.execute = fake_execute
    try:
        report = asyncio.run(service.execute_pending(
            lambda: _Uow(), as_of=as_of, batch_limit=3,
        ))
    finally:
        MatureScanOutcomesService.execute = original

    assert seen_windows[0]["limit"] == 3
    # 45 自然日保守窗口：观察窗不足（<20 未来交易日）的 scan 不会进窗口
    assert seen_windows[0]["older_than"] == as_of - timedelta(days=45)
    assert report["matured_scans"] == 0
    assert report["pending_count"] == 1  # 保持 PENDING，不伪装成熟
    assert report["pending_labels"] == 5
