"""P1-01~05：Deep 数据接入契约测试（任务书 §19-§23）。

- P1-01 两阶段 pipeline（run_to_machine/complete_deep 等价 execute）+ 60m 事实
- P1-02 MarketRegimeScore（index 50/breadth 30/appetite 20，missing 归一）
- P1-03 行业无可靠源 → missing + NO_RELIABLE_INDUSTRY_CONTEXT
- P1-04 components_detail {raw, normalized, weight, missing, source}
- P1-05 Final=RAW_TOP30 + AI_REVIEW_STATUS=NOT_CONNECTED
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.v3.application.candidate_pipeline import (
    AI_REVIEW_STATUS,
    CandidatePipeline,
)
from app.v3.candidate_engine.deep_rank import (
    DeepRankService,
    evaluate_reversal_evidence,
    _minute60_score,
)
from app.v3.candidate_engine.market_regime_score import MarketRegimeScoreService
from app.v3.candidate_engine.trace import ScanTraceBuilder
from app.v3.domain.candidate_engine import (
    AI_REVIEW_STATUS as DOMAIN_AI_REVIEW_STATUS,
)
from app.v3.domain.candidate_engine import (
    DeepContext,
    DeepRankEntry,
    DeepRankResult,
    Minute60Fact,
)
from tests.v3.test_candidate_pipeline_trace import (
    NOW,
    _stocks_by_id,
    _universe,
)


def _deep_item(**extra) -> dict:
    base = {
        "security_id": uuid4(),
        "code": f"{uuid4().int % 1000000:06d}",
        "machine_score": 80.0,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# P1-01：60m 事实 → 执行分（stale/UNTRUSTED 不当事实）
# ---------------------------------------------------------------------------


class TestMinute60Score:
    @pytest.mark.parametrize(("state", "raw"), [
        ("UP", 90.0), ("SIDEWAYS", 60.0), ("DOWN", 30.0),
    ])
    def test_state_mapping(self, state, raw):
        assert _minute60_score({"state": state}) == (raw, ())

    def test_unknown_state_not_a_fact(self):
        score, reasons = _minute60_score({"state": "UNKNOWN"})
        assert score is None
        assert reasons == ()

    def test_missing_fact_silent(self):
        assert _minute60_score(None) == (None, ())

    def test_stale_not_fact(self):
        score, reasons = _minute60_score({"state": "UP", "stale": True})
        assert score is None
        assert reasons == ("minute_60_stale_not_fact",)

    def test_untrusted_quality_not_fact(self):
        score, reasons = _minute60_score({"state": "UP", "quality": "UNTRUSTED"})
        assert score is None
        assert reasons == ("minute_60_untrusted_quality",)

    def test_service_sources_distinguish_fetched_vs_not(self):
        """P1-04 source：带 minute_60 键=已抓取路径，否则 not_fetched。"""
        fetched = DeepRankService().execute([
            _deep_item(minute_60={"state": "UP"}),
        ]).entries[0]
        assert fetched.components_detail["minute_60_execution"]["source"] == "minute_60_fetch"
        assert fetched.components_detail["minute_60_execution"]["raw"] == 90.0

        not_fetched = DeepRankService().execute([_deep_item()]).entries[0]
        detail = not_fetched.components_detail["minute_60_execution"]
        assert detail["source"] == "not_fetched"
        assert detail["missing"] is True


class TestReversalEvidenceGroupD:
    """条件组 D（60m trend==UP）仅当数据已接入（item 带 minute_60_state 键）
    才参与判定；接入路径内非 UP 一律不当作通过（§9.2）。"""

    BASE = dict(
        weekly_state="DOWN", daily_state="UP",
        multi_state="WEEKLY_DOWN_DAILY_BOUNCE",
        weekly_decline_deceleration=0.03, rv_score=75.0,
    )

    def test_no_key_keeps_three_condition_semantics(self):
        item = _deep_item(**self.BASE)
        assert evaluate_reversal_evidence(item) is True

    def test_key_up_passes(self):
        item = _deep_item(**self.BASE, minute_60_state="UP")
        assert evaluate_reversal_evidence(item) is True

    @pytest.mark.parametrize("state", ["SIDEWAYS", "DOWN", "UNKNOWN", None])
    def test_key_not_up_fails(self, state):
        item = _deep_item(**self.BASE, minute_60_state=state)
        assert evaluate_reversal_evidence(item) is False

    def test_conflict_reasons_present_when_group_d_fails(self):
        item = _deep_item(**self.BASE, minute_60_state="SIDEWAYS")
        entry = DeepRankService().execute([item]).entries[0]
        assert entry.trend_conflict is True
        assert "trend_conflict_no_reversal_evidence" in entry.reasons


# ---------------------------------------------------------------------------
# P1-02：Market Regime Score
# ---------------------------------------------------------------------------

_FULL_REGIME = {
    "regime_snapshot_id": "snap-1",
    "index_states": {"status": "UP"},
    "breadth": {"advance_decline_ratio": 1.0, "mean_return_3d": 0.0, "observed": 100},
    "risk_appetite_facts": {"volume_expansion_count": 10, "breakout_20d_count": 5},
    "coverage": 0.8,
    "stale": False,
}


class TestMarketRegimeScore:
    def test_full_snapshot_value(self):
        value, detail = MarketRegimeScoreService().compute(_FULL_REGIME)
        # index UP 80*0.5 + breadth(0.5→50)*0.3 + appetite(0.8/0.5/0.5→65)*0.2 = 68
        assert value == pytest.approx(68.0)
        assert detail["index_state"] == 80.0
        assert detail["breadth"] == pytest.approx(50.0)
        assert detail["risk_appetite"] == pytest.approx(65.0)

    def test_down_index_low_not_zero(self):
        snapshot = {**_FULL_REGIME, "index_states": {"status": "DOWN"}}
        value, detail = MarketRegimeScoreService().compute(snapshot)
        assert detail["index_state"] == 35.0
        assert value > 0  # §20.4 市场 DOWN 降分不 hard reject

    def test_all_missing_returns_none(self):
        value, detail = MarketRegimeScoreService().compute({})
        assert value is None
        assert detail["confidence"] == 0.0

    def test_observed_zero_appetite_subitems_missing(self):
        snapshot = {
            **_FULL_REGIME,
            "breadth": {**_FULL_REGIME["breadth"], "observed": 0},
        }
        _value, detail = MarketRegimeScoreService().compute(snapshot)
        assert detail["appetite_rates"]["volume_expansion_rate"] is None
        assert detail["appetite_rates"]["breakout_rate"] is None

    def test_partial_missing_normalizes(self):
        """breadth 缺 mean_return_3d：AD 单维有效权重归一（P0-05 语义）。"""
        snapshot = {
            **_FULL_REGIME,
            "breadth": {"advance_decline_ratio": 1.0, "observed": 100},
        }
        _value, detail = MarketRegimeScoreService().compute(snapshot)
        assert detail["breadth"] == pytest.approx(50.0)  # ratio 1:1 → 0.5 满权


# ---------------------------------------------------------------------------
# P1-03/P1-04：components_detail 证据结构
# ---------------------------------------------------------------------------


class TestComponentsDetail:
    def test_machine_component_detail(self):
        entry = DeepRankService().execute([_deep_item()]).entries[0]
        detail = entry.components_detail["machine"]
        assert detail == {
            "raw": 80.0, "normalized": 0.8, "weight": 45.0,
            "missing": False, "source": "machine_rank",
        }

    def test_industry_context_always_missing_with_reason(self):
        """P1-03：无可靠行业源 → missing + 明确原因，禁止默认分。"""
        item = _deep_item(industry_missing_reason="NO_RELIABLE_INDUSTRY_CONTEXT")
        entry = DeepRankService().execute([item]).entries[0]
        detail = entry.components_detail["industry_context"]
        assert detail["missing"] is True
        assert detail["raw"] is None
        assert detail["source"] == "NO_RELIABLE_INDUSTRY_CONTEXT"
        assert "NO_RELIABLE_INDUSTRY_CONTEXT" in entry.reasons

    def test_regime_source_propagates(self):
        item = _deep_item(market_regime_score=68.5, market_regime_source="snap-1")
        entry = DeepRankService().execute([item]).entries[0]
        detail = entry.components_detail["market_regime"]
        assert detail["source"] == "snap-1"
        assert detail["raw"] == 68.5
        assert detail["missing"] is False

    def test_regime_absent_source_is_no_snapshot(self):
        entry = DeepRankService().execute([_deep_item()]).entries[0]
        detail = entry.components_detail["market_regime"]
        assert detail["source"] == "no_regime_snapshot"
        assert detail["missing"] is True

    def test_rr_component_source(self):
        item = _deep_item(rr_score=70.0)
        entry = DeepRankService().execute([item]).entries[0]
        detail = entry.components_detail["risk_reward_refined"]
        assert detail["source"] == "rr_engine"
        assert detail["normalized"] == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# P1-01：两阶段 pipeline 等价性 + DeepContext 注入
# ---------------------------------------------------------------------------


class TestTwoPhasePipeline:
    def test_two_phase_equals_execute(self):
        candidates = _universe()
        stocks = _stocks_by_id(candidates)
        whole = CandidatePipeline().execute(candidates, stocks, trade_date=NOW)

        pipeline = CandidatePipeline()
        state = pipeline.run_to_machine(candidates, stocks, trade_date=NOW)
        assert state.machine is not None
        assert state.scan_id == state.builder.build().scan_id
        result = pipeline.complete_deep(state)

        assert [e.code for e in result.final_entries] == \
            [e.code for e in whole.final_entries]
        assert [e.deep_score for e in result.deep.entries] == \
            [e.deep_score for e in whole.deep.entries]
        assert [s.output_count for s in result.funnel.stages] == \
            [s.output_count for s in whole.funnel.stages]

    def test_machine_phase_does_not_run_deep(self):
        state = CandidatePipeline().run_to_machine(
            _universe(), _stocks_by_id(_universe()), trade_date=NOW,
        )
        stage_names = [s.stage for s in state.stages]
        assert stage_names == ["UNIVERSE", "SAFETY", "RECALL", "ENRICH",
                               "PARETO", "MACHINE"]
        assert state.machine.top_n == state.stages[-1].output_count

    def test_deep_context_injects_minute60_and_regime(self):
        """DeepContext 注入后：60m 分进池、regime 分进 components_detail。"""
        candidates = _universe()
        stocks = _stocks_by_id(candidates)
        pipeline = CandidatePipeline()
        state = pipeline.run_to_machine(candidates, stocks, trade_date=NOW)
        top_id = state.machine.entries[0].security_id
        context = DeepContext(
            minute_60_by_id={top_id: Minute60Fact(state="UP")},
            market_regime_score=68.5,
            market_regime_source="snap-1",
        )
        result = pipeline.complete_deep(state, deep_context=context)
        entry = next(e for e in result.deep.entries if e.security_id == top_id)
        assert entry.components_detail["minute_60_execution"]["raw"] == 90.0
        assert entry.components_detail["market_regime"]["raw"] == 68.5
        assert entry.components_detail["market_regime"]["source"] == "snap-1"

    def test_deep_context_none_degrades(self):
        """deep_context=None（离线路径）：60m/regime 恒 missing，不伪造。"""
        result = CandidatePipeline().execute(
            _universe(), _stocks_by_id(_universe()), trade_date=NOW,
        )
        for entry in result.deep.entries:
            assert entry.components_detail["minute_60_execution"]["missing"] is True
            assert entry.components_detail["market_regime"]["missing"] is True
            assert entry.components_detail["industry_context"]["missing"] is True


# ---------------------------------------------------------------------------
# P1-05：Final = RAW_TOP30 + AI_REVIEW_STATUS
# ---------------------------------------------------------------------------


class TestFinalContract:
    def test_constant_single_source(self):
        assert AI_REVIEW_STATUS == DOMAIN_AI_REVIEW_STATUS == "NOT_CONNECTED"

    def test_final_alive_rows_carry_contract(self):
        builder = ScanTraceBuilder(scan_id=uuid4(), trade_date=NOW)
        top = uuid4()
        builder.record_deep(DeepRankResult(
            evaluated_count=2, top_n=2,
            entries=(
                DeepRankEntry(security_id=top, code="000001", deep_score=90.0, rank=1),
                DeepRankEntry(security_id=uuid4(), code="000002",
                              deep_score=70.0, rank=31),
            ),
        ))
        builder.record_final((
            DeepRankEntry(security_id=top, code="000001", deep_score=90.0, rank=1),
        ))
        trace = builder.build().traces[0]
        final_record = trace.stage("FINAL")
        assert final_record.alive is True
        assert final_record.detail["top_type"] == "RAW_TOP30"
        assert final_record.detail["ai_review_status"] == "NOT_CONNECTED"

    def test_deep_tail_final_dead_row(self):
        """Deep Top60 未进 Final 的股票带 alive=False 行（final_rank_below_top30）。"""
        candidates = _universe()
        result = CandidatePipeline().execute(candidates, _stocks_by_id(candidates),
                                             trade_date=NOW)
        if len(result.deep.entries) <= len(result.final_entries):
            pytest.skip("deep 未超过 final 规模，无尾行场景")
        final_ids = {e.security_id for e in result.final_entries}
        tail = next(e for e in result.deep.entries if e.security_id not in final_ids)
        trace = result.trace.for_code(tail.code)
        record = trace.stage("FINAL")
        assert record.alive is False
        assert record.drop_reason == "final_rank_below_top30"


# ---------------------------------------------------------------------------
# R2.1-P0-06：Deep 全池（≤Top120）保留 rank/score + trace dead 行
# ---------------------------------------------------------------------------


class TestDeepFullPoolTrace:
    """DeepRankService 不再截断 top_n——#61~120 selected=False，
    Trace 记 deep_rank_below_top60 dead 行（Why Not 可查 Deep #85）。"""

    @staticmethod
    def _pool(n: int) -> list[dict]:
        return [_deep_item(machine_score=80.0 - i * 0.1) for i in range(n)]

    @staticmethod
    def _ranked(n: int):
        result = DeepRankService(top_n=60).execute(TestDeepFullPoolTrace._pool(n))
        builder = ScanTraceBuilder(scan_id=uuid4(), trade_date=NOW)
        builder.record_deep(result)
        return result, builder

    def test_full_pool_kept_not_truncated(self):
        result, _ = self._ranked(65)
        assert len(result.entries) == 65
        assert result.top_n == 60
        assert sum(1 for entry in result.entries if entry.selected) == 60
        assert all(entry.selected for entry in result.entries[:60])
        assert not any(entry.selected for entry in result.entries[60:])
        assert [entry.rank for entry in result.entries] == list(range(1, 66))
        # 未入选者保留真实分数（不许 0 分占位）
        assert result.entries[63].deep_score > 0

    def test_trace_dead_rows_keep_rank_score(self):
        result, builder = self._ranked(65)
        traces = {t.security_id: t for t in builder.build().traces}
        tail = result.entries[63]
        record = traces[tail.security_id].stage("DEEP")
        assert record.alive is False
        assert record.drop_reason == "deep_rank_below_top60"
        assert record.rank == 64
        assert record.score == tail.deep_score
        # 入选者仍 alive
        head = traces[result.entries[0].security_id].stage("DEEP")
        assert head.alive is True

    def test_final_tail_reason_and_61_120_no_final_row(self):
        result, builder = self._ranked(65)
        builder.record_final(tuple(result.entries[:30]))
        traces = {t.security_id: t for t in builder.build().traces}
        # Deep #31~60：FINAL dead final_rank_below_top30（保留 deep rank/score）
        mid = result.entries[44]
        final_record = traces[mid.security_id].stage("FINAL")
        assert final_record.alive is False
        assert final_record.drop_reason == "final_rank_below_top30"
        assert final_record.rank == 45
        assert final_record.score == mid.deep_score
        # Deep #61~120：无 FINAL 行（死在 DEEP，从未进 Final 候选池）
        tail = result.entries[63]
        assert traces[tail.security_id].stage("FINAL") is None
