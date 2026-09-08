"""Step 17-18：Outcome Label / Metrics / Miss Audit / Shadow Pool 测试。

覆盖：A/B/C 阈值边界、观察窗不足、time_to 首触序、触前回撤不含到达日、
Recall@K 分母、Precision@K、NDCG 手算、miss audit 触发条件与证据、
shadow 四组分层/seed 复现/不重叠、TraceView 转换。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.v3.application.backtest_metrics import BacktestMetricsService
from app.v3.application.miss_audit import MissAuditService
from app.v3.application.outcome_label import LabelThresholds, OutcomeLabelService
from app.v3.application.shadow_pool import ShadowPoolService
from app.v3.application.trace_view import TraceView, to_trace_views
from app.v3.domain.candidate_engine import OutcomeLabelResult

# ---------------------------------------------------------------------------
# Outcome Label（§26）
# ---------------------------------------------------------------------------


def _bars(closes_high: list[float], lows: list[float] | None = None) -> list[tuple[float, float]]:
    """以 close_t=10 为基准，传相对涨幅序列生成 (high, low)。"""
    out = []
    for index, gain in enumerate(closes_high):
        low_gain = (lows or [])[index] if lows and index < len(lows) else 0.0
        out.append((round(10.0 * (1.0 + gain), 6), round(10.0 * (1.0 + low_gain), 6)))
    return out


def _flat(n: int, high_gain=0.0, low_gain=0.0) -> list[tuple[float, float]]:
    return [(10.0 * (1 + high_gain), 10.0 * (1 + low_gain))] * n


class TestOutcomeLabel:
    def setup_method(self):
        self.svc = OutcomeLabelService()

    def test_grade_a_path(self):
        # 前 10 根内触 +16%，此前最低 -3%（≥-5%）
        highs = [0.02] * 9 + [0.16] + [0.16] * 10
        lows = [-0.03] * 20
        result = self.svc.evaluate("600001", 10.0, _bars(highs, lows))
        assert result.time_to_15 == 10
        assert result.label == "A"
        assert result.mfe_20 == pytest.approx(0.16)

    def test_grade_a_boundary_exact(self):
        # MFE 恰 0.15、time 恰 15、触前回撤恰 -0.05 → 全部压线成立
        highs = [0.0] * 14 + [0.15] + [0.15] * 5
        lows = [-0.05] * 20
        result = self.svc.evaluate("600001", 10.0, _bars(highs, lows))
        assert result.time_to_15 == 15
        assert result.label == "A"

    def test_grade_a_reject_pullback_too_deep(self):
        # MFE 足够但触前回撤 -0.09：A(-0.05)/B(-0.06)/C(-0.08) 全部不达 → None
        highs = [0.0] * 9 + [0.16] + [0.16] * 10
        lows = [-0.09] * 20
        result = self.svc.evaluate("600001", 10.0, _bars(highs, lows))
        assert result.label is None

    def test_grade_a_reject_too_late(self):
        highs = [0.0] * 15 + [0.16] + [0.16] * 4
        lows = [-0.01] * 20
        result = self.svc.evaluate("600001", 10.0, _bars(highs, lows))
        assert result.time_to_15 == 16
        assert result.label != "A"

    def test_grade_b(self):
        # 触 +12% 但没到 +15%；触前回撤 -0.04（≥-0.06）
        highs = [0.01] * 5 + [0.12] + [0.12] * 14
        lows = [-0.04] * 20
        result = self.svc.evaluate("600001", 10.0, _bars(highs, lows))
        assert result.time_to_10 == 6
        assert result.time_to_15 is None
        assert result.label == "B"

    def test_grade_b_boundary_mae_floor(self):
        highs = [0.01] * 5 + [0.12] + [0.12] * 14
        lows = [-0.06] * 20
        result = self.svc.evaluate("600001", 10.0, _bars(highs, lows))
        assert result.label == "B"  # -0.06 >= -0.06 压线成立

    def test_grade_c_fallback(self):
        # MFE 0.09 < B 门 0.10 → 走 C；MAE 恰 -0.08 压线
        highs = [0.09] * 20
        lows = [-0.08] * 20
        result = self.svc.evaluate("600001", 10.0, _bars(highs, lows))
        assert result.label == "C"

    def test_grade_c_reject_mae_too_deep(self):
        highs = [0.09] * 20
        lows = [-0.09] * 20
        result = self.svc.evaluate("600001", 10.0, _bars(highs, lows))
        assert result.label is None

    def test_no_move_no_label(self):
        result = self.svc.evaluate("600001", 10.0, _flat(20))
        assert result.label is None
        assert result.mfe_20 == 0.0
        assert result.mae_20 == 0.0

    def test_incomplete_window_pending(self):
        """观察窗 19 根 < 20 → label 恒 None（宁可 PENDING 不可猜）。"""
        highs = [0.16] * 19
        lows = [-0.01] * 19
        result = self.svc.evaluate("600001", 10.0, _bars(highs, lows))
        assert result.bars_used == 19
        assert result.label is None
        assert result.is_good is False

    def test_time_to_first_touch(self):
        highs = [0.02, 0.05, 0.09, 0.09, 0.11, 0.11, 0.11] + [0.11] * 13
        result = self.svc.evaluate("600001", 10.0, _bars(highs))
        assert result.time_to_8 == 3
        assert result.time_to_10 == 5

    def test_mae_before_target_excludes_touch_day(self):
        """触前回撤不含到达日：第 3 天长下影反转（low -10%）不应毁掉 A。"""
        highs = [0.02, 0.05, 0.16] + [0.16] * 17
        lows = [-0.01, -0.02, -0.10] + [-0.02] * 17  # 第 3 根是到达日
        result = self.svc.evaluate("600001", 10.0, _bars(highs, lows))
        assert result.time_to_15 == 3
        assert result.label == "A"  # 触前窗口 lows[:2] 最低 -0.02

    def test_custom_thresholds(self):
        svc = OutcomeLabelService(LabelThresholds(a_mfe=0.30))
        highs = [0.16] * 20
        result = svc.evaluate("600001", 10.0, _bars(highs, [-0.01] * 20))
        assert result.label == "B"  # 0.16 不再满足收紧后的 A，落 B


# ---------------------------------------------------------------------------
# Metrics（§27-§29）
# ---------------------------------------------------------------------------


def _labels() -> list[OutcomeLabelResult]:
    def _label(code, label):
        return OutcomeLabelResult(code=code, label=label, close_t=10.0)

    return [_label("600001", "A"), _label("600002", "B"), _label("600003", "C"),
            _label("600004", None), _label("600005", "A")]


class TestBacktestMetrics:
    """任务书 P0-12/P0-13：Pool 语义 + Recall300≠120 + Precision@60[DEEP]
    + NDCG 漏选惩罚 + PENDING。"""

    def setup_method(self):
        self.svc = BacktestMetricsService()
        self.labels = _labels()

    def _rankings(self, machine=None, deep=None, final=None):
        return {"machine": machine or [], "deep": deep or [], "final": final or []}

    def test_recall_pools_denominator_all_good(self):
        pools = {
            "recall_pool": [("600001", 1), ("600004", 2), ("600009", 3)],
            "pareto_pool": [("600001", 1), ("600002", 2), ("600003", 3), ("600005", 4)],
        }
        result = self.svc.compute(self.labels, pools, rankings=self._rankings())
        entries = {e.pool: e for e in result.entries if e.pool}
        assert result.good_count == 3  # A+B+A
        assert result.labeled_count == 4  # 三个评级 + 一个 None
        assert entries["recall_pool"].numerator == 1
        assert entries["recall_pool"].denominator == 3
        assert abs(entries["recall_pool"].value - 1 / 3) < 1e-9
        assert entries["pareto_pool"].value == 1.0
        assert entries["pareto_pool"].k == 4

    def test_recall300_differs_from_120(self):
        """任务书 P0-13：A 类在 Machine rank=250 → Recall@300 命中、@120 不命中。"""
        # Machine 全量 ranking 350 只；600005(A) 排 250
        machine = [(f"9{i:04d}", i) for i in range(1, 250)]
        machine += [("600005", 250)]
        machine += [(f"9{i:04d}", i) for i in range(250, 350)]
        # 显式池只给 recall_pool，TopK 池由 ranking 派生
        result = self.svc.compute(
            self.labels, {}, rankings=self._rankings(machine=machine),
        )
        entries = {e.pool: e for e in result.entries if e.pool}
        assert entries["top300_machine"].numerator == 1  # rank250 ∈ Top300
        assert abs(entries["top300_machine"].value - 1 / 3) < 1e-9
        assert entries["top120_machine"].numerator == 0  # rank250 ∉ Top120
        assert entries["top120_machine"].value == 0.0
        # 派生池 k 取实际容量
        assert entries["top300_machine"].k == 300
        assert entries["top120_machine"].k == 120

    def test_precision60_uses_deep_source(self):
        """Deep 有 60 只 → Precision@60 用 DEEP；Final 只有 30 不硬凑。"""
        deep = [(f"8{i:04d}", i) for i in range(1, 61)]
        deep[0] = ("600001", 1)  # Deep 第 1 名是 A
        deep[29] = ("600002", 30)  # Deep 第 30 名是 B
        final = [(f"7{i:04d}", i) for i in range(1, 31)]
        final[0] = ("600005", 1)  # Final 第 1 名是 A
        result = self.svc.compute(
            self.labels, {}, rankings=self._rankings(deep=deep, final=final),
        )
        entries = {(e.metric, e.k): e for e in result.entries}
        p60 = entries[("precision", 60)]
        assert p60.ranking_source == "deep"
        assert p60.numerator == 2  # 600001 + 600002
        assert abs(p60.value - 2 / 60) < 1e-9
        p30 = entries[("precision", 30)]
        assert p30.ranking_source == "final"
        assert p30.numerator == 1
        p10 = entries[("precision", 10)]
        assert p10.value == 0.1

    def test_precision_source_smaller_than_k_not_applicable(self):
        """任务书 §14.4 方案 A：Final30 无 Precision@60、Deep 不足 → NOT_APPLICABLE。"""
        final = [(f"7{i:04d}", i) for i in range(1, 31)]
        result = self.svc.compute(
            self.labels, {}, rankings=self._rankings(final=final),
        )
        entries = {(e.metric, e.k): e for e in result.entries}
        p60 = entries[("precision", 60)]
        assert p60.status == "NOT_APPLICABLE"
        assert p60.value is None
        assert p60.reason == "POOL_SMALLER_THAN_K"

    def test_ndcg_missed_a_penalized(self):
        """任务书 P0-13：GT 有 A+B，ranking 只含 B → NDCG<1（旧版=1 是 bug）。"""
        # 只把 600002(B) 排进 final；600001(A)/600005(A) 漏选
        final = [("600002", 1)]
        result = self.svc.compute(
            self.labels, {}, rankings=self._rankings(final=final),
        )
        ndcg10 = [e for e in result.entries
                  if e.metric == "ndcg" and e.k == 10 and e.ranking_source == "final"][0]
        assert ndcg10.value < 1.0
        # DCG = 3（B gain2→2^2-1=3）
        # GT 理想序 gains [3,3,2,1]（A,A,B,C）→ IDCG = 7/1+7/l3+3/l4+1/l5
        dcg = 3.0
        idcg = 7.0 + 7.0 / _log2(3) + 3.0 / _log2(4) + 1.0 / _log2(5)
        assert abs(ndcg10.value - dcg / idcg) < 1e-9

    def test_ndcg_ideal_order_is_one(self):
        # ranking 覆盖 GT 全部正例（A,A,B,C）且按理想序 → NDCG=1
        final = [("600001", 1), ("600005", 2), ("600002", 3), ("600003", 4)]
        result = self.svc.compute(
            self.labels, {}, rankings=self._rankings(final=final),
        )
        ndcg10 = [e for e in result.entries
                  if e.metric == "ndcg" and e.k == 10 and e.ranking_source == "final"][0]
        assert abs(ndcg10.value - 1.0) < 1e-9

    def test_ndcg_partial_gain_order(self):
        # final 只含 600003(C, gain=1) 与 600005(A, gain=3)，K=10
        # IDCG 来自 GT（A,A,B,C 理想序），不再是 ranking 自身 gains
        final = [("600003", 1), ("600005", 2)]
        result = self.svc.compute(
            self.labels, {}, rankings=self._rankings(final=final),
        )
        ndcg10 = [e for e in result.entries
                  if e.metric == "ndcg" and e.k == 10 and e.ranking_source == "final"][0]
        dcg = 1.0 + 7.0 / _log2(3)
        idcg = 7.0 + 7.0 / _log2(3) + 3.0 / _log2(4) + 1.0 / _log2(5)
        assert abs(ndcg10.value - dcg / idcg) < 1e-9

    def test_pending_when_no_matured(self):
        """任务书 P0-13：无成熟 outcome → status=PENDING / value=None。"""
        labels = [OutcomeLabelResult(code="600001", label=None),
                  OutcomeLabelResult(code="600002", label=None)]
        result = self.svc.compute(
            labels, {}, rankings=self._rankings(final=[("600001", 1)]),
        )
        assert result.entries
        for entry in result.entries:
            assert entry.status == "PENDING"
            assert entry.value is None
            assert entry.reason == "OUTCOME_WINDOW_NOT_MATURE"

    def test_no_good_no_crash(self):
        labels = [OutcomeLabelResult(code="600001", label="C")]
        result = self.svc.compute(
            labels, {}, rankings=self._rankings(final=[("600001", 1)]),
        )
        assert result.good_count == 0
        # 已成熟但域内无 GOOD → Recall NOT_APPLICABLE 不伪装 0；
        # NDCG 域内有 C（gain=1）仍可算；Precision 分母是 K，0 命中是真实 0
        by_metric = {}
        for entry in result.entries:
            by_metric.setdefault(entry.metric, set()).add(entry.status)
        assert by_metric["recall"] == {"NOT_APPLICABLE"}
        assert by_metric["ndcg"] == {"OK"}
        # final 仅 1 只 < K、deep 空 → Precision 全 NOT_APPLICABLE（不硬凑）
        assert by_metric["precision"] == {"NOT_APPLICABLE"}
        assert all(entry.value is None for entry in result.entries
                   if entry.metric != "ndcg")


def _log2(value: float) -> float:
    import math
    return math.log2(value)


# ---------------------------------------------------------------------------
# Miss Audit（§30）
# ---------------------------------------------------------------------------


def _views() -> list[TraceView]:
    def _view(code, **kw):
        drop = kw.get("drop")
        default_last = {"MACHINE": "PARETO", "PARETO": "MACHINE",
                        "SAFETY": "UNIVERSE", "DEEP": "MACHINE", "RECALL": "SAFETY"}
        return TraceView(code=code, security_id=uuid4(),
                         last_alive_stage=kw.get("last", default_last.get(drop, "SAFETY")),
                         drop_stage=drop, drop_reason=kw.get("reason"),
                         machine_rank=kw.get("mrank"),
                         pareto_front=kw.get("front"),
                         pareto_protected=kw.get("protected", False),
                         expert_ranks=kw.get("experts", {}),
                         recall_rank=kw.get("rrank"),
                         deep_rank=kw.get("drank"),
                         final=kw.get("final", False))

    return [
        _view("600001", final=True),                                # Final 不审计
        _view("600002", drop="MACHINE", mrank=80, experts={"LOW_BASE": 1}),   # good → 审计
        _view("600003", drop="SAFETY", reason="ST"),                 # SAFETY 误杀审计
        _view("600004", final=True),                                # Final 不审计
        _view("600005", drop="PARETO", front=2, protected=True, mrank=45, experts={"BOTTOM_REVERSAL": 2, "LOW_POSITION": 5}, rrank=44, drank=None),
    ]


class TestMissAudit:
    def test_only_good_non_final_collected(self):
        labels = [OutcomeLabelResult(code="600001", label="A"),
                  OutcomeLabelResult(code="600002", label="B"),
                  OutcomeLabelResult(code="600003", label="A"),
                  OutcomeLabelResult(code="600005", label="A")]
        entries = MissAuditService().collect(_views(), labels)
        assert [e.code for e in entries] == ["600002", "600003", "600005"]  # 按 code 排序
        assert entries[0].future_label == "B"
        assert entries[0].drop_stage == "MACHINE"
        assert entries[0].last_alive_stage == "PARETO"
        # SAFETY 淘汰但未来 A：资格误杀正是审计重点
        assert entries[1].drop_stage == "SAFETY"
        assert entries[1].future_label == "A"

    def test_evidence_fields(self):
        labels = [OutcomeLabelResult(code="600005", label="A", mfe_20=0.18, mae_20=-0.04, time_to_10=6)]
        entries = MissAuditService().collect(_views(), labels)
        audit = entries[0].audit
        assert audit["pareto_front"] == 2
        assert audit["protected"] is True
        assert audit["machine_rank"] == 45
        # P0-11：experts=[{expert, rank}] 按 rank ASC；recall_rank 是
        # Trace RECALL 行真实 RRF 名次，≠ min(expert_rank)
        assert audit["experts"] == [
            {"expert": "BOTTOM_REVERSAL", "rank": 2},
            {"expert": "LOW_POSITION", "rank": 5},
        ]
        assert audit["recall_rank"] == 44
        assert "deep_rank" not in audit  # 未进 Deep 不伪造
        assert audit["mfe_20"] == 0.18

    def test_missing_view_skipped(self):
        labels = [OutcomeLabelResult(code="699999", label="A")]  # 不在 views
        assert MissAuditService().collect(_views(), labels) == []


# ---------------------------------------------------------------------------
# Shadow Pool（§31.2）
# ---------------------------------------------------------------------------


def _shadow_views() -> list[TraceView]:
    def _view(code, drop, **kw):
        return TraceView(code=code, security_id=uuid4(),
                         last_alive_stage="MACHINE", drop_stage=drop,
                         drop_reason=kw.get("reason"),
                         machine_rank=kw.get("mrank"),
                         pareto_front=kw.get("front"),
                         expert_ranks=kw.get("experts", {}))

    # P0-10：near_miss 窗口 = (machine_top_n, machine_top_n+window] = (120, 220]
    return [
        _view("600001", "MACHINE", mrank=80),                                   # random（rank≤120 不该 dead，防御归 random）
        _view("600002", "PARETO", front=2),                                     # near_miss（前沿近线）
        _view("600003", "DEEP", experts={"BOTTOM_REVERSAL": 1}),                # single_expert
        _view("600004", "SAFETY", reason="ST"),                                 # other
        _view("600005", "MACHINE", mrank=500),                                  # random（离线太远）
        _view("600006", "RECALL", reason="no_expert_hit"),                      # random
        _view("600007", "MACHINE", mrank=130),                                  # near_miss（121~220 窗口）
        _view("600008", "PARETO", front=1),                                     # near_miss
        _view("600009", "DEEP", experts={"MACD_DIVERGENCE": 2, "VOLUME_SPIKE": 3}),  # 双专家→random
        _view("600010", "MACHINE", mrank=150),                                  # near_miss
    ]


class TestShadowPool:
    def test_stratification_rules(self):
        # size 全开（10）→ 每只都抽中，可精确断言分组
        svc = ShadowPoolService(sizes={"near_miss": 10, "single_expert": 10,
                                       "random": 10, "other": 10}, seed=1)
        chosen = svc.sample(_shadow_views())
        by_code = {e.code: e.sample_group for e in chosen}
        # P0-10：rank 80 在 (120,220] 窗口外（top120 内不该 dead）→ random
        assert by_code["600001"] == "random"
        assert by_code["600002"] == "near_miss"   # PARETO front 2
        assert by_code["600007"] == "near_miss"   # MACHINE rank 130 ∈ (120,220]
        assert by_code["600008"] == "near_miss"
        assert by_code["600003"] == "single_expert"  # 单专家 rank1
        assert by_code["600004"] == "other"       # SAFETY 资格淘汰
        assert by_code["600009"] == "random"      # 双专家不算 single_expert
        assert by_code["600005"] == "random"      # MACHINE rank 500 离线
        assert by_code["600006"] == "random"
        assert by_code["600010"] == "near_miss"   # MACHINE rank 150 ∈ (120,220]

    def test_group_sizes_capped(self):
        svc = ShadowPoolService(sizes={"near_miss": 1, "single_expert": 1,
                                       "random": 1, "other": 1}, seed=1)
        chosen = svc.sample(_shadow_views())
        groups = [e.sample_group for e in chosen]
        for group in ("near_miss", "single_expert", "random", "other"):
            assert groups.count(group) == 1

    def test_seed_reproducible(self):
        a = ShadowPoolService(seed=7).sample(_shadow_views())
        b = ShadowPoolService(seed=7).sample(_shadow_views())
        assert [(e.code, e.sample_group) for e in a] == [(e.code, e.sample_group) for e in b]

    def test_samples_do_not_overlap(self):
        svc = ShadowPoolService(sizes={"near_miss": 10, "single_expert": 10,
                                       "random": 10, "other": 10}, seed=3)
        chosen = svc.sample(_shadow_views())
        codes = [e.code for e in chosen]
        assert len(codes) == len(set(codes))

    def test_final_never_sampled(self):
        views = _shadow_views()
        views.append(TraceView(code="600011", security_id=uuid4(),
                               last_alive_stage="FINAL", drop_stage=None,
                               drop_reason=None, final=True))
        chosen = ShadowPoolService().sample(views)
        assert all(e.code != "600011" for e in chosen)

    def test_single_expert_requires_single_hit(self):
        """双专家命中 rank≤3 不算 single_expert（600009）。

        P0-10 reallocation 后可经 random 组补抽（reason 记来源），
        但 sample_group 绝不为 single_expert。"""
        svc = ShadowPoolService(sizes={"near_miss": 0, "single_expert": 5,
                                       "random": 0, "other": 0}, seed=1)
        chosen = svc.sample(_shadow_views())
        for entry in chosen:
            if entry.code == "600009":
                assert entry.sample_group == "random"
                assert entry.sample_reason is not None
        assert all(e.sample_group != "single_expert" for e in chosen
                   if e.code == "600009")


# ---------------------------------------------------------------------------
# Mature 编排纯函数（T 日窗口切分）
# ---------------------------------------------------------------------------


class TestMatureSplitTDay:
    def test_t_day_window_converts_utc_morning_to_shanghai_day(self):
        from datetime import datetime, timezone

        from app.v3.application.mature_scan_outcomes import t_day_window

        # 2026-09-01 08:00 UTC = 上海 16:00 同日
        since, t_ordinal = t_day_window(datetime(2026, 9, 1, 8, tzinfo=timezone.utc))
        assert since.utcoffset() is not None
        # 窗口起点是上海 9-1 00:00（= UTC 8-31 16:00）
        assert since.hour == 16 and since.day == 31 and since.month == 8

    def test_split_t_day_first_bar_is_t_day(self):
        from datetime import datetime, timezone

        from app.v3.application.mature_scan_outcomes import MatureScanOutcomesService

        # T 日 = 2026-09-01 上海；首根 bar 9-1 07:00 UTC（15:00 北京收盘）
        t_ordinal = datetime(2026, 9, 1, tzinfo=timezone.utc).date().toordinal()
        series = [
            (datetime(2026, 9, 1, 7, tzinfo=timezone.utc), 11.0, 9.9, 10.5),
            (datetime(2026, 9, 2, 7, tzinfo=timezone.utc), 11.5, 10.2, 11.2),
        ]
        close_t, future = MatureScanOutcomesService._split_t_day(series, t_ordinal)
        assert close_t == 10.5
        assert future == [(11.5, 10.2)]

    def test_split_t_day_suspension_pending(self):
        """T 日停牌（首根已是次日）→ 基准缺失 PENDING。"""
        from datetime import datetime, timezone

        from app.v3.application.mature_scan_outcomes import MatureScanOutcomesService

        t_ordinal = datetime(2026, 9, 1, tzinfo=timezone.utc).date().toordinal()
        series = [
            (datetime(2026, 9, 2, 7, tzinfo=timezone.utc), 11.0, 9.9, 10.5),
        ]
        close_t, future = MatureScanOutcomesService._split_t_day(series, t_ordinal)
        assert close_t is None
        assert future == []

    def test_split_t_day_empty(self):
        from app.v3.application.mature_scan_outcomes import MatureScanOutcomesService

        assert MatureScanOutcomesService._split_t_day([], 1) == (None, [])


# ---------------------------------------------------------------------------
# TraceView 转换（内存路径）
# ---------------------------------------------------------------------------


class TestTraceViewConversion:
    def test_to_trace_views_from_pipeline(self):
        from tests.v3.test_candidate_pipeline_trace import _run
        result, _ = _run()
        views = to_trace_views(result)
        assert len(views) == len(result.trace.traces)
        by_code = {v.code: v for v in views}
        # 000001 一路存活进 Final
        assert by_code["000001"].final is True
        assert by_code["000001"].drop_stage is None
        assert by_code["000001"].machine_rank is not None
        # ST 股死在 SAFETY，无机器名次
        assert by_code["000003"].drop_stage == "SAFETY"
        assert by_code["000003"].final is False
        assert by_code["000003"].machine_rank is None
        # expert_ranks 来自 union
        assert by_code["000001"].expert_ranks  # 全特征股至少一个专家命中
        # P0-11：recall_rank 来自 union RRF 真实名次；进 Deep 的股 deep_rank 非空
        assert by_code["000001"].recall_rank is not None
        assert by_code["000001"].deep_rank is not None
        # ST 股无 Recall/Deep 行
        assert by_code["000003"].recall_rank is None
        assert by_code["000003"].deep_rank is None
