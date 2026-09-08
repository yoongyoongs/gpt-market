"""L2 特征类召回专家（设计 §5-§10：LP/BT/RV/AC/PB/RS）。

权重表逐条对照详细设计各专家评分节；召回阶段无 60m/行业/资金流
数据的维度按 missing 处理（0 分 + 降 confidence），不允许 0 伪装。

数据代理映射（第一版，实施报告需列明）：
- LP「上方压力空间」无 major_resistance → |distance_120d_high| 代理；
- LP「250d位置」与「52w位置」窗口重合 → 120d 位置承载中期位置；
- AC「换手率温和改善」无换手率序列 → volume_5_20 温和抬升带代理；
- RV「60m Higher Low」「下降趋势线破坏」→ missing（Deep 阶段补评）；
- PB「60m重新转强」→ missing（Deep 阶段补评）。
"""

from __future__ import annotations

from app.v3.candidate_engine.experts.base import BaseExpert
from app.v3.candidate_engine.soft import (
    position_52w_score,
    smoothstep,
    soft_band,
    soft_high_better,
    soft_low_better,
    volume_5_20_score,
)
from app.v3.domain.candidate_engine import ExpertInput, ExpertScore

__all__ = [
    "LowPositionExpert",
    "BottomingExpert",
    "ReversalExpert",
    "AccumulationExpert",
    "PullbackExpert",
    "RelativeStrengthExpert",
]


class LowPositionExpert(BaseExpert):
    """Expert 1：Low Position 低位空间（设计 §5.2，Top300）。

    52w 位置用推荐形状（0~5% 仅 70 分：贴近新低不天然满分）。
    """

    def name(self) -> str:
        return "LP"

    def top_n(self) -> int:
        return 300

    def required_features(self) -> tuple[str, ...]:
        return ("position_250d",)

    def evaluate(self, stock: ExpertInput) -> ExpertScore | None:
        view = stock.feature
        if self.cannot_evaluate(view, self.required_features()):
            return None
        f = view.number
        position_250d = f("position_250d")
        return self.combine([
            (
                "52w_position", 25.0,
                position_52w_score(position_250d) / 100.0 if position_250d is not None else None,
            ),
            (
                "position_120d", 15.0,
                position_52w_score(f("position_120d")) / 100.0
                if f("position_120d") is not None else None,
            ),
            (
                "dist_60d_low", 15.0,
                soft_low_better(f("distance_60d_low"), 0.03, 0.30) / 100.0
                if f("distance_60d_low") is not None else None,
            ),
            (
                "dist_250d_high", 15.0,
                soft_band(abs(f("distance_250d_high")), 0.15, 0.25, 0.55, 0.75) / 100.0
                if f("distance_250d_high") is not None else None,
            ),
            (
                "extension_20d", 10.0,
                soft_low_better(abs(f("return_20d")), 0.04, 0.18) / 100.0
                if f("return_20d") is not None else None,
            ),
            (
                "overhead_space", 10.0,
                soft_high_better(abs(f("distance_120d_high")), 0.05, 0.50) / 100.0
                if f("distance_120d_high") is not None else None,
            ),
            (
                "drawdown_250d", 10.0,
                soft_high_better(f("return_250d"), -0.50, -0.10) / 100.0
                if f("return_250d") is not None else None,
            ),
        ])


