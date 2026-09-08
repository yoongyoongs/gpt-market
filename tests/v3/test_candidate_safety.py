"""Step 3：L1 Hard Safety Filter 单元与边界测试（任务书 §4/§35）。

覆盖：
- 每条硬规则独立触发（资格类 + 数据质量类）；
- 配置化开关（exclude_st / reject_stale / min_daily_bars）；
- NEW_STOCK_POOL 不静默丢弃、不计 reject；
- 多原因并存；
- 无隐藏机会型硬过滤（机会型指标异常值不导致 reject）。
"""

from __future__ import annotations

import pytest

from app.v3.candidate_engine.config import SafetyConfig
from app.v3.candidate_engine.safety import HardSafetyFilterService
from app.v3.domain.candidate_engine import SafetyCandidateInput
from app.v3.domain.market_data import Market, SecurityMember


def _member(**overrides) -> SecurityMember:
    values = {
        "code": "000001",
        "market": Market.SZ,
        "name": "平安银行",
        "trading_status": "ACTIVE",
        "is_st": False,
        "suspended": False,
        "is_new_listing": False,
        "delisting_risk": False,
    }
    values.update(overrides)
    return SecurityMember(**values)


def _candidate(**overrides) -> SafetyCandidateInput:
    values = {
        "security_id": "00000000-0000-0000-0000-000000000001",
        "member": _member(),
        "close": 10.0,
        "stale": False,
        "coverage": 1.0,
        "bar_count": 250,
    }
    values.update(overrides)
    return SafetyCandidateInput(**values)


@pytest.fixture()
def service() -> HardSafetyFilterService:
    return HardSafetyFilterService(SafetyConfig())


class TestTradingEligibilityRules:
    def test_suspended_flag_rejects(self, service):
        result = service.execute((_candidate(member=_member(suspended=True)),))
        verdict = result.rejected[0]
        assert not verdict.eligible
        assert "SUSPENDED" in verdict.hard_reasons

    def test_suspended_trading_status_rejects(self, service):
        result = service.execute((_candidate(member=_member(trading_status="SUSPENDED")),))
        assert "SUSPENDED" in result.rejected[0].hard_reasons

    def test_delisting_risk_rejects(self, service):
        result = service.execute((_candidate(member=_member(delisting_risk=True)),))
        assert "DELISTING_RISK" in result.rejected[0].hard_reasons

    def test_delisted_status_rejects(self, service):
        result = service.execute((_candidate(member=_member(trading_status="DELISTED")),))
        assert "DELISTED" in result.rejected[0].hard_reasons

    def test_st_rejected_by_default(self, service):
        result = service.execute((_candidate(member=_member(is_st=True, name="ST某股")),))
        assert "ST" in result.rejected[0].hard_reasons

    def test_st_kept_when_exclude_st_disabled(self):
        service = HardSafetyFilterService(SafetyConfig(exclude_st=False))
        result = service.execute((_candidate(member=_member(is_st=True)),))
        assert len(result.eligible) == 1

    def test_unknown_trading_status_does_not_reject(self, service):
        # UNKNOWN 是数据缺失语义，不是资格问题（universe 默认值）
        result = service.execute((_candidate(member=_member(trading_status="UNKNOWN")),))
        assert len(result.eligible) == 1


class TestDataQualityRules:
    def test_missing_close_rejects(self, service):
        result = service.execute((_candidate(close=None),))
        assert "QUOTE_INVALID" in result.rejected[0].hard_reasons

    def test_non_positive_price_rejects(self, service):
        result = service.execute((_candidate(close=0.0),))
        assert "QUOTE_INVALID" in result.rejected[0].hard_reasons

    def test_stale_rejects_by_default(self, service):
        result = service.execute((_candidate(stale=True),))
        assert "DATA_STALE" in result.rejected[0].hard_reasons

    def test_stale_kept_when_reject_stale_disabled(self):
        service = HardSafetyFilterService(SafetyConfig(reject_stale=False))
        result = service.execute((_candidate(stale=True),))
        assert len(result.eligible) == 1

    def test_missing_feature_row_rejects(self, service):
        row = _candidate(bar_count=None)
        result = service.execute((row,))
        assert "DAILY_KLINE_MISSING" in result.rejected[0].hard_reasons

    def test_bar_count_boundary_equals_minimum_passes(self, service):
        # 边界：bar_count == min_daily_bars（120）恰好通过
        result = service.execute((_candidate(bar_count=120),))
        assert len(result.eligible) == 1

    def test_bar_count_below_minimum_rejects(self, service):
        result = service.execute((_candidate(bar_count=119),))
        assert "DAILY_KLINE_INSUFFICIENT" in result.rejected[0].hard_reasons

    def test_new_listing_goes_to_pool_not_rejected(self, service):
        # 新股历史不足：进 NEW_STOCK_POOL，不静默丢弃也不占 reject
        result = service.execute(
            (_candidate(member=_member(is_new_listing=True), bar_count=30),)
        )
        assert not result.eligible
        assert not result.rejected
        assert len(result.new_stock_pool) == 1
        assert result.new_stock_pool[0].new_stock_pool is True

    def test_insufficient_bars_non_new_listing_is_rejected(self, service):
        result = service.execute((_candidate(bar_count=50),))
        assert len(result.rejected) == 1
        assert not result.new_stock_pool

    def test_new_listing_with_other_hard_reason_still_rejected(self, service):
        # 新股 + 停牌：资格问题优先，不允许借新股池绕过资格硬过滤
        result = service.execute(
            (_candidate(member=_member(is_new_listing=True, suspended=True), bar_count=30),)
        )
        assert len(result.rejected) == 1
        assert not result.new_stock_pool


class TestNoOpportunityHardFilters:
    """整改核心验证：机会型指标即使极端，也不允许硬杀。"""

    def test_low_price_small_amount_stock_survives(self, service):
        # 旧 V1 链会因成交额不足硬杀；新 L1 必须存活
        result = service.execute((_candidate(close=3.2, bar_count=250),))
        assert len(result.eligible) == 1
        assert result.eligible[0].hard_reasons == ()

    def test_all_reasons_accumulate(self, service):
        # 多原因并存：停牌 + 过期 + 价格异常同时记录，可解释可追溯
        result = service.execute(
            (_candidate(member=_member(suspended=True), stale=True, close=None),)
        )
        verdict = result.rejected[0]
        assert {"SUSPENDED", "DATA_STALE", "QUOTE_INVALID"} <= set(verdict.hard_reasons)

    def test_universe_count_accounts_every_stock(self, service):
        inputs = (
            _candidate(),
            _candidate(member=_member(suspended=True)),
            _candidate(member=_member(code="300001"), bar_count=10),
            _candidate(member=_member(is_new_listing=True, code="600999"), bar_count=30),
        )
        result = service.execute(inputs)
        assert result.universe_count == 4
        assert (
            len(result.eligible) + len(result.rejected) + len(result.new_stock_pool) == 4
        )

    def test_data_quality_reported_for_eligible(self, service):
        result = service.execute((_candidate(),))
        quality = result.eligible[0].data_quality
        assert quality.quote_ok is True
        assert quality.daily_kline_ok is True
        assert quality.feature_stale is False
