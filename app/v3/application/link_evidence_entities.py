from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

from pydantic import Field, field_validator

from app.v3.contracts.base import V3Contract
from app.v3.domain.evidence import EntityLink, EntityLinkStatus, NormalizedEvidence
from app.v3.domain.market_data import UniverseSnapshot


class EntityCatalogEntry(V3Contract):
    entity_type: str = Field(min_length=1, max_length=32)
    entity_id: str = Field(min_length=1, max_length=128)
    canonical_name: str = Field(min_length=1, max_length=128)
    aliases: tuple[str, ...] = ()

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


class EvidenceEntityMatcher:
    def __init__(self, entries: Iterable[EntityCatalogEntry]) -> None:
        self._entries = tuple(entries)
        aliases: dict[str, list[EntityCatalogEntry]] = defaultdict(list)
        for entry in self._entries:
            for alias in (entry.canonical_name, *entry.aliases):
                aliases[alias.casefold()].append(entry)
        self._aliases = aliases

    @classmethod
    def from_universe(
        cls,
        snapshot: UniverseSnapshot,
        *,
        industry_entries: Iterable[EntityCatalogEntry] = (),
    ) -> "EvidenceEntityMatcher":
        entries = [
            EntityCatalogEntry(
                entity_type="SECURITY",
                entity_id=f"{member.market.value}:{member.code}",
                canonical_name=member.name,
                aliases=(member.code, f"{member.market.value}{member.code}"),
            )
            for member in snapshot.members
        ]
        entries.extend(industry_entries)
        entries.append(EntityCatalogEntry(
            entity_type="MARKET",
            entity_id="CN_A_SHARES",
            canonical_name="A股",
            aliases=("A股市场", "中国A股", "沪深京市场", "沪深两市"),
        ))
        return cls(entries)

    def links_for(self, records: tuple[NormalizedEvidence, ...]) -> tuple[EntityLink, ...]:
        links = []
        for record in records:
            searchable = "\n".join(self._strings(record.normalized_payload)).casefold()
            searchable += "\n" + "\n".join(self._strings(record.payload)).casefold()
            matched_entities: dict[tuple[str, str], EntityLink] = {}
            for alias, entries in self._aliases.items():
                if not self._contains(searchable, alias):
                    continue
                ambiguous = len(entries) > 1
                for entry in entries:
                    key = (entry.entity_type, entry.entity_id)
                    confidence = 0.7 if ambiguous else (1.0 if alias.isdigit() else 0.98)
                    link = EntityLink.build(
                        evidence_id=record.evidence_id,
                        entity_type=entry.entity_type,
                        entity_id=entry.entity_id,
                        match_basis={
                            "method": "CATALOG_ALIAS",
                            "alias": alias,
                            "canonical_name": entry.canonical_name,
                            "ambiguous": ambiguous,
                        },
                        confidence=confidence,
                        status=(
                            EntityLinkStatus.CANDIDATE
                            if ambiguous
                            else EntityLinkStatus.CONFIRMED
                        ),
                    )
                    current = matched_entities.get(key)
                    if current is None or link.confidence > current.confidence:
                        matched_entities[key] = link
            links.extend(matched_entities.values())
        return tuple(links)

    @staticmethod
    def _strings(value: object) -> Iterable[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from EvidenceEntityMatcher._strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from EvidenceEntityMatcher._strings(item)

    @staticmethod
    def _contains(text: str, alias: str) -> bool:
        if alias.isdigit():
            return re.search(rf"(?<!\d){re.escape(alias)}(?!\d)", text) is not None
        return alias in text
