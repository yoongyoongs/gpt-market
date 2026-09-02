"""RT-04：AttentionEvent Domain（实时方案 §6.3/§10.2/§10.3）。

AttentionEvent 是"哪些客观条件变了，值得让 AI 重新看"的事实记录：

- 它不是交易信号：EVENT != TRADE，EVENT != Decision；
- 同一条件靠 dedupe_key + 冷却窗口去抖，绝不骚扰；
- status 流转 OPEN → ACKED → RESOLVED/EXPIRED 由人工/系统维护。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.v3.contracts.base import V3Contract, require_aware


class AttentionEventType(StrEnum):
    ENTRY_TRIGGER_NEAR = "ENTRY_TRIGGER_NEAR"
    ENTRY_TRIGGER_MET = "ENTRY_TRIGGER_MET"
    ENTRY_CANCEL_MET = "ENTRY_CANCEL_MET"
    STOP_NEAR = "STOP_NEAR"
    STOP_HIT = "STOP_HIT"
    TARGET_NEAR = "TARGET_NEAR"
    TARGET_HIT = "TARGET_HIT"
    STRUCTURE_CHANGED = "STRUCTURE_CHANGED"
    NEW_EVIDENCE = "NEW_EVIDENCE"
    RELATIVE_STRENGTH_CHANGED = "RELATIVE_STRENGTH_CHANGED"
    TIME_EFFICIENCY_CHANGED = "TIME_EFFICIENCY_CHANGED"
    INTRADAY_ANOMALY = "INTRADAY_ANOMALY"
    DATA_QUALITY_DEGRADED = "DATA_QUALITY_DEGRADED"


class AttentionStatus(StrEnum):
    OPEN = "OPEN"
    ACKED = "ACKED"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"


class IntradayAttentionEvent(V3Contract):
    attention_event_id: UUID = Field(default_factory=uuid4)  # noqa: F821
    subject_type: str = "SECURITY"
    security_id: UUID | None = None
    code: str | None = None
    market: str | None = None
    account_id: UUID | None = None
    entry_plan_id: UUID | None = None
    position_review_id: UUID | None = None
    event_type: AttentionEventType
    severity: str = "INFO"
    facts: dict[str, Any] = {}
    as_of: datetime
    known_at: datetime
    source_snapshot_ids: list[str] = []
    status: AttentionStatus = AttentionStatus.OPEN
    dedupe_key: str
    content_hash: str

    @model_validator(mode="before")
    @classmethod
    def _check_aware(cls, values):
        return _require_aware(values, ("as_of", "known_at"))


def _require_aware(values: dict, fields: tuple[str, ...]) -> dict:
    for field in fields:
        value = values.get(field)
        if isinstance(value, datetime):
            require_aware(value, field)
    return values
