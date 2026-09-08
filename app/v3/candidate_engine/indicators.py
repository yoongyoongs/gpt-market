"""候选引擎补充指标库（纯函数，无状态）。

设计 §6/§7/§8/§10 要求但现有 calculate_features 未覆盖的指标：
MACD（含 histogram delta）、OBV 斜率、swing high/low 局部极值与
低点回归斜率、ATR 收敛、周K跌速减缓、涨跌量比、连续上涨天数等。

约定：
- 输入序列均按时间升序，[-1] 为最新；
- 数据不足返回 None（missing），不用 0 伪装；
- 斜率类输出一律按价格归一（除以基准价），跨股票可比。
"""

from __future__ import annotations

import statistics
from typing import Sequence

BarLike = tuple[float, float, float]  # (high, low, volume) 的最小 bar 投影


def _ema(values: Sequence[float], span: int) -> list[float]:
    """标准 EMA：alpha = 2/(span+1)，首值用首个输入初始化。"""
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    output = [values[0]]
    for value in values[1:]:
        output.append(output[-1] + alpha * (value - output[-1]))
    return output


def macd(
    closes: Sequence[float],
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float | None, float | None]:
    """返回 (macd_hist 最新值, hist_delta = hist[-1] - hist[-(signal_delta+1)])。

    设计 §7.3：改善信号是绿柱持续收窄（hist_delta > 0 即可为正贡献），
    而不是 MACD>0 布尔判断。数据不足返回 (None, None)。
    """
    if len(closes) < slow + signal:
        return None, None
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    diff = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(diff[slow - 1:], signal)
    hist_tail = [d - e for d, e in zip(diff[slow - 1:], dea)]
    if not hist_tail:
        return None, None
    hist_now = hist_tail[-1]
    delta = None
    if len(hist_tail) >= 4:
        delta = hist_now - hist_tail[-4]
    return hist_now, delta


def obv_slope_z(
    closes: Sequence[float], volumes: Sequence[float], *, window: int = 20
) -> float | None:
    """OBV 斜率 z 分（设计 §8.3）。

    OBV 按涨跌号累计成交量；对近 window 日 OBV 做线性回归，
    用每日 OBV 增量的标准差归一（z = slope / std(diff)），
    跨股票可比。无变化（横盘 OBV 恒定）返回 0.0 而非 None。
    """
    if len(closes) < window + 1 or len(volumes) != len(closes):
        return None
    obv = [0.0]
    for index in range(1, len(closes)):
        step = volumes[index] if closes[index] > closes[index - 1] else (
            -volumes[index] if closes[index] < closes[index - 1] else 0.0
        )
        obv.append(obv[-1] + step)
    sample = obv[-window:]
    diffs = [sample[i] - sample[i - 1] for i in range(1, len(sample))]
    scale = statistics.fmean(abs(d) for d in diffs)
    slope = _linreg_slope(list(range(len(sample))), sample)
    if scale <= 0:
        # 窗口内 OBV 完全无波动：无趋势信息
        return 0.0
    return slope / scale


def _linreg_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    """最小二乘斜率；xs 等差时等效 OLS。退化（n<2 或零方差）返回 0。"""
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    return cov / var_x if var_x > 0 else 0.0


def swing_lows(lows: Sequence[float]) -> list[int]:
    """局部极值 swing low 索引（设计 §6.2.2）：

    low[i] < low[i-1] 且 low[i] < low[i-2] 且
    low[i] <= low[i+1] 且 low[i] <= low[i+2]。
    """
    result: list[int] = []
    for i in range(2, len(lows) - 2):
        if (
            lows[i] < lows[i - 1]
            and lows[i] < lows[i - 2]
            and lows[i] <= lows[i + 1]
            and lows[i] <= lows[i + 2]
        ):
            result.append(i)
    return result


def swing_highs(highs: Sequence[float]) -> list[int]:
    """局部极值 swing high 索引（对称规则）。"""
    result: list[int] = []
    for i in range(2, len(highs) - 2):
        if (
            highs[i] > highs[i - 1]
            and highs[i] > highs[i - 2]
            and highs[i] >= highs[i + 1]
            and highs[i] >= highs[i + 2]
        ):
            result.append(i)
    return result


def swing_low_trend(
    lows: Sequence[float], *, max_swings: int = 4, baseline: float | None = None
) -> float | None:
    """最近 max_swings 个 swing low 的线性回归斜率（价格归一）。

    a > 0 代表低点抬升（设计 §6.2.2 Higher Low）。归一化保证
    3 元股与 300 元股的斜率可比。swing 不足 2 个返回 None。
    """
    if len(lows) < 5:
        return None
    reference = baseline if baseline is not None and baseline > 0 else lows[-1]
    indices = swing_lows(lows)[-max_swings:]
    if len(indices) < 2:
        return None
    slope = _linreg_slope([float(i) for i in indices], [lows[i] for i in indices])
    return slope / reference


