from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid5

from pydantic import Field, field_validator, model_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.context import (
    CANDIDATE_COMPARISON_BUILDER_VERSION,
    CANDIDATE_COMPARISON_FIELD_PROFILE_VERSION,
    CANDIDATE_COMPARISON_SCHEMA_VERSION,
    CandidateComparisonMember,
    CandidateComparisonPack,
)
from app.v3.domain.evidence import SecurityEvidenceView
from app.v3.domain.hashing import canonical_hash
from app.v3.repositories.errors import RepositoryNotFoundError
from app.v3.repositories.protocols import UnitOfWork


CANDIDATE_SET_NAMESPACE = UUID("edb489bb-d143-4f13-b559-ee4244150ceb")


class CandidateComparisonQuery(V3Contract):
    candidate_set_id: UUID | None = None
    codes: tuple[str, ...] = Field(default=(), max_length=100)
    feature_run_id: UUID | None = None
    recall_run_id: UUID | None = None
    field_profile_version: str = Field(
        default=CANDIDATE_COMPARISON_FIELD_PROFILE_VERSION,
        min_length=1,
        max_length=64,
    )
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")

    @model_validator(mode="after")
    def validate_input_mode(self) -> "CandidateComparisonQuery":
        if (self.candidate_set_id is None) == (not self.codes):
            raise ValueError("provide exactly one of candidate_set_id or codes")
        if self.codes:
            if not 20 <= len(self.codes) <= 100:
                raise ValueError("codes must contain 20 to 100 candidates")
            normalized = [value.strip().upper().replace(".", ":") for value in self.codes]
            if len(normalized) != len(set(normalized)):
                raise ValueError("candidate codes must be unique")
        elif self.feature_run_id is not None or self.recall_run_id is not None:
            raise ValueError("run selectors are only valid when building from codes")
        return self


