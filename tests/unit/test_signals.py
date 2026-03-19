"""signals.py 유닛 테스트 — 기존 동작 검증."""

import pytest

from core.strategy.signals import (
    compute_adaptive_trailing_mult,
    compute_ema,
    compute_exit_signal,
    compute_rsi_series,
    compute_trend_signal,
    detect_bearish_divergence,
    detect_bearish_divergences,
    find_pivot_highs,
)


# ── EMA ─────────────────────────────────

class TestComputeEma:
    def test_insufficient_data(self):
        assert compute_ema([1.0, 2.0], period=5) is None

    def test_exact_period(self):
        prices = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = compute_ema(prices, period=5)
        assert result == pytest.approx(3.0)  # SMA of first 5

    def test_basic_calculation(self):
        prices = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        result = compute_ema(prices, period=3)
        assert result is not None
        assert result > 0


# ── RSI Series ──────────────────────────

class TestComputeRsiSeries:
    def test_insufficient_data(self):
        result = compute_rsi_series([1.0, 2.0], period=14)
        assert all(r is None for r in result)

    def test_output_length(self):
        closes = list(range(1, 22))  # 21개
        result = compute_rsi_series(closes, period=14)
        assert len(result) == len(closes)

    def test_all_gains(self):
        """연속 상승 → RSI ≈ 100."""
        closes = [float(i) for i in range(1, 20)]
        result = compute_rsi_series(closes, period=14)
        assert result[-1] == 100.0

    def test_all_losses(self):
        """연속 하락 → RSI ≈ 0."""
        closes = [float(20 - i) for i in range(20)]
        result = compute_rsi_series(closes, period=14)
        assert result[-1] is not None
        assert result[-1] < 5.0


# ── Exit Signal ─────────────────────────

class TestComputeExitSignal:
    def _default_params(self, **overrides):
        p = {
            "rsi_overbought": 75,
            "rsi_extreme": 80,
            "rsi_breakdown": 40,
            "ema_slope_weak_threshold": 0.03,
            "partial_exit_profit_atr": 2.0,
            "tighten_stop_atr": 1.0,
        }
        p.update(overrides)
        return p

    def test_hold_normal_conditions(self):
        result = compute_exit_signal(
            ema_slope_pct=0.5, rsi=55.0, atr=10.0,
            current_price=100.0, entry_price=90.0,
            params=self._default_params(),
        )
        assert result["action"] == "hold"

    def test_full_exit_negative_slope(self):
        result = compute_exit_signal(
            ema_slope_pct=-0.1, rsi=55.0, atr=10.0,
            current_price=100.0, entry_price=90.0,
            params=self._default_params(),
        )
        assert result["action"] == "full_exit"

    def test_full_exit_rsi_breakdown(self):
        result = compute_exit_signal(
            ema_slope_pct=0.5, rsi=35.0, atr=10.0,
            current_price=100.0, entry_price=90.0,
            params=self._default_params(),
        )
        assert result["action"] == "full_exit"

    def test_tighten_stop_rsi_extreme(self):
        result = compute_exit_signal(
            ema_slope_pct=0.5, rsi=85.0, atr=10.0,
            current_price=120.0, entry_price=90.0,
            params=self._default_params(),
        )
        assert result["action"] == "tighten_stop"

    def test_tighten_stop_profit_target(self):
        result = compute_exit_signal(
            ema_slope_pct=0.5, rsi=55.0, atr=10.0,
            current_price=120.0, entry_price=90.0,  # 이익 30 > ATR*2 = 20
            params=self._default_params(),
        )
        assert result["action"] == "tighten_stop"


# ── Adaptive Trailing Mult ──────────────

class TestAdaptiveTrailingMult:
    def test_initial_phase(self):
        """강한 추세 → 넓은 배수."""
        result = compute_adaptive_trailing_mult(0.5, 55.0, {})
        assert result == 2.0

    def test_mature_low_slope(self):
        """기울기 둔화 → 좁은 배수."""
        result = compute_adaptive_trailing_mult(0.03, 55.0, {})
        assert result == 1.2

    def test_mature_high_rsi(self):
        """RSI 과매수 → 좁은 배수."""
        result = compute_adaptive_trailing_mult(0.5, 80.0, {})
        assert result == 1.2


