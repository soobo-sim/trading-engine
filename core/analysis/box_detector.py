"""
core/analysis/box_detector.py

박스권 감지 독립 모듈.
BoxMeanReversionManager._find_cluster를 분리하고,
캔들 리스트 → 박스 감지 결과를 반환하는 detect_box() 제공.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ──────────────────────────────────────────
# Low-level cluster finder (구 _find_cluster)
# ──────────────────────────────────────────

def find_cluster(
    prices: list[float],
    tolerance_pct: float,
    min_touches: int,
    mode: str,
) -> tuple[Optional[float], int]:
    """
    tolerance_pct 이내 가격들을 클러스터로 묶어 최다 빈도 클러스터 반환.
    mode="high" → 높은 쪽 우선, mode="low" → 낮은 쪽 우선.
    """
    if not prices:
        return None, 0

    tol = tolerance_pct / 100
    sorted_prices = sorted(prices, reverse=(mode == "high"))
    clusters: list[list[float]] = []

    for p in sorted_prices:
        placed = False
        for cluster in clusters:
            center = sum(cluster) / len(cluster)
            if abs(p - center) / center <= tol:
                cluster.append(p)
                placed = True
                break
        if not placed:
            clusters.append([p])

    best_cluster = max(clusters, key=len)
    if len(best_cluster) < min_touches:
        return None, 0

    return sum(best_cluster) / len(best_cluster), len(best_cluster)


# ──────────────────────────────────────────
# Result dataclass
# ──────────────────────────────────────────

@dataclass
class BoxDetectResult:
    box_detected: bool
    upper_bound: Optional[float] = None
    lower_bound: Optional[float] = None
    upper_touch_count: int = 0
    lower_touch_count: int = 0
    width_pct: Optional[float] = None
    reason: Optional[str] = None  # box_detected=False 시 이유


# ──────────────────────────────────────────
# Public API
# ──────────────────────────────────────────

def find_cluster_percentile(
    prices: list[float],
    tolerance_pct: float,
    min_touches: int,
    mode: str,
    percentile: float = 100.0,
) -> tuple[Optional[float], int]:
    """
    Percentile 필터링 후 find_cluster 실행.

    mode="high": 상위 percentile% (고가 극단) 추출 → 상단 클러스터
    mode="low":  하위 percentile% (저가 극단) 추출 → 하단 클러스터
    percentile=100 → 전체 사용 = find_cluster와 완전 동일 (v1 호환)

    Args:
        prices: OHLC 기반 고가 목록(mode=high) 또는 저가 목록(mode=low)
        tolerance_pct: 클러스터 허용 오차 (%)
        min_touches: 최소 터치 횟수
        mode: "high" | "low"
        percentile: 사용할 극단 비율 (0 < percentile ≤ 100)

    Returns:
        (cluster_center, touch_count)
    """
    if percentile >= 100:
        return find_cluster(prices, tolerance_pct, min_touches, mode)

    if not prices:
        return None, 0

    pct = max(0.0, min(100.0, percentile))
    n = max(1, int(len(prices) * pct / 100))

    if mode == "high":
        filtered = sorted(prices, reverse=True)[:n]
    else:
        filtered = sorted(prices)[:n]

    return find_cluster(filtered, tolerance_pct, min_touches, mode)


def detect_box(
    highs: list[float],
    lows: list[float],
    tolerance_pct: float = 0.5,
    min_touches: int = 3,
    cluster_percentile: float = 100.0,
) -> BoxDetectResult:
    """
    캔들 high/low 리스트로 박스권 감지.

    Args:
        highs: 각 캔들의 고가 리스트
        lows:  각 캔들의 저가 리스트
        tolerance_pct: 클러스터 허용 오차 (%)
        min_touches: 최소 터치 횟수
        cluster_percentile: 극단 percentile 필터 (100=기존 동작, v1 호환)

    Returns:
        BoxDetectResult
    """
    if not highs or not lows:
        return BoxDetectResult(box_detected=False, reason="캔들 없음")

    if len(highs) < min_touches * 2:
        return BoxDetectResult(
            box_detected=False,
            reason=f"캔들 부족: {len(highs)}개 (최소 {min_touches * 2}개 필요)",
        )

    upper, upper_count = find_cluster_percentile(
        highs, tolerance_pct, min_touches, mode="high", percentile=cluster_percentile
    )
    lower, lower_count = find_cluster_percentile(
        lows, tolerance_pct, min_touches, mode="low", percentile=cluster_percentile
    )

    if upper is None or lower is None:
        return BoxDetectResult(
            box_detected=False,
            reason=f"클러스터 미형성 (upper={'있음' if upper else '없음'}, lower={'있음' if lower else '없음'})",
        )

    if upper <= lower:
        return BoxDetectResult(box_detected=False, reason="상단 ≤ 하단 (박스 무효)")

    width_pct = (upper - lower) / lower * 100

    return BoxDetectResult(
        box_detected=True,
        upper_bound=upper,
        lower_bound=lower,
        upper_touch_count=upper_count,
        lower_touch_count=lower_count,
        width_pct=round(width_pct, 4),
    )


# ──────────────────────────────────────────
# Formation progress (partial detection)
# ──────────────────────────────────────────

@dataclass
class BoxFormationProgress:
    """박스 미형성 시 진행 상황."""
    upper_touches: int
    lower_touches: int
    min_touches: int
    upper_center: Optional[float] = None
    lower_center: Optional[float] = None
    candles_remaining: int = 0  # 최소 필요 캔들 수 추정


def detect_box_progress(
    highs: list[float],
    lows: list[float],
    tolerance_pct: float = 0.5,
    min_touches: int = 3,
) -> BoxFormationProgress:
    """
    박스 형성 진행 상황 계산. min_touches=1로 클러스터를 찾아
    현재 터치 카운트를 반환한다.
    """
    if not highs or not lows:
        return BoxFormationProgress(
            upper_touches=0, lower_touches=0, min_touches=min_touches,
        )

    # min_touches=1로 raw 클러스터 추출
    upper_center, upper_count = find_cluster(highs, tolerance_pct, 1, mode="high")
    lower_center, lower_count = find_cluster(lows, tolerance_pct, 1, mode="low")

    upper_count = upper_count if upper_center else 0
    lower_count = lower_count if lower_center else 0

    # 남은 캔들 추정: 양쪽 모두 min_touches 이상이어야 형성
    upper_needed = max(min_touches - upper_count, 0)
    lower_needed = max(min_touches - lower_count, 0)
    candles_remaining = max(upper_needed, lower_needed)

    return BoxFormationProgress(
        upper_touches=upper_count,
        lower_touches=lower_count,
        min_touches=min_touches,
        upper_center=round(upper_center, 6) if upper_center else None,
        lower_center=round(lower_center, 6) if lower_center else None,
        candles_remaining=candles_remaining,
    )
