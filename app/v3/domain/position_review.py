"""RT-07：PositionReviewPayload 类型化（实时方案 §9.2/§9.5/§9.6/§17.3）。

- 动作枚举冻结为 HOLD / ADD / REDUCE / EXIT；
- 兼容层：旧 AI 输出 SELL → EXIT（不长期并存多个语义重复的动作）；
- REDUCE 必须给出 reduce_ratio（§9.6：锁定部分利润需要比例建议）；
- Position Review 只是建议，绝不创建 Trade。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from app.v3.contracts.base import V3Contract


class RecommendedAction(StrEnum):
    HOLD = "HOLD"
    ADD = "ADD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"

    @classmethod
    def parse(cls, value: Any) -> "RecommendedAction":
        """兼容层：SELL → EXIT；未知动作拒绝。"""
        normalized = str(value).strip().upper()
        if normalized == "SELL":
            normalized = "EXIT"
        try:
            return cls(normalized)
        except ValueError as exc:
            raise ValueError(
                f"invalid recommended_action: {value!r} "
                f"(allowed: HOLD/ADD/REDUCE/EXIT, legacy SELL maps to EXIT)"
            ) from exc


class PositionReviewPayload(V3Contract):
    recommended_action: RecommendedAction
    reduce_ratio: float | None = Field(default=None, ge=0, le=1)
    exit_trigger: str | None = None
    add_zone: dict[str, Any] | None = None
    updated_stop: float | None = None
    updated_targets: tuple[float, ...] = ()
    thesis_status: str = "MAINTAINED"
    time_efficiency: str = "UNKNOWN"
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_action(self) -> "PositionReviewPayload":
        if self.recommended_action == RecommendedAction.REDUCE:
            if self.reduce_ratio is None or self.reduce_ratio <= 0:
                raise ValueError(
                    "REDUCE requires a positive reduce_ratio (§9.6)"
                )
        return self

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PositionReviewPayload":
        """宽容解析 AI Result 里的 review dict；动作非法抛 ValueError。"""
        if not isinstance(payload, dict):
            raise ValueError("position review payload must be an object")
        targets = payload.get("updated_targets") or []
        return cls(
            recommended_action=RecommendedAction.parse(
                payload.get("recommended_action")
            ),
            reduce_ratio=payload.get("reduce_ratio"),
            exit_trigger=payload.get("exit_trigger"),
            add_zone=(
                payload["add_zone"]
                if isinstance(payload.get("add_zone"), dict)
                else None
            ),
            updated_stop=payload.get("updated_stop"),
            updated_targets=tuple(float(item) for item in targets),
            thesis_status=str(payload.get("thesis_status", "MAINTAINED")),
            time_efficiency=str(payload.get("time_efficiency", "UNKNOWN")),
            reason=str(payload.get("reason") or "No reason supplied"),
        )