class BottomingExpert(BaseExpert):
    """Expert 2：Bottoming 筑底（设计 §6.3，Top250）。

    周K 仍下跌但跌速明显减缓也算改善（设计 §6.2.4）；
    下跌缩量不无限奖励极低成交（floor=30）。
    """

    def name(self) -> str:
        return "BT"

    def top_n(self) -> int:
        return 250

    def required_features(self) -> tuple[str, ...]:
        return ("atr_contraction",)

    def evaluate(self, stock: ExpertInput) -> ExpertScore | None:
        view = stock.feature
        if self.cannot_evaluate(view, self.required_features()):
            return None
        f = view.number
        down_ratio = f("down_volume_ratio")
        down_score: float | None = None
        if down_ratio is not None:
            # 越低越好，但极端缩量（成交将死）封顶 30 分（设计 §6.2.3）
            low_cap = 30.0 + 70.0 * smoothstep((down_ratio - 0.05) / 0.10)
            down_score = min(soft_low_better(down_ratio, 0.35, 1.10), low_cap) / 100.0
        return self.combine([
            (
                "atr_contraction", 20.0,
                soft_high_better(f("atr_contraction"), 0.0, 0.40) / 100.0
                if f("atr_contraction") is not None else None,
            ),
            (
                "low_slope_20", 20.0,
                soft_high_better(f("low_slope_20"), -0.002, 0.002) / 100.0
                if f("low_slope_20") is not None else None,
            ),
            (
                "weekly_deceleration", 15.0,
                soft_high_better(f("weekly_decline_deceleration"), -0.002, 0.003) / 100.0
                if f("weekly_decline_deceleration") is not None else None,
            ),
            (
                "range_contraction", 15.0,
                soft_high_better(f("daily_range_contraction"), 0.0, 0.30) / 100.0
                if f("daily_range_contraction") is not None else None,
            ),
            ("down_volume_ratio", 10.0, down_score),
            (
                "base_duration", 10.0,
                soft_high_better(float(f("base_duration")), 3.0, 15.0) / 100.0
                if f("base_duration") is not None else None,
            ),
            (
                "higher_low", 10.0,
                soft_high_better(f("swing_low_trend"), 0.0, 0.002) / 100.0
                if f("swing_low_trend") is not None else None,
            ),
        ])


class ReversalExpert(BaseExpert):
    """Expert 3：Reversal 反转启动（设计 §7.4，Top250）。

    奖励「由负转零、由更负变得不那么负」，禁止 MACD>0 布尔判断；
    MA20 斜率与 MACD 改善任一可评即可（required 全缺才跳过）。
    """

    def name(self) -> str:
        return "RV"

    def top_n(self) -> int:
        return 250

    def required_features(self) -> tuple[str, ...]:
        return ("ma20_slope_delta", "macd_hist_delta")

    def evaluate(self, stock: ExpertInput) -> ExpertScore | None:
        view = stock.feature
        if self.cannot_evaluate(view, self.required_features()):
            return None
        f = view.number
        close = view.close
        hist_delta = f("macd_hist_delta")
        hist_norm = (
            hist_delta / close
            if hist_delta is not None and close is not None and close > 0
            else None
        )
        return self.combine([
            (
                "ma20_slope_delta", 20.0,
                soft_high_better(f("ma20_slope_delta"), 0.0, 0.004) / 100.0
                if f("ma20_slope_delta") is not None else None,
            ),
            (
                "ma20_ma60_converge", 15.0,
                soft_low_better(abs(f("ma20_ma60_gap")), 0.005, 0.08) / 100.0
                if f("ma20_ma60_gap") is not None else None,
            ),
            (
                "near_ma20", 15.0,
                soft_band(f("price_ma20_distance"), -0.08, -0.04, 0.01, 0.06) / 100.0
                if f("price_ma20_distance") is not None else None,
            ),
            (
                "macd_hist_delta", 10.0,
                soft_high_better(hist_norm, 0.0, 0.004) / 100.0
                if hist_norm is not None else None,
            ),
            (
                "daily_higher_low", 15.0,
                soft_high_better(f("swing_low_trend"), 0.0, 0.002) / 100.0
                if f("swing_low_trend") is not None else None,
            ),
            ("higher_low_60m", 15.0, None),
            ("trendline_break", 10.0, None),
        ])


class AccumulationExpert(BaseExpert):
    """Expert 4：Accumulation 资金潜伏（设计 §8.5，Top250）。

    量价代理版（C2 已确认）：不奖励单日爆量（U/D >3.5 或量比 >2.5
    均归零）；VWAP 承接/资金流持续性/尾盘承接无数据源 → missing。
    """

    def name(self) -> str:
        return "AC"

    def top_n(self) -> int:
        return 250

    def required_features(self) -> tuple[str, ...]:
        return ("up_down_volume_ratio", "obv_slope_z", "volume_5_20")

    def evaluate(self, stock: ExpertInput) -> ExpertScore | None:
        view = stock.feature
        if self.cannot_evaluate(view, self.required_features()):
            return None
        f = view.number
        ratio_5_20 = f("volume_5_20")
        return self.combine([
            (
                "up_down_volume_ratio", 20.0,
                soft_band(f("up_down_volume_ratio"), 0.9, 1.1, 2.0, 3.5) / 100.0
                if f("up_down_volume_ratio") is not None else None,
            ),
            (
                "obv_slope_z", 15.0,
                soft_high_better(f("obv_slope_z"), 0.0, 0.5) / 100.0
                if f("obv_slope_z") is not None else None,
            ),
            (
                "volume_5_20", 15.0,
                volume_5_20_score(ratio_5_20) / 100.0 if ratio_5_20 is not None else None,
            ),
            (
                "turnover_mild_up", 15.0,
                soft_band(ratio_5_20, 1.00, 1.05, 1.30, 1.80) / 100.0
                if ratio_5_20 is not None else None,
            ),
            ("vwap_support", 10.0, None),
            ("fund_flow_persistence", 15.0, None),
            ("late_session_support", 10.0, None),
        ])


