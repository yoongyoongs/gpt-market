"""RC-05B（CTX-001）：Position Context 全量载荷（离线）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.v3.application.read_position_context import ReadPositionContextService
from app.v3.domain.features import PublishedSecurityFeatureView
from app.v3.domain.index_benchmark import IndexBenchmarkBar  # noqa: F401  (fixture parity)
from app.v3.domain.market_data import (
    AdjustType,
    BarPeriod,
    BarSeriesRevision,
    BarSeriesRevisionContent,
    MarketBar,
    PointInTimePrecision,
)


NOW = datetime(2026, 8, 28, 7, tzinfo=timezone.utc)


def _plan(version: int, *, plan_id=None, supersedes=None, horizon="D3_10"):
    plan = {
        "entry_price_low": "10", "entry_price_high": "11", "quantity": "1000",
    }
    if version == 2:
        plan["stop_loss"] = "9.5"
        plan["take_profit"] = "12"
        plan["invalidation"] = "跌破 9.5 且周线未转 BASE 则计划失效"
    return {
        "entry_plan_id": str(plan_id or uuid4()),
        "version": version,
        "supersedes_entry_plan_id": str(supersedes) if supersedes else None,
        "effective_from": (NOW - timedelta(days=12)).isoformat(),
        "expected_horizon": horizon,
        "plan": plan,
        "content_hash": "a" * 64,
    }


def _facts(plan_v1, plan_v2):
    security_id = uuid4()
    plan_id = plan_v1["entry_plan_id"]
    return {
        "security": {"security_id": security_id, "code": "600300",
                     "market": "SH", "name": "fixture"},
        "position": {
            "quantity": Decimal("1000"), "cost_basis": Decimal("10005"),
            "average_cost": Decimal("10.005"), "cash_impact": Decimal("-10005"),
            "realized_pnl": Decimal("0"), "projection_version": 2,
            "input_hash": "a" * 64, "rebuilt_at": NOW - timedelta(hours=1),
        },
        "trades": ({
            "trade_id": str(uuid4()), "side": "BUY",
            "quantity": "1000", "price": "10.0", "fee": "5",
            "trade_time": (NOW - timedelta(days=10)).isoformat(),
            "entry_plan_id": plan_id, "entry_plan_version": 1,
        },),
        "decisions": (),
        "entry_plans": (plan_v1, plan_v2),
        "latest_position_review": None,
        "position_review_history": (),
        "write_capabilities": {},
    }


def _feature():
    return PublishedSecurityFeatureView(
        feature_run_id=uuid4(), security_id=uuid4(), series_revision_id=uuid4(),
        as_of=NOW - timedelta(hours=2), close=11.0, atr_pct=0.03,
        coverage=1.0, stale=False,
        features={"weekly_trend_state": "UP", "daily_trend_state": "SIDEWAYS"},
        input_hash="a" * 64, source_content_hash="b" * 64,
    )


def _daily_revision(security_id):
    bars = tuple(
        MarketBar(
            bar_time=NOW - timedelta(days=30 - index),
            open=10.0 + index * 0.04, high=10.6 + index * 0.04,
            low=9.8 + index * 0.04, close=10.0 + index * 0.04,
            volume=1_000_000, amount=1e7,
            fetch_time=NOW - timedelta(hours=3),
        )
        for index in range(25)
    )
    return BarSeriesRevision.build(BarSeriesRevisionContent(
        revision_id=uuid4(), security_id=security_id, period=BarPeriod.DAY,
        adjust_type=AdjustType.QFQ, source="fixture", upstream_source="fixture",
        raw_bar_available=False,
        point_in_time_precision=PointInTimePrecision.LIMITED,
        precision_reason="fixture QFQ only",
        known_at=NOW - timedelta(hours=3), bars=bars,
    ))


class _WeekdayCalendar:
    def is_trading_day(self, value):
        return value.weekday() < 5


class _FakeDeepService:
    def __init__(self) -> None:
        self.calls = 0

    async def get_intraday_structure(self, code, *, as_of):
        self.calls += 1
        return {
            "code": code, "as_of": as_of, "known_at": NOW,
            "source": "eastmoney",
            "periods": {
                period: {"status": "AVAILABLE", "precision": "LIMITED",
                         "reason": "MINUTE_FACTS_ARE_FETCH_TIME_FACTS",
                         "bar_count": 32}
                for period in ("5m", "15m", "60m")
            },
        }


class _FakeUow:
    def __init__(self, facts, feature, revision, regime=None):
        self._facts = facts
        self._feature = feature
        self._revision = revision
        self._regime = regime
        self.portfolios = self
        self.features = self
        self.bars = self
        self.evidence = self

    async def position_context(self, account_id, code, market=None):
        return self._facts

    async def latest_security_feature(self, security_id, *, as_of):
        return self._feature

    async def latest_regime(self):
        return self._regime

    async def latest_daily_revisions(self, security_ids, *, as_of):
        return (self._revision,) if self._revision else ()

    async def retrieve_view(self, *, query):
        from app.v3.domain.evidence import EvidenceRepositoryPage
        return EvidenceRepositoryPage(views=(), coverage_counts={})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _service(facts, feature, revision, *, calendar=True, deep=True, clock=NOW,
             quote_service=None):
    return ReadPositionContextService(
        lambda: _FakeUow(facts, feature, revision),
        clock=lambda: clock,
        calendar=_WeekdayCalendar() if calendar else None,
        deep_market_data=_FakeDeepService() if deep else None,
        quote_service=quote_service,
    )


@pytest.mark.asyncio
async def test_full_position_context_payload_assembles_all_sections():
    plan_v1 = _plan(1)
    plan_v2 = _plan(2, supersedes=plan_v1["entry_plan_id"])
    facts = _facts(plan_v1, plan_v2)
    revision = _daily_revision(facts["security"]["security_id"])
    payload = await _service(facts, _feature(), revision).execute(
        facts["security"]["security_id"] and uuid4(), "600300", "SH", as_of=NOW,
    )

    for key in ("security", "as_of", "known_at", "position", "market", "holding",
                "trades", "entry_plan", "levels", "multi_timeframe", "fundamental",
                "industry", "market_regime", "evidence", "risk", "time_efficiency",
                "latest_position_review", "previous_position_review_id", "data_quality"):
        assert key in payload, key

    assert payload["position"]["cost_method"] == "WEIGHTED_AVERAGE"
    assert payload["market"]["unrealized_pnl"] == Decimal("995.0")
    assert payload["market"]["return_pct"] == pytest.approx(11.0 / 10.005 - 1, abs=1e-9)
    assert payload["market"]["price_source"] == "FEATURE_LKG"
    assert payload["holding"]["first_buy_time"] == (NOW - timedelta(days=10)).isoformat()
    assert payload["holding"]["holding_sessions"] > 0
    assert payload["entry_plan"]["original"]["version"] == 1
    assert payload["entry_plan"]["current"]["version"] == 2
    assert payload["entry_plan"]["trade_bound"]["version"] == 1
    # 支撑/阻力来自版本化计算（最近 20 根日 K）
    assert payload["levels"]["calculation_version"] == "support-resistance-20d-v1"
    assert payload["levels"]["support"] == pytest.approx(9.8 + 5 * 0.04)
    assert payload["levels"]["resistance"] == pytest.approx(10.6 + 24 * 0.04)
    assert payload["levels"]["stop"] == "9.5"
    assert payload["levels"]["target"] == "12"
    # §14.1：失效条件随计划透出；计划缺失时诚实 UNKNOWN
    assert payload["levels"]["invalidation"] == "跌破 9.5 且周线未转 BASE 则计划失效"
    assert payload["multi_timeframe"]["weekly"]["state"] == "UP"
    assert payload["multi_timeframe"]["daily"]["state"] == "SIDEWAYS"
    # §14.2：合成事实随特征透出（确定性规则，非 Final Score）
    assert payload["multi_timeframe"]["state"] == "WEEKLY_UP_DAILY_SIDEWAYS"
    assert payload["multi_timeframe"]["rule"] is None
    assert payload["multi_timeframe"]["5m"]["status"] == "AVAILABLE"
    assert payload["multi_timeframe"]["5m"]["precision"] == "LIMITED"
    assert payload["fundamental"] == {"status": "NOT_AVAILABLE",
                                      "reason": "NO_RELIABLE_SOURCE"}
    assert payload["industry"] == {"status": "NOT_AVAILABLE",
                                   "reason": "NO_RELIABLE_SOURCE"}


@pytest.mark.asyncio
async def test_missing_pieces_degrade_explicitly_without_fabrication():
    plan_v1 = _plan(1)
    facts = _facts(plan_v1, _plan(2, supersedes=plan_v1["entry_plan_id"]))
    facts["entry_plans"][1]["plan"].pop("stop_loss")
    facts["entry_plans"][1]["plan"].pop("take_profit")
    facts["entry_plans"][1]["plan"].pop("invalidation")
    payload = await _service(facts, None, None, calendar=False, deep=False).execute(
        uuid4(), "600300", "SH", as_of=NOW,
    )
    assert payload["market"]["status"] == "UNKNOWN"
    assert payload["market"]["reason"] == "NO_FEATURE_PRICE"
    assert payload["holding"]["holding_sessions"]["status"] == "UNKNOWN"
    assert payload["holding"]["holding_sessions"]["reason"] == "CALENDAR_NOT_BOUND"
    assert payload["levels"]["support"]["status"] == "UNKNOWN"
    assert payload["levels"]["support"]["reason"] == "NO_DAILY_BARS"
    assert payload["levels"]["stop"]["status"] == "UNKNOWN"
    assert payload["levels"]["stop"]["reason"] == "PLAN_HAS_NO_STOP_TARGET"
    # §14.1：计划未写失效条件 → 诚实 UNKNOWN，绝不编造
    assert payload["levels"]["invalidation"]["status"] == "UNKNOWN"
    assert payload["levels"]["invalidation"]["reason"] == "PLAN_HAS_NO_INVALIDATION"
    for period in ("5m", "15m", "60m"):
        assert payload["multi_timeframe"][period]["status"] == "UNKNOWN"
        assert payload["multi_timeframe"][period]["reason"] == "DEEP_MARKET_DATA_NOT_BOUND"
    assert payload["multi_timeframe"]["weekly"]["status"] == "UNKNOWN"
    assert payload["multi_timeframe"]["weekly"]["reason"] == "NO_FEATURE"
    # 特征缺失 → 合成事实诚实 UNKNOWN
    assert payload["multi_timeframe"]["state"]["status"] == "UNKNOWN"
    assert payload["multi_timeframe"]["state"]["reason"] == "NO_FEATURE"


class _FakeQuoteSnapshot:
    def __init__(self, last_price="10.8", *, suspended=False, stale=False):
        self.last_price = last_price
        self.known_at = NOW
        self.source = "eastmoney"
        self.suspended = suspended
        self.stale = stale


class _FakeQuoteService:
    def __init__(self, quote=None, *, error=None):
        self._quote = quote
        self._error = error
        self.requested: list[str] = []

    async def get_quote_snapshot(self, code, *, as_of):
        self.requested.append(code)
        if self._error is not None:
            raise self._error
        return self._quote


@pytest.mark.asyncio
async def test_realtime_quote_overrides_price_and_keeps_eod_fact():
    """NEW-CTX-001：绑定实时 Quote → latest_price=REALTIME_QUOTE，
    EOD LKG close 保留为独立事实（eod_feature_close），两时间戳并存。"""
    plan_v1 = _plan(1)
    plan_v2 = _plan(2, supersedes=plan_v1["entry_plan_id"])
    facts = _facts(plan_v1, plan_v2)
    feature = _feature()
    revision = _daily_revision(facts["security"]["security_id"])
    quote_service = _FakeQuoteService(_FakeQuoteSnapshot("10.8"))
    payload = await _service(
        facts, feature, revision, quote_service=quote_service,
    ).execute(uuid4(), "600300", "SH", as_of=NOW)

    market = payload["market"]
    assert market["price_source"] == "REALTIME_QUOTE"
    assert market["latest_price"] == Decimal("10.8")
    assert market["price_known_at"] == NOW.isoformat()
    assert market["quote_source"] == "eastmoney"
    assert market["quote_stale"] is False
    assert market["eod_feature_close"] == Decimal("11.0")
    assert market["eod_feature_known_at"] == feature.as_of.isoformat()
    # 持仓盈亏按实时价计算
    assert payload["market"]["unrealized_pnl"] == (Decimal("10.8") - Decimal("10.005")) * 1000
    assert quote_service.requested == ["600300"]


@pytest.mark.asyncio
async def test_quote_failure_falls_back_to_feature_lkg_honestly():
    """NEW-CTX-001：行情失败回退 FEATURE_LKG，quote_status 如实标注，
    绝不伪造实时价。"""
    plan_v1 = _plan(1)
    plan_v2 = _plan(2, supersedes=plan_v1["entry_plan_id"])
    facts = _facts(plan_v1, plan_v2)
    revision = _daily_revision(facts["security"]["security_id"])
    quote_service = _FakeQuoteService(error=RuntimeError("quote down"))
    payload = await _service(
        facts, _feature(), revision, quote_service=quote_service,
    ).execute(uuid4(), "600300", "SH", as_of=NOW)

    market = payload["market"]
    assert market["price_source"] == "FEATURE_LKG"
    assert market["latest_price"] == Decimal("11.0")
    assert market["quote_status"] == "UNKNOWN: RuntimeError: quote down"
    assert market["eod_feature_close"] == Decimal("11.0")


@pytest.mark.asyncio
async def test_suspended_quote_does_not_replace_lkg_price():
    """NEW-CTX-001：停牌快照不冒充可交易实时价。"""
    plan_v1 = _plan(1)
    plan_v2 = _plan(2, supersedes=plan_v1["entry_plan_id"])
    facts = _facts(plan_v1, plan_v2)
    revision = _daily_revision(facts["security"]["security_id"])
    quote_service = _FakeQuoteService(_FakeQuoteSnapshot("10.8", suspended=True))
    payload = await _service(
        facts, _feature(), revision, quote_service=quote_service,
    ).execute(uuid4(), "600300", "SH", as_of=NOW)

    market = payload["market"]
    assert market["price_source"] == "FEATURE_LKG"
    assert market["quote_status"] == "SUSPENDED"
    assert market["latest_price"] == Decimal("11.0")

@pytest.mark.asyncio
async def test_multi_timeframe_fetches_deep_structure_once():
    """R3-P2-008：get_intraday_structure 一次已含 60m/15m/5m——
    一个 Context 只允许调用一次，绝不按周期重复抓取。"""
    plan_v1 = _plan(1)
    facts = _facts(plan_v1, _plan(2, supersedes=plan_v1["entry_plan_id"]))
    deep = _FakeDeepService()
    service = ReadPositionContextService(
        lambda: _FakeUow(facts, _feature(), _daily_revision(facts["security"]["security_id"])),
        clock=lambda: NOW,
        calendar=_WeekdayCalendar(),
        deep_market_data=deep,
    )
    payload = await service.execute(
        facts["security"]["security_id"] and uuid4(), "600300", "SH", as_of=NOW,
    )
    assert deep.calls == 1
    for period in ("60m", "15m", "5m"):
        assert payload["multi_timeframe"][period]["status"] == "AVAILABLE"
        assert payload["multi_timeframe"][period]["precision"] == "LIMITED"