class BuildCandidateComparisonService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(self, query: CandidateComparisonQuery) -> CandidateComparisonPack:
        if query.as_of > self._clock():
            raise ValueError("comparison as_of cannot be in the future")
        if query.candidate_set_id is not None:
            async with self._uow_factory() as uow:
                pack = await uow.candidate_comparisons.latest_for_candidate_set(
                    query.candidate_set_id,
                    field_profile_version=query.field_profile_version,
                    as_of=query.as_of,
                )
            if pack is None:
                raise RepositoryNotFoundError("candidate comparison pack not found")
            return pack

        async with self._uow_factory() as uow:
            source = await uow.candidate_comparisons.load_source(
                query.codes,
                as_of=query.as_of,
                feature_run_id=query.feature_run_id,
                recall_run_id=query.recall_run_id,
            )
            if source is None:
                raise RepositoryNotFoundError(
                    "published feature run is unavailable at comparison as_of"
                )
            evidence = await uow.evidence.for_securities(
                tuple(member.security_id for member in source.members),
                as_of=query.as_of,
            )

        evidence_by_security: dict[UUID, list[SecurityEvidenceView]] = defaultdict(list)
        for item in evidence:
            evidence_by_security[item.security_id].append(item)
        members = tuple(
            self._member(
                order=index,
                source=member,
                evidence=tuple(evidence_by_security.get(member.security_id, ())),
            )
            for index, member in enumerate(source.members, start=1)
        )
        candidate_set_id = uuid5(
            CANDIDATE_SET_NAMESPACE,
            canonical_hash(
                {
                    "universe_snapshot_id": source.feature_run.universe_snapshot_id,
                    "security_ids": [member.security_id for member in source.members],
                }
            ),
        )
        missing = Counter(
            field for member in members for field in member.missing_fields
        )
        pack = CandidateComparisonPack.build(
            candidate_set_id=candidate_set_id,
            builder_version=CANDIDATE_COMPARISON_BUILDER_VERSION,
            schema_version=CANDIDATE_COMPARISON_SCHEMA_VERSION,
            field_profile_version=query.field_profile_version,
            universe_snapshot_id=source.feature_run.universe_snapshot_id,
            feature_run_id=source.feature_run.feature_run_id,
            recall_run_id=source.recall_run_id,
            regime_snapshot_id=source.regime_snapshot_id,
            as_of=query.as_of,
            known_at=max(query.as_of, self._clock()),
            coverage=sum(member.coverage for member in members) / len(members),
            missing_summary=dict(sorted(missing.items())),
            trim_summary={
                "requested_candidates": len(query.codes),
                "returned_candidates": len(members),
                "evidence_items_per_candidate": 5,
                "omitted": ["minute_bars", "deep_evidence_payload", "unified_final_score"],
            },
            members=members,
        )
        async with self._uow_factory() as uow:
            created = await uow.candidate_comparisons.publish(pack)
            if created:
                await uow.commit()
                return pack
            replay = await uow.candidate_comparisons.get_by_content_hash(
                pack.content_hash
            )
        if replay is None:
            raise RuntimeError("idempotent comparison pack replay could not be read")
        return replay

    @staticmethod
    def _member(*, order, source, evidence) -> CandidateComparisonMember:
        feature = source.feature
        recall_hits = tuple(
            {
                "channel": hit.channel_code,
                "rank": hit.channel_rank,
                "strength": hit.strength,
                "reasons": list(hit.reasons),
                "coverage": hit.coverage,
            }
            for hit in source.recall_hits
        )
        ranked_evidence = sorted(
            evidence,
            key=lambda item: (
                -item.effective_relevance,
                item.record.source_priority,
                -item.record.known_at.timestamp(),
                str(item.record.evidence_id),
            ),
        )
        evidence_types = Counter(item.record.evidence_type.value for item in evidence)
        source_types = Counter(item.record.source_type.value for item in evidence)
        conflict_count = sum(
            item.record.conflict_state != "NONE" for item in evidence
        )
        financial = [
            item for item in ranked_evidence
            if item.record.normalized_payload.get("report_name")
            and item.record.normalized_payload.get("report_period")
        ]
        fundamental_summary: dict[str, Any]
        if financial:
            fundamental_summary = {
                "status": "AVAILABLE",
                "reports": [
                    {
                        "evidence_id": str(item.record.evidence_id),
                        "report_name": item.record.normalized_payload.get("report_name"),
                        "report_period": item.record.normalized_payload.get("report_period"),
                        "values": item.record.normalized_payload.get("values", {}),
                        "source": item.record.source,
                        "confidence": item.record.confidence,
                    }
                    for item in financial[:2]
                ],
            }
        else:
            fundamental_summary = {"status": "UNKNOWN", "reports": []}

        missing_fields = tuple(dict.fromkeys(
            (*feature.missing_fields,)
            + (() if recall_hits else ("recall_summary",))
            + (() if evidence else ("evidence_summary",))
            + (() if financial else ("fundamental_summary",))
        ))
        return CandidateComparisonMember(
            security_id=source.security_id,
            candidate_order=order,
            market=source.market,
            code=source.code,
            name=source.name,
            recall_summary={
                "hit_count": len(recall_hits),
                "channels": [item["channel"] for item in recall_hits],
                "hits": list(recall_hits),
            },
            trend_summary={
                "close": feature.close,
                "return_3d": feature.return_3d,
                "return_5d": feature.return_5d,
                "return_10d": feature.return_10d,
                "return_20d": feature.return_20d,
                "return_60d": feature.return_60d,
                "return_120d": feature.return_120d,
                "return_250d": feature.return_250d,
                "ma20": feature.ma20,
                "ma60": feature.ma60,
                "ma20_slope": feature.ma20_slope,
                "ma60_slope": feature.ma60_slope,
                "relative_index_strength": feature.relative_index_strength,
                "relative_industry_strength": feature.relative_industry_strength,
                "daily_trend_state": feature.features.get("daily_trend_state"),
                "weekly_trend_state": feature.features.get("weekly_trend_state"),
                "multi_timeframe_state": feature.features.get("multi_timeframe_state"),
                "multi_timeframe_rule": feature.features.get("multi_timeframe_rule"),
                "latest_bar_time": feature.features.get("latest_bar_time"),
            },
            position_summary={
                "position_60d": feature.position_60d,
                "position_120d": feature.position_120d,
                "position_250d": feature.position_250d,
                "distance_60d_high": feature.distance_60d_high,
                "distance_60d_low": feature.distance_60d_low,
                "breakout_20d": feature.breakout_20d,
                "pullback_20d": feature.pullback_20d,
                "overheated": feature.features.get("overheated"),
            },
            volatility_summary={
                "atr14": feature.atr14,
                "atr_pct": feature.atr_pct,
                "volatility20": feature.volatility20,
            },
            volume_price_summary={
                "amount": feature.amount,
                "volume_ratio_5d": feature.volume_ratio_5d,
                "volume_expansion": feature.volume_expansion,
            },
            liquidity_summary={
                "amount": feature.amount,
                "liquidity_quality": feature.features.get("liquidity_quality"),
            },
            fundamental_summary=fundamental_summary,
            risk_summary={
                "status": "AVAILABLE" if evidence else "UNKNOWN",
                "evidence_conflict_count": conflict_count,
                "feature_source_errors": list(feature.source_errors),
            },
            evidence_summary={
                "count": len(evidence),
                "type_counts": dict(sorted(evidence_types.items())),
                "source_type_counts": dict(sorted(source_types.items())),
                "top_items": [
                    {
                        "evidence_id": str(item.record.evidence_id),
                        "type": item.record.evidence_type.value,
                        "source": item.record.source,
                        "claim_key": item.record.claim_key,
                        "known_at": item.record.known_at.isoformat(),
                        "effective_relevance": item.effective_relevance,
                        "conflict_state": item.record.conflict_state,
                    }
                    for item in ranked_evidence[:5]
                ],
            },
            quality={
                "coverage": feature.coverage,
                "stale": feature.stale,
                "missing_fields": list(feature.missing_fields),
                "source_errors": list(feature.source_errors),
                "feature_quality": feature.quality,
            },
            coverage=feature.coverage,
            stale=feature.stale,
            missing_fields=missing_fields,
        )