class PullbackExpert(BaseExpert):
    """Expert 5：Pullback 突破回踩（设计 §9.3，Top180）。

    突破与「距离突破点不过远」共用 breakout_extension_20d
    （一体两面，报告注明同源）；回踩缩量须 pullback_20d 为真。
    """

    def name(self) -> str:
        return "PB"

    def top_n(self) -> int:
        return 180

    def required_features(self) -> tuple[str, ...]:
        return ("breakout_extension_20d",)

    def evaluate(self, stock: ExpertInput) -> ExpertScore | None:
        view = stock.feature
        if self.cannot_evaluate(view, self.required_features()):
            return None
        f = view.number
        extension = f("breakout_extension_20d")
        pullback = view.flag("pullback_20d")
        ratio_5_20 = f("volume_5_20")
        retrace_score: float | None
        if pullback is None:
            retrace_score = None
        elif pullback is False:
            retrace_score = 0.0
        elif ratio_5_20 is None:
            retrace_score = None
        else:
            retrace_score = soft_low_better(ratio_5_20, 0.8, 1.2) / 100.0
        return self.combine([
            (
                "first_breakout", 25.0,
                soft_band(extension, 0.0005, 0.005, 0.04, 0.10) / 100.0
                if extension is not None else None,
            ),
            (
                "near_breakout_point", 15.0,
                soft_low_better(extension, 0.02, 0.10) / 100.0
                if extension is not None else None,
            ),
            ("pullback_shrink_volume", 20.0, retrace_score),
            (
                "support_not_broken", 15.0,
                soft_high_better(f("price_ma20_distance"), -0.03, 0.0) / 100.0
                if f("price_ma20_distance") is not None else None,
            ),
            ("restrong_60m", 15.0, None),
            (
                "no_chase_extension", 10.0,
                soft_low_better(f("return_5d"), 0.05, 0.15) / 100.0
                if f("return_5d") is not None else None,
            ),
        ])


class RelativeStrengthExpert(BaseExpert):
    """Expert 6：RS 拐点（设计 §10.3，Top180）。

    找「从弱转强」而非「最强」：绝对 RS 水平只占 10 分；
    行业 RS 两维无行业分类数据 → missing（C3 适配）。
    """

    def name(self) -> str:
        return "RS"

    def top_n(self) -> int:
        return 180

    def required_features(self) -> tuple[str, ...]:
        return ("rs_5d_slope", "rs_20d_slope")

    def evaluate(self, stock: ExpertInput) -> ExpertScore | None:
        view = stock.feature
        if self.cannot_evaluate(view, self.required_features()):
            return None
        f = view.number
        low_higher = view.flag("rs_low_higher")
        return self.combine([
            (
                "rs_5d_slope", 30.0,
                soft_high_better(f("rs_5d_slope"), 0.0, 0.002) / 100.0
                if f("rs_5d_slope") is not None else None,
            ),
            (
                "rs_20d_slope", 20.0,
                soft_high_better(f("rs_20d_slope"), 0.0, 0.001) / 100.0
                if f("rs_20d_slope") is not None else None,
            ),
            (
                "rs_low_higher", 15.0,
                1.0 if low_higher is True else (0.0 if low_higher is False else None),
            ),
            ("industry_rs_delta", 15.0, None),
            ("stock_vs_industry_delta", 10.0, None),
            (
                "absolute_rs_level", 10.0,
                soft_band(f("relative_index_strength"), -0.05, 0.0, 0.10, 0.25) / 100.0
                if f("relative_index_strength") is not None else None,
            ),
        ])
