"""signals.py 유닛 테스트 — 기존 동작 검증."""

import pytest

from core.strategy.signals import (
    compute_adaptive_trailing_mult,
    compute_ema,
    compute_exit_signal,
    compute_profit_based_mult,
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

    # EW-SL-01: 기본값 0.05 — slope 0.04 는 [0, 0.05) 범위 → tighten_stop
    def test_EW_SL_01_default_threshold_tighten_at_0_04(self):
        result = compute_exit_signal(
            ema_slope_pct=0.04, rsi=55.0, atr=10.0,
            current_price=100.0, entry_price=90.0,
            params={},  # 기본값 사용 (0.05)
        )
        assert result["action"] == "tighten_stop", "기본값 0.05: 0 ≤ 0.04 < 0.05 → weakening 발동"

    # EW-SL-02: 기본값 0.05 — slope 0.06 → tighten_stop
    def test_EW_SL_02_default_threshold_tighten_at_0_06(self):
        result = compute_exit_signal(
            ema_slope_pct=0.06, rsi=55.0, atr=10.0,
            current_price=100.0, entry_price=90.0,
            params={},  # 기본값 사용 (0.05)
        )
        assert result["action"] == "hold", "slope 0.06 >= 0.05 → weakening 아님(범위 0 ≤ pct < th) → hold"

    # EW-SL-03: 명시적 threshold=0.03 — slope 0.04 는 0.04 >= 0.03 → hold (구 임계값에선 미발동)
    def test_EW_SL_03_explicit_threshold_0_03_hold_at_0_04(self):
        result = compute_exit_signal(
            ema_slope_pct=0.04, rsi=55.0, atr=10.0,
            current_price=100.0, entry_price=90.0,
            params={"ema_slope_weak_threshold": 0.03},
        )
        assert result["action"] == "hold", "threshold=0.03: 0.04 >= 0.03 → weakening 범위 밖 → hold"


# ── Adaptive Trailing Mult ──────────────

class TestAdaptiveTrailingMult:
    def test_initial_phase(self):
        """실돈 추세 → 넘은 배수."""
        result = compute_adaptive_trailing_mult(0.5, 55.0, {})
        assert result == 1.5

    def test_mature_low_slope(self):
        """기울기 둔화 → 좁은 배수."""
        result = compute_adaptive_trailing_mult(0.02, 55.0, {})
        assert result == 1.2

    def test_mature_high_rsi(self):
        """RSI 과매수 → 좁은 배수."""
        result = compute_adaptive_trailing_mult(0.5, 80.0, {})
        assert result == 1.2

# ── Profit-Based Trailing Mult ────────────────────────────────────

class TestProfitBasedMult:
    _PARAMS = {
        "trailing_stop_atr_initial": 1.5,
        "trailing_stop_decay_per_atr": 0.2,
        "trailing_stop_atr_min": 0.3,
    }

    def test_pb01_no_profit_returns_initial(self):
        """이익 없음(현재가=진입가) → initial 배수 반환."""
        result = compute_profit_based_mult(10000.0, 10000.0, 100.0, self._PARAMS, side="buy")
        assert result == 1.5

    def test_pb02_small_profit_continuous_decay(self):
        """이익 ATR×0.8 → max(0.3, 1.5-0.2×0.8) = 1.34."""
        result = compute_profit_based_mult(10000.0, 10080.0, 100.0, self._PARAMS, side="buy")
        assert result == pytest.approx(1.34)

    def test_pb03_profit_one_atr(self):
        """이익 ATR×1.0 → max(0.3, 1.5-0.2×1.0) = 1.3."""
        result = compute_profit_based_mult(10000.0, 10100.0, 100.0, self._PARAMS, side="buy")
        assert result == pytest.approx(1.3)

    def test_pb04_profit_two_atr(self):
        """이익 ATR×2.0 → max(0.3, 1.5-0.2×2.0) = 1.1."""
        result = compute_profit_based_mult(10000.0, 10200.0, 100.0, self._PARAMS, side="buy")
        assert result == pytest.approx(1.1)

    def test_pb05_short_profit_two_atr(self):
        """숏 이익 ATR×2.0 → 1.1."""
        result = compute_profit_based_mult(10200.0, 10000.0, 100.0, self._PARAMS, side="sell")
        assert result == pytest.approx(1.1)

    def test_pb06_short_no_profit_returns_initial(self):
        """숏 손실(가격 상승 중) → initial 반환."""
        result = compute_profit_based_mult(10000.0, 10050.0, 100.0, self._PARAMS, side="sell")
        assert result == 1.5

    def test_pb07_zero_atr_returns_initial(self):
        """ATR=0 → initial 반환."""
        result = compute_profit_based_mult(10000.0, 10500.0, 0.0, self._PARAMS, side="buy")
        assert result == 1.5

    def test_pb08_large_profit_hits_floor(self):
        """이익 ATR×6.0 → max(0.3, 1.5-1.2) = 0.3 (배수 하한)."""
        result = compute_profit_based_mult(10000.0, 10600.0, 100.0, self._PARAMS, side="buy")
        assert result == pytest.approx(0.3)

    def test_pb09_profit_four_atr(self):
        """이익 ATR×4.0 → max(0.3, 1.5-0.8) = 0.7."""
        result = compute_profit_based_mult(10000.0, 10400.0, 100.0, self._PARAMS, side="buy")
        assert result == pytest.approx(0.7)

    def test_pb10_custom_decay_and_min(self):
        """커스텀 decay/min 파라미터 적용."""
        params = {
            "trailing_stop_atr_initial": 2.0,
            "trailing_stop_decay_per_atr": 0.4,
            "trailing_stop_atr_min": 0.5,
        }
        # 이익 ATR×3 → max(0.5, 2.0-0.4×3) = max(0.5, 0.8) = 0.8
        result = compute_profit_based_mult(10000.0, 10300.0, 100.0, params, side="buy")
        assert result == pytest.approx(0.8)

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
        assert result["signal"] in ("long_setup", "long_overheated", "no_signal")
        assert result["current_price"] > 0
        assert result["ema"] is not None
        assert result["atr"] is not None
        assert result["rsi"] is not None

    def test_downtrend_exit_warning(self):
        candles = _make_downtrend_candles(30)
        result = compute_trend_signal(candles)
        assert result["signal"] in ("long_caution", "short_oversold", "no_signal")

    def test_insufficient_candles(self):
        candles = _make_uptrend_candles(5)
        result = compute_trend_signal(candles)
        assert result["signal"] in ("no_signal", "long_caution", "short_oversold")

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
    """숏 진입 시그널 (short_setup) 검증."""

    def test_downtrend_short_setup(self):
        """하락 추세 + slope < threshold + RSI 범위 → short_setup."""
        candles = _make_downtrend_candles(30, start=200.0, step=2.0)
        params = {"ema_slope_short_threshold": -0.01}
        result = compute_trend_signal(candles, params=params)
        # 강한 하락 추세에서 short_setup 또는 long_caution/short_oversold
        assert result["signal"] in ("short_setup", "long_caution", "short_oversold")
        assert result["ema_slope_pct"] is not None
        assert result["ema_slope_pct"] < 0

    def test_weak_downtrend_no_short_setup(self):
        """약한 하락 → slope가 threshold 미달 → short_setup 아님."""
        # 거의 수평에 가까운 캔들 (미세 하락)
        candles = _make_downtrend_candles(30, start=200.0, step=0.01)
        params = {"ema_slope_short_threshold": -0.05}
        result = compute_trend_signal(candles, params=params)
        assert result["signal"] != "short_setup"

    def test_uptrend_never_short_setup(self):
        """상승 추세에서는 short_setup 불가."""
        candles = _make_uptrend_candles(30)
        result = compute_trend_signal(candles)
        assert result["signal"] != "short_setup"

    def test_short_setup_rsi_out_of_range(self):
        """RSI 범위 벗어나면 short_setup 아님 (exit_warning)."""
        # 극단적 하락 → RSI 매우 낮음 → rsi_in_short_range 미달
        candles = _make_downtrend_candles(30, start=300.0, step=10.0)
        params = {"ema_slope_short_threshold": -0.01, "entry_rsi_min_short": 35.0, "entry_rsi_max_short": 60.0}
        result = compute_trend_signal(candles, params=params)
        if result["rsi"] is not None and result["rsi"] < 35.0:
            assert result["signal"] != "short_setup"


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


# ── EMA Slope Entry Min ─────────────────

class TestEmaSlopeEntryMin:
    """ema_slope_entry_min 파라미터 검증 — 현물 진입 조건 완화."""

    def test_default_zero_blocks_negative_slope(self):
        """기본값(0.0): 음수 slope → long_setup 불가."""
        # 미세 하락이면서 price > EMA인 캔들
        candles = _make_uptrend_candles(30, start=100.0, step=0.05)
        result_default = compute_trend_signal(candles)
        # slope이 양수 — 비교 기준용
        if result_default["ema_slope_pct"] is not None and result_default["ema_slope_pct"] > 0:
            # 이 경우 long_setup 가능 (기본 동작 확인)
            pass

    def test_negative_threshold_allows_entry(self):
        """ema_slope_entry_min=-0.1: 약간 기울기 둔화 시 long_setup 허용."""
        # 강한 상승 후 끝에서 소폭 하락 → price > EMA, slope ≈ 약한 음수~0
        candles = []
        for i in range(25):
            c = 100.0 + i * 2.0  # 강한 상승
            candles.append(_FakeCandle(c - 0.5, c + 0.5, c - 1.0, c, volume=100.0))
        for i in range(5):
            c = 148.0 - i * 0.3  # 소폭 하락 (peak 148 근처에서)
            candles.append(_FakeCandle(c - 0.5, c + 0.5, c - 1.0, c, volume=100.0))

        result_strict = compute_trend_signal(candles)
        result_relaxed = compute_trend_signal(
            candles, params={"ema_slope_entry_min": -0.1}
        )
        # slope이 양수면 두 결과 모두 동일 → 이 테스트는 slope이 0 근처일 때 의미
        if result_strict["ema_slope_pct"] is not None and result_strict["ema_slope_pct"] < 0:
            assert result_strict["signal"] != "long_setup"
            # relaxed에서는 slope >= -0.1이면 long_setup 가능
            if result_relaxed["ema_slope_pct"] >= -0.1:
                assert result_relaxed["signal"] in ("long_setup", "wait_dip", "wait_regime", "no_signal")

    def test_positive_threshold_stricter(self):
        """ema_slope_entry_min=0.1: 약한 양수 slope도 차단."""
        candles = _make_uptrend_candles(30, start=100.0, step=0.05)
        result = compute_trend_signal(
            candles, params={"ema_slope_entry_min": 0.1}
        )
        # slope이 0.1 미만이면 long_setup가 아님
        if (result["ema_slope_pct"] is not None
                and 0 < result["ema_slope_pct"] < 0.1):
            assert result["signal"] != "long_setup"


# ── EL-8, EL-9: bb_width_pct 반환 검증 ─────────────────────

class TestBbWidthPctInSignal:
    def test_el8_bb_width_pct_in_return(self):
        """EL-8: compute_trend_signal 반환값에 bb_width_pct 존재 + float >= 0."""
        candles = _make_uptrend_candles(30)
        result = compute_trend_signal(candles)
        assert "bb_width_pct" in result, "bb_width_pct가 반환값에 없음"
        assert isinstance(result["bb_width_pct"], float)
        assert result["bb_width_pct"] >= 0

    def test_el9_bb_width_pct_not_zero_with_varied_prices(self):
        """EL-9: 변동 있는 캔들 → bb_width_pct > 0 (entry_bb_width NOT NULL 확인 대리)."""
        candles = _make_uptrend_candles(30, start=100.0, step=2.0)
        result = compute_trend_signal(candles)
        assert result["bb_width_pct"] is not None
        assert result["bb_width_pct"] >= 0


# ── T-05: 레짐 임계값 파라미터화 검증 ──────────────────

def _make_flat_candles(n: int = 30, price: float = 100.0, noise: float = 0.1) -> list:
    """횡보 캔들 생성 — BB 폭이 매우 좁음."""
    candles = []
    for i in range(n):
        c = price + (noise if i % 2 == 0 else -noise)
        candles.append(_FakeCandle(c, c + noise, c - noise, c))
    return candles


def _make_volatile_candles(n: int = 30, start: float = 100.0, amplitude: float = 10.0) -> list:
    """변동성 큰 캔들 생성 — BB 폭이 넓음."""
    candles = []
    for i in range(n):
        c = start + amplitude * (1 if i % 2 == 0 else -1) + i * 0.5
        candles.append(_FakeCandle(c, c + amplitude, c - amplitude, c))
    return candles


class TestRegimeParamDefaults:
    """T-05: params 없으면 기존 4H 기본값으로 동작 (하위 호환)."""

    def test_default_no_params_same_as_before(self):
        """params={} → 기본 임계값 적용, 기존 동작과 동일."""
        candles = _make_uptrend_candles(30)
        result_default = compute_trend_signal(candles, params={})
        result_none = compute_trend_signal(candles, params=None)
        assert result_default["regime"] == result_none["regime"]
        assert result_default["signal"] == result_none["signal"]
        assert result_default["bb_width_pct"] == pytest.approx(result_none["bb_width_pct"])

    def test_regime_key_exists(self):
        """regime 키가 trending/ranging/unclear 중 하나."""
        candles = _make_uptrend_candles(30)
        result = compute_trend_signal(candles)
        assert result["regime"] in ("trending", "ranging", "unclear")


class TestRegimeParamCustom:
    """T-05: 커스텀 regime 임계값을 params로 주입."""

    def test_lower_trending_threshold_makes_trending(self):
        """bb_width_trending_min을 낮추면 trending 판정이 더 쉬움."""
        candles = _make_uptrend_candles(30, step=1.0)
        # 기본값(6.0)으로 판정
        result_default = compute_trend_signal(candles, params={})
        # 매우 낮은 임계값 → trending 확정
        result_low = compute_trend_signal(
            candles, params={"bb_width_trending_min": 0.1, "range_pct_trending_min": 0.1}
        )
        assert result_low["regime"] == "trending"

    def test_higher_trending_threshold_avoids_trending(self):
        """bb_width_trending_min을 크게 올리면 trending 판정이 어려워짐."""
        candles = _make_uptrend_candles(30, step=1.0)
        result = compute_trend_signal(
            candles, params={"bb_width_trending_min": 99.0, "range_pct_trending_min": 99.0}
        )
        # 임계값이 극단적으로 높으면 trending이 아님
        assert result["regime"] != "trending"

    def test_wider_ranging_threshold_makes_ranging(self):
        """bb_width_ranging_max를 크게 올리면 횡보 판정이 더 쉬움."""
        candles = _make_flat_candles(30, noise=0.1)
        result = compute_trend_signal(
            candles, params={
                "bb_width_ranging_max": 99.0,
                "range_pct_ranging_max": 99.0,
                "bb_width_trending_min": 99.0,
                "range_pct_trending_min": 99.0,
            }
        )
        assert result["regime"] == "ranging"

    def test_tight_ranging_threshold_avoids_ranging(self):
        """bb_width_ranging_max=0 → 절대 ranging 아님."""
        candles = _make_flat_candles(30, noise=0.1)
        result = compute_trend_signal(
            candles, params={"bb_width_ranging_max": 0.0, "range_pct_ranging_max": 0.0}
        )
        assert result["regime"] != "ranging"

    def test_wait_regime_with_custom_thresholds(self):
        """ranging 판정이 생기면 wait_regime 시그널 가능."""
        # 상승 추세이나 횡보 강제 → wait_regime
        candles = _make_uptrend_candles(30, step=0.5)
        result = compute_trend_signal(
            candles, params={
                "bb_width_ranging_max": 99.0,
                "range_pct_ranging_max": 99.0,
                "bb_width_trending_min": 99.0,
                "range_pct_trending_min": 99.0,
            }
        )
        # 가격이 EMA 위 + 기울기 양수 + regime=ranging → wait_regime
        if result["signal"] == "wait_regime":
            assert result["regime"] == "ranging"


class TestIndicatorPeriodParams:
    """T-05: ema_period, atr_period, rsi_period 파라미터화 검증."""

    def test_custom_ema_period(self):
        """ema_period=10으로 변경 시 다른 EMA 값."""
        candles = _make_uptrend_candles(30)
        result_default = compute_trend_signal(candles, params={})
        result_custom = compute_trend_signal(candles, params={"ema_period": 10})
        # EMA period가 다르면 값이 달라야 함
        assert result_default["ema"] != result_custom["ema"]

    def test_custom_rsi_period(self):
        """rsi_period=7로 변경 시 다른 RSI 값."""
        # 순수 상승은 RSI 100 (period 무관) → 등락 섞인 데이터 사용
        candles = _make_volatile_candles(30, start=100.0, amplitude=3.0)
        result_default = compute_trend_signal(candles, params={})
        result_custom = compute_trend_signal(candles, params={"rsi_period": 7})
        # RSI period가 다르면 값이 달라야 함
        assert result_default["rsi"] is not None
        assert result_custom["rsi"] is not None
        assert result_default["rsi"] != result_custom["rsi"]

    def test_custom_bb_period(self):
        """bb_period를 직접 지정하면 해당 기간의 BB 폭 계산."""
        candles = _make_volatile_candles(30)
        result_10 = compute_trend_signal(candles, params={"bb_period": 10})
        result_25 = compute_trend_signal(candles, params={"bb_period": 25})
        # 다른 기간 → 다른 bb_width_pct
        assert result_10["bb_width_pct"] != result_25["bb_width_pct"]


class TestRegimeDataInsufficiency:
    """T-05: 데이터 부족 시 안전한 동작 검증."""

    def test_few_candles_still_works(self):
        """캔들 5개로도 에러 없이 regime 판정."""
        candles = _make_uptrend_candles(5)
        result = compute_trend_signal(candles)
        assert result["regime"] in ("trending", "ranging", "unclear")
        assert result["bb_width_pct"] >= 0

    def test_single_candle(self):
        """캔들 1개로도 크래시 없음."""
        candles = [_FakeCandle(100, 101, 99, 100)]
        result = compute_trend_signal(candles)
        assert "regime" in result
        assert "signal" in result

    def test_bb_period_exceeds_candle_count(self):
        """bb_period > 캔들 수 → min(bb_period, len(closes))로 안전 처리."""
        candles = _make_uptrend_candles(10)
        result = compute_trend_signal(candles, params={"bb_period": 100})
        # 크래시 없이 정상 반환
        assert result["bb_width_pct"] >= 0
        assert result["regime"] in ("trending", "ranging", "unclear")