def atr_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    window: int = 20,
) -> list[float] | None:
    """滚动 ATR(window) 序列（TR 的 SMA），长度 = len(closes)-window。

    TR = max(high-low, |high-prev_close|, |low-prev_close|)。
    """
    n = len(closes)
    if n < window + 1 or not (len(highs) == len(lows) == n):
        return None
    trs: list[float] = []
    for i in range(1, n):
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    series = [
        statistics.fmean(trs[i - window : i]) for i in range(window, len(trs) + 1)
    ]
    return series or None


def atr_contraction(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    window: int = 20,
    baseline_lookback: int = 60,
) -> float | None:
    """ATR 收敛度（设计 §6.2.1）：

    contraction = 1 - atr_pct_now / median(atr_pct over previous baseline)
    越高代表波动越收敛。基线不足或中位数为 0 返回 None。
    """
    series = atr_series(highs, lows, closes, window=window)
    if series is None or len(series) < 2:
        return None
    closes_aligned = closes[-len(series):]
    atr_pct = [value / close for value, close in zip(series, closes_aligned) if close > 0]
    if len(atr_pct) < 2:
        return None
    now = atr_pct[-1]
    baseline_start = max(0, len(atr_pct) - 1 - baseline_lookback)
    baseline = atr_pct[baseline_start:-1]
    if not baseline:
        return None
    median = statistics.median(baseline)
    if median <= 0:
        return None
    return 1.0 - now / median


def daily_range_contraction(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], *, window: int = 10
) -> float | None:
    """日K振幅收缩：近 window 日均振幅 / 前 window 日均振幅 的补（越高越收缩）。"""
    n = len(closes)
    if n < 2 * window or not (len(highs) == len(lows) == n):
        return None
    def mean_range(start: int, end: int) -> float | None:
        values = [
            (highs[i] - lows[i]) / closes[i] for i in range(start, end) if closes[i] > 0
        ]
        return statistics.fmean(values) if values else None
    recent = mean_range(n - window, n)
    previous = mean_range(n - 2 * window, n - window)
    if recent is None or previous is None or previous <= 0:
        return None
    return 1.0 - recent / previous


def down_volume_ratio(
    closes: Sequence[float], volumes: Sequence[float], *, window: int = 20
) -> float | None:
    """下跌缩量比（设计 §6.2.3）：下跌日均量 / 全体日均量（越低越缩量）。"""
    n = min(len(closes), len(volumes))
    if n < window + 1:
        return None
    sample_c = closes[-window:]
    sample_v = volumes[-window:]
    prev_c = closes[-window - 1 : -1]
    down = [v for c, p, v in zip(sample_c, prev_c, sample_v) if c < p]
    total = statistics.fmean(sample_v)
    if total <= 0:
        return None
    if not down:
        return 0.0
    return statistics.fmean(down) / total


UP_DOWN_RATIO_CAP = 50.0


def up_down_volume_ratio(
    closes: Sequence[float], volumes: Sequence[float], *, window: int = 20
) -> float | None:
    """涨跌量比（设计 §8.2）：U/D = 上涨日总量 / max(下跌日总量, eps)。

    窗口内无下跌日（D=0）时截断为 UP_DOWN_RATIO_CAP——语义是
    "显著吸收"，不返回 inf（不可序列化、也不可参与数值比较）。
    全无波动（U=0 且 D=0）返回 None。
    """
    n = min(len(closes), len(volumes))
    if n < window + 1:
        return None
    sample_c = closes[-window:]
    sample_v = volumes[-window:]
    prev_c = closes[-window - 1 : -1]
    up = sum(v for c, p, v in zip(sample_c, prev_c, sample_v) if c > p)
    down = sum(v for c, p, v in zip(sample_c, prev_c, sample_v) if c < p)
    if down > 0:
        return min(up / down, UP_DOWN_RATIO_CAP)
    return UP_DOWN_RATIO_CAP if up > 0 else None


def consecutive_up_days(closes: Sequence[float]) -> int:
    """尾部连续上涨天数（close>prev_close），用于 NonChase。"""
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            count += 1
        else:
            break
    return count


def volume_spike(
    volumes: Sequence[float], *, recent: int = 5, baseline: int = 20
) -> float | None:
    """近 recent 日最大量 / 基线均量；爆量检测（Penalty/NonChase）。"""
    if len(volumes) < recent + baseline:
        return None
    base = statistics.fmean(volumes[-recent - baseline : -recent])
    if base <= 0:
        return None
    return max(volumes[-recent:]) / base


def low_slope(
    lows: Sequence[float], *, window: int = 20
) -> float | None:
    """近 window 日低点回归斜率（价格归一）：低点不再下移的判据。"""
    if len(lows) < window:
        return None
    sample = lows[-window:]
    slope = _linreg_slope([float(i) for i in range(len(sample))], list(sample))
    reference = sample[-1]
    return slope / reference if reference > 0 else None


