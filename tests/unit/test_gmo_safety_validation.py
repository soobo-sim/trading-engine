"""
_validate_gmo_safety 단위 테스트.

커버 항목:
  - GMO FX(prefix=gmo): position_size_pct 초과 → HTTPException
  - GMO FX(prefix=gmo): leverage 초과 → HTTPException
  - GMO FX(prefix=gmo): regime 임계값 누락 → HTTPException
  - GMO FX(prefix=gmo): 유효한 파라미터 → 통과
  - GMO Coin(prefix=gmoc): position_size_pct 100% → 제한 없이 통과 (BTC 허용)
  - GMO Coin(prefix=gmoc): regime 임계값 없어도 → 통과 (BTC 기본값 사용)
  - BitFlyer(prefix=bf): 어떤 파라미터도 → 통과 (검증 없음)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from api.routes.strategies import _validate_gmo_safety


def _state(prefix: str) -> MagicMock:
    s = MagicMock()
    s.prefix = prefix
    return s


# ── GMO FX (prefix=gmo) 안전장치 적용 ─────────────────────────

def test_gmofx_position_size_over_limit():
    """GMO FX에서 position_size_pct > 50% → 400."""
    state = _state("gmo")
    with pytest.raises(HTTPException) as exc:
        _validate_gmo_safety({"position_size_pct": 100.0, "trading_style": "box_mean_reversion"}, state)
    assert exc.value.status_code == 400
    assert "position_size_pct" in exc.value.detail


def test_gmofx_position_size_exact_limit_passes():
    """GMO FX에서 position_size_pct == 50% → 통과."""
    state = _state("gmo")
    _validate_gmo_safety({"position_size_pct": 50.0, "trading_style": "box_mean_reversion"}, state)


def test_gmofx_leverage_over_limit():
    """GMO FX에서 leverage > 5 → 400."""
    state = _state("gmo")
    with pytest.raises(HTTPException) as exc:
        _validate_gmo_safety({"leverage": 10.0}, state)
    assert exc.value.status_code == 400
    assert "leverage" in exc.value.detail


def test_gmofx_regime_keys_missing_for_trend_following():
    """GMO FX trend_following에서 regime 임계값 누락 → 400."""
    state = _state("gmo")
    with pytest.raises(HTTPException) as exc:
        _validate_gmo_safety({"trading_style": "trend_following", "position_size_pct": 30.0}, state)
    assert exc.value.status_code == 400
    assert "regime" in exc.value.detail


def test_gmofx_with_all_regime_keys_passes():
    """GMO FX trend_following + regime 임계값 있으면 통과."""
    state = _state("gmo")
    _validate_gmo_safety(
        {
            "trading_style": "trend_following",
            "position_size_pct": 30.0,
            "bb_width_trending_min": 0.8,
            "range_pct_trending_min": 1.5,
            "bb_width_ranging_max": 0.35,
            "range_pct_ranging_max": 0.9,
        },
        state,
    )


# ── GMO Coin (prefix=gmoc) — 안전장치 없음 ────────────────────

def test_gmoc_position_size_100_passes():
    """GMO Coin BTC_JPY에서 position_size_pct=100% → 제한 없이 통과."""
    state = _state("gmoc")
    _validate_gmo_safety(
        {"position_size_pct": 100.0, "trading_style": "trend_following"},
        state,
    )


def test_gmoc_no_regime_keys_passes():
    """GMO Coin에서 regime 임계값 없어도 통과 (BTC 기본값 적용 대상)."""
    state = _state("gmoc")
    _validate_gmo_safety(
        {"trading_style": "trend_following", "position_size_pct": 100.0},
        state,
    )


def test_gmoc_leverage_over_5_passes():
    """GMO Coin은 leverage 제한도 없음 (암호화폐 레버리지는 별도 정책)."""
    state = _state("gmoc")
    _validate_gmo_safety({"leverage": 10.0}, state)


# ── BitFlyer (prefix=bf) — 안전장치 없음 ──────────────────────

def test_bf_ignores_all_checks():
    """BitFlyer는 모든 안전장치 검증이 스킵됨."""
    state = _state("bf")
    _validate_gmo_safety(
        {
            "position_size_pct": 200.0,
            "leverage": 100.0,
            "trading_style": "trend_following",
        },
        state,
    )
