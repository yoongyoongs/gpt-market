from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from app.v3.contracts.base import V3Contract, require_aware
from app.v3.domain.context import (
    CONTEXT_PACK_BUILDER_VERSION,
    CONTEXT_PACK_SCHEMA_VERSION,
    ContextEvidenceSelection,
    ContextLevel,
    ContextPack,
    ContextSubjectType,
    EvidenceSelectionSide,
)
from app.v3.domain.evidence import EvidenceReadQuery
from app.v3.domain.hashing import canonical_json
from app.v3.repositories.errors import RepositoryNotFoundError
from app.v3.repositories.protocols import UnitOfWork


LEVEL_SETTINGS = {
    ContextLevel.FAST: (3_000, 8, 500),
    ContextLevel.NORMAL: (6_500, 20, 1_000),
    ContextLevel.DEEP: (12_000, 40, 2_000),
}


# --- 确定性规则抽为模块级函数（PF-002：Deterministic Replay 重放
# Context 证据选择阶段时复用同一实现，避免逻辑漂移）---


def sanitized(value, text_limit: int):
    if isinstance(value, str):
        return value[:text_limit]
    if isinstance(value, dict):
        return {
            str(key)[:128]: sanitized(item, text_limit)
            for key, item in list(value.items())[:50]
        }
    if isinstance(value, (list, tuple)):
        return [sanitized(item, text_limit) for item in value[:50]]
    return value


def evidence_side(view) -> EvidenceSelectionSide:
    value = str(view.record.normalized_payload.get("side", "NEUTRAL")).upper()
    return EvidenceSelectionSide(value) if value in EvidenceSelectionSide else EvidenceSelectionSide.NEUTRAL


def evidence_retrieval_score(view, as_of) -> float:
    authority = max(0.0, 1 - min(view.record.source_priority, 1_000) / 1_000)
    conflict_bonus = 0.05 if view.conflict_status != "NONE" else 0
    return round(min(1.0, (
        0.55 * view.record.effective_relevance(as_of)
        + 0.25 * view.record.confidence
        + 0.2 * authority
        + conflict_bonus
    )), 7)


def evidence_ranking_key(view, as_of):
    side_priority = 0 if evidence_side(view) is EvidenceSelectionSide.CONTRARY else 1
    return (
        side_priority,
        -evidence_retrieval_score(view, as_of),
        view.record.source_priority,
        -view.record.known_at.timestamp(),
        str(view.record.evidence_id),
    )


def evidence_item_payload(view, as_of, text_limit: int) -> dict:
    record = view.record
    return {
        "evidence_id": str(record.evidence_id),
        "type": record.evidence_type.value,
        "source_type": record.source_type.value,
        "source": record.source,
        "upstream_source": record.upstream_source,
        "claim_key": record.claim_key,
        "event_time": None if record.event_time is None else record.event_time.isoformat(),
        "publish_time": None if record.publish_time is None else record.publish_time.isoformat(),
        "known_at": record.known_at.isoformat(),
        "confidence": record.confidence,
        "effective_relevance": record.effective_relevance(as_of),
        "conflict_state": view.conflict_status,
        "data": sanitized(record.normalized_payload, text_limit),
    }


