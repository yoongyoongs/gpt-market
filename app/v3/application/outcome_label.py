"""Step 17：结果标签（设计 §26）。

T 日扫描收盘为基准，取其后 ≤20 个交易日日K：
- MFE_h = max(high_i / close_T - 1)，前 h 个交易日
- MAE_h = min(low_i / close_T - 1)
- time_to_8/10/15 = 首次触及 +8%/+10%/+15% 的交易日序（1-based）
- 分级（阈值全部配置化，§26.2）：
  A: MFE20≥15% 且触及 +15% 前回撤 ≥-5% 且 ≤15 交易日
  B: MFE20≥10% 且触及 +10% 前回撤 ≥-6% 且 ≤15 交易日
  C: MFE20≥8% 且 MAE20 ≥-8%
GOOD_OPPORTUNITY = A or B。

纯函数式：bars 以 (high, low) 序列传入，不触碰数据库。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.v3.domain.candidate_engine import OutcomeLabelResult

__all__ = ["OutcomeLabelService", "LabelThresholds"]

# 比率阈值比较容差：价格两位小数除法会产生 1e-16 级误差，
# 压线值（如恰好 -0.08）不能因浮点被拒
_EPS = 1e-9


@dataclass(frozen=True)
class LabelThresholds:
    """§26.2 分级阈值（回测时支持多组对照）。"""

    a_mfe: float = 0.15
    a_mae_floor: float = -0.05
    b_mfe: float = 0.10
    b_mae_floor: float = -0.06
    c_mfe: float = 0.08
    c_mae_floor: float = -0.08
    max_days_to_target: int = 15
    touch_8: float = 0.08
    touch_10: float = 0.10
    touch_15: float = 0.15
    horizon: int = 20


@dataclass(frozen=True)
class _MfeMae:
    mfe_5: float | None
    mfe_10: float | None
    mfe_20: float | None
    mae_5: float | None
    mae_10: float | None
    mae_20: float | None
    time_to_8: int | None
    time_to_10: int | None
    time_to_15: int | None


class OutcomeLabelService:
    def __init__(self, thresholds: LabelThresholds | None = None) -> None:
        self._t = thresholds or LabelThresholds()

    def evaluate(
        self,
        code: str,
        close_t: float,
        future_bars: list[tuple[float, float]],
        *,
        security_id=None,
    ) -> OutcomeLabelResult:
        """future_bars: 按交易日升序的 (high, low) 序列（≤horizon 截断）。"""
        t = self._t
        if not close_t or close_t <= 0:
            # 无 T 日基准（停牌/数据缺失）：不给评级，宁可 PENDING 不可猜
            return OutcomeLabelResult(
                code=code, security_id=security_id, close_t=None,
                label=None, bars_used=0,
            )
        bars = future_bars[: t.horizon]
        stats = self._stats(close_t, bars)

        label = self._grade(close_t, bars, stats)
        return OutcomeLabelResult(
            code=code,
            security_id=security_id,
            close_t=close_t,
            mfe_5=stats.mfe_5, mfe_10=stats.mfe_10, mfe_20=stats.mfe_20,
            mae_5=stats.mae_5, mae_10=stats.mae_10, mae_20=stats.mae_20,
            time_to_8=stats.time_to_8, time_to_10=stats.time_to_10,
            time_to_15=stats.time_to_15,
            label=label,
            bars_used=len(bars),
        )

    def _stats(self, close_t: float, bars: list[tuple[float, float]]) -> _MfeMae:
        t = self._t
        highs = [high / close_t - 1.0 for high, _ in bars]
        lows = [low / close_t - 1.0 for _, low in bars]

        def _mfe(h: int) -> float | None:
            window = highs[:h]
            return max(window) if window else None

        def _mae(h: int) -> float | None:
            window = lows[:h]
            return min(window) if window else None

        def _time_to(level: float) -> int | None:
            for index, gain in enumerate(highs, start=1):
                if gain >= level - _EPS:
                    return index
            return None

        return _MfeMae(
            mfe_5=_mfe(5), mfe_10=_mfe(10), mfe_20=_mfe(20),
            mae_5=_mae(5), mae_10=_mae(10), mae_20=_mae(20),
            time_to_8=_time_to(t.touch_8),
            time_to_10=_time_to(t.touch_10),
            time_to_15=_time_to(t.touch_15),
        )

    def _grade(self, close_t: float, bars: list[tuple[float, float]], stats: _MfeMae) -> str | None:
        t = self._t
        if len(bars) < t.horizon:
            return None  # 观察窗不完整：不给评级，宁可 PENDING 不可猜测

        lows = [low / close_t - 1.0 for _, low in bars]

        def _mae_before_target(time_to_target: int | None) -> float | None:
            """触及 target 当日之前的最大回撤（不含到达日）。"""
            if time_to_target is None:
                return None
            window = lows[: time_to_target - 1]
            return min(window) if window else 0.0

        mfe20 = stats.mfe_20
        if mfe20 is not None and mfe20 >= t.a_mfe - _EPS:
            mae_before = _mae_before_target(stats.time_to_15)
            if (
                stats.time_to_15 is not None
                and stats.time_to_15 <= t.max_days_to_target
                and mae_before is not None
                and mae_before >= t.a_mae_floor - _EPS
            ):
                return "A"
        if mfe20 is not None and mfe20 >= t.b_mfe - _EPS:
            mae_before = _mae_before_target(stats.time_to_10)
            if (
                stats.time_to_10 is not None
                and stats.time_to_10 <= t.max_days_to_target
                and mae_before is not None
                and mae_before >= t.b_mae_floor - _EPS
            ):
                return "B"
        if (
            mfe20 is not None and mfe20 >= t.c_mfe - _EPS
            and stats.mae_20 is not None and stats.mae_20 >= t.c_mae_floor - _EPS
        ):
            return "C"
        return None
