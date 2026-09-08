"""L2 Multi-Recall 八路专家（设计 §5-§12）。

EXPERTS 为固定注册序（RRF 权重第一版统一 1.0，与顺序无关）。
"""

from __future__ import annotations

from app.v3.candidate_engine.experts.base import BaseExpert
from app.v3.candidate_engine.experts.evidence_experts import (
    CatalystExpert,
    FundamentalRepairExpert,
)
from app.v3.candidate_engine.experts.feature_experts import (
    AccumulationExpert,
    BottomingExpert,
    LowPositionExpert,
    PullbackExpert,
    RelativeStrengthExpert,
    ReversalExpert,
)
from app.v3.domain.candidate_engine import RecallExpert

__all__ = [
    "BaseExpert",
    "EXPERT_NAMES",
    "default_experts",
    "AccumulationExpert",
    "BottomingExpert",
    "CatalystExpert",
    "FundamentalRepairExpert",
    "LowPositionExpert",
    "PullbackExpert",
    "RecallExpert",
    "RelativeStrengthExpert",
    "ReversalExpert",
]

EXPERT_NAMES = ("LP", "BT", "RV", "AC", "PB", "RS", "FQ", "CAT")


def default_experts() -> tuple[RecallExpert, ...]:
    """默认八路专家实例（注入式：扫描服务可按配置裁剪）。"""
    return (
        LowPositionExpert(),
        BottomingExpert(),
        ReversalExpert(),
        AccumulationExpert(),
        PullbackExpert(),
        RelativeStrengthExpert(),
        FundamentalRepairExpert(),
        CatalystExpert(),
    )
