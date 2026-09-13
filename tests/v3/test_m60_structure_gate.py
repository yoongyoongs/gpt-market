"""R2.1-P0-01：60m Deep 数据读取路径（period["structure"]）+ Gate 测试。

此前 scan_universe._fetch_minute_60 错误地从 period 顶层读
trend/support/resistance（真实结构在 period["structure"] 内），
真实 60m 抓到也按 missing 运行。修复后 Gate 语义：

- status==AVAILABLE + 非 stale + 非 UNTRUSTED/UNAVAILABLE + trend 有值
  才注入 Minute60Fact；
- 拒收记 reason（M60_NOT_AVAILABLE/M60_STALE/M60_UNTRUSTED/
  M60_STRUCTURE_MISSING）与四类计数（available/missing/stale/error）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.v3.application.scan_universe import UniverseScanOrchestrator
from tests.v3.test_scan_universe import _uow

NOW = datetime(2026, 9, 7, 8, tzinfo=timezone.utc)

GOOD_60M = {
    "status": "AVAILABLE",
    "bar_count": 12,
    "stale": False,
    "quality": "OK",
    "known_at": NOW,
    "structure": {"trend": "UP", "support": 9.5, "resistance": 10.8},
}


def _period(**overrides) -> dict:
    period = dict(GOOD_60M)
    period.update(overrides)
    return period


class _FakeDeepService:
    """按 code 返回预置 60m period；raises 集合内的 code 抛异常。"""

    def __init__(self, period_by_code: dict, raise_codes: set[str] = frozenset()):
        self._period_by_code = period_by_code
        self._raise_codes = raise_codes

    async def get_intraday_structure(self, code, as_of=None):
        if code in self._raise_codes:
            raise RuntimeError("upstream down")
        return type("S", (), {
            "periods": {"60m": self._period_by_code[code]},
            "known_at": as_of,
        })()


def _orchestrator(period_by_code, raise_codes=frozenset()):
    return UniverseScanOrchestrator(
        deep_service=_FakeDeepService(period_by_code, raise_codes),
        minute60_concurrency=4,
    )


class TestMinute60StructureRead:
    @pytest.mark.asyncio
    async def test_real_structure_fact_injected(self):
        """§3.6：真实返回结构 → trend=UP/support=9.5/resistance=10.8。"""
        uow = _uow(("000001",))
        orchestrator = _orchestrator({"000001": _period()})
        summary = await orchestrator.execute(uow, as_of=NOW)
        assert summary["status"] == "ok"
        assert summary["m60_available"] == 1
        assert summary["m60_missing"] == 0
        assert summary["minute60_fetched"] == 1

    @pytest.mark.asyncio
    async def test_stale_period_rejected_as_m60_stale(self):
        uow = _uow(("000001",))
        orchestrator = _orchestrator({"000001": _period(stale=True)})
        summary = await orchestrator.execute(uow, as_of=NOW)
        assert summary["m60_stale"] == 1
        assert summary["m60_available"] == 0
        assert summary["m60_reasons"]["M60_STALE"] == 1
        assert summary["minute60_fetched"] == 0

    @pytest.mark.asyncio
    async def test_untrusted_quality_rejected(self):
        uow = _uow(("000001",))
        orchestrator = _orchestrator({"000001": _period(quality="UNTRUSTED")})
        summary = await orchestrator.execute(uow, as_of=NOW)
        assert summary["m60_available"] == 0
        assert summary["m60_reasons"]["M60_UNTRUSTED"] == 1

    @pytest.mark.asyncio
    async def test_not_available_status_rejected(self):
        uow = _uow(("000001",))
        orchestrator = _orchestrator({"000001": _period(status="UNKNOWN")})
        summary = await orchestrator.execute(uow, as_of=NOW)
        assert summary["m60_available"] == 0
        assert summary["m60_reasons"]["M60_NOT_AVAILABLE"] == 1

    @pytest.mark.asyncio
    async def test_missing_structure_rejected(self):
        """§3.6：structure 缺失 → missing（不得默认给 60 分）。"""
        uow = _uow(("000001",))
        period = _period()
        del period["structure"]
        orchestrator = _orchestrator({"000001": period})
        summary = await orchestrator.execute(uow, as_of=NOW)
        assert summary["m60_available"] == 0
        assert summary["m60_reasons"]["M60_STRUCTURE_MISSING"] == 1

    @pytest.mark.asyncio
    async def test_unknown_trend_rejected(self):
        uow = _uow(("000001",))
        orchestrator = _orchestrator({
            "000001": _period(structure={"trend": "UNKNOWN", "support": None, "resistance": None}),
        })
        summary = await orchestrator.execute(uow, as_of=NOW)
        assert summary["m60_available"] == 0
        assert summary["m60_reasons"]["M60_STRUCTURE_MISSING"] == 1

    @pytest.mark.asyncio
    async def test_provider_error_counted_not_fatal(self):
        """单股失败不阻断扫描，记 m60_error。"""
        uow = _uow(("000001", "000002"))
        orchestrator = _orchestrator(
            {"000001": _period(), "000002": _period()},
            raise_codes={"000001"},
        )
        summary = await orchestrator.execute(uow, as_of=NOW)
        assert summary["m60_error"] == 1
        assert summary["m60_available"] == 1

    @pytest.mark.asyncio
    async def test_no_deep_service_all_zero(self):
        uow = _uow(("000001",))
        summary = await UniverseScanOrchestrator().execute(uow, as_of=NOW)
        assert summary["minute60_fetched"] == 0
        assert summary["m60_available"] == 0


class TestMinute60ObjectStructure:
    """§3.3：structure 返回对象（非 dict）时按属性安全读取。"""

    @pytest.mark.asyncio
    async def test_object_structure_read(self):
        structure = type("St", (), {"trend": "UP", "support": 9.5, "resistance": 10.8})()
        uow = _uow(("000001",))
        orchestrator = _orchestrator({"000001": _period(structure=structure)})
        summary = await orchestrator.execute(uow, as_of=NOW)
        assert summary["m60_available"] == 1
