from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.hashing import canonical_hash


class AgentType(StrEnum):
    CHATGPT_WEB = "CHATGPT_WEB"
    EXTERNAL_AGENT = "EXTERNAL_AGENT"
    HUMAN_IMPORT = "HUMAN_IMPORT"


class AgentProvider(StrEnum):
    OPENAI = "OPENAI"
    UNKNOWN = "UNKNOWN"


class SubjectType(StrEnum):
    MARKET = "MARKET"
    STOCK = "STOCK"
    POSITION = "POSITION"
    PORTFOLIO = "PORTFOLIO"


class Subject(V3Contract):
    type: SubjectType
    code: str | None = None
    account_id: UUID | None = None

    @model_validator(mode="after")
    def validate_subject(self) -> "Subject":
        if self.type in {SubjectType.STOCK, SubjectType.POSITION} and not self.code:
            raise ValueError("stock and position subjects require code")
        if self.type is SubjectType.POSITION and self.account_id is None:
            raise ValueError("position subjects require account_id")
        return self


class AgentIdentity(V3Contract):
    agent_type: AgentType
    provider: AgentProvider
    model: str = "UNKNOWN"
    model_version: str | None = None


class AgentTask(V3Contract):
    task_id: UUID
    task_run_id: UUID
    task_type: str = Field(min_length=1, max_length=64)
    subject: Subject
    task_profile: str = Field(min_length=1, max_length=64)
    trigger_type: str = Field(min_length=1, max_length=64)
    as_of: datetime
    context_pack_id: UUID
    context_pack_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_result_type: str = Field(min_length=1, max_length=64)
    constraints: dict[str, Any] = Field(default_factory=dict)

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")


class AIResultEnvelopeContent(V3Contract):
    schema_version: str = "v3.0"
    result_id: UUID
    result_type: str = Field(min_length=1, max_length=64)
    agent: AgentIdentity
    task_id: UUID
    task_run_id: UUID
    task_profile: str = Field(min_length=1, max_length=64)
    trigger_type: str = Field(min_length=1, max_length=64)
    context_pack_id: UUID
    context_pack_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_version: str = Field(min_length=1, max_length=64)
    strategy_version: str = Field(min_length=1, max_length=64)
    produced_at: datetime
    as_of: datetime
    evidence_ids: tuple[UUID, ...] = ()
    result: dict[str, Any]
    @field_validator("produced_at", "as_of")
    @classmethod
    def validate_datetimes(cls, value: datetime, info) -> datetime:
        return require_aware(value, info.field_name)

    def computed_content_hash(self) -> str:
        return canonical_hash(self)


class AIResultEnvelope(AIResultEnvelopeContent):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(cls, value: AIResultEnvelopeContent | dict[str, Any]) -> "AIResultEnvelope":
        content = value if isinstance(value, AIResultEnvelopeContent) else AIResultEnvelopeContent.model_validate(value)
        return cls(**content.model_dump(), content_hash=content.computed_content_hash())

    @model_validator(mode="after")
    def validate_content_hash(self) -> "AIResultEnvelope":
        content = AIResultEnvelopeContent.model_validate(self.model_dump(exclude={"content_hash"}))
        if content.computed_content_hash() != self.content_hash:
            raise ValueError("content_hash does not match the canonical envelope payload")
        return self
