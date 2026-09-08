"""任务书 P0-10：Shadow Pool near_miss 窗口 + fallback reallocation。

- Machine near miss = (machine_top_n, machine_top_n+window]（120,220]，
  旧逻辑 near_rank_ceiling=max(size,100) 恒假永不产生 near_miss；
- Pareto near miss = front<=5 且 selected=False；
- 组缺口 fallback：sample_group 保持原语义，reason 记
  quota_reallocation_from_<group>；
- 总量 < SHADOW_TARGET_MIN(200) → shortfall_reason 不静默。
"""

from __future__ import annotations

import uuid

from app.v3.application.shadow_pool import SHADOW_TARGET_MIN, ShadowPoolService
from app.v3.application.trace_view import TraceView


def _view(
    code: str,
    *,
    drop_stage: str = "MACHINE",
    machine_rank: int | None = None,
    pareto_front: int | None = None,
    pareto_selected: bool = False,
    expert_ranks: dict[str, int] | None = None,
    final: bool = False,
) -> TraceView:
    return TraceView(
        code=code,
        security_id=uuid.uuid4(),
        last_alive_stage=None,
        drop_stage=drop_stage,
        drop_reason=f"drop:{code}",
        machine_rank=machine_rank,
        machine_selected=machine_rank is not None and machine_rank <= 120,
        pareto_front=pareto_front,
        pareto_selected=pareto_selected,
        expert_ranks=expert_ranks or {},
        final=final,
    )


class TestNearMissWindow:
    def test_machine_rank_121_to_220_is_near_miss(self):
        """P0-10 核心：rank∈(120,220] 进 near_miss（旧逻辑恒不命中）。"""
        views = [_view(f"1{i:05d}", machine_rank=120 + i) for i in range(1, 101)]
        service = ShadowPoolService()
        entries = service.sample(views)
        near = [e for e in entries if e.sample_group == "near_miss"]
        assert len(near) == 100
        assert all(e.sample_reason is None for e in near)

    def test_machine_rank_beyond_window_not_near_miss(self):
        views = (
            [_view(f"1{i:05d}", machine_rank=120 + i) for i in range(1, 101)]
            + [_view(f"2{i:05d}", machine_rank=221 + i) for i in range(60)]
        )
        service = ShadowPoolService()
        entries = service.sample(views)
        near = [e for e in entries if e.sample_group == "near_miss"]
        assert len(near) == 100
        assert all(int(e.code[1:]) <= 220 for e in near)

    def test_pareto_front_le_5_unselected_is_near_miss(self):
        views = [
            _view(f"3{i:05d}", drop_stage="PARETO", pareto_front=i,
                  pareto_selected=False)
            for i in range(1, 6)
        ]
        service = ShadowPoolService(sizes={
            "near_miss": 5, "single_expert": 0, "random": 0, "other": 0,
        })
        entries = service.sample(views)
        assert {e.sample_group for e in entries} == {"near_miss"}

    def test_pareto_selected_never_near_miss(self):
        """front<=5 但已入选的不进 near_miss（淘汰池无此股，防御）。"""
        views = [
            _view("400001", drop_stage="PARETO", pareto_front=1,
                  pareto_selected=True),
        ]
        service = ShadowPoolService(sizes={
            "near_miss": 5, "single_expert": 0, "random": 0, "other": 0,
        })
        entries = service.sample(views)
        assert all(e.sample_group != "near_miss" for e in entries)


class TestQuotaReallocation:
    def test_shortfall_group_reallocated_with_reason(self):
        """组缺口 → 名额转 near_miss/random，reason 记来源；全池抽干。"""
        views = (
            # near_miss 池 30（<100）+ random 池 80；single_expert/other 空
            [_view(f"1{i:05d}", machine_rank=121 + i) for i in range(30)]
            + [
                _view(f"2{i:05d}", drop_stage="RECALL", expert_ranks={"LP": 5})
                for i in range(80)
            ]
        )
        service = ShadowPoolService(sizes={
            "near_miss": 100, "single_expert": 50, "random": 50, "other": 50,
        })
        entries = service.sample(views)
        assert len(entries) == 110  # 池总量 30+80 全部抽干
        realloc = [e for e in entries if e.sample_reason is not None]
        assert realloc, "缺口必须触发 reallocation"
        assert all(
            e.sample_reason.startswith("quota_reallocation_from_")
            for e in realloc
        )
        # 组语义不被篡改：填充条目的 group 是 near_miss/random，不是来源组
        assert {e.sample_group for e in realloc} <= {"near_miss", "random"}

    def test_shortfall_reason_when_below_target_min(self):
        views = [_view(f"5{i:05d}", machine_rank=121 + i) for i in range(50)]
        service = ShadowPoolService()
        entries = service.sample(views)
        assert len(entries) < SHADOW_TARGET_MIN
        assert service.last_shortfall_reason is not None
        assert service.last_shortfall_reason.startswith("shadow_shortfall:")
        assert f"near_miss={min(50, 100)}" in service.last_shortfall_reason

    def test_no_shortfall_reason_when_enough(self):
        views = (
            [_view(f"6{i:05d}", machine_rank=121 + i) for i in range(100)]
            + [
                _view(f"7{i:05d}", drop_stage="RECALL", expert_ranks={"LP": 5})
                for i in range(120)
            ]
        )
        service = ShadowPoolService()
        entries = service.sample(views)
        assert len(entries) >= SHADOW_TARGET_MIN
        assert service.last_shortfall_reason is None
