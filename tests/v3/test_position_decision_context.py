"""RT-07：Position Decision Context（实时方案 §9.4/§18.2/§27 RT-07）。

在完整 Position Context（RC-04D）之上补充卖出决策所需的确定性部分：

- source 等点时字段（§18.4）；
- objective_sell_facts：stop/target 相对最新价的客观事实——只陈述、
  绝不产生卖出建议，SELL 决策永远由 AI/人做。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.v3.application.read_position_decision_context import (
    ReadPositionDecisionContextService,
)

NOW = datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)


def _context(last_price=9.5, stop="9.0", target="12.0"):
    return {
        "security": {"code": "000001", "market": "SZ"},
        "as_of": NOW.isoformat(),
        "known_at": NOW.isoformat(),
        "market": {
            "status": "AVAILABLE", "latest_price": last_price,
            # R3-P1-007：确定性触达事实只允许新鲜实时快照——fixture 缺省
            # 即新鲜实时；stale/LKG 场景在具体测试里显式覆盖
            "price_source": "REALTIME_QUOTE", "quote_stale": False,
        },
        "levels": {
            "stop": stop, "target": target,
            "support": 8.9, "resistance": 10.1,
            "invalidation": "跌破 9.0 失效",
        },
        "data_quality": {"status": "AVAILABLE"},
    }


class _FakeContextService:
    def __init__(self, context):
        self._context = context
        self.calls = []

    async def execute(self, account_id, code, market=None, *, as_of=None):
        self.calls.append((account_id, code, market, as_of))
        return self._context


@pytest.mark.asyncio
async def test_objective_sell_facts_only_statement() -> None:
    service = ReadPositionDecisionContextService(
        _FakeContextService(_context(last_price=8.9)),
    )
    report = await service.execute("acc", "000001", as_of=NOW)
    facts = report["objective_sell_facts"]
    assert facts["stop_hit"] is True     # 8.9 <= 9.0
    assert facts["target_hit"] is False
    # 只有事实，绝无建议字段
    assert "recommended_action" not in facts
    assert "advice" not in facts
    assert report["source"] == "position-decision-context-v1"


@pytest.mark.asyncio
async def test_target_hit_objective_fact() -> None:
    service = ReadPositionDecisionContextService(
        _FakeContextService(_context(last_price=12.5)),
    )
    report = await service.execute("acc", "000001", as_of=NOW)
    facts = report["objective_sell_facts"]
    assert facts["target_hit"] is True
    assert facts["stop_hit"] is False


@pytest.mark.asyncio
async def test_missing_levels_are_honest_none() -> None:
    service = ReadPositionDecisionContextService(
        _FakeContextService(_context(stop="UNKNOWN", target=None)),
    )
    report = await service.execute("acc", "000001", as_of=NOW)
    facts = report["objective_sell_facts"]
    assert facts["stop_hit"] is None
    assert facts["target_hit"] is None


@pytest.mark.asyncio
async def test_passthrough_full_context() -> None:
    service = ReadPositionDecisionContextService(
        _FakeContextService(_context()),
    )
    report = await service.execute("acc", "000001", market="SZ", as_of=NOW)
    assert report["levels"]["invalidation"] == "跌破 9.0 失效"
    assert report["data_quality"]["status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_objective_facts_expose_price_source_and_eod_fact() -> None:
    """NEW-CTX-001：stop/target 客观事实必须携带价格来源与 EOD 事实，
    FEATURE_LKG 收盘价不被冒充为实时价。"""
    context = _context(last_price=8.9)
    context["market"].update({
        "price_source": "REALTIME_QUOTE",
        "price_known_at": NOW.isoformat(),
        "eod_feature_close": 9.1,
    })
    service = ReadPositionDecisionContextService(_FakeContextService(context))
    report = await service.execute("acc", "000001", as_of=NOW)
    facts = report["objective_sell_facts"]
    assert facts["price_source"] == "REALTIME_QUOTE"
    assert facts["price_known_at"] == NOW.isoformat()
    assert facts["eod_feature_close"] == 9.1
    assert facts["stop_hit"] is True
    assert facts["assessment_reason"] is None


@pytest.mark.asyncio
async def test_stale_realtime_quote_yields_unknown_hits() -> None:
    """R3-P1-007：Quote stale 时 stop/target 触达不可判定——显式 UNKNOWN
    + reason，绝不给确定性事实。"""
    context = _context(last_price=8.9)
    context["market"]["quote_stale"] = True
    service = ReadPositionDecisionContextService(_FakeContextService(context))
    facts = (await service.execute("acc", "000001", as_of=NOW))[
        "objective_sell_facts"
    ]
    assert facts["stop_hit"] is None
    assert facts["target_hit"] is None
    assert facts["assessment_reason"] == "STALE_REALTIME_QUOTE"


@pytest.mark.asyncio
async def test_lkg_price_never_yields_definitive_hits() -> None:
    """R3-P1-007：LKG close 只可作展示/PnL 参考，绝不冒充当前
    Stop/Target 判断（与 Entry Context stale 不得 READY 纪律一致）。"""
    context = _context(last_price=8.9)
    context["market"]["price_source"] = "FEATURE_LKG"
    service = ReadPositionDecisionContextService(_FakeContextService(context))
    facts = (await service.execute("acc", "000001", as_of=NOW))[
        "objective_sell_facts"
    ]
    assert facts["stop_hit"] is None
    assert facts["target_hit"] is None
    assert facts["assessment_reason"] == "NO_REALTIME_PRICE"
