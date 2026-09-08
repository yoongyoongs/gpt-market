"""Step 8-9：Pareto 多目标保护 + 单专家保护测试（任务书 §35）。

第二轮 P0-01/P0-02 补充：
- Test 1~4（任务书 §3.6）：F1~F3 保护不被 target_min 提前打断、
  边界 Front 按 crowding→RRF→code 截断、crowding 参与 survivor 选择；
- swap-in 语义：保护注入不扩池（≤ target_max），protected 保留
  真实 front/scores/rrf；
- 单专家定义收严：hit_count == 1 且 rank <= ceiling。
"""

from __future__ import annotations

from uuid import uuid4

from app.v3.candidate_engine.pareto import ParetoConfig, ParetoService
from app.v3.domain.candidate_engine import (
    CROWDING_BOUNDARY,
    ExpertHit,
    ParetoCandidateInput,
)

DIMS = ("position", "transition", "accumulation", "quality_catalyst", "risk_reward")


def _candidate(code: str, rrf: float = 50.0, **scores) -> ParetoCandidateInput:
    values = {dim: None for dim in DIMS}
    values.update(scores)
    missing = tuple(dim for dim, value in values.items() if value is None)
    return ParetoCandidateInput(
        security_id=uuid4(), code=code, rrf_norm=rrf,
        scores=values, missing_dimensions=missing,
    )


def _hit(expert: str, code: str, rank: int, security_id) -> ExpertHit:
    return ExpertHit(
        expert=expert, security_id=security_id, code=code,
        score=90.0, rank=rank,
    )


class TestDomination:
    def test_dominates_requires_all_ge_and_one_gt(self):
        config = ParetoConfig(target_min=1, target_max=10, reserve_ratio=0.0)
        a = _candidate("A", position=60, transition=60, accumulation=60,
                       quality_catalyst=60, risk_reward=60)
        b = _candidate("B", position=60, transition=60, accumulation=60,
                       quality_catalyst=60, risk_reward=59)
        c = _candidate("C", position=60, transition=60, accumulation=60,
                       quality_catalyst=60, risk_reward=60)
        result = ParetoService(config).execute([a, b, c])
        by_code = {e.code: e for e in result.entries}
        # A 支配 B → B front=2；A 与 C 互不支配 → 同 front
        assert by_code["A"].front == 1
        assert by_code["C"].front == 1
        assert by_code["B"].front == 2

    def test_missing_dimension_neutral_not_zero(self):
        """缺 P5 的 A 与 P5=50 的 B 完全等价（中性语义，不惩罚缺失）。"""
        config = ParetoConfig(target_min=1, target_max=10, reserve_ratio=0.0)
        a = _candidate("A", position=70, transition=70, accumulation=70,
                       quality_catalyst=70)  # risk_reward 缺失
        b = _candidate("B", position=70, transition=70, accumulation=70,
                       quality_catalyst=70, risk_reward=50.0)
        result = ParetoService(config).execute([a, b])
        fronts = {e.code: e.front for e in result.entries}
        assert fronts["A"] == fronts["B"]

    def test_p5_missing_for_all_degrades_gracefully(self):
        """P5 全体缺失（v1 无 RiskReward）→ Pareto 仍出 front。"""
        config = ParetoConfig(target_min=2, target_max=10, reserve_ratio=0.0)
        a = _candidate("A", position=80)
        b = _candidate("B", position=50)
        result = ParetoService(config).execute([a, b])
        by_code = {e.code: e for e in result.entries}
        assert by_code["A"].front == 1
        assert by_code["B"].front == 2


def _layer(index: int, size: int, front_no: int) -> ParetoCandidateInput:
    """构造第 front_no 层第 index 只（共 size 只）。

    P1 归一化到同一值域 [0, 99]（层间值域重叠，浅层总能支配深层），
    P2 = (200 - front_no) - P1 整体随层号下沉。层内 P1 升 / P2 降
    互不支配；层间被浅层支配。"""
    p = index * 99.0 / max(1, size - 1)
    return _candidate(
        f"F{front_no}_{index:04d}",
        position=p,
        transition=(200.0 - front_no) - p,
        accumulation=50.0,
        quality_catalyst=50.0,
        risk_reward=50.0,
    )


