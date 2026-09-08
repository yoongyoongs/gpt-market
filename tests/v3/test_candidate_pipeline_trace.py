"""Step 13：候选主链路装配 + 全链 Trace 测试。

覆盖：漏斗单调递减、Safety 淘汰原因进 trace、no_expert_hit、
Final=Deep 前 30、生命轨迹 stage 完整性、for_code 查询。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.v3.application.candidate_pipeline import FINAL_TOP_N, CandidatePipeline
from app.v3.candidate_engine.trace import ScanTraceBuilder
from app.v3.domain.candidate_engine import (
    DeepRankEntry,
    DeepRankResult,
    ExpertFeatureView,
    ExpertInput,
    MachineRankEntry,
    MachineRankResult,
    ParetoEntry,
    ParetoResult,
    SafetyCandidateInput,
    TRACE_STAGES,
)
from app.v3.domain.market_data import Market, SecurityMember

NOW = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)

GOOD_FEATURES = {
    "position_250d": 0.2,
    "position_52w": 0.25,
    "distance_60d_low": 0.10,
    "distance_60d_high": -0.20,
    "distance_120d_high": -0.15,
    "return_20d": 0.02,
    "return_5d": 0.01,
    "atr_contraction": 0.6,
    "low_slope_20": -0.001,
    "weekly_slope_8w": -0.01,
    "weekly_decline_deceleration": 0.01,
    "ma20_slope_delta": 0.002,
    "macd_hist_delta": 0.001,
    "close_ma20_distance": -0.02,
    "swing_low_trend": 0.0,
    "up_down_volume_ratio": 1.05,
    "obv_slope_z": 0.4,
    "volume_5_20": 1.1,
    "turnover_rate": 1.2,
    "volume_spike": 1.0,
    "breakout_extension_20d": 0.02,
    "pullback_20d": False,
    "rs_5d_slope": 0.003,
    "rs_20d_slope": 0.002,
    "consecutive_up_days": 1,
    "daily_trend_state": "UP",
    "weekly_trend_state": "UP",
    "multi_timeframe_state": "WEEKLY_UP_DAILY_UP",
}


def _member(code: str, **overrides) -> SecurityMember:
    values = dict(
        code=code,
        market=Market.SZ if code.startswith("0") else Market.SH,
        name=f"股票{code}",
    )
    values.update(overrides)
    return SecurityMember(**values)


def _universe() -> tuple[SafetyCandidateInput, ...]:
    codes = ["000001", "000002", "600001", "600002", "600003", "600004"]
    entries = [
        SafetyCandidateInput(
            security_id=uuid4(), member=_member(code), close=10.0, bar_count=250,
        )
        for code in codes
    ]
    # ST / 停牌 / 新股 / 无特征行
    entries.append(SafetyCandidateInput(
        security_id=uuid4(), member=_member("000003", is_st=True), close=10.0, bar_count=250,
    ))
    entries.append(SafetyCandidateInput(
        security_id=uuid4(), member=_member("000004", suspended=True), close=10.0, bar_count=250,
    ))
    entries.append(SafetyCandidateInput(
        security_id=uuid4(), member=_member("000005", is_new_listing=True),
        close=10.0, bar_count=30,
    ))
    entries.append(SafetyCandidateInput(
        security_id=uuid4(), member=_member("000006"), close=None, bar_count=None,
    ))
    return tuple(entries)


def _stocks_by_id(candidates) -> dict:
    stocks: dict = {}
    for candidate in candidates:
        if candidate.member.code in {"000006"}:
            continue  # 无特征行
        stocks[candidate.security_id] = ExpertInput(
            feature=ExpertFeatureView(
                security_id=candidate.security_id,
                code=candidate.member.code,
                close=candidate.close or 10.0,
                as_of=NOW,
                features=dict(GOOD_FEATURES),
            ),
        )
    return stocks


def _run():
    candidates = _universe()
    result = CandidatePipeline().execute(
        candidates, _stocks_by_id(candidates), trade_date=NOW,
    )
    return result, candidates


class TestCandidatePipeline:
    def test_funnel_monotonic_and_stage_names(self):
        result, _ = _run()
        names = [stage.stage for stage in result.funnel.stages]
        assert names == ["UNIVERSE", "SAFETY", "RECALL", "ENRICH", "PARETO",
                         "MACHINE", "DEEP", "FINAL"]
        counts = [stage.output_count for stage in result.funnel.stages]
        assert counts == sorted(counts, reverse=True)
        assert counts[0] == 10

    def test_safety_drops_into_trace(self):
        result, _ = _run()
        for code, reason_part in (
            ("000003", "ST"), ("000004", "SUSPENDED"), ("000006", "DAILY_KLINE_MISSING"),
        ):
            trace = result.trace.for_code(code)
            assert trace is not None
            assert trace.drop_stage == "SAFETY"
            assert reason_part in (trace.drop_reason or "")
        new_stock = result.trace.for_code("000005")
        assert new_stock.drop_reason == "NEW_STOCK_POOL"

    def test_eligible_stock_full_trajectory(self):
        result, _ = _run()
        trace = result.trace.for_code("000001")
        assert trace is not None
        stages = [record.stage for record in trace.records]
        assert stages == list(TRACE_STAGES)
        assert trace.last_alive_stage is not None
        recall = trace.stage("RECALL")
        assert recall is not None and recall.alive
        assert recall.rank is not None

    def test_final_entries_are_deep_head(self):
        result, _ = _run()
        deep_codes = [entry.code for entry in result.deep.entries]
        final_codes = [entry.code for entry in result.final_entries]
        assert final_codes == deep_codes[:FINAL_TOP_N]
        assert len(final_codes) <= FINAL_TOP_N

    def test_trace_covers_whole_universe(self):
        result, candidates = _run()
        assert len(result.trace.traces) == len(candidates)
        for trace in result.trace.traces:
            assert trace.records[0].stage == "UNIVERSE"
            assert trace.records[0].alive is True

    def test_no_expert_hit_recorded(self):
        """有特征但一路不中的股票：RECALL 层显式 drop，不静默。"""
        candidates = _universe()
        stocks = _stocks_by_id(candidates)
        # 抹掉全部特征 → 六特征专家全部不可评
        for stock in stocks.values():
            object.__setattr__(stock.feature, "features", {})
        result = CandidatePipeline().execute(
            tuple(candidates), stocks, trade_date=NOW,
        )
        trace = result.trace.for_code("000001")
        assert trace.drop_stage == "RECALL"
        assert trace.drop_reason == "no_expert_hit"

    def test_scan_result_contract_complete(self):
        result, _ = _run()
        assert result.safety.universe_count == 10
        assert result.union.evaluated_count == result.safety.universe_count - 4
        assert result.pareto.evaluated_count == result.union.union_count
        assert result.funnel.scan_id == result.scan_id
        assert result.trace.scan_id == result.scan_id


class TestTraceUnselectedKeepRank:
    """任务书 P0-08：未入选也保存真实 rank/score（Why Not 可查）。"""

    @staticmethod
    def _builder() -> ScanTraceBuilder:
        return ScanTraceBuilder(scan_id=uuid4(), trade_date=NOW)

    def test_machine_unselected_keep_real_rank_score(self):
        builder = self._builder()
        kept, dropped = uuid4(), uuid4()
        builder.record_universe([])  # 补 UNIVERSE 行逻辑走 _dead 内部
        builder.record_machine(MachineRankResult(
            evaluated_count=2, top_n=1,
            entries=(
                MachineRankEntry(security_id=kept, code="000001",
                                 machine_score=88.0, rank=1, selected=True),
                MachineRankEntry(security_id=dropped, code="000002",
                                 machine_score=61.5, rank=167, selected=False),
            ),
        ))
        trace = next(t for t in builder.build().traces if t.security_id == dropped)
        record = trace.stage("MACHINE")
        assert record is not None and record.alive is False
        assert record.rank == 167
        assert record.score == 61.5
        assert record.drop_reason == "machine_rank_below_top1"

    def test_pareto_unselected_keep_front_detail(self):
        builder = self._builder()
        dropped = uuid4()
        builder.record_pareto(ParetoResult(
            evaluated_count=1, selected_count=0, protected_count=0,
            front_sizes=(1,),
            entries=(ParetoEntry(
                security_id=dropped, code="000003", rrf_norm=42.5,
                scores={}, front=5, crowding=1.25, selected=False,
            ),),
        ))
        trace = next(t for t in builder.build().traces if t.security_id == dropped)
        record = trace.stage("PARETO")
        assert record is not None and record.alive is False
        assert record.detail["front"] == 5
        assert record.detail["crowding"] == 1.25
        assert record.detail["rrf_norm"] == 42.5

    def test_final_dead_rows_keep_deep_rank_score(self):
        builder = self._builder()
        in_final, deep_only = uuid4(), uuid4()
        builder.record_deep(DeepRankResult(
            evaluated_count=2, top_n=2,
            entries=(
                DeepRankEntry(security_id=in_final, code="000004",
                              deep_score=90.0, rank=1),
                DeepRankEntry(security_id=deep_only, code="000005",
                              deep_score=70.0, rank=31),
            ),
        ))
        builder.record_final((DeepRankEntry(
            security_id=in_final, code="000004", deep_score=90.0, rank=1,
        ),))
        trace = next(t for t in builder.build().traces if t.security_id == deep_only)
        record = trace.stage("FINAL")
        assert record is not None and record.alive is False
        assert record.drop_reason == "final_not_top30"
        assert record.rank == 31
        assert record.score == 70.0