def estimate_tokens(payload) -> int:
    return max(1, (len(canonical_json(payload).encode("utf-8")) + 3) // 4)


class BuildContextPackCommand(V3Contract):
    context_level: ContextLevel
    subject_type: ContextSubjectType
    subject_id: str = Field(min_length=1, max_length=128)
    task_profile_id: UUID
    task_profile_version: int = Field(ge=1)
    as_of: datetime
    feature_run_id: UUID | None = None
    recall_run_id: UUID | None = None
    comparison_pack_id: UUID | None = None

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")


class BuildContextPackService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(self, command: BuildContextPackCommand) -> ContextPack:
        if command.as_of > self._clock():
            raise ValueError("context as_of cannot be in the future")
        async with self._uow_factory() as uow:
            source = await uow.context_packs.load_source(
                subject_type=command.subject_type.value,
                subject_id=command.subject_id,
                as_of=command.as_of,
                feature_run_id=command.feature_run_id,
                recall_run_id=command.recall_run_id,
            )
            comparison = None
            if command.comparison_pack_id is not None:
                comparison = await uow.candidate_comparisons.get(
                    command.comparison_pack_id
                )
            evidence_subject_id = (
                f"{source.market}:{source.code}"
                if source is not None and source.code is not None
                else command.subject_id
            )
            evidence_page = await uow.evidence.retrieve_view(
                query=EvidenceReadQuery(
                    subject_type=command.subject_type.value,
                    subject_id=evidence_subject_id,
                    as_of=command.as_of,
                    include_candidates=False,
                    limit=200,
                )
            )
        if source is None:
            raise RepositoryNotFoundError(
                "published context source is unavailable at as_of"
            )
        if comparison is not None:
            if (
                comparison.feature_run_id != source.feature_run.feature_run_id
                or comparison.universe_snapshot_id
                != source.feature_run.universe_snapshot_id
                or comparison.known_at > command.as_of
            ):
                raise ValueError("comparison pack is incompatible with context source")
            if command.subject_type is ContextSubjectType.SECURITY and not any(
                item.market == source.market and item.code == source.code
                for item in comparison.members
            ):
                raise ValueError("security is not part of comparison pack")

        token_budget, evidence_limit, text_limit = LEVEL_SETTINGS[command.context_level]
        ranked = sorted(
            evidence_page.views,
            key=lambda item: self._ranking_key(item, command.as_of),
        )
        base_payload = self._base_payload(command, source, comparison)
        selected_payload = []
        selected_views = []
        trimmed_for_budget = 0
        for view in ranked[:evidence_limit]:
            item = self._evidence_payload(view, command.as_of, text_limit)
            candidate_payload = {
                **base_payload,
                "evidence": {
                    "boundary": "UNTRUSTED_DATA",
                    "items": [*selected_payload, item],
                },
            }
            if self._estimate_tokens(candidate_payload) > token_budget:
                trimmed_for_budget += 1
                continue
            selected_payload.append(item)
            selected_views.append(view)
        payload = {
            **base_payload,
            "evidence": {
                "boundary": "UNTRUSTED_DATA",
                "candidate_count": len(ranked),
                "candidate_evidence_ids": [
                    str(item.record.evidence_id) for item in ranked
                ],
                "retrieval_config": {
                    "version": "context-evidence-retrieval.v1",
                    "include_candidate_links": False,
                    "limit": 200,
                },
                "items": selected_payload,
            },
        }
        actual_tokens = self._estimate_tokens(payload)
        selections = tuple(
            ContextEvidenceSelection(
                evidence_id=view.record.evidence_id,
                evidence_known_at=view.record.known_at,
                selection_reason=self._selection_reason(view),
                side=self._side(view),
                retrieval_score=self._retrieval_score(view, command.as_of),
                relevance=view.record.effective_relevance(command.as_of),
                source_priority=view.record.source_priority,
                final_order=index,
            )
            for index, view in enumerate(selected_views, start=1)
        )
        missing_fields = []
        if source.feature is None and command.subject_type is ContextSubjectType.SECURITY:
            missing_fields.append("security_feature")
        elif source.feature is not None:
            missing_fields.extend(source.feature.missing_fields)
        if source.regime is None:
            missing_fields.append("market_regime")
        if source.recall_run_id is None:
            missing_fields.append("recall_run")
        if not ranked:
            missing_fields.append("evidence")
        if (
            command.subject_type is ContextSubjectType.POSITION
            and source.portfolio is None
        ):
            missing_fields.append("portfolio_context")
        coverage_values = [source.feature_run.coverage]
        if source.feature is not None:
            coverage_values.append(source.feature.coverage)
        if source.regime is not None:
            coverage_values.append(source.regime.coverage)
        pack = ContextPack.build(
            context_level=command.context_level,
            subject_type=command.subject_type,
            subject_id=(
                f"{source.market}:{source.code}"
                # POSITION 主体需保留账户维度，subject_id 维持 account:market:code
                if command.subject_type is ContextSubjectType.SECURITY
                and source.code is not None
                else command.subject_id
            ),
            task_profile_id=command.task_profile_id,
            task_profile_version=command.task_profile_version,
            builder_version=CONTEXT_PACK_BUILDER_VERSION,
            schema_version=CONTEXT_PACK_SCHEMA_VERSION,
            as_of=command.as_of,
            known_at=max(command.as_of, self._clock()),
            universe_snapshot_id=source.feature_run.universe_snapshot_id,
            feature_run_id=source.feature_run.feature_run_id,
            recall_run_id=source.recall_run_id,
            regime_snapshot_id=None if source.regime is None else source.regime.regime_snapshot_id,
            comparison_pack_id=command.comparison_pack_id,
            token_budget=token_budget,
            actual_tokens=actual_tokens,
            coverage=sum(coverage_values) / len(coverage_values),
            missing_fields=tuple(dict.fromkeys(missing_fields)),
            trim_summary={
                "candidate_evidence_count": len(ranked),
                "selected_evidence_count": len(selections),
                "level_limit": evidence_limit,
                "trimmed_by_level": max(0, len(ranked) - evidence_limit),
                "trimmed_by_budget": trimmed_for_budget,
                "token_estimator": "canonical-json-utf8-bytes-div-4.v1",
            },
            payload=payload,
            references=self._references(source, comparison),
            evidence_selections=selections,
        )
        async with self._uow_factory() as uow:
            created = await uow.context_packs.publish(pack)
            if created:
                await uow.commit()
                return pack
            replay = await uow.context_packs.get_by_content_hash(pack.content_hash)
        if replay is None:
            raise RuntimeError("idempotent context pack replay could not be read")
        return replay

    @staticmethod
    def _base_payload(command, source, comparison) -> dict[str, Any]:
        feature = source.feature
        return {
            "subject": {
                "type": command.subject_type.value,
                "market": source.market,
                "code": source.code,
                "name": source.name,
            },
            "feature": None if feature is None else {
                "close": feature.close,
                "returns": {
                    "3d": feature.return_3d, "5d": feature.return_5d,
                    "10d": feature.return_10d, "20d": feature.return_20d,
                    "60d": feature.return_60d, "120d": feature.return_120d,
                    "250d": feature.return_250d,
                },
                "position": {
                    "60d": feature.position_60d, "120d": feature.position_120d,
                    "250d": feature.position_250d,
                },
                "trend": {
                    "ma20": feature.ma20, "ma60": feature.ma60,
                    "ma20_slope": feature.ma20_slope,
                    "ma60_slope": feature.ma60_slope,
                    "daily": feature.features.get("daily_trend_state"),
                    "weekly": feature.features.get("weekly_trend_state"),
                },
                "volatility": {
                    "atr14": feature.atr14, "atr_pct": feature.atr_pct,
                    "volatility20": feature.volatility20,
                },
                "volume_price": {
                    "amount": feature.amount,
                    "volume_ratio_5d": feature.volume_ratio_5d,
                    "volume_expansion": feature.volume_expansion,
                },
                "quality": {
                    "coverage": feature.coverage,
                    "stale": feature.stale,
                    "missing_fields": list(feature.missing_fields),
                    "source_errors": list(feature.source_errors),
                    **feature.quality,
                },
            },
            "market_regime": None if source.regime is None else {
                "index_states": source.regime.index_states,
                "breadth": source.regime.breadth,
                "turnover": source.regime.turnover,
                "limit_structure": source.regime.limit_structure,
                "size_style": source.regime.size_style,
                "growth_value_style": source.regime.growth_value_style,
                "industry_rotation": source.regime.industry_rotation,
                "risk_appetite_facts": source.regime.risk_appetite_facts,
                "coverage": source.regime.coverage,
                "confidence": source.regime.confidence,
                "stale": source.regime.stale,
            },
            "comparison": None if comparison is None else {
                "comparison_pack_id": str(comparison.comparison_pack_id),
                "content_hash": comparison.content_hash,
                "candidate_set_id": str(comparison.candidate_set_id),
            },
            "portfolio": (
                {"status": "AVAILABLE", **source.portfolio.model_dump(mode="json")}
                if source.portfolio is not None
                else {
                    "status": "NOT_APPLICABLE",
                    "reason": "SECURITY_SUBJECT_HAS_NO_ACCOUNT_BINDING",
                }
            ),
        }

    @staticmethod
    def _evidence_payload(view, as_of, text_limit):
        return evidence_item_payload(view, as_of, text_limit)

    _sanitize = staticmethod(sanitized)

    _side = staticmethod(evidence_side)

    _retrieval_score = staticmethod(evidence_retrieval_score)

    @classmethod
    def _ranking_key(cls, view, as_of):
        return evidence_ranking_key(view, as_of)

    @staticmethod
    def _selection_reason(view) -> str:
        parts = ["时点内可用", f"match={view.match_type.value}"]
        if view.conflict_status != "NONE":
            parts.append(f"conflict={view.conflict_status}")
        return "；".join(parts)

    @staticmethod
    def _estimate_tokens(payload) -> int:
        return estimate_tokens(payload)

    @staticmethod
    def _references(source, comparison):
        references = [
            {"type": "UNIVERSE_SNAPSHOT", "id": str(source.feature_run.universe_snapshot_id)},
            {"type": "FEATURE_RUN", "id": str(source.feature_run.feature_run_id), "hash": source.feature_run.content_hash},
        ]
        if source.feature is not None:
            references.append({
                "type": "SECURITY_FEATURE",
                "id": str(source.feature.security_id),
                "hash": source.feature.source_content_hash,
            })
        if source.recall_run_id is not None:
            references.append({"type": "RECALL_RUN", "id": str(source.recall_run_id)})
        if source.regime is not None:
            references.append({"type": "MARKET_REGIME", "id": str(source.regime.regime_snapshot_id), "hash": source.regime.content_hash})
        if comparison is not None:
            references.append({"type": "CANDIDATE_COMPARISON", "id": str(comparison.comparison_pack_id), "hash": comparison.content_hash})
        return tuple(references)
