"""
box_detector 유닛 테스트 (사만다 설계 BD-1~BD-6).
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.analysis.box_detector import detect_box, find_cluster
from core.strategy.box_mean_reversion import BoxMeanReversionManager


# ── BD-1: 명확한 박스권 캔들 → 박스 감지 ─────────────────────

def test_bd1_clear_box_detected():
    """명확한 고점/저점 클러스터 → box_detected=True."""
    # 10개 캔들: 고점 ~11200000, 저점 ~10800000 반복
    highs = [11200000, 11210000, 11190000, 11205000, 11195000,
             11202000, 11198000, 11207000, 11193000, 11200000]
    lows  = [10800000, 10810000, 10795000, 10805000, 10798000,
             10802000, 10796000, 10808000, 10797000, 10800000]
    result = detect_box(highs, lows, tolerance_pct=0.5, min_touches=3)
    assert result.box_detected is True
    assert result.upper_bound is not None
    assert result.lower_bound is not None
    assert result.upper_bound > result.lower_bound


# ── BD-2: 강한 추세 캔들 → 박스 미감지 ─────────────────────

def test_bd2_strong_trend_no_box():
    """단조 상승 추세 → 클러스터 미형성 → box_detected=False."""
    base = 10_000_000
    # 고점과 저점이 계속 상승 (클러스터 없음)
    highs = [base + i * 100_000 for i in range(10)]
    lows  = [base - 200_000 + i * 100_000 for i in range(10)]
    result = detect_box(highs, lows, tolerance_pct=0.3, min_touches=3)
    assert result.box_detected is False


# ── BD-3: 캔들 0건 → 빈 입력 처리 ─────────────────────────────

def test_bd3_no_candles_detect_box_returns_false():
    """캔들 0건 → detect_box가 box_detected=False 반환."""
    result = detect_box([], [], tolerance_pct=0.5, min_touches=3)
    assert result.box_detected is False
    assert result.reason is not None


# ── BD-4: tolerance_pct=0 → 박스 미형성 ─────────────────────

def test_bd4_zero_tolerance_no_box():
    """tolerance=0이면 완전 동일 가격만 클러스터 → 현실 데이터에서 박스 미형성."""
    highs = [11200000, 11210000, 11190000, 11205000, 11195000,
             11202000, 11198000, 11207000, 11193000, 11200000]
    lows  = [10800000, 10810000, 10795000, 10805000, 10798000,
             10802000, 10796000, 10808000, 10797000, 10800000]
    result = detect_box(highs, lows, tolerance_pct=0.0, min_touches=3)
    # tolerance=0이면 정확히 동일한 가격만 클러스터 → min_touches=3 미충족
    assert result.box_detected is False


# ── BD-5: 회귀 테스트 — find_cluster == _find_cluster ────────

def test_bd5_regression_find_cluster():
    """find_cluster와 BoxMeanReversionManager._find_cluster가 동일 결과."""
    test_cases = [
        ([100.0, 101.0, 99.5, 100.2, 98.0, 97.5, 98.2], 1.5, 3, "high"),
        ([100.0, 101.0, 99.5, 100.2, 98.0, 97.5, 98.2], 1.5, 3, "low"),
        ([], 0.5, 3, "high"),
        ([105.0], 0.5, 3, "high"),
        ([100.0, 100.1, 100.2, 50.0, 50.1, 50.2], 0.5, 3, "high"),
    ]
    for prices, tol, mt, mode in test_cases:
        r1 = find_cluster(prices, tol, mt, mode)
        r2 = BoxMeanReversionManager._find_cluster(prices, tol, mt, mode)
        assert r1 == r2, f"회귀 불일치: inputs=({prices},{tol},{mt},{mode}) → {r1} != {r2}"


# ── BD-6: detect_box edge cases ───────────────────────────────

def test_bd6_detect_box_upper_lte_lower():
    """상단 ≤ 하단이면 box_detected=False (역전 방어)."""
    # 저점이 고점보다 높은 비정상 데이터
    highs = [10800000] * 6
    lows  = [11200000] * 6
    result = detect_box(highs, lows, tolerance_pct=0.1, min_touches=3)
    assert result.box_detected is False


def test_bd6b_detect_box_insufficient_candles():
    """캔들 부족(min_touches*2 미만) → box_detected=False + reason."""
    highs = [11200000, 11210000]
    lows  = [10800000, 10810000]
    result = detect_box(highs, lows, tolerance_pct=0.5, min_touches=3)
    assert result.box_detected is False
    assert result.reason is not None
    assert "캔들 부족" in result.reason or "부족" in result.reason


def test_bd6c_detect_box_empty_input():
    """빈 입력 → box_detected=False."""
    result = detect_box([], [], tolerance_pct=0.5, min_touches=3)
    assert result.box_detected is False
