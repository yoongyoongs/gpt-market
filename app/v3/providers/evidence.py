from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import Field, model_validator

from app.v3.contracts.base import V3Contract
from app.v3.domain.evidence import (
    EntityLink,
    EvidenceSource,
    FetchedDocument,
    NormalizedEvidence,
    RawDocument,
)


class EvidenceFetchBatch(V3Contract):
    documents: tuple[FetchedDocument, ...]
    next_cursor: dict[str, object] | None = None
    exhausted: bool = True
    upstream_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_cursor(self) -> "EvidenceFetchBatch":
        if not self.exhausted and not self.next_cursor:
            raise ValueError("non-exhausted evidence batch requires next_cursor")
        return self


class ParsedEvidenceBundle(V3Contract):
    records: tuple[NormalizedEvidence, ...]
    links: tuple[EntityLink, ...] = ()


class EvidenceProvider(Protocol):
    source: EvidenceSource

    async def fetch(
        self,
        *,
        window_start: datetime | None,
        window_end: datetime | None,
        cursor: dict[str, object] | None,
    ) -> EvidenceFetchBatch: ...

    async def close(self) -> None: ...


class EvidenceParser(Protocol):
    code: str
    version: str

    def parse(self, raw: RawDocument, source: EvidenceSource) -> ParsedEvidenceBundle: ...