class TestFrontProtectionRound2:
    """任务书 P0-01 §3.6 Test 1~3。"""

    def test_f1_to_f3_all_kept_even_past_target_min(self):
        """Test 1：F1=100 F2=175 F3=50 → 全保留 selected=325，
        绝不因 target_min=250 在 F2 后提前停止。"""
        config = ParetoConfig(target_min=250, target_max=400, reserve_ratio=0.0)
        candidates = (
            [_layer(i, 100, 1) for i in range(100)]
            + [_layer(j, 175, 2) for j in range(175)]
            + [_layer(k, 50, 3) for k in range(50)]
        )
        result = ParetoService(config).execute(candidates)
        assert result.front_sizes[:3] == (100, 175, 50)
        assert result.selected_count == 325
        fronts_kept = {
            e.front for e in result.entries if e.selected
        }
        assert fronts_kept == {1, 2, 3}

    def test_boundary_front_truncated_by_crowding_not_rrf(self):
        """Test 2：F1=150 F2=150 F3=200 → F3 只取 100，选中者是
        crowding 更高的稀疏组，而非 RRF 靠前的候选（rrf 全同）。"""
        config = ParetoConfig(target_min=250, target_max=400, reserve_ratio=0.0)
        candidates = (
            [_layer(i, 150, 1) for i in range(150)]
            + [_layer(j, 150, 2) for j in range(150)]
        )
        # F3 全体被上层支配：dense 挤在 [39.6, 40.5]（gap≈0.009，crowding 低），
        # sparse 铺在 [0,39.5]∪[41,99]（gap 0.8~1.2，crowding 高），
        # P1/P2 全局边界解均落在 sparse 组。
        dense, sparse = [], []
        for i in range(50):
            p = i * (39.5 / 49.0)
            sparse.append(_candidate(f"F3S_{i:03d}", position=p,
                                     transition=197.0 - p,
                                     accumulation=50.0, quality_catalyst=50.0,
                                     risk_reward=50.0))
        for i in range(100):
            p = 39.6 + i * (0.9 / 99.0)
            dense.append(_candidate(f"F3D_{i:03d}", position=p,
                                    transition=197.0 - p,
                                    accumulation=50.0, quality_catalyst=50.0,
                                    risk_reward=50.0))
        for i in range(50):
            p = 41.0 + i * (58.0 / 49.0)
            sparse.append(_candidate(f"F3S_{i + 50:03d}", position=p,
                                     transition=197.0 - p,
                                     accumulation=50.0, quality_catalyst=50.0,
                                     risk_reward=50.0))
        candidates.extend(dense)
        candidates.extend(sparse)
        result = ParetoService(config).execute(candidates)
        assert result.front_sizes[:3] == (150, 150, 200)
        assert result.selected_count == 400
        f3_selected = [e for e in result.entries
                       if e.code.startswith("F3") and e.selected]
        f3_dropped = [e for e in result.entries
                      if e.code.startswith("F3") and not e.selected]
        assert len(f3_selected) == 100
        assert all(e.code.startswith("F3S") for e in f3_selected)
        assert all(e.code.startswith("F3D") for e in f3_dropped)
        assert min(e.crowding for e in f3_selected) >= max(
            e.crowding for e in f3_dropped
        )

    def test_target_min_extends_into_front4(self):
        """Test 3：F1~F3=210 不足 250 → 追加 F4=80，最终 290。"""
        config = ParetoConfig(target_min=250, target_max=400, reserve_ratio=0.0)
        candidates = (
            [_layer(i, 70, 1) for i in range(70)]
            + [_layer(j, 70, 2) for j in range(70)]
            + [_layer(k, 70, 3) for k in range(70)]
            + [_layer(m, 80, 4) for m in range(80)]
        )
        result = ParetoService(config).execute(candidates)
        assert 250 <= result.selected_count <= 400
        assert result.selected_count == 290
        fronts_kept = {e.front for e in result.entries if e.selected}
        assert fronts_kept == {1, 2, 3, 4}


class TestCrowdingSurvival:
    """任务书 P0-01 §3.6 Test 4：crowding 真正参与 survival。"""

    def test_crowding_beats_rrf_on_boundary_front(self):
        """同一边界 Front：A crowding 高 / rrf 低 vs B crowding 低 / rrf 高，
        只剩两个名额 → A（边界解）入选，B（rrf 最高）被挤出。"""
        config = ParetoConfig(target_min=1, target_max=2, reserve_ratio=0.0)
        a = _candidate("A", rrf=10.0, position=0.0, transition=3.0,
                       accumulation=50.0, quality_catalyst=50.0, risk_reward=50.0)
        b = _candidate("B", rrf=90.0, position=1.0, transition=2.0,
                       accumulation=50.0, quality_catalyst=50.0, risk_reward=50.0)
        c = _candidate("C", rrf=50.0, position=2.0, transition=1.0,
                       accumulation=50.0, quality_catalyst=50.0, risk_reward=50.0)
        result = ParetoService(config).execute([a, b, c])
        by_code = {e.code: e for e in result.entries}
        assert by_code["A"].selected is True
        assert by_code["C"].selected is True
        assert by_code["B"].selected is False
        # 边界解 crowding 高于中间解
        assert by_code["A"].crowding == CROWDING_BOUNDARY
        assert by_code["B"].crowding < CROWDING_BOUNDARY


