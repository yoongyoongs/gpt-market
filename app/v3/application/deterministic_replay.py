"""Deterministic Replay Engine（RC-06B / PF-002）。

整改方案 §9.2 的两层边界，边界必须写进 Replay result：

- Gate 1（保留既有 `_check_references` 语义）：point-in-time 泄漏 /
  缺输入检查，不过 Gate 时两层都不执行，status=BLOCKED；
- Server deterministic replay，三个确定性子层：
  1) Feature 重算：在 pinned Bar Revision 上按同一确定性规则重算
     Feature（CalculateSecurityFeatureService），并与当时落库的
     immutable Feature 做逐字段核验（只比较仅依赖 pinned 日 K 的字段）；
     未 pin 的输入（指数/行业 20d 收益、周线）显式声明排除，不静默；
  2) Regime 重算（PF-002 扩展）：从 immutable Regime 快照所属 feature
     run 的全部落库特征行，按同一规则（CalculateMarketRegimeService）
     重算 breadth/turnover/risk_appetite/stale 并逐字段核验；
     index_states/coverage/confidence 依赖未 pin 的指数基准与
     expected_count 语义，显式声明排除；
  3) Context 证据选择重放（PF-002 扩展）：从 immutable Context Pack
     payload + 同查询键的 evidence 页，用同一排序/预算裁剪规则
     （BuildContextPackService 的模块级确定性函数）重导出
     candidate_evidence_ids 与 items 顺序并核验；
- 其余链路阶段（Recall/Comparison/Entry Trigger）的输入未 pin 或依赖
  实时行情，显式声明 excluded_stages，不静默；
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

from app.v3.application.build_context_pack import (
    LEVEL_SETTINGS,
    evidence_item_payload,
    evidence_ranking_key,
    estimate_tokens,
)
from app.v3.application.calculate_features import CalculateSecurityFeatureService
from app.v3.application.calculate_market_regime import CalculateMarketRegimeService
from app.v3.domain.context import ContextLevel
from app.v3.domain.evidence import EvidenceReadQuery
from app.v3.domain.features import SecurityFeature
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

# Regime 重算可比字段路径（stale 规则 + breadth/turnover/risk 计数，
# 全部只依赖落库特征行）
_REGIME_COMPARABLE_PATHS: tuple[tuple[str, ...], ...] = (
    ("stale",), ("stale_reason",),
    ("breadth", "observed"), ("breadth", "advancing"),
    ("breadth", "declining"), ("breadth", "unchanged"),
    ("breadth", "advance_decline_ratio"), ("breadth", "mean_return_3d"),
    ("turnover", "observed"), ("turnover", "total_amount"),
    ("risk_appetite_facts", "volume_expansion_count"),
    ("risk_appetite_facts", "breakout_20d_count"),
    ("risk_appetite_facts", "stale_count"),
)
# 依赖未 pin 输入（指数基准）或 run 级语义（expected_count）的 Regime 字段
_REGIME_EXCLUDED_FIELDS = (
    "index_states", "coverage", "confidence", "limit_structure",
    "size_style", "growth_value_style", "industry_rotation",
)
_REGIME_EXCLUSION_REASON = (
    "INDEX_BENCHMARK_AND_EXPECTED_COUNT_INPUTS_NOT_PINNED"
)
# 链路上未纳入本轮重放的阶段（输入未 pin 或依赖实时数据），显式声明
_EXCLUDED_STAGES = (
    {"stage": "recall_channels", "reason": "UNIVERSE_SNAPSHOT_REPLAY_NOT_PINNED"},
    {"stage": "raw_opportunity_comparison", "reason": "CANDIDATE_SET_REPLAY_NOT_PINNED"},
    {"stage": "entry_trigger_cancel", "reason": "LIVE_QUOTE_DEPENDENT_NOT_REPLAYABLE"},
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
            "regime_recompute": await self._regime_layer(uow, targets),
            "context_evidence_replay": await self._context_evidence_layer(
                uow, command.context_pack_ids,
            ),
            "excluded_stages": [dict(item) for item in _EXCLUDED_STAGES],
            "excluded_unpinned_inputs": list(_EXCLUDED_UNPINNED_INPUTS),
        }

    async def _regime_layer(self, uow, targets) -> dict[str, Any]:
        """PF-002：从 immutable 落库特征行重算 Regime 确定性聚合并核验。"""
        service = CalculateMarketRegimeService()
        checked = 0
        matched = 0
        mismatched: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for target in targets:
            if not target["available"]:
                continue
            feature_run_id = target["feature_run_id"]
            data = await uow.performance.regime_replay_input(feature_run_id)
            if data is None:
                skipped.append({
                    "feature_run_id": str(feature_run_id),
                    "reason": "NO_REGIME_FOR_FEATURE_RUN",
                })
                continue
            checked += 1
            rows = data["features"]
            features = tuple(
                SecurityFeature.build(
                    feature_run_id=feature_run_id,
                    security_id=uuid4(),  # 身份占位：regime 聚合不使用
                    series_revision_id=uuid4(),
                    as_of=self._clock(),
                    close=1.0,
                    coverage=1.0,
                    stale=row["stale"],
                    return_3d=row["return_3d"],
                    amount=row["amount"],
                    volume_expansion=row["volume_expansion"],
                    breakout_20d=row["breakout_20d"],
                    input_hash="0" * 64,  # 哈希占位：regime 聚合不使用
                )
                for row in rows
            )
            recomputed = service.execute(
                feature_run_id=feature_run_id,
                features=features,
                as_of=self._clock(),
                known_at=self._clock(),
                expected_count=len(features),
                index_benchmark=None,
            ).model_dump(mode="json")
            stored_regime = data["regime"]
            run_mismatched = 0
            for path in _REGIME_COMPARABLE_PATHS:
                stored_value = _pluck(stored_regime, path)
                recomputed_value = _pluck(recomputed, path)
                if not _json_equal(recomputed_value, stored_value):
                    run_mismatched += 1
                    mismatched.append({
                        "feature_run_id": str(feature_run_id),
                        "field": ".".join(path),
                        "recomputed": recomputed_value,
                        "stored": stored_value,
                    })
            if not run_mismatched:
                matched += 1
        return {
            "executed": True,
            "checked_count": checked,
            "matched_count": matched,
            "mismatched": mismatched,
            "skipped": skipped,
            "excluded_fields": list(_REGIME_EXCLUDED_FIELDS),
            "exclusion_reason": _REGIME_EXCLUSION_REASON,
        }

    async def _context_evidence_layer(self, uow, context_pack_ids) -> dict[str, Any]:
        """PF-002：Context 证据选择确定性重放（排序 + 预算裁剪重导出）。"""
        packs = await uow.performance.context_pack_replay_payloads(context_pack_ids)
        checked = 0
        matched = 0
        mismatched: list[dict[str, Any]] = []
        for pack in packs:
            pack_id = str(pack["context_pack_id"])
            if not pack["available"]:
                mismatched.append({
                    "context_pack_id": pack_id, "field": None,
                    "reason": pack["reason"],
                })
                continue
            payload = pack["payload"]
            evidence_block = payload.get("evidence")
            if not isinstance(evidence_block, dict) or (
                "candidate_evidence_ids" not in evidence_block
            ):
                mismatched.append({
                    "context_pack_id": pack_id, "field": None,
                    "reason": "PAYLOAD_HAS_NO_EVIDENCE_BLOCK",
                })
                continue
            checked += 1
            as_of = pack["as_of"]
            page = await uow.evidence.retrieve_view(query=EvidenceReadQuery(
                subject_type=pack["subject_type"],
                subject_id=pack["subject_id"],
                as_of=as_of,
                include_candidates=False,
                limit=200,
            ))
            ranked = sorted(
                page.views,
                key=lambda view: evidence_ranking_key(view, as_of),
            )
            recomputed_candidates = [
                str(view.record.evidence_id) for view in ranked
            ]
            stored_candidates = [
                str(value) for value in evidence_block["candidate_evidence_ids"]
            ]
            pack_mismatched = False
            if recomputed_candidates != stored_candidates:
                pack_mismatched = True
                mismatched.append({
                    "context_pack_id": pack_id,
                    "field": "candidate_evidence_ids",
                    "recomputed_count": len(recomputed_candidates),
                    "stored_count": len(stored_candidates),
                })
            recomputed_selected = self._replay_evidence_selection(
                payload, ranked, pack["context_level"], pack["token_budget"], as_of,
            )
            stored_selected = [
                str(item.get("evidence_id"))
                for item in evidence_block.get("items", [])
                if isinstance(item, dict) and item.get("evidence_id")
            ]
            if recomputed_selected != stored_selected:
                pack_mismatched = True
                mismatched.append({
                    "context_pack_id": pack_id,
                    "field": "selected_evidence_ids",
                    "recomputed_count": len(recomputed_selected),
                    "stored_count": len(stored_selected),
                })
            if not pack_mismatched:
                matched += 1
        return {
            "executed": True,
            "checked_count": checked,
            "matched_count": matched,
            "mismatched": mismatched,
            "replayed_fields": ("candidate_evidence_ids", "selected_evidence_ids"),
        }

    @staticmethod
    def _replay_evidence_selection(
        payload: dict, ranked, context_level: str, token_budget: int, as_of,
    ) -> list[str]:
        """与 BuildContextPackService 同一裁剪规则重导出入选证据 id 序列。"""
        _, evidence_limit, text_limit = LEVEL_SETTINGS[ContextLevel(context_level)]
        base = {key: value for key, value in payload.items() if key != "evidence"}
        selected: list[dict] = []
        for view in ranked[:evidence_limit]:
            item = evidence_item_payload(view, as_of, text_limit)
            candidate_payload = {
                **base,
                "evidence": {
                    "boundary": "UNTRUSTED_DATA",
                    "items": [*selected, item],
                },
            }
            if estimate_tokens(candidate_payload) > token_budget:
                continue
            selected.append(item)
        return [item["evidence_id"] for item in selected]

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


def _pluck(payload: dict, path: tuple[str, ...]):
    value = payload
    for key in path:
        value = value[key]
    return value


def _json_equal(recomputed, stored) -> bool:
    """数值容差（storage 精度）+ 结构递归相等的核验比较。"""
    if isinstance(recomputed, dict) and isinstance(stored, dict):
        return recomputed.keys() == stored.keys() and all(
            _json_equal(recomputed[key], stored[key]) for key in recomputed
        )
    if isinstance(recomputed, (list, tuple)) and isinstance(stored, (list, tuple)):
        return len(recomputed) == len(stored) and all(
            _json_equal(left, right)
            for left, right in zip(recomputed, stored)
        )
    if isinstance(recomputed, bool) or isinstance(stored, bool):
        return bool(recomputed) == bool(stored)
    if recomputed is None or stored is None:
        return recomputed is None and stored is None
    try:
        left, right = float(recomputed), float(stored)
    except (TypeError, ValueError):
        return recomputed == stored
    return abs(left - right) <= max(1e-9, 1e-9 * max(abs(left), abs(right)))