# ── Trend Signal ────────────────────────

class _FakeCandle:
    """signals.py duck-typing 호환 캔들."""
    def __init__(self, open_: float, high: float, low: float, close: float, volume: float = 100.0):
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def _make_uptrend_candles(n: int = 30, start: float = 100.0, step: float = 1.0) -> list:
    """상승 추세 캔들 생성."""
    candles = []
    for i in range(n):
        c = start + i * step
        candles.append(_FakeCandle(c - 0.5, c + 0.5, c - 1.0, c, volume=100.0 + i))
    return candles


def _make_downtrend_candles(n: int = 30, start: float = 200.0, step: float = 1.0) -> list:
    """하락 추세 캔들 생성."""
    candles = []
    for i in range(n):
        c = start - i * step
        candles.append(_FakeCandle(c + 0.5, c + 1.0, c - 0.5, c, volume=100.0 + i))
    return candles


class TestComputeTrendSignal:
    def test_uptrend_entry(self):
        candles = _make_uptrend_candles(30)
        result = compute_trend_signal(candles)
        assert result["signal"] in ("entry_ok", "wait_dip", "no_signal")
        assert result["current_price"] > 0
        assert result["ema"] is not None
        assert result["atr"] is not None
        assert result["rsi"] is not None

    def test_downtrend_exit_warning(self):
        candles = _make_downtrend_candles(30)
        result = compute_trend_signal(candles)
        assert result["signal"] == "exit_warning"

    def test_insufficient_candles(self):
        candles = _make_uptrend_candles(5)
        result = compute_trend_signal(candles)
        # EMA None일 수 있음 → no_signal
        assert result["signal"] in ("no_signal", "exit_warning")

    def test_result_keys(self):
        candles = _make_uptrend_candles(30)
        result = compute_trend_signal(candles)
        expected_keys = {"signal", "current_price", "ema", "ema_slope_pct", "atr",
                         "stop_loss_price", "rsi", "rsi_series", "regime", "exit_signal"}
        assert expected_keys.issubset(result.keys())


# ── Pivot Highs ─────────────────────────

class TestFindPivotHighs:
    def test_basic_pivot(self):
        candles = [
            _FakeCandle(10, 11, 9, 10),
            _FakeCandle(10, 12, 9, 11),
            _FakeCandle(10, 15, 9, 14),    # pivot high
            _FakeCandle(10, 13, 9, 12),
            _FakeCandle(10, 11, 9, 10),
        ]
        pivots = find_pivot_highs(candles, left=2, right=2)
        assert len(pivots) == 1
        assert pivots[0]["price"] == 15.0

    def test_no_pivot(self):
        candles = _make_uptrend_candles(5)
        pivots = find_pivot_highs(candles, left=2, right=2)
        # 순수 상승 → 피봇 없을 수 있음
        assert isinstance(pivots, list)


# ── Bearish Divergence ──────────────────

class TestDetectBearishDivergence:
    def test_disabled(self):
        result = detect_bearish_divergence([], [], {"divergence_enabled": False})
        assert result["detected"] is False

    def test_insufficient_pivots(self):
        candles = _make_uptrend_candles(10)
        closes = [c.close for c in candles]
        rsi = compute_rsi_series(closes)
        result = detect_bearish_divergence(candles, rsi, {})
        assert isinstance(result["detected"], bool)


class TestDetectBearishDivergences:
    def test_disabled(self):
        result = detect_bearish_divergences([], [], {"divergence_enabled": False})
        assert result["rsi_divergence"] is False
        assert result["volume_divergence"] is False
        assert result["both"] is False

    def test_volume_disabled(self):
        result = detect_bearish_divergences(
            [], [], {"divergence_enabled": False, "volume_divergence_enabled": False},
        )
        assert result["volume_divergence"] is False