class TestTargetWindow:
    def _bulk(self, n: int) -> list[ParetoCandidateInput]:
        return [
            _candidate(f"{i:06d}",
                       position=float(30 + i % 40), transition=float(i % 50),
                       accumulation=float(20 + i % 60))
            for i in range(n)
        ]

    def test_overflow_truncated_to_max(self):
        """450 只互不支配（同分）→ F1=450 > 400 → 截断至 400。"""
        config = ParetoConfig(target_min=250, target_max=400, reserve_ratio=0.0)
        candidates = [
            _candidate(f"{i:06d}", position=50.0, transition=50.0,
                       accumulation=50.0, quality_catalyst=50.0, risk_reward=50.0)
            for i in range(450)
        ]
        result = ParetoService(config).execute(candidates)
        assert result.front_sizes[0] == 450
        assert result.selected_count == 400

    def test_underflow_extends_to_deeper_fronts(self):
        """候选 300（全 F1）≥250 → 全收且不越 400 上限。"""
        config = ParetoConfig(target_min=250, target_max=400, reserve_ratio=0.0)
        candidates = [
            _candidate(f"{i:06d}", position=float(i % 30), transition=float(i % 30),
                       accumulation=float(i % 30), quality_catalyst=float(i % 30),
                       risk_reward=float(i % 30))
            for i in range(300)
        ]
        result = ParetoService(config).execute(candidates)
        assert 250 <= result.selected_count <= 400

    def test_underflow_takes_all_when_few_candidates(self):
        config = ParetoConfig(target_min=250, target_max=400, reserve_ratio=0.0)
        result = ParetoService(config).execute(self._bulk(80))
        assert result.selected_count == 80

    def test_front1_all_selected_first(self):
        config = ParetoConfig(target_min=1, target_max=1, reserve_ratio=0.0)
        a = _candidate("A", position=90, transition=90, accumulation=90,
                       quality_catalyst=90, risk_reward=90)
        b = _candidate("B", position=10, transition=10, accumulation=10,
                       quality_catalyst=10, risk_reward=10)
        result = ParetoService(config).execute([b, a])
        selected_codes = [e.code for e in result.entries if e.selected]
        assert selected_codes == ["A"]

    def test_crowding_boundary_solutions_get_boundary_distance(self):
        config = ParetoConfig(target_min=5, target_max=10, reserve_ratio=0.0)
        candidates = [
            _candidate(f"{i:06d}", position=float(i), transition=50.0,
                       accumulation=50.0, quality_catalyst=50.0, risk_reward=50.0)
            for i in range(5)
        ]
        result = ParetoService(config).execute(candidates)
        crowdings = [e.crowding for e in result.entries]
        assert max(crowdings) == CROWDING_BOUNDARY
        assert all(0.0 <= c <= CROWDING_BOUNDARY for c in crowdings)


