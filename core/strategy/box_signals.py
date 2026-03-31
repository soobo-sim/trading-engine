"""
박스 역추세 전략 — 순수 판정 함수.

실전 BoxMeanReversionManager와 백테스트 엔진이 공유하는 로직.
여기에 정의된 함수가 유일한 정본이다.

  - classify_price_in_box: 가격의 박스 내 위치 분류
  - check_box_invalidation: 4H 종가 이탈 + 수렴 삼각형 무효화
  - linear_slope: 간이 선형 회귀 기울기
"""
from __future__ import annotations

from typing import List, Optional


def classify_price_in_box(
    price: float,
    upper: float,
    lower: float,
    near_bound_pct: float,
) -> str:
    """
    현재 가격이 박스 어느 구간에 있는지 분류.

    Args:
        price: 현재가
        upper: 박스 상단
        lower: 박스 하단
        near_bound_pct: 경계 밴드 (%, e.g. 0.3 → 0.3%)

    Returns:
        "near_lower" | "near_upper" | "middle" | "outside"
    """
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
    """
    박스 유효성 검사 — 4H 종가 이탈 또는 수렴 삼각형 감지 시 무효화 사유 반환.

    Args:
        close: 최신 캔들 종가
        candle_highs: 최근 N개 캔들 고가 리스트
        candle_lows: 최근 N개 캔들 저가 리스트
        upper: 박스 상단
        lower: 박스 하단
        tolerance_pct: 허용 오차 (%, e.g. 0.3)
        triangle_lookback: 삼각형 감지 윈도우
        triangle_min_candles: 삼각형 감지 최소 캔들 수

    Returns:
        무효화 사유 문자열 또는 None(유효)
    """
    tol = tolerance_pct / 100

    # D-3: 종가 이탈
    if close < lower * (1 - tol):
        return "4h_close_below_lower"
    if close > upper * (1 + tol):
        return "4h_close_above_upper"

    # D-4: 수렴 삼각형
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
    """간이 선형 회귀 기울기."""
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
