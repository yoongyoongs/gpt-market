"""RT-06：Typed EntryPlanPayload（实时方案 §17.2）。

EntryPlanVersion.plan 仍是 JSONB，但应用层必须走类型化 Schema，
避免不同 AI Result 各写各的字段。触发/取消条件区分两类：

- PRICE_ABOVE / PRICE_BELOW：客观可评估（给定 last_price 判定真假）；
- TEXT：人工/AI 语义条件，系统只陈述、绝不假装客观判定。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from app.v3.contracts.base import V3Contract


class EntryZone(V3Contract):
    low: float = Field(gt=0)
    high: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> "EntryZone":
        if self.low > self.high:
            raise ValueError("entry zone low must not exceed high")
        return self


class StopSpec(V3Contract):
    price: float = Field(gt=0)
    reason: str | None = None


class TargetSpec(V3Contract):
    price: float = Field(gt=0)
    target_type: str = "T1"


class PlanCondition(V3Contract):
    kind: Literal["PRICE_ABOVE", "PRICE_BELOW", "TEXT"]
    value: float | None = None
    description: str | None = None

    @model_validator(mode="after")
    def validate_kind(self) -> "PlanCondition":
        if self.kind == "TEXT":
            if not self.description:
                raise ValueError("TEXT condition requires description")
            if self.value is not None:
                raise ValueError("TEXT condition must not carry a price value")
        else:
            if self.value is None or self.value <= 0:
                raise ValueError(f"{self.kind} condition requires positive value")
        return self

    @property
    def objective(self) -> bool:
        return self.kind in {"PRICE_ABOVE", "PRICE_BELOW"}


class EntryPlanPayload(V3Contract):
    entry_mode: str = Field(min_length=1)
    entry_zone: EntryZone | None = None
    triggers: tuple[PlanCondition, ...] = ()
    confirms: tuple[str, ...] = ()
    cancels: tuple[PlanCondition, ...] = ()
    stop: StopSpec | None = None
    targets: tuple[TargetSpec, ...] = ()
    max_wait_sessions: int | None = Field(default=None, ge=1)
    suggested_position: str | None = None

    @classmethod
    def from_plan(cls, plan: dict[str, Any]) -> "EntryPlanPayload":
        """宽容解析 AI 写入的 plan dict；结构非法抛 ValueError。"""
        if not isinstance(plan, dict):
            raise ValueError("entry plan must be an object")
        triggers = plan.get("triggers") or []
        cancels = plan.get("cancels") or []
        if not isinstance(triggers, list) or not isinstance(cancels, list):
            raise ValueError("triggers/cancels must be lists")
        entry_zone = plan.get("entry_zone")
        stop = plan.get("stop")
        targets = plan.get("targets") or []
        return cls(
            entry_mode=str(plan.get("entry_mode") or "UNSPECIFIED"),
            entry_zone=EntryZone(**entry_zone) if isinstance(entry_zone, dict) else None,
            triggers=tuple(PlanCondition(**item) for item in triggers),
            confirms=tuple(str(item) for item in (plan.get("confirms") or [])),
            cancels=tuple(PlanCondition(**item) for item in cancels),
            stop=StopSpec(**stop) if isinstance(stop, dict) else None,
            targets=tuple(
                TargetSpec(**item) if isinstance(item, dict) else TargetSpec(price=item)
                for item in targets
            ),
            max_wait_sessions=plan.get("max_wait_sessions"),
            suggested_position=(
                str(plan["suggested_position"])
                if plan.get("suggested_position") is not None
                else None
            ),
        )