class TestSingleExpertProtection:
    """第二轮 P0-02：swap-in 名额语义 + hit_count==1 收严定义。"""

    def _four_layer_scenario(self):
        """A(90)→F1 B(80)→F2 C(70)→F3 WEAK(1)→F4；target_max=2 时
        base 只含 A/B（F3 因 max 截断），WEAK 在池外。"""
        strong_a = _candidate("A", position=90, transition=90, accumulation=90,
                              quality_catalyst=90, risk_reward=90)
        strong_b = _candidate("B", position=80, transition=80, accumulation=80,
                              quality_catalyst=80, risk_reward=80)
        strong_c = _candidate("C", position=70, transition=70, accumulation=70,
                              quality_catalyst=70, risk_reward=70)
        weak = _candidate("WEAK", position=1.0, transition=1.0, accumulation=1.0,
                          quality_catalyst=1.0, risk_reward=1.0)
        config = ParetoConfig(target_min=0, target_max=2, reserve_ratio=0.15)
        return config, [strong_a, strong_b, strong_c, weak], weak, strong_a, strong_b

    def test_top10_unselected_get_protected(self):
        config, candidates, weak, strong_a, strong_b = self._four_layer_scenario()
        result = ParetoService(config).execute(candidates, {
            "LP": (_hit("LP", "WEAK", 1, weak.security_id),
                   _hit("LP", "A", 2, strong_a.security_id)),
        })
        by_code = {e.code: e for e in result.entries}
        assert by_code["WEAK"].protected is True
        assert by_code["WEAK"].protected_reason == "single_expert:LP#rank1"
        assert by_code["WEAK"].selected is True
        assert by_code["A"].selected is True
        assert result.protected_count == 1

    def test_swap_in_respects_target_max(self):
        """保护注入不扩池：base 2 + protected 1 > target_max 2
        → 从 base 移出 front 更深者（B）补位。"""
        config, candidates, weak, strong_a, strong_b = self._four_layer_scenario()
        result = ParetoService(config).execute(candidates, {
            "LP": (_hit("LP", "WEAK", 1, weak.security_id),),
        })
        by_code = {e.code: e for e in result.entries}
        selected = {e.code for e in result.entries if e.selected}
        assert selected == {"A", "WEAK"}
        assert by_code["B"].selected is False
        assert result.selected_count == 2
        assert result.protected_count == 1

    def test_protected_keeps_real_front_scores(self):
        """protected 不退化为 front=0/scores={} 空壳，保留真实 Pareto 信息。"""
        config, candidates, weak, _, _ = self._four_layer_scenario()
        result = ParetoService(config).execute(candidates, {
            "RS": (_hit("RS", "WEAK", 3, weak.security_id),),
        })
        by_code = {e.code: e for e in result.entries}
        weak_entry = by_code["WEAK"]
        assert weak_entry.front == 4
        assert weak_entry.rrf_norm == 50.0
        assert weak_entry.scores.get("position") == 1.0
        assert weak_entry.protected_reason == "single_expert:RS#rank3"

    def test_multi_expert_hit_not_protected(self):
        """多专家共同命中（hit_count=2）不进 single_expert 保护。"""
        config, candidates, weak, _, _ = self._four_layer_scenario()
        result = ParetoService(config).execute(candidates, {
            "LP": (_hit("LP", "WEAK", 1, weak.security_id),),
            "BT": (_hit("BT", "WEAK", 1, weak.security_id),),
        })
        by_code = {e.code: e for e in result.entries}
        assert by_code["WEAK"].protected is False
        assert by_code["WEAK"].selected is False
        assert result.protected_count == 0

    def test_quota_and_dedup(self):
        config = ParetoConfig(target_min=0, target_max=400, reserve_ratio=0.15)
        # 60 名额：8 专家 × Top10 = 80 候选（互不重复）→ 截到 60
        candidates: list[ParetoCandidateInput] = []
        expert_hits: dict[str, tuple[ExpertHit, ...]] = {}
        for i in range(100):
            candidate = _candidate(f"P{i:03d}", position=float(i % 50))
            candidates.append(candidate)
        for index, name in enumerate(("LP", "BT", "RV", "AC", "PB", "RS", "FQ", "CAT")):
            expert_hits[name] = tuple(
                _hit(name, f"P{index * 10 + rank:03d}", rank + 1,
                     candidates[index * 10 + rank].security_id)
                for rank in range(10)
            )
        result = ParetoService(config).execute(candidates, expert_hits)
        assert result.protected_count == 60
        protected_ids = [e.security_id for e in result.entries if e.protected]
        assert len(protected_ids) == len(set(protected_ids))
        # 总池不越 target_max
        assert result.selected_count <= config.target_max

    def test_rank_beyond_10_not_protected(self):
        config = ParetoConfig(target_min=0, target_max=400, reserve_ratio=0.15)
        candidate = _candidate("RANK11", position=1.0)
        result = ParetoService(config).execute(
            [candidate],
            {"LP": tuple(_hit("LP", f"C{r}", r + 1, candidate.security_id) if r == 10
                         else _hit("LP", f"C{r}", r + 1, uuid4()) for r in range(11))},
        )
        protected_ids = [e.security_id for e in result.entries if e.protected]
        assert candidate.security_id not in protected_ids
        assert len(protected_ids) == 10  # Top10 名额被其他占满

    def test_selected_ids_includes_protected(self):
        config = ParetoConfig(target_min=1, target_max=10, reserve_ratio=0.15)
        weak = _candidate("W", position=1.0, transition=1.0, accumulation=1.0,
                          quality_catalyst=1.0, risk_reward=1.0)
        result = ParetoService(config).execute(
            [weak], {"RS": (_hit("RS", "W", 1, weak.security_id),)},
        )
        assert weak.security_id in result.selected_ids
