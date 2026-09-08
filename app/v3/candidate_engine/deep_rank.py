"""L6 Deep Rank（设计 §22）。

权重：Machine 45 / 周K 15 / 日K 15 / 60m 10 / 市场 5 / 行业 5 / RR 5。
60m/市场/行业 v1 missing → combine 降权（有效权重 0.85 归一）；
周K下降+日K上升 且无反转证据 → trend_conflict，压 DailyStructure（§22.2）。
Top60。
"""

from __future__ import annotations

from uuid import UUID

from app.v3.candidate_engine.machine_rank import MachineRankResult
from app.v3.candidate_engine.soft import weighted_combine
from app.v3.domain.candidate_engine import DeepRankEntry, DeepRankResult

__all__ = ["DeepRankService", "evaluate_reversal_evidence"]

WEIGHTS = {
    "machine": 45.0,
    "weekly_structure": 15.0,
    "daily_structure": 15.0,
    "minute_60_execution": 10.0,
    "market_regime": 5.0,
    "industry_context": 5.0,
    "risk_reward_refined": 5.0,
}

_REVERSAL_DECEL_MIN = 0.01  # 周级减速"有意义"阈值（0.0001 级噪声不算）
_REVERSAL_RV_MIN = 70.0  # 反转启动专家（RV）足够强的门槛
_CONFLICT_REASONS = (
    "trend_conflict_no_reversal_evidence",
    "WEEKLY_DOWN_DAILY_BOUNCE_UNCONFIRMED",  # 任务书 P0-07 §9.4
)


def evaluate_reversal_evidence(item: dict) -> bool:
    """周K下降中的反弹是否具备明确反转证据（任务书 P0-07 §9.3 第一阶段）。

    「跌速变慢」只是风险减轻，不是明确反转——decel>0 不再单独解除冲突。
    60m 尚未接通（P1-01）前，解除须同时满足：

    - 条件组 A：weekly_decline_deceleration > _REVERSAL_DECEL_MIN（1 个证据）
    - 条件组 B：RV 专家分 >= _REVERSAL_RV_MIN
    - 条件组 C：daily_state == UP（日K结构确认）

    条件组 D（60m 确认）接入后追加为第四条（missing 不当作通过）。
    """
    decel = item.get("weekly_decline_deceleration")
    rv = item.get("rv_score")
    daily = item.get("daily_state")
    return bool(
        decel is not None and decel > _REVERSAL_DECEL_MIN
        and rv is not None and rv >= _REVERSAL_RV_MIN
        and daily == "UP"
    )

_STATE_SCORE = {"UP": 90.0, "FLAT": 60.0, "DOWN": 30.0}


def _state_score(state: str | None, proxy: float | None) -> float | None:
    """趋势状态 → 结构分；状态缺失退回特征代理。"""
    if state is not None:
        return _STATE_SCORE.get(state)
    return proxy


def _slope_proxy(slope: float | None) -> float | None:
    """周K 8 周斜率 → 0~100 代理（0 → 60 中性，±5% 饱和）。"""
    if slope is None:
        return None
    return 60.0 + max(-60.0, min(40.0, slope / 0.05 * 40.0))


def _scale(value: float | None) -> float | None:
    return None if value is None else value / 100.0


def _opt(value) -> float | None:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value / 100.0 if 0.0 <= value <= 100.0 else None


class DeepRankService:
    """输入 = Machine Top120 池，每项携带 machine 分与结构/RR 数据。"""

    def __init__(self, top_n: int = 60):
        if top_n <= 0:
            raise ValueError("Deep Rank top_n must be positive")
        self.top_n = top_n

    def execute(self, pool: list[dict]) -> DeepRankResult:
        """pool item 键：security_id/code/machine_score/machine_rank/
        weekly_state/daily_state/multi_state/weekly_slope_8w/
        weekly_decline_deceleration/rr_score。

        60m 执行/市场状态/行业上下文 v1 无数据源，恒 missing 降权。
        """
        ranked: list[tuple[float, str, UUID, dict]] = []
        for item in pool:
            reasons: list[str] = []
            conflict = self._trend_conflict(item, reasons)
            weekly = _state_score(item.get("weekly_state"), _slope_proxy(item.get("weekly_slope_8w")))
            daily = _state_score(item.get("daily_state"), None)
            if conflict:
                daily = min(daily if daily is not None else 40.0, 40.0)
                reasons.append("weekly_down_daily_bounce_without_reversal")
            parts = [
                ("machine", WEIGHTS["machine"], item["machine_score"] / 100.0),
                ("weekly_structure", WEIGHTS["weekly_structure"], _scale(weekly)),
                ("daily_structure", WEIGHTS["daily_structure"], _scale(daily)),
                ("minute_60_execution", WEIGHTS["minute_60_execution"], None),
                ("market_regime", WEIGHTS["market_regime"], None),
                ("industry_context", WEIGHTS["industry_context"], None),
                ("risk_reward_refined", WEIGHTS["risk_reward_refined"], _opt(item.get("rr_score"))),
            ]
            value, confidence, comb_reasons, _ = weighted_combine(parts)
            # effective==0（machine_score 恒在，理论不可达）防御：记 0 分，
            # confidence=0 已标 missing（P0-05，不伪装真实分）
            value = 0.0 if value is None else value
            reasons.extend(comb_reasons)
            components = {
                "machine": item["machine_score"],
                "weekly_structure": _scale(weekly),
                "daily_structure": _scale(daily),
                "risk_reward_refined": _opt(item.get("rr_score")),
            }
            ranked.append((round(value, 4), item["code"], item["security_id"], {
                "confidence": confidence,
                "conflict": conflict,
                "reasons": reasons,
                "components": components,
            }))

        ranked.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
        entries: list[DeepRankEntry] = []
        for rank, (score, code, security_id, meta) in enumerate(ranked[: self.top_n], start=1):
            entries.append(DeepRankEntry(
                security_id=security_id,
                code=code,
                deep_score=score,
                confidence=round(meta["confidence"], 4),
                trend_conflict=meta["conflict"],
                components=meta["components"],
                reasons=tuple(meta["reasons"]),
                rank=rank,
            ))
        return DeepRankResult(
            evaluated_count=len(pool),
            top_n=len(entries),
            entries=tuple(entries),
        )

    # ---------- helpers ----------

    def _trend_conflict(self, item: dict, reasons: list[str]) -> bool:
        """§22.2：周K下降 + 日K上升，且无明确反转证据 → 冲突降分。

        P0-07：反转证据由 evaluate_reversal_evidence 综合判定
        （decel 有意义 + RV 足够强 + 日K UP），不再以 decel>0 单独解除。
        """
        if item.get("multi_state") == "WEEKLY_DOWN_DAILY_BOUNCE":
            if not evaluate_reversal_evidence(item):
                reasons.extend(_CONFLICT_REASONS)
                return True
        return False
