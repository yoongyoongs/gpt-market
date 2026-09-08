"""Soft scoring 通用函数（任务书 §5 / 设计 §4.3）。

设计要求：机会型指标一律以连续软分数参与评分，不允许布尔硬过滤。
所有函数输出 0~100，分段过渡使用 Smoothstep（3t²-2t³）保证 C1 连续——
边界两侧 epsilon 级输入差只产生 epsilon 级输出差，不允许断崖。

soft_band(x, a, b, c, d)：
    x <= a        -> 0
    a < x < b     -> 0 -> 100 平滑上升
    b <= x <= c   -> 100
    c < x < d     -> 100 -> 0 平滑下降
    x >= d        -> 0

soft_low_better(x, a, b)：x<=a 全分，a..b 平滑衰减到 0（越低越好）。
soft_high_better(x, a, b)：x>=b 全分，a..b 平滑上升到 100（越高越好）。
"""

from __future__ import annotations

import math


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def smoothstep(t: float) -> float:
    """S 曲线 s(t) = 3t² - 2t³，t 先截断到 [0,1]。"""
    u = _clamp01(t)
    return u * u * (3.0 - 2.0 * u)


def _ramp_up(x: float, a: float, b: float) -> float:
    """a..b 区间 0 -> 100 平滑上升；a==b 时退化为阶跃（允许配置退化）。"""
    if b <= a:
        return 100.0 if x >= b else 0.0
    return 100.0 * smoothstep((x - a) / (b - a))


def soft_band(x: float, a: float, b: float, c: float, d: float) -> float:
    """梯形软带宽：全分区 [b,c]，两侧 Smoothstep 过渡到 0。"""
    if not (a <= b <= c <= d):
        raise ValueError(f"soft_band 要求 a<=b<=c<=d，收到 {a},{b},{c},{d}")
    if x <= a or x >= d:
        return 0.0
    if b <= x <= c:
        return 100.0
    if x < b:
        return _ramp_up(x, a, b)
    return 100.0 - _ramp_up(x, c, d)


def soft_low_better(x: float, a: float, b: float, *, floor: float = 0.0) -> float:
    """越低越好：x<=a 得 100；a..b Smoothstep 从 100 衰减到 floor。"""
    if b <= a:
        raise ValueError(f"soft_low_better 要求 a<b，收到 {a},{b}")
    if x <= a:
        return 100.0
    if x >= b:
        return floor
    return 100.0 + (floor - 100.0) * smoothstep((x - a) / (b - a))


def soft_high_better(x: float, a: float, b: float, *, floor: float = 0.0) -> float:
    """越高越好：x>=b 得 100；a..b Smoothstep 从 floor 上升到 100。"""
    if b <= a:
        raise ValueError(f"soft_high_better 要求 a<b，收到 {a},{b}")
    if x >= b:
        return 100.0
    if x <= a:
        return floor
    return floor + (100.0 - floor) * smoothstep((x - a) / (b - a))


def soft_target(
    x: float,
    low: float,
    center_low: float,
    center_high: float,
    high: float,
) -> float:
    """目标区间分数：soft_band 的语义别名（任务书 §5 soft_target）。"""
    return soft_band(x, low, center_low, center_high, high)


def position_52w_score(position: float) -> float:
    """52 周位置分（设计 §5.2.4 特殊规则）。

    0~5% 只给 70 分（贴近新低可能只是持续下跌，不天然满分），
    10~30% 为 100 分高台，>55% 逐步降低。全程连续无断崖。
    分段实现用 soft_band 三段叠加：
      - 0~10%：70 -> 100 上升（5% 前 70 分基线 + 平滑过渡）
      - 10~30%：100
      - 30%~90%：100 -> 0 平滑下降
    """
    if position < 0 or position > 1:
        raise ValueError(f"position 须在 [0,1]，收到 {position}")
    # 0~5% 平台 70 分；5~10% 上升到 100（等效：min(70+smoothstep*30, 100)）
    below = 70.0 + 30.0 * smoothstep((position - 0.05) / 0.05)
    # 30%~90% 从 100 平滑降到 0
    above = 100.0 * (1.0 - smoothstep((position - 0.30) / 0.60))
    return round(min(below, above), 4)


def volume_5_20_score(ratio: float) -> float:
    """温和放量分（设计 §8.4）：高分区 1.05~1.60，>2.5 归 0。

    追涨方向交给 Penalty Engine，这里只负责不加分。
    """
    return soft_band(ratio, 0.85, 1.05, 1.60, 2.50)


def is_finite(value: float | None) -> bool:
    """missing（None）与非法值（NaN/inf）统一判定为不可用。"""
    return value is not None and math.isfinite(value)


def weighted_combine(
    parts: list[tuple[str, float, float | None]],
    *,
    extra_confidence: float = 1.0,
) -> tuple[float | None, float, tuple[str, ...], dict[str, float | None]]:
    """通用加权合成（设计 §11.3 missing 语义，第二轮 P0-05 修正）。

    parts = [(维度名, 权重, 子分或 None)]；missing ≠ negative：

    - value 按**有效权重归一**：value = Σ(weight_i*score_i)/effective*100，
      缺失维度不再既降 confidence 又拖低 value（双重惩罚）；
    - confidence = effective/total * extra_confidence，覆盖度单独体现；
    - effective == 0（全 missing）→ value=None、confidence=0
      （不得伪装成真实 0 分；domain 类型冻结 value 非 None 的调用方，
      经 confidence==0 判 missing——任务书 §7.5 允许路径）。

    返回 (value 0~100 或 None, confidence, reasons, features_used)。
    专家与 SoftOpportunity 等多维度合成的公共实现。
    """
    raw = 0.0
    effective = 0.0
    reasons: list[str] = []
    features_used: dict[str, float | None] = {}
    for label, weight, subscore in parts:
        if subscore is None:
            features_used[label] = None
            reasons.append(f"{label}:missing")
            continue
        raw += weight * max(0.0, min(1.0, subscore))
        effective += weight
        features_used[label] = round(subscore, 6)
        reasons.append(f"{label}:{subscore:.3f}*{weight:g}")
    total = sum(weight for _, weight, _ in parts)
    if effective <= 0.0 or total <= 0.0:
        return (None, 0.0, tuple(reasons), features_used)
    confidence = (effective / total) * extra_confidence
    return (
        round(min(raw / effective * 100.0, 100.0), 4),
        round(max(0.0, min(confidence, 1.0)), 4),
        tuple(reasons),
        features_used,
    )
