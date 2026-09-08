from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from typing import Iterable, Sequence
from uuid import UUID

from app.v3.candidate_engine import indicators as ce_ind
from app.v3.domain.features import SecurityFeature
from app.v3.domain.hashing import canonical_hash
from app.v3.domain.market_data import AdjustType, BarPeriod, BarSeriesRevision, MarketBar


RETURN_WINDOWS = (3, 5, 10, 20, 60, 120, 250)
POSITION_WINDOWS = (60, 120, 250)
MA_WINDOWS = (5, 10, 20, 60)


class CalculateSecurityFeatureService:
    feature_fields = 28

    def execute(
        self,
        *,
        feature_run_id: UUID,
        revision: BarSeriesRevision,
        as_of: datetime,
        weekly_revision: BarSeriesRevision | None = None,
        index_return_20d: float | None = None,
        industry_return_20d: float | None = None,
        benchmark_series: Sequence[tuple[datetime, float]] | None = None,
        stale_after: timedelta = timedelta(days=7),
    ) -> SecurityFeature:
        if revision.period is not BarPeriod.DAY or revision.adjust_type is not AdjustType.QFQ:
            raise ValueError("full-market features require a published QFQ DAY revision")
        bars = tuple(bar for bar in revision.bars if bar.bar_time <= as_of and not bar.provisional)
        if not bars:
            raise ValueError("no complete bar is known at as_of")

        closes = [bar.close for bar in bars]
        values: dict[str, object] = {}
        missing: list[str] = []
        for window in RETURN_WINDOWS:
            name = f"return_{window}d"
            values[name] = self._return(closes, window)
            if values[name] is None:
                missing.append(name)
        for window in POSITION_WINDOWS:
            name = f"position_{window}d"
            values[name] = self._position(bars, window)
            if values[name] is None:
                missing.append(name)
        for window in MA_WINDOWS:
            name = f"ma{window}"
            values[name] = self._mean(closes, window)
            if values[name] is None:
                missing.append(name)

        values["ma20_slope"] = self._ma_slope(closes, 20)
        values["ma60_slope"] = self._ma_slope(closes, 60)
        values["atr14"] = self._atr(bars, 14)
        values["atr_pct"] = (
            values["atr14"] / closes[-1] if values["atr14"] is not None else None
        )
        values["volatility20"] = self._volatility(closes, 20)
        values["distance_60d_high"], values["distance_60d_low"] = self._distances(bars, 60)
        values["breakout_20d"] = self._breakout(bars, 20)
        values["pullback_20d"] = self._pullback(closes)
        values["amount"] = bars[-1].amount
        values["volume_ratio_5d"] = self._volume_ratio(bars, 5)
        values["volume_expansion"] = (
            values["volume_ratio_5d"] >= 1.5 if values["volume_ratio_5d"] is not None else None
        )
        return20 = values["return_20d"]
        values["relative_index_strength"] = (
            return20 - index_return_20d
            if return20 is not None and index_return_20d is not None
            else None
        )
        values["relative_industry_strength"] = (
            return20 - industry_return_20d
            if return20 is not None and industry_return_20d is not None
            else None
        )
        for name in (
            "ma20_slope", "ma60_slope", "atr14", "atr_pct", "volatility20",
            "distance_60d_high", "distance_60d_low", "breakout_20d", "pullback_20d",
            "amount", "volume_ratio_5d", "volume_expansion", "relative_index_strength",
            "relative_industry_strength",
        ):
            if values[name] is None:
                missing.append(name)

        latest_time = bars[-1].bar_time
        stale = as_of - latest_time > stale_after
        input_hash = canonical_hash(
            {
                "series_revision_id": revision.revision_id,
                "series_content_hash": revision.content_hash,
                "factor_revision_id": revision.factor_revision_id,
                "as_of": as_of,
            }
        )
        available = self.feature_fields - len(set(missing))
        daily_trend = self._trend_state(values)
        weekly_trend = self._weekly_trend_state(weekly_revision, as_of)
        multi_state, multi_rule = self._multi_timeframe(daily_trend, weekly_trend)
        extras = {
            "bar_count": len(bars),
            "latest_bar_time": latest_time,
            "daily_trend_state": daily_trend,
            "weekly_trend_state": weekly_trend,
            "multi_timeframe_state": multi_state,
            "multi_timeframe_rule": multi_rule,
            "overheated": bool(values["position_60d"] is not None and values["position_60d"] >= 0.9),
            "liquidity_quality": self._liquidity_quality(bars[-1].amount),
        }
        extras.update(
            self._candidate_engine_extras(
                bars, closes, weekly_revision, as_of, benchmark_series, values
            )
        )
        return SecurityFeature.build(
            feature_run_id=feature_run_id,
            security_id=revision.security_id,
            series_revision_id=revision.revision_id,
            factor_revision_id=revision.factor_revision_id,
            as_of=as_of,
            close=closes[-1],
            **values,
            coverage=max(0.0, available / self.feature_fields),
            stale=stale,
            missing_fields=tuple(sorted(set(missing))),
            source_errors=(),
            quality={
                "adjust_type": revision.adjust_type.value,
                "point_in_time_precision": revision.point_in_time_precision.value,
                "raw_bar_available": revision.raw_bar_available,
                "precision_reason": revision.precision_reason,
            },
            features=extras,
            input_hash=input_hash,
        )

    @staticmethod
    def _return(closes: list[float], window: int) -> float | None:
        return None if len(closes) <= window else closes[-1] / closes[-window - 1] - 1

    @staticmethod
    def _mean(values: list[float], window: int) -> float | None:
        return None if len(values) < window else statistics.fmean(values[-window:])

    @classmethod
    def _ma_slope(cls, closes: list[float], window: int) -> float | None:
        if len(closes) <= window:
            return None
        current = cls._mean(closes, window)
        previous = statistics.fmean(closes[-window - 1:-1])
        return None if current is None or previous == 0 else current / previous - 1

    @staticmethod
    def _position(bars: tuple[MarketBar, ...], window: int) -> float | None:
        if len(bars) < window:
            return None
        sample = bars[-window:]
        low, high = min(item.low for item in sample), max(item.high for item in sample)
        return 0.5 if high == low else (sample[-1].close - low) / (high - low)

    @staticmethod
    def _atr(bars: tuple[MarketBar, ...], window: int) -> float | None:
        if len(bars) <= window:
            return None
        ranges = []
        for previous, current in zip(bars[-window - 1:-1], bars[-window:]):
            ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
        return statistics.fmean(ranges)

    @staticmethod
    def _volatility(closes: list[float], window: int) -> float | None:
        if len(closes) <= window:
            return None
        returns = [closes[index] / closes[index - 1] - 1 for index in range(len(closes) - window, len(closes))]
        return statistics.stdev(returns) * math.sqrt(250) if len(returns) > 1 else 0.0

    @staticmethod
    def _distances(bars: tuple[MarketBar, ...], window: int) -> tuple[float | None, float | None]:
        if len(bars) < window:
            return None, None
        sample, close = bars[-window:], bars[-1].close
        high, low = max(item.high for item in sample), min(item.low for item in sample)
        return close / high - 1, close / low - 1

    @staticmethod
    def _breakout(bars: tuple[MarketBar, ...], window: int) -> bool | None:
        if len(bars) <= window:
            return None
        return bars[-1].close > max(item.high for item in bars[-window - 1:-1])

    @classmethod
    def _pullback(cls, closes: list[float]) -> bool | None:
        if len(closes) <= 20:
            return None
        ma20 = cls._mean(closes, 20)
        slope = cls._ma_slope(closes, 20)
        return bool(ma20 and slope is not None and slope > 0 and abs(closes[-1] / ma20 - 1) <= 0.03)

    @staticmethod
    def _volume_ratio(bars: tuple[MarketBar, ...], window: int) -> float | None:
        if len(bars) <= window:
            return None
        baseline = statistics.fmean(item.volume for item in bars[-window - 1:-1])
        return None if baseline <= 0 else bars[-1].volume / baseline

    @staticmethod
    def _multi_timeframe(daily: str, weekly: str) -> tuple[str, str | None]:
        """§14.2 多周期合成事实（确定性规则，非 Final Score）。

        weekly=DOWN 且日线上涨 → 默认描述"下降趋势中的反弹"，
        反转必须有明确可解释证据；任一关键周期 UNKNOWN → UNKNOWN，
        绝不用别的周期数据冒充（§14.3）。
        """
        if daily == "UNKNOWN" or weekly == "UNKNOWN":
            return "UNKNOWN", None
        if weekly == "DOWN" and daily == "UP":
            return "WEEKLY_DOWN_DAILY_BOUNCE", "下降趋势中的反弹"
        return f"WEEKLY_{weekly}_DAILY_{daily}", None

    @staticmethod
    def _trend_state(values: dict[str, object]) -> str:
        ma20, ma60 = values.get("ma20"), values.get("ma60")
        if ma20 is None or ma60 is None:
            return "UNKNOWN"
        if ma20 > ma60 and (values.get("ma20_slope") or 0) > 0:
            return "UP"
        if ma20 < ma60 and (values.get("ma20_slope") or 0) < 0:
            return "DOWN"
        return "SIDEWAYS"

    @classmethod
    def _weekly_trend_state(
        cls, weekly_revision: BarSeriesRevision | None, as_of: datetime
    ) -> str:
        """周级趋势状态（RC-04-01 冻结语义）。

        输入必须是已发布的 QFQ WEEK revision，按同一 as_of 过滤完整周 K。
        以周收盘计算 MA10W/MA30W 及 MA10W 斜率：比率 > +1% 且斜率 > 0 为 UP，
        < -1% 且斜率 < 0 为 DOWN，|比率| <= 1% 为 BASE（周均线收敛筑底），
        其余为 RANGE；无周 K revision 或不足 30 根完整周 K 一律 UNKNOWN，
        不猜测。
        """
        if weekly_revision is None:
            return "UNKNOWN"
        if (
            weekly_revision.period is not BarPeriod.WEEK
            or weekly_revision.adjust_type is not AdjustType.QFQ
        ):
            raise ValueError("weekly trend state requires a published QFQ WEEK revision")
        bars = tuple(
            bar for bar in weekly_revision.bars
            if bar.bar_time <= as_of and not bar.provisional
        )
        closes = [bar.close for bar in bars]
        if len(closes) < 30:
            return "UNKNOWN"
        ma10 = cls._mean(closes, 10)
        ma30 = statistics.fmean(closes[-30:])
        slope = cls._ma_slope(closes, 10)
        if ma10 is None or ma30 == 0 or slope is None:
            return "UNKNOWN"
        ratio = ma10 / ma30 - 1
        if ratio > 0.01 and slope > 0:
            return "UP"
        if ratio < -0.01 and slope < 0:
            return "DOWN"
        if abs(ratio) <= 0.01:
            return "BASE"
        return "SIDEWAYS"

    @staticmethod
    def _liquidity_quality(amount: float | None) -> str:
        if amount is None:
            return "UNKNOWN"
        if amount >= 100_000_000:
            return "HIGH"
        if amount >= 20_000_000:
            return "MEDIUM"
        return "LOW"

    @classmethod
    def _candidate_engine_extras(
        cls,
        bars: tuple[MarketBar, ...],
        closes: list[float],
        weekly_revision: BarSeriesRevision | None,
        as_of: datetime,
        benchmark_series: Sequence[tuple[datetime, float]] | None,
        values: dict[str, object],
    ) -> dict[str, object]:
        """候选引擎扩展指标（additive，全部落在 features JSONB）。

        缺失一律为 None（missing ≠ 0 分，设计 §11.3）；不计入
        feature_fields/coverage（28 字段语义冻结，向后兼容）。
        指标定义见 app/v3/candidate_engine/indicators.py。
        """
        highs = [bar.high for bar in bars]
        lows = [bar.low for bar in bars]
        volumes = [bar.volume for bar in bars]
        close = closes[-1]

        macd_hist, macd_hist_delta = ce_ind.macd(closes)
        obv_z = ce_ind.obv_slope_z(closes, volumes)
        # 5/20 日量能关系（设计 §8.4）
        volume_5_20 = None
        if len(volumes) >= 20:
            mean5 = statistics.fmean(volumes[-5:])
            mean20 = statistics.fmean(volumes[-20:])
            volume_5_20 = mean5 / mean20 if mean20 > 0 else None
        ma20, ma60 = values.get("ma20"), values.get("ma60")
        extra: dict[str, object] = {
            # RV 反转启动（设计 §7）
            "macd_hist": macd_hist,
            "macd_hist_delta": macd_hist_delta,
            "ma20_slope_delta": ce_ind.ma_slope_delta(closes, 20),
            "ma20_ma60_gap": (
                ma20 / ma60 - 1
                if isinstance(ma20, (int, float)) and isinstance(ma60, (int, float)) and ma60 > 0
                else None
            ),
            "price_ma20_distance": (
                close / ma20 - 1 if isinstance(ma20, (int, float)) and ma20 > 0 else None
            ),
            "swing_low_trend": ce_ind.swing_low_trend(lows, baseline=close),
            # BT 筑底（设计 §6）
            "atr_contraction": ce_ind.atr_contraction(highs, lows, closes),
            "daily_range_contraction": ce_ind.daily_range_contraction(highs, lows, closes),
            "down_volume_ratio": ce_ind.down_volume_ratio(closes, volumes),
            "up_down_volume_ratio": ce_ind.up_down_volume_ratio(closes, volumes),
            "low_slope_20": ce_ind.low_slope(lows, window=20),
            "base_duration": ce_ind.base_duration(closes),
            # AC 资金潜伏（设计 §8）
            "obv_slope_z": obv_z,
            "volume_5_20": volume_5_20,
            # NonChase / Penalty（设计 §19.2/§20）
            "consecutive_up_days": ce_ind.consecutive_up_days(closes),
            "volume_spike": ce_ind.volume_spike(volumes),
            # LP 低位（设计 §5.2）：250d 高点距离补充
            "distance_250d_high": (
                cls._distance_to_window_high(bars, 250)
            ),
            # PB 回踩（设计 §9）：close 相对前 20 日平台高点的超出幅度
            "breakout_extension_20d": (
                cls._breakout_extension(bars, 20)
            ),
        }

        # 周K 跌速（设计 §6.2.4）：须发布 QFQ WEEK revision
        if weekly_revision is not None and (
            weekly_revision.period is BarPeriod.WEEK
            and weekly_revision.adjust_type is AdjustType.QFQ
        ):
            weekly_closes = [
                bar.close
                for bar in weekly_revision.bars
                if bar.bar_time <= as_of and not bar.provisional
            ]
            recent, _prev, decel = ce_ind.weekly_decline_metrics(weekly_closes)
            extra["weekly_slope_8w"] = recent
            extra["weekly_decline_deceleration"] = decel
        else:
            extra["weekly_slope_8w"] = None
            extra["weekly_decline_deceleration"] = None

        # RS 相对强度序列指标（设计 §10）：按交易日 inner join（P0-04），
        # benchmark 序列缺失时全 None（missing ≠ 0 分）
        extra.update(
            ce_ind.rs_metrics(
                [(bar.bar_time, bar.close) for bar in bars], benchmark_series
            )
            if benchmark_series
            else {
                "rs_5d_slope": None,
                "rs_20d_slope": None,
                "rs_slope_delta": None,
                "rs_low_higher": None,
            }
        )
        return extra

    @staticmethod
    def _distance_to_window_high(
        bars: tuple[MarketBar, ...], window: int
    ) -> float | None:
        sample = bars[-window:] if len(bars) >= window else None
        if sample is None or not sample:
            return None
        high = max(bar.high for bar in sample)
        return bars[-1].close / high - 1 if high > 0 else None

    @staticmethod
    def _breakout_extension(
        bars: tuple[MarketBar, ...], window: int
    ) -> float | None:
        """close 相对前 window 日平台高点（不含当日）的超出幅度。

        >0 = 已突破（幅度=延伸度，PB 用它控追高）；<=0 = 未突破。
        """
        if len(bars) <= window:
            return None
        prev_high = max(bar.high for bar in bars[-window - 1:-1])
        return bars[-1].close / prev_high - 1 if prev_high > 0 else None


def mean_available(values: Iterable[float | None]) -> float | None:
    available = [value for value in values if value is not None]
    return statistics.fmean(available) if available else None
