"""
박스 역추세 전략 — 순수 판정 함수.

실전 BoxMeanReversionManager와 백테스트 엔진이 공유하는 로직.
여기에 정의된 함수가 유일한 정본이다.

Canonical location: core/shared/box_signals.py
Backward-compat shim at: core/strategy/box_signals.py
"""
from __future__ import annotations

from typing import List, Optional


def classify_price_in_box(
    price: float,
    upper: float,
    lower: float,
    near_bound_pct: float,
) -> str:
    near_pct = near_bound_pct / 100

    if lower * (1 - near_pct) <= price <= lower * (1 + near_pct):
        return "near_lower"
    elif upper * (1 - near_pct) <= price <= upper * (1 + near_pct):
        return "near_upper"
    elif lower * (1 + near_pct) < price < upper * (1 - near_pct):
        return "middle"
    else:
        return "outside"


def check_box_invalidation(
    close: float,
    candle_highs: List[float],
    candle_lows: List[float],
    upper: float,
    lower: float,
    tolerance_pct: float,
    triangle_lookback: int = 20,
    triangle_min_candles: int = 8,
) -> Optional[str]:
    tol = tolerance_pct / 100

    if close < lower * (1 - tol):
        return "4h_close_below_lower"
    if close > upper * (1 + tol):
        return "4h_close_above_upper"

    lookback = min(len(candle_highs), len(candle_lows), triangle_lookback)
    if lookback >= triangle_min_candles:
        highs = candle_highs[-lookback:]
        lows = candle_lows[-lookback:]
        xs = list(range(lookback))
        high_slope = linear_slope(xs, highs)
        low_slope = linear_slope(xs, lows)
        if high_slope < -1e-6 and low_slope > 1e-6:
            return "converging_triangle"

    return None


def linear_slope(xs: List[int], ys: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_x2 = sum(x * x for x in xs)
    denom = n * sum_x2 - sum_x ** 2
    if denom == 0:
        return 0.0
    return (n * sum_xy - sum_x * sum_y) / denom
