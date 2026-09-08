"""P1-02：Market Regime → Deep Rank 单一可解释市场分（任务书 §20.3）。

输入 = feature_run_id PIT 绑定的 MarketRegimeSnapshot 落库事实（dict），
不重算市场指标、不拿「当前最新 regime」混入历史 scan。

权重：index_state 50% / breadth 30% / risk_appetite 20%；
部分 missing → weighted_combine 有效权重归一（P0-05 语义）。
市场 DOWN 只降分+提示风险，不 hard reject（§20.4）。
"""

from __future__ import annotations

from app.v3.candidate_engine.soft import weighted_combine

__all__ = ["MarketRegimeScoreService"]

# §20.3 Index State
_INDEX_STATE_SCORE = {"UP": 80.0, "RANGE": 60.0, "DOWN": 35.0, "UNKNOWN": None}

_WEIGHTS = {"index_state": 50.0, "breadth": 30.0, "risk_appetite": 20.0}

# 软分饱和点（第一版简单可解释，硬编码便于回放审计）
_MEAN_RETURN_SATURATION = 0.03  # ±3% 3 日均收益 → 0~1 满量程
_EXPANSION_RATE_SATURATION = 0.20  # 20% 个股放量 → 满分
_BREAKOUT_RATE_SATURATION = 0.10  # 10% 个股突破 20 日高 → 满分


def _clamp01(value: float) -> float:
    return max(0.0, min(value, 1.0))


def _breadth_score(breadth: dict) -> float | None:
    """advance_decline_ratio（ratio/(1+ratio)，1:1 → 0.5）70%
    + mean_return_3d（±3% 满量程）30%，子项 missing 归一。"""
    ratio = breadth.get("advance_decline_ratio")
    mean_ret = breadth.get("mean_return_3d")
    parts = [
        ("advance_decline_ratio", 70.0,
         None if ratio is None or ratio <= 0 else _clamp01(ratio / (1.0 + ratio))),
        ("mean_return_3d", 30.0,
         None if mean_ret is None
         else _clamp01(0.5 + mean_ret / _MEAN_RETURN_SATURATION * 0.5)),
    ]
    value, _confidence, _reasons, _ = weighted_combine(parts)
    return value


def _appetite_score(facts: dict, observed: int) -> tuple[float | None, dict]:
    """coverage + 放量/突破占比 轻度修正；缺 observed 分母 → missing。"""
    coverage = facts.get("coverage")
    expansion = facts.get("volume_expansion_count")
    breakout = facts.get("breakout_20d_count")
    if observed > 0:
        expansion_rate = (
            None if expansion is None else _clamp01(expansion / observed / _EXPANSION_RATE_SATURATION)
        )
        breakout_rate = (
            None if breakout is None else _clamp01(breakout / observed / _BREAKOUT_RATE_SATURATION)
        )
    else:
        expansion_rate = breakout_rate = None
    parts = [
        ("coverage", 50.0, None if coverage is None else _clamp01(coverage)),
        ("volume_expansion_rate", 25.0, expansion_rate),
        ("breakout_rate", 25.0, breakout_rate),
    ]
    value, _confidence, _reasons, _ = weighted_combine(parts)
    return value, {
        "volume_expansion_rate": expansion_rate,
        "breakout_rate": breakout_rate,
    }


class MarketRegimeScoreService:
    """snapshot dict 键：index_states/breadth/risk_appetite_facts/coverage。"""

    def compute(self, snapshot: dict) -> tuple[float | None, dict]:
        """返回 (score 0~100 | None, components_detail)。全 missing → None。"""
        index_states = snapshot.get("index_states") or {}
        breadth = snapshot.get("breadth") or {}
        facts = snapshot.get("risk_appetite_facts") or {}
        observed = breadth.get("observed") or 0

        index_raw = _INDEX_STATE_SCORE.get(index_states.get("status"))
        breadth_raw = _breadth_score(breadth)
        appetite_raw, appetite_rates = _appetite_score(
            {**facts, "coverage": snapshot.get("coverage")}, observed,
        )

        parts = [
            (name, _WEIGHTS[name],
             None if raw is None else raw / 100.0)
            for name, raw in (
                ("index_state", index_raw),
                ("breadth", breadth_raw),
                ("risk_appetite", appetite_raw),
            )
        ]
        value, confidence, reasons, _ = weighted_combine(parts)
        detail = {
            "index_state": index_raw,
            "breadth": breadth_raw,
            "risk_appetite": appetite_raw,
            "breadth_rates": {"advance_decline_ratio": breadth.get("advance_decline_ratio")},
            "appetite_rates": appetite_rates,
            "confidence": confidence,
            "reasons": list(reasons),
        }
        return value, detail
