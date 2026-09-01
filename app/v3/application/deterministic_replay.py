"""Deterministic Replay Engine（RC-06B / PF-002）。

整改方案 §9.2 的两层边界，边界必须写进 Replay result：

- Gate 1（保留既有 `_check_references` 语义）：point-in-time 泄漏 /
  缺输入检查，不过 Gate 时两层都不执行，status=BLOCKED；
- Server deterministic replay：在 pinned Bar Revision 上按同一确定性
  规则重算 Feature（CalculateSecurityFeatureService），并与当时落库的
  immutable Feature 做逐字段核验（只比较仅依赖 pinned 日 K 的字段）；
  未 pin 的输入（指数/行业 20d 收益、周线）显式声明排除，不静默；
- AI Decision replay：服务器没有模型 API，绝不假装重新得到同样
  Decision。有 immutable AI Result（Result Envelope）时只做
  "结果回放"（RESULT_REPLAY_FROM_IMMUTABLE_OUTPUT）；没有时显式
  NO_IMMUTABLE_AI_OUTPUT。真正的模型重跑属于 Product/V4 决策。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.v3.application.calculate_features import CalculateSecurityFeatureService
from app.v3.domain.performance import ReplayRunCreate

AI_DECISION_BOUNDARY = "SERVER_HAS_NO_MODEL_API"

# 仅依赖 pinned 日 K 的可比较字段（storage Numeric 精度容差内逐字段核验）
_COMPARABLE_FIELDS = (
    "close", "return_3d", "return_5d", "return_10d", "return_20d",
    "return_60d", "return_120d", "return_250d",
    "position_60d", "position_120d", "position_250d",
    "ma5", "ma10", "ma20", "ma60", "ma20_slope", "ma60_slope",
    "atr14", "atr_pct", "volatility20",
    "distance_60d_high", "distance_60d_low",
    "breakout_20d", "pullback_20d", "amount",
    "volume_ratio_5d", "volume_expansion",
)
_BOOL_FIELDS = frozenset({"breakout_20d", "pullback_20d", "volume_expansion"})
_EXCLUDED_UNPINNED_INPUTS = (
    "relative_index_strength", "relative_industry_strength",
    "weekly_trend_state", "stale", "coverage",
)


class DeterministicReplayService:
    def __init__(
        self,
        uow_factory: Callable,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(self, command: ReplayRunCreate) -> dict[str, Any]:
        async with self._uow_factory() as uow:
            checks, revision_set = await uow.performance.replay_gate(
                command.bar_revision_ids, command.evidence_ids,
                command.context_pack_ids, replay_as_of=command.replay_as_of,
            )
            blocked = any(not item["passed"] for item in checks)
            if blocked:
                result = {
                    "executed": False,
                    "reason": "POINT_IN_TIME_LEAKAGE_OR_MISSING_INPUT",
                    "layers": self._layers_not_executed(),
                }
            else:
                result = {
                    "executed": True,
                    "mode": "POINT_IN_TIME",
                    "input_count": sum(map(len, revision_set.values())),
                    "layers": await self._execute_layers(uow, command),
                }
            payload = {
                "strategy_version": command.strategy_version,
                "replay_as_of": command.replay_as_of,
                "revision_set": revision_set,
                "parameters": command.parameters,
                "status": "BLOCKED" if blocked else "COMPLETED",
                "leakage_checks": checks,
                "result": result,
            }
            record = await uow.performance.record_replay(command, payload)
            await uow.commit()
        return record

    async def _execute_layers(self, uow, command: ReplayRunCreate) -> dict[str, Any]:
        return {
            "server_deterministic": await self._deterministic_layer(uow, command),
            "ai_decision_replay": await self._ai_decision_layer(uow, command.context_pack_ids),
        }

    async def _deterministic_layer(self, uow, command: ReplayRunCreate) -> dict[str, Any]:
        revisions = await uow.bars.load_revisions_by_ids(
            command.bar_revision_ids, as_of=command.replay_as_of,
        )
        feature_service = CalculateSecurityFeatureService()
        recomputed: dict[UUID, dict[str, Any]] = {}
        skipped: list[dict[str, Any]] = []
        for revision in revisions:
            if revision.period.value != "DAY" or revision.adjust_type.value != "QFQ":
                skipped.append({
                    "revision_id": str(revision.revision_id),
                    "reason": "FEATURE_RECOMPUTE_REQUIRES_QFQ_DAY",
                })
                continue
            recomputed[revision.security_id] = feature_service.execute(
                feature_run_id=uuid4(), revision=revision, as_of=command.replay_as_of,
            ).model_dump(mode="json")

        targets = await uow.performance.replay_verification_targets(
            command.context_pack_ids
        )
        matched = 0
        mismatched: list[dict[str, Any]] = []
        verified = 0
        for target in targets:
            if not target["available"]:
                mismatched.append({
                    "context_pack_id": str(target["context_pack_id"]),
                    "field": None,
                    "reason": target["reason"],
                })
                continue
            stored = await uow.performance.load_run_feature(
                target["feature_run_id"], target["security_id"]
            )
            feature = recomputed.get(target["security_id"])
            if stored is None or feature is None:
                mismatched.append({
                    "context_pack_id": str(target["context_pack_id"]),
                    "field": None,
                    "reason": (
                        "STORED_FEATURE_NOT_FOUND" if stored is None
                        else "PINNED_REVISION_NOT_FOUND"
                    ),
                })
                continue
            verified += 1
            target_mismatches = [
                {
                    "context_pack_id": str(target["context_pack_id"]),
                    "field": field,
                    "recomputed": feature.get(field),
                    "stored": stored.get(field),
                }
                for field in _COMPARABLE_FIELDS
                if not self._field_equal(feature.get(field), stored.get(field))
            ]
            if target_mismatches:
                mismatched.extend(target_mismatches)
            else:
                matched += 1

        return {
            "executed": True,
            "feature_recompute": {
                "recomputed_count": len(recomputed),
                "skipped_non_qfq_day": skipped,
                "verified_count": verified,
                "matched_count": matched,
                "mismatched": mismatched,
            },
            "excluded_unpinned_inputs": list(_EXCLUDED_UNPINNED_INPUTS),
        }

    async def _ai_decision_layer(self, uow, context_pack_ids) -> dict[str, Any]:
        immutable_results = []
        for pack_id in context_pack_ids:
            result = await uow.performance.immutable_ai_result_for_pack(pack_id)
            if result is not None:
                immutable_results.append(result)
        if immutable_results:
            first = immutable_results[0]
            return {
                "executed": False,
                "boundary": AI_DECISION_BOUNDARY,
                "reason": "AI_DECISION_REQUIRES_EXTERNAL_MODEL",
                "immutable_result_replay": {
                    "available": True,
                    "mode": "RESULT_REPLAY_FROM_IMMUTABLE_OUTPUT",
                    "result_count": len(immutable_results),
                    "result_id": str(first["result_id"]),
                    "result_type": first.get("result_type"),
                    "content_hash": first.get("content_hash"),
                },
            }
        return {
            "executed": False,
            "boundary": AI_DECISION_BOUNDARY,
            "reason": "AI_DECISION_REQUIRES_EXTERNAL_MODEL",
            "immutable_result_replay": {
                "available": False,
                "reason": "NO_IMMUTABLE_AI_OUTPUT",
            },
        }

    @staticmethod
    def _layers_not_executed() -> dict[str, Any]:
        reason = "GATE_FAILED"
        return {
            "server_deterministic": {"executed": False, "reason": reason},
            "ai_decision_replay": {"executed": False, "reason": reason},
        }

    @staticmethod
    def _field_equal(recomputed, stored) -> bool:
        if recomputed is None or stored is None:
            return recomputed is None and stored is None
        if isinstance(recomputed, bool) or isinstance(stored, bool):
            return bool(recomputed) == bool(stored)
        return abs(float(recomputed) - float(stored)) <= 1e-9
