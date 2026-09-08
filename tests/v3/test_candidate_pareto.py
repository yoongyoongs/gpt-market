"""Step 8-9：Pareto 多目标保护 + 单专家保护测试（任务书 §35）。

覆盖：支配关系边界（全>=且一>）、front 分层、目标数量 250~400
（超额截断/不足追加）、crowding 边界解、维度缺失中性语义、
单专家保护 15% 配额与去重。
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


def _candidate(code: str, **scores) -> ParetoCandidateInput:
    values = {dim: None for dim in DIMS}
    values.update(scores)
    missing = tuple(dim for dim, value in values.items() if value is None)
    return ParetoCandidateInput(
        security_id=uuid4(), code=code, rrf_norm=50.0,
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
        config = ParetoConfig(target_min=1, target_max=400, reserve_ratio=0.0)
        a = _candidate("A", position=90, transition=90, accumulation=90,
                       quality_catalyst=90, risk_reward=90)
        b = _candidate("B", position=10, transition=10, accumulation=10,
                       quality_catalyst=10, risk_reward=10)
        result = ParetoService(config).execute([b, a])
        selected_codes = [e.code for e in result.entries if e.selected]
        assert selected_codes == ["A"]


class TestCrowding:
    def test_boundary_solutions_get_boundary_distance(self):
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
    def test_top10_unselected_get_protected(self):
        config = ParetoConfig(target_min=1, target_max=10, reserve_ratio=0.15)
        weak = _candidate("WEAK", position=1.0, transition=1.0, accumulation=1.0,
                          quality_catalyst=1.0, risk_reward=1.0)
        strong = _candidate("STRONG", position=90, transition=90, accumulation=90,
                            quality_catalyst=90, risk_reward=90)
        result = ParetoService(config).execute([weak, strong], {
            "LP": (_hit("LP", "WEAK", 1, weak.security_id),
                   _hit("LP", "STRONG", 2, strong.security_id)),
        })
        by_code = {e.code: e for e in result.entries}
        assert by_code["STRONG"].selected is True
        assert by_code["WEAK"].protected is True
        assert by_code["WEAK"].protected_reason == "single_expert:LP#rank1"
        assert result.protected_count == 1

    def test_quota_and_dedup(self):
        config = ParetoConfig(target_min=0, target_max=400, reserve_ratio=0.15)
        # 60 名额：10 个专家 × Top10 = 100 候选 → 截到 60；同股两专家去重
        candidates: list[ParetoCandidateInput] = []
        expert_hits: dict[str, tuple[ExpertHit, ...]] = {}
        for i in range(100):
            candidate = _candidate(f"P{i:03d}", position=float(i % 50))
            candidates.append(candidate)
        for index, name in enumerate(("LP", "BT", "RV", "AC", "PB", "RS", "FQ", "CAT")):
            expert_hits[name] = tuple(
                _hit(name, f"P{i:03d}", rank + 1, candidates[index * 10 + rank].security_id)
                for rank in range(10)
            )
        # 前两名在 LP 与 BT 重复 → 去重后仍不超配额
        result = ParetoService(config).execute(candidates, expert_hits)
        assert result.protected_count == 60
        protected_ids = [e.security_id for e in result.entries if e.protected]
        assert len(protected_ids) == len(set(protected_ids))

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
