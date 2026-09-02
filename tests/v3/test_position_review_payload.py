"""RT-07：PositionReviewPayload 类型化 + 动作枚举（实时方案 §9.2/§9.5/§17.3）。

- 冻结枚举 HOLD/ADD/REDUCE/EXIT；兼容层 SELL → EXIT（不长期并存多个语义）；
- REDUCE 必须给出 reduce_ratio（§9.6）；未知动作拒绝；
- 不产生 Trade：Position Review 只是建议。
"""

from __future__ import annotations

import pytest

from app.v3.domain.position_review import PositionReviewPayload, RecommendedAction


def test_action_enum_is_frozen_vocabulary() -> None:
    assert {action.value for action in RecommendedAction} == {
        "HOLD", "ADD", "REDUCE", "EXIT",
    }


def test_sell_maps_to_exit() -> None:
    payload = PositionReviewPayload.from_payload({
        "recommended_action": "SELL",
        "reason": "兼容旧 AI 输出",
    })
    assert payload.recommended_action == RecommendedAction.EXIT


def test_reduce_requires_reduce_ratio() -> None:
    with pytest.raises(ValueError):
        PositionReviewPayload.from_payload({
            "recommended_action": "REDUCE", "reason": "到 T1",
        })
    payload = PositionReviewPayload.from_payload({
        "recommended_action": "REDUCE", "reduce_ratio": 0.5, "reason": "到 T1",
    })
    assert payload.reduce_ratio == 0.5


def test_unknown_action_rejected() -> None:
    with pytest.raises(ValueError):
        PositionReviewPayload.from_payload({
            "recommended_action": "BUY_ALL_IN", "reason": "胡写",
        })


def test_hold_defaults_are_honest() -> None:
    payload = PositionReviewPayload.from_payload({
        "recommended_action": "HOLD", "reason": "维持",
    })
    assert payload.recommended_action == RecommendedAction.HOLD
    assert payload.reduce_ratio is None
    assert payload.thesis_status == "MAINTAINED"
    assert payload.updated_targets == ()
