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


# ── Short Entry Signal ──────────────────

class TestShortEntrySignal:
    """숏 진입 시그널 (entry_sell) 검증."""

    def test_downtrend_entry_sell(self):
        """하락 추세 + slope < threshold + RSI 범위 → entry_sell."""
        candles = _make_downtrend_candles(30, start=200.0, step=2.0)
        params = {"ema_slope_short_threshold": -0.01}
        result = compute_trend_signal(candles, params=params)
        # 강한 하락 추세에서 entry_sell 또는 exit_warning
        assert result["signal"] in ("entry_sell", "exit_warning")
        assert result["ema_slope_pct"] is not None
        assert result["ema_slope_pct"] < 0

    def test_weak_downtrend_no_entry_sell(self):
        """약한 하락 → slope가 threshold 미달 → entry_sell 아님."""
        # 거의 수평에 가까운 캔들 (미세 하락)
        candles = _make_downtrend_candles(30, start=200.0, step=0.01)
        params = {"ema_slope_short_threshold": -0.05}
        result = compute_trend_signal(candles, params=params)
        assert result["signal"] != "entry_sell"

    def test_uptrend_never_entry_sell(self):
        """상승 추세에서는 entry_sell 불가."""
        candles = _make_uptrend_candles(30)
        result = compute_trend_signal(candles)
        assert result["signal"] != "entry_sell"

    def test_entry_sell_rsi_out_of_range(self):
        """RSI 범위 벗어나면 entry_sell 아님 (exit_warning)."""
        # 극단적 하락 → RSI 매우 낮음 → rsi_in_short_range 미달
        candles = _make_downtrend_candles(30, start=300.0, step=10.0)
        params = {"ema_slope_short_threshold": -0.01, "entry_rsi_min_short": 35.0, "entry_rsi_max_short": 60.0}
        result = compute_trend_signal(candles, params=params)
        if result["rsi"] is not None and result["rsi"] < 35.0:
            assert result["signal"] != "entry_sell"


# ── Short Exit Signal ───────────────────

class TestShortExitSignal:
    """숏 포지션 청산 시그널 검증."""

    def test_short_full_exit_on_positive_slope(self):
        """숏 보유 중 EMA 기울기 양전환 → full_exit."""
        result = compute_exit_signal(
            ema_slope_pct=0.5,
            rsi=50.0,
            atr=1000.0,
            current_price=15_000_000.0,
            entry_price=15_500_000.0,
            params={},
            side="sell",
        )
        assert result["action"] == "full_exit"
        assert result["triggers"]["ema_slope_negative"] is True

    def test_short_full_exit_on_rsi_overbought(self):
        """숏 보유 중 RSI 극단 과매수 → full_exit (매수 압력)."""
        result = compute_exit_signal(
            ema_slope_pct=-0.3,
            rsi=85.0,
            atr=1000.0,
            current_price=15_000_000.0,
            entry_price=15_500_000.0,
            params={"rsi_extreme": 80},
            side="sell",
        )
        assert result["action"] == "full_exit"
        assert result["triggers"]["rsi_breakdown"] is True

    def test_short_hold_when_trend_continues(self):
        """숏 보유 중 하락 추세 지속 → hold."""
        result = compute_exit_signal(
            ema_slope_pct=-0.5,
            rsi=45.0,
            atr=1_000_000.0,
            current_price=14_900_000.0,
            entry_price=15_000_000.0,
            params={},
            side="sell",
        )
        assert result["action"] == "hold"

    def test_short_profit_target(self):
        """숏 이익목표 달성 → tighten_stop."""
        result = compute_exit_signal(
            ema_slope_pct=-0.3,
            rsi=45.0,
            atr=200_000.0,
            current_price=14_000_000.0,
            entry_price=15_000_000.0,
            params={"partial_exit_profit_atr": 2.0},
            side="sell",
        )
        # (entry - current) = 1M > atr(200K) * 2 = 400K → profit target hit
        assert result["action"] == "tighten_stop"
        assert result["triggers"]["profit_target_hit"] is True

    def test_short_adjusted_trailing_stop_above_price(self):
        """숏 스탑은 가격 위에 설정."""
        result = compute_exit_signal(
            ema_slope_pct=-0.3,
            rsi=45.0,
            atr=100_000.0,
            current_price=15_000_000.0,
            entry_price=15_500_000.0,
            params={"tighten_stop_atr": 1.0},
            side="sell",
        )
        # 숏: stop = price + atr * mult = 15_000_000 + 100_000 = 15_100_000
        assert result["adjusted_trailing_stop"] > 15_000_000.0

    def test_long_adjusted_trailing_stop_below_price(self):
        """롱 스탑은 가격 아래에 설정 (기존 동작 유지)."""
        result = compute_exit_signal(
            ema_slope_pct=0.3,
            rsi=55.0,
            atr=100_000.0,
            current_price=15_000_000.0,
            entry_price=14_500_000.0,
            params={"tighten_stop_atr": 1.0},
            side="buy",
        )
        assert result["adjusted_trailing_stop"] < 15_000_000.0


# ── Short Stop Loss in Trend Signal ─────

class TestShortStopLoss:
    """compute_trend_signal에서 side=sell 시 stop_loss_price 방향 검증."""

    def test_short_stop_above_price(self):
        """숏 스탑로스는 현재가 위에."""
        candles = _make_downtrend_candles(30, start=200.0, step=2.0)
        result = compute_trend_signal(candles, side="sell")
        if result["stop_loss_price"] is not None:
            assert result["stop_loss_price"] > result["current_price"]

    def test_long_stop_below_price(self):
        """롱 스탑로스는 현재가 아래 (기존 동작)."""
        candles = _make_uptrend_candles(30)
        result = compute_trend_signal(candles, side="buy")
        if result["stop_loss_price"] is not None:
            assert result["stop_loss_price"] < result["current_price"]
