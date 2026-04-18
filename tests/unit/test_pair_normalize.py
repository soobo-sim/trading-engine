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


# ── core.pair.normalize_pair 직접 테스트 (NP-01 ~ NP-05) ────

class TestCorePairNormalize:
    """core.pair.normalize_pair 유틸 직접 검증."""

    def test_np01_upper_to_lower(self):
        """NP-01: 대문자 입력 → 소문자 반환."""
        from core.pair import normalize_pair
        assert normalize_pair("BTC_JPY") == "btc_jpy"

    def test_np02_already_lower_idempotent(self):
        """NP-02: 이미 소문자 → 그대로 반환 (멱등)."""
        from core.pair import normalize_pair
        assert normalize_pair("btc_jpy") == "btc_jpy"

    def test_np03_strip_whitespace(self):
        """NP-03: 앞뒤 공백 제거."""
        from core.pair import normalize_pair
        assert normalize_pair("  BTC_JPY  ") == "btc_jpy"

    def test_np04_mixed_case(self):
        """NP-04: 혼합 대소문자 → 소문자."""
        from core.pair import normalize_pair
        assert normalize_pair("Btc_Jpy") == "btc_jpy"

    def test_np05_appstate_delegates_to_core(self):
        """NP-05: AppState.normalize_pair 가 core.pair.normalize_pair 와 동일 결과."""
        from core.pair import normalize_pair
        from api.dependencies import AppState
        state = _make_state("pair")
        assert AppState.normalize_pair(state, "BTC_JPY") == normalize_pair("BTC_JPY")

    def test_np06_empty_string(self):
        """NP-06: 빈 문자열 → 빈 문자열 (크래시 없음)."""
        from core.pair import normalize_pair
        assert normalize_pair("") == ""

    def test_np07_whitespace_only(self):
        """NP-07: 공백만 → 빈 문자열 (strip 후 lower)."""
        from core.pair import normalize_pair
        assert normalize_pair("   ") == ""

    def test_np08_idempotent_multiple_calls(self):
        """NP-08: 여러 번 적용해도 결과 동일 (멱등성)."""
        from core.pair import normalize_pair
        once = normalize_pair("BTC_JPY")
        twice = normalize_pair(normalize_pair("BTC_JPY"))
        assert once == twice == "btc_jpy"

    def test_np09_advisory_post_stores_lowercase(self):
        """NP-09: advisory POST 시 pair 정규화 경로 확인 (소스 검증)."""
        import os
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        route_path = os.path.join(base, "api", "routes", "advisories.py")
        with open(route_path) as f:
            src = f.read()
        assert "normalize_pair(body.pair)" in src, "POST advisory에서 pair 정규화 누락"

    def test_np10_fetch_advisory_normalizes_pair(self):
        """NP-10: _fetch_advisory 진입 시 pair 정규화 경로 확인 (소스 검증)."""
        import os
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        src_path = os.path.join(base, "core", "decision", "rachel_advisory.py")
        with open(src_path) as f:
            src = f.read()
        assert "pair = normalize_pair(pair)" in src, "_fetch_advisory에서 pair 정규화 누락"