def ma_series(closes: Sequence[float], window: int) -> list[float | None]:
    """滚动 MA 序列（前 window-1 个为 None）。"""
    result: list[float | None] = [None] * len(closes)
    running = 0.0
    for i, value in enumerate(closes):
        running += value
        if i >= window:
            running -= closes[i - window]
        if i >= window - 1:
            result[i] = running / window
    return result


def ma_slope_delta(
    closes: Sequence[float], window: int = 20, *, gap: int = 5
) -> float | None:
    """MA 斜率变化（设计 §7.2）：slope_now - slope_gap 日前。

    奖励"由负转零、由更负变得不那么负"。MA 序列不足返回 None。
    """
    if len(closes) < window + gap:
        return None
    series = ma_series(closes, window)
    tail = series[-(gap + 1) :]
    if any(value is None for value in tail):
        return None
    def point_slope(seq: list[float | None]) -> float | None:
        prev = seq[-2]
        cur = seq[-1]
        if prev in (None, 0) or cur is None:
            return None
        return cur / prev - 1
    now = point_slope(tail)
    older_series = series[-(2 * gap + 1) : -gap + 1] if len(series) >= 2 * gap + 1 else None
    if older_series is None or any(value is None for value in older_series):
        older = None
    else:
        older = point_slope(list(older_series))
    if now is None or older is None:
        return None
    return now - older


def rs_metrics(
    stock_closes: Sequence[float],
    benchmark_closes: Sequence[float],
) -> dict[str, float | None]:
    """相对强度序列指标（设计 §10）。

    rs_t = stock_t / benchmark_t（按尾部对齐 min(len)）：
    rs_5d_slope / rs_20d_slope（价格归一回归斜率）、
    rs_slope_delta（5d 斜率 - 前 5d 斜率）、rs_low_higher（swing low 斜率>0 的 0/1）。
    benchmark 缺失时全部 None（missing ≠ 0 分）。
    """
    if not stock_closes or not benchmark_closes:
        return {
            "rs_5d_slope": None,
            "rs_20d_slope": None,
            "rs_slope_delta": None,
            "rs_low_higher": None,
        }
    n = min(len(stock_closes), len(benchmark_closes))
    rs = [s / b for s, b in zip(stock_closes[-n:], benchmark_closes[-n:]) if b > 0]
    result: dict[str, float | None] = {
        "rs_5d_slope": None,
        "rs_20d_slope": None,
        "rs_slope_delta": None,
        "rs_low_higher": None,
    }
    if len(rs) < 25:
        return result

    def window_slope(size: int, offset: int = 0) -> float | None:
        end = len(rs) - offset
        sample = rs[end - size : end]
        if len(sample) < size or sample[-1] <= 0:
            return None
        slope = _linreg_slope([float(i) for i in range(size)], sample)
        return slope / sample[-1]

    result["rs_5d_slope"] = window_slope(5)
    result["rs_20d_slope"] = window_slope(20)
    recent = window_slope(5)
    previous = window_slope(5, offset=5)
    result["rs_slope_delta"] = (
        recent - previous if recent is not None and previous is not None else None
    )
    low_trend = swing_low_trend(rs[-40:], baseline=rs[-1])
    result["rs_low_higher"] = (
        1.0 if low_trend is not None and low_trend > 0 else 0.0
    ) if low_trend is not None else None
    return result


def weekly_decline_metrics(
    weekly_closes: Sequence[float], *, recent: int = 8
) -> tuple[float | None, float | None, float | None]:
    """周K跌速指标（设计 §6.2.4）：(slope_recent_8w, slope_prev_8w, deceleration)。

    slope 为价格归一回归斜率；deceleration = recent - prev，
    即使 slope 仍 <0，deceleration 明显 >0 也属改善。
    不足 2*recent+1 周返回 (None, None, None)。
    """
    if len(weekly_closes) < 2 * recent + 1:
        return None, None, None
    def slope_of(sample: Sequence[float]) -> float | None:
        if sample[-1] <= 0:
            return None
        return _linreg_slope([float(i) for i in range(len(sample))], list(sample)) / sample[-1]
    recent_slope = slope_of(weekly_closes[-recent:])
    prev_slope = slope_of(weekly_closes[-2 * recent : -recent])
    if recent_slope is None or prev_slope is None:
        return None, None, None
    return recent_slope, prev_slope, recent_slope - prev_slope


def base_duration(
    closes: Sequence[float], ma_window: int = 20, *, tolerance: float = 0.002
) -> int | None:
    """底部横盘持续天数（设计 §6.1 base_duration）：

    从尾部往前数 |MA 斜率| <= tolerance 的天数。数据不足返回 None。
    """
    if len(closes) < ma_window + 2:
        return None
    series = ma_series(closes, ma_window)
    count = 0
    for i in range(len(series) - 1, 0, -1):
        prev, cur = series[i - 1], series[i]
        if prev in (None, 0) or cur is None:
            break
        if abs(cur / prev - 1) <= tolerance:
            count += 1
        else:
            break
    return count
