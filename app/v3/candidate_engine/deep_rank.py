"""L6 Deep Rank（设计 §22）。

权重：Machine 45 / 周K 15 / 日K 15 / 60m 10 / 市场 5 / 行业 5 / RR 5。
P1-01/02/03：60m（Machine Top120 抓取）、市场 regime（feature_run_id PIT）、
行业上下文（无可靠源 → missing + NO_RELIABLE_INDUSTRY_CONTEXT）按
weighted_combine 有效权重归一（missing ≠ 负分）；
周K下降+日K上升 且无反转证据 → trend_conflict，压 DailyStructure（§22.2）。
Top60。
"""

from __future__ import annotations

from uuid import UUID

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
    """周K下降中的反弹是否具备明确反转证据（任务书 §9.2/§9.3）。

    解除须同时满足：

    - 条件组 A：weekly_decline_deceleration > _REVERSAL_DECEL_MIN
    - 条件组 B：RV 专家分 >= _REVERSAL_RV_MIN
    - 条件组 C：daily_state == UP（日K结构确认）
    - 条件组 D（P1-01 接入后）：60m trend == UP——仅当 pool item
      携带 minute_60_state 键（数据已接入路径）才参与判定；
      接入路径内 missing/UNKNOWN 一律不当作通过（§9.2）。
    """
    decel = item.get("weekly_decline_deceleration")
    rv = item.get("rv_score")
    daily = item.get("daily_state")
    base = bool(
        decel is not None and decel > _REVERSAL_DECEL_MIN
        and rv is not None and rv >= _REVERSAL_RV_MIN
        and daily == "UP"
    )
    if "minute_60_state" in item and base:
        return item.get("minute_60_state") == "UP"
    return base

_STATE_SCORE = {"UP": 90.0, "FLAT": 60.0, "DOWN": 30.0}
# P1-01 §19.5：60m 状态 → 执行分
_MINUTE60_STATE_SCORE = {"UP": 90.0, "SIDEWAYS": 60.0, "DOWN": 30.0, "UNKNOWN": None}
_MINUTE60_TRUSTED_QUALITY = "UNTRUSTED"  # 该 quality 一律不可当事实


def _minute60_score(fact: dict | None) -> tuple[float | None, tuple[str, ...]]:
    """§19.5/§19.6：60m 事实 → 执行分；stale/UNTRUSTED 不当事实。"""
    if fact is None:
        return None, ()
    if fact.get("stale"):
        return None, ("minute_60_stale_not_fact",)
    if fact.get("quality") == _MINUTE60_TRUSTED_QUALITY:
        return None, ("minute_60_untrusted_quality",)
    return _MINUTE60_STATE_SCORE.get(fact.get("state")), ()


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
        weekly_decline_deceleration/rr_score，及 P1 注入：

        - minute_60: {state, stale, quality, support, resistance, ...} | None
          （P1-01 抓取事实；缺键 = 60m 未接入路径）
        - minute_60_state: str（反转证据条件组 D 判定用，与 minute_60 同源）
        - market_regime_score: float | None（P1-02，feature_run_id PIT）
        - industry_missing_reason: str（P1-03，默认 NO_RELIABLE_INDUSTRY_CONTEXT）
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
            m60_raw = item.get("minute_60")
            m60_norm, m60_reasons = _minute60_score(m60_raw)
            reasons.extend(m60_reasons)
            industry_reason = item.get("industry_missing_reason")
            if industry_reason:
                reasons.append(industry_reason)
            raws = {
                "machine": item["machine_score"],
                "weekly_structure": weekly,
                "daily_structure": daily,
                "minute_60_execution": m60_norm,
                "market_regime": item.get("market_regime_score"),
                "industry_context": None,  # P1-03：无可靠源恒 missing
                "risk_reward_refined": item.get("rr_score"),
            }
            parts = [
                (name, WEIGHTS[name], None if raws[name] is None else raws[name] / 100.0)
                for name in WEIGHTS
            ]
            value, confidence, comb_reasons, _ = weighted_combine(parts)
            # effective==0（machine_score 恒在，理论不可达）防御：记 0 分，
            # confidence=0 已标 missing（P0-05，不伪装真实分）
            value = 0.0 if value is None else value
            reasons.extend(comb_reasons)
            components = {name: _opt(raws[name]) for name in WEIGHTS}
            # P1-04：每组件 raw/normalized/weight/missing/source 完整证据
            components_detail = {
                "machine": self._detail(raws["machine"], WEIGHTS["machine"],
                                        "machine_rank"),
                "weekly_structure": self._detail(raws["weekly_structure"],
                                                 WEIGHTS["weekly_structure"], "daily_bars"),
                "daily_structure": self._detail(raws["daily_structure"],
                                                WEIGHTS["daily_structure"], "daily_bars"),
                "minute_60_execution": self._detail(
                    raws["minute_60_execution"], WEIGHTS["minute_60_execution"],
                    "minute_60_fetch" if m60_raw is not None else "not_fetched",
                ),
                "market_regime": self._detail(
                    raws["market_regime"], WEIGHTS["market_regime"],
                    item.get("market_regime_source") or "no_regime_snapshot",
                ),
                "industry_context": {
                    "raw": None, "normalized": None, "weight": WEIGHTS["industry_context"],
                    "missing": True,
                    "source": industry_reason or "NO_RELIABLE_INDUSTRY_CONTEXT",
                },
                "risk_reward_refined": self._detail(raws["risk_reward_refined"],
                                                    WEIGHTS["risk_reward_refined"], "rr_engine"),
            }
            ranked.append((round(value, 4), item["code"], item["security_id"], {
                "confidence": confidence,
                "conflict": conflict,
                "reasons": reasons,
                "components": components,
                "components_detail": components_detail,
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
                components_detail=meta["components_detail"],
                reasons=tuple(meta["reasons"]),
                rank=rank,
            ))
        return DeepRankResult(
            evaluated_count=len(pool),
            top_n=len(entries),
            entries=tuple(entries),
        )

    @staticmethod
    def _detail(raw: float | None, weight: float, source: str) -> dict:
        """P1-04：单组件证据（raw 0~100 / normalized 0~1 / 权重 / missing）。"""
        return {
            "raw": raw,
            "normalized": None if raw is None else raw / 100.0,
            "weight": weight,
            "missing": raw is None,
            "source": source,
        }

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
