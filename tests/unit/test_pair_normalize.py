"""
pair 정규화 테스트 (GD-10 ~ GD-15).
GMO Coin 전용 — pair_column="pair", 소문자 정규화.
"""
import pytest
from unittest.mock import MagicMock


def _make_state(pair_column: str):
    from api.dependencies import AppState
    state = MagicMock(spec=AppState)
    state.pair_column = pair_column
    return state


# ── GD-10: USD_JPY (대문자) → GMO → 소문자 ─────────────────

def test_gd10_usd_jpy_upper_to_lower():
    from api.dependencies import AppState
    state = _make_state("pair")  # GMO Coin
    assert AppState.normalize_pair(state, "USD_JPY") == "usd_jpy"


# ── GD-11: usd_jpy (소문자) → GMO → 소문자 유지 ───────────

def test_gd11_usd_jpy_lower_stays_lower():
    from api.dependencies import AppState
    state = _make_state("pair")
    assert AppState.normalize_pair(state, "usd_jpy") == "usd_jpy"


# ── GD-12: BTC_JPY (대문자) → GMO Coin → 소문자 변환 ───────

def test_gd12_btc_jpy_upper_to_lower():
    from api.dependencies import AppState
    state = _make_state("pair")  # GMO Coin
    assert AppState.normalize_pair(state, "BTC_JPY") == "btc_jpy"


# ── GD-13: btc_jpy (소문자) → GMO Coin → 소문자 유지 ───────

def test_gd13_btc_jpy_lower_stays_lower():
    from api.dependencies import AppState
    state = _make_state("pair")
    assert AppState.normalize_pair(state, "btc_jpy") == "btc_jpy"


# ── GD-14: GMO trend-signal USD_JPY → normalize 후 소문자 ──

def test_gd14_gmo_trend_signal_pair_normalized():
    from api.dependencies import AppState
    state = _make_state("pair")
    normalized = AppState.normalize_pair(state, "USD_JPY")
    assert normalized == "usd_jpy"


# ── GD-15: GMO BB API USD_JPY → normalize 후 소문자 ────────

def test_gd15_gmo_bb_pair_normalized():
    from api.dependencies import AppState
    state = _make_state("pair")
    normalized = AppState.normalize_pair(state, "USD_JPY")
    assert normalized == "usd_jpy"


# ── 추가: 혼합 케이스 ────────────────────────────────────────

@pytest.mark.parametrize("pair_col,inp,expected", [
    ("pair", "EUR_JPY", "eur_jpy"),
    ("pair", "eur_jpy", "eur_jpy"),
    ("pair", "GBP_USD", "gbp_usd"),
    ("pair", "BTC_JPY", "btc_jpy"),
    ("pair", "btc_jpy", "btc_jpy"),
])
def test_normalize_pair_parametrized(pair_col, inp, expected):
    from api.dependencies import AppState
    state = _make_state(pair_col)
    assert AppState.normalize_pair(state, inp) == expected
