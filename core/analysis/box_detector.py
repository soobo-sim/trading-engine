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

def detect_box(
    highs: list[float],
    lows: list[float],
    tolerance_pct: float = 0.5,
    min_touches: int = 3,
) -> BoxDetectResult:
    """
    캔들 high/low 리스트로 박스권 감지.

    Args:
        highs: 각 캔들의 고가 리스트
        lows:  각 캔들의 저가 리스트
        tolerance_pct: 클러스터 허용 오차 (%)
        min_touches: 최소 터치 횟수

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

    upper, upper_count = find_cluster(highs, tolerance_pct, min_touches, mode="high")
    lower, lower_count = find_cluster(lows, tolerance_pct, min_touches, mode="low")

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
