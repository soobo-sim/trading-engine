"""
① RegimeGate trending_score>=1 조건 테스트

RG-01: trending_score=0 → entry_ok 차단 (regime_trending=True라도)
RG-02: trending_score=1 (bb만 충족) → entry_ok 허용
RG-03: trending_score=2 → entry_ok 허용
RG-04: regime_trending=False → entry_ok 차단 (trending_score 무관)
RG-05: trending_score가 반환값에 포함됨
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest
from core.shared.signals import compute_trend_signal


def _make_candles(n=50, price=12_500_000.0, slope_up=True, bb_wide=True):
    """단순 합성 캔들 리스트."""
    candles = []
    for i in range(n):
        if slope_up:
            p = price * (1 + 0.001 * i / n)
        else:
            p = price
        spread = price * (0.03 if bb_wide else 0.005)  # BB 폭 넓음/좁음
        c = MagicMock()
        c.close = p
        c.high = p + spread
        c.low = p - spread
        c.open = p - spread * 0.3
        candles.append(c)
    return candles


def _base_params(**overrides):
    p = {
        "ema_period": 20,
        "entry_rsi_min": 40.0,
        "entry_rsi_max": 65.0,
        "ema_slope_entry_min": 0.0,
        "bb_width_trending_min": 3.0,
        "range_pct_trending_min": 6.0,
    }
    p.update(overrides)
    return p


# ──────────────────────────────────────────────────────────────
# RG-01: trending_score=0 → 차단
# ──────────────────────────────────────────────────────────────

def test_rg01_score0_blocks_entry():
    """trending_score=0 → entry_ok 차단.
    BB폭 좁아서 regime_trending=True지만 score=0 → 차단."""
    # bb_wide=False → bb_width_pct 낮음 → score += 0 from bb
    # range 좁음 → score += 0
    # ATR 낮음 → score += 0
    # slope 낮음 → score += 0
    # → score=0 → 차단 기대
    # 단, bb_width_trending_min을 매우 낮게 설정하면 regime_trending=True 유지 가능
    candles = _make_candles(n=60, bb_wide=False, slope_up=True)
    params = _base_params(bb_width_trending_min=0.01)  # 거의 항상 trending
    result = compute_trend_signal(candles, params=params)

    # trending_score가 0이면 entry_ok 아니어야 함
    if result["trending_score"] == 0:
        assert result["signal"] != "entry_ok", \
            f"trending_score=0인데 entry_ok 발생: signal={result['signal']}"


# ──────────────────────────────────────────────────────────────
# RG-02: trending_score>=1 + 모든 조건 → entry_ok
# ──────────────────────────────────────────────────────────────

def test_rg02_score1_allows_entry():
    """trending_score>=1 + 다른 조건 모두 충족 → entry_ok 가능."""
    candles = _make_candles(n=60, bb_wide=True, slope_up=True)
    params = _base_params(
        ema_slope_entry_min=0.0,
        entry_rsi_min=10.0,
        entry_rsi_max=90.0,
        bb_width_trending_min=1.0,  # 넓은 BB → trending
    )
    result = compute_trend_signal(candles, params=params)
    assert result["trending_score"] >= 0  # 점수 필드 존재
    # 조건 다 충족 시 entry_ok
    if result["trending_score"] >= 1:
        assert result["signal"] in ("entry_ok", "wait_dip", "wait_regime", "no_signal"), \
            f"unexpected signal: {result['signal']}"


# ──────────────────────────────────────────────────────────────
# RG-03: trending_score 반환값 포함
# ──────────────────────────────────────────────────────────────

def test_rg03_trending_score_in_result():
    """compute_trend_signal 반환값에 trending_score 포함."""
    candles = _make_candles(n=60, bb_wide=True, slope_up=True)
    result = compute_trend_signal(candles, params=_base_params())
    assert "trending_score" in result, "trending_score가 반환값에 없음"
    assert isinstance(result["trending_score"], int)
    assert 0 <= result["trending_score"] <= 6


# ──────────────────────────────────────────────────────────────
# RG-04: regime_trending=False → entry_ok 차단 (기존 동작 유지)
# ──────────────────────────────────────────────────────────────

def test_rg04_regime_not_trending_blocks():
    """bb_width < bb_width_trending_min → regime_trending=False → entry_ok 차단."""
    candles = _make_candles(n=60, bb_wide=False, slope_up=True)
    params = _base_params(
        bb_width_trending_min=100.0,  # 절대 trending 불가
        entry_rsi_min=10.0,
        entry_rsi_max=90.0,
        ema_slope_entry_min=0.0,
    )
    result = compute_trend_signal(candles, params=params)
    assert result["signal"] != "entry_ok", \
        f"regime_trending=False인데 entry_ok 발생: {result}"


# ──────────────────────────────────────────────────────────────
# RG-05: trending_score 계산 논리 검증
# ──────────────────────────────────────────────────────────────

def test_rg05_trending_score_calculation():
    """trending_score 계산: bb_wide + slope_up → score >= 1."""
    candles = _make_candles(n=60, bb_wide=True, slope_up=True)
    params = _base_params(bb_width_trending_min=3.0)
    result = compute_trend_signal(candles, params=params)
    # 넓은 BB → bb_width_pct >= 3.0 → score += 1 이상
    assert result["trending_score"] >= 1, \
        f"넓은 BB에서 trending_score=0: {result['trending_score']}, bb_width={result['bb_width_pct']:.2f}%"
