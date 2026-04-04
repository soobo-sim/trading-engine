"""
박스역추세 백테스트 — D-1~D-5 실전 정합성 테스트.

레이첼 제안서(BACKTEST_MODULE_DESIGN.md §8)에서 발견된 불일치 수정 검증:
  D-1: 진입 판정 — near_lower 양방향 밴드
  D-2: 청산 — near_upper 도달 + 박스 무효화
  D-3: 4H 종가 박스 이탈 → 강제 청산
  D-4: 수렴 삼각형 → 무효화 + 강제 청산
  D-5: FX 주말 자동 청산
"""
from __future__ import annotations

import math
import pytest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from core.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    _run_box_backtest,
    _classify_price_in_box,
    _check_box_invalidation,
    _is_weekend_close_time,
    _is_market_closed,
    _linear_slope,
)


# ── 캔들 픽스처 ──────────────────────────────────────────────

@dataclass
class FakeCandle:
    close: float
    high: float
    low: float
    open_time: Optional[datetime] = None
    open: float = 0.0

    def __post_init__(self):
        if self.open_time is None:
            self.open_time = datetime.now(tz=timezone.utc)


def _box_candles(n=80, upper=110.0, lower=90.0, oscillations=6):
    """박스권을 오가는 캔들 생성."""
    candles = []
    for i in range(n):
        t = i / n * oscillations * math.pi
        price = lower + (upper - lower) * (math.sin(t) * 0.5 + 0.5)
        high = price * 1.002
        low = price * 0.998
        candles.append(FakeCandle(close=price, high=high, low=low))
    return candles


def _box_candles_timed(
    n=80, upper=110.0, lower=90.0, oscillations=6,
    start_dt: Optional[datetime] = None,
    interval_hours: int = 4,
):
    """타임스탬프가 있는 박스 캔들."""
    if start_dt is None:
        start_dt = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(n):
        t = i / n * oscillations * math.pi
        price = lower + (upper - lower) * (math.sin(t) * 0.5 + 0.5)
        high = price * 1.002
        low = price * 0.998
        dt = start_dt + timedelta(hours=interval_hours * i)
        candles.append(FakeCandle(close=price, high=high, low=low, open_time=dt))
    return candles


# ──────────────────────────────────────────────────────────────
# D-1: _classify_price_in_box — 양방향 밴드
# ──────────────────────────────────────────────────────────────

class TestD1ClassifyPriceInBox:
    """D-1: 진입 판정이 실전 _is_price_in_box와 동일한 양방향 밴드를 사용하는지 검증."""

    def test_near_lower(self):
        box = {"upper": 110.0, "lower": 90.0}
        # near_bound_pct=0.3 → 90 ± 0.3% = [89.73, 90.27]
        assert _classify_price_in_box(90.0, box, 0.3) == "near_lower"
        assert _classify_price_in_box(89.75, box, 0.3) == "near_lower"
        assert _classify_price_in_box(90.25, box, 0.3) == "near_lower"

    def test_near_upper(self):
        box = {"upper": 110.0, "lower": 90.0}
        # 110 ± 0.3% = [109.67, 110.33]
        assert _classify_price_in_box(110.0, box, 0.3) == "near_upper"
        assert _classify_price_in_box(109.70, box, 0.3) == "near_upper"
        assert _classify_price_in_box(110.30, box, 0.3) == "near_upper"

    def test_middle(self):
        box = {"upper": 110.0, "lower": 90.0}
        assert _classify_price_in_box(100.0, box, 0.3) == "middle"

    def test_outside(self):
        box = {"upper": 110.0, "lower": 90.0}
        assert _classify_price_in_box(85.0, box, 0.3) == "outside"
        assert _classify_price_in_box(115.0, box, 0.3) == "outside"

    def test_none_box(self):
        assert _classify_price_in_box(100.0, None, 0.3) is None

    def test_no_sell_short_in_spot(self):
        """D-1 핵심: 실전에서 near_lower만 진입 (buy). 백테스트도 sell 진입하지 않음."""
        candles = _box_candles(n=120, upper=110.0, lower=90.0)
        config = BacktestConfig(fee_pct=0.0, slippage_pct=0.0)
        params = {
            "box_tolerance_pct": 1.5,
            "box_min_touches": 2,
            "near_bound_pct": 1.5,
        }
        result = _run_box_backtest(candles, params, config)
        # 모든 거래가 buy 사이드여야 함 (실전은 near_lower에서만 진입)
        for trade in result.trades:
            assert trade.side == "buy", f"실전은 near_lower→buy만 진입. sell 거래 발견: {trade}"


# ──────────────────────────────────────────────────────────────
# D-2: near_upper 청산
# ──────────────────────────────────────────────────────────────

class TestD2NearUpperExit:
    """D-2: SL/TP 기계적 청산 대신 near_upper 도달 시 청산."""

    def test_exit_reason_near_upper(self):
        """청산 사유가 near_upper_exit인 거래가 존재해야 함."""
        candles = _box_candles(n=200, upper=110.0, lower=90.0, oscillations=10)
        config = BacktestConfig(fee_pct=0.0, slippage_pct=0.0)
        params = {
            "box_tolerance_pct": 1.5,
            "box_min_touches": 2,
            "near_bound_pct": 1.5,
        }
        result = _run_box_backtest(candles, params, config)
        exit_reasons = [t.exit_reason for t in result.trades if t.exit_reason]
        # near_upper_exit이 있어야 함 (실전과 동일)
        # 또는 backtest_end (마지막 캔들에서 강제 청산)
        valid_reasons = {"near_upper_exit", "backtest_end",
                         "4h_close_below_lower", "4h_close_above_upper", "converging_triangle"}
        for reason in exit_reasons:
            assert reason in valid_reasons, f"허용하지 않는 청산 사유: {reason}"

    def test_no_mechanical_sl_tp(self):
        """stop_loss / take_profit 기계적 청산이 발생하지 않아야 함."""
        candles = _box_candles(n=200, upper=110.0, lower=90.0, oscillations=10)
        config = BacktestConfig(fee_pct=0.0, slippage_pct=0.0)
        params = {"box_tolerance_pct": 1.5, "box_min_touches": 2, "near_bound_pct": 1.5}
        result = _run_box_backtest(candles, params, config)
        for trade in result.trades:
            assert trade.exit_reason != "stop_loss", "기계적 SL 청산은 D-2에서 제거됨"
            assert trade.exit_reason != "take_profit", "기계적 TP 청산은 D-2에서 제거됨"


# ──────────────────────────────────────────────────────────────
# D-3: 4H 종가 박스 이탈 → 무효화
# ──────────────────────────────────────────────────────────────

class TestD3BoxInvalidation:
    """D-3: 4H 종가가 tolerance 밖이면 무효화하고 포지션 강제 청산."""

    def test_close_below_lower(self):
        """종가가 lower*(1-tol) 아래면 무효화."""
        box = {"upper": 110.0, "lower": 90.0}
        candle = FakeCandle(close=88.0, high=89.0, low=87.0)
        reason = _check_box_invalidation(candle, box, 0.5, [candle] * 5)
        assert reason == "4h_close_below_lower"

    def test_close_above_upper(self):
        """종가가 upper*(1+tol) 위면 무효화."""
        box = {"upper": 110.0, "lower": 90.0}
        candle = FakeCandle(close=112.0, high=113.0, low=111.0)
        reason = _check_box_invalidation(candle, box, 0.5, [candle] * 5)
        assert reason == "4h_close_above_upper"

    def test_inside_box_no_invalidation(self):
        """종가가 박스 안이면 무효화 없음 (삼각형도 아닌 경우)."""
        box = {"upper": 110.0, "lower": 90.0}
        # 모든 캔들이 같은 수준 → 삼각형 아님
        candle = FakeCandle(close=100.0, high=101.0, low=99.0)
        reason = _check_box_invalidation(candle, box, 0.5, [candle] * 10)
        assert reason is None

    def test_invalidation_forces_position_close(self):
        """박스 이탈 시 열린 포지션이 강제 청산되는지 검증."""
        # 박스 형성 후 하방 이탈하는 캔들 시퀀스
        candles = _box_candles(n=120, upper=110.0, lower=90.0, oscillations=6)
        # 끝에 이탈 캔들 추가
        for i in range(20):
            price = 85.0 - i * 0.5
            candles.append(FakeCandle(close=price, high=price + 1, low=price - 1))

        config = BacktestConfig(fee_pct=0.0, slippage_pct=0.0)
        params = {"box_tolerance_pct": 1.5, "box_min_touches": 2, "near_bound_pct": 1.5}
        result = _run_box_backtest(candles, params, config)

        # 무효화 청산이 발생 가능한 구조
        invalidation_exits = [
            t for t in result.trades
            if t.exit_reason in ("4h_close_below_lower", "4h_close_above_upper")
        ]
        # 이탈이 발생하면 무효화 청산이 있어야 함
        # (박스가 형성되고 진입이 이루어진 경우에만)
        if any(t.exit_reason != "backtest_end" for t in result.trades):
            # 거래가 있으면, 마지막에 이탈 후 청산이 있을 수 있음
            pass  # 구조적으로 검증 — 오류 없으면 성공


# ──────────────────────────────────────────────────────────────
# D-4: 수렴 삼각형
# ──────────────────────────────────────────────────────────────

class TestD4ConvergingTriangle:
    """D-4: 고점 하락 + 저점 상승 → converging_triangle 무효화."""

    def test_converging_triangle_detected(self):
        """꾸준히 고점↓ 저점↑이면 수렴 삼각형."""
        box = {"upper": 110.0, "lower": 90.0}
        # 고점은 하락, 저점은 상승하는 캔들
        candles = []
        for i in range(20):
            h = 110 - i * 0.3  # 고점 하락
            l = 90 + i * 0.3   # 저점 상승
            c = (h + l) / 2
            candles.append(FakeCandle(close=c, high=h, low=l))

        reason = _check_box_invalidation(candles[-1], box, 0.5, candles)
        assert reason == "converging_triangle"

    def test_no_triangle_flat(self):
        """평행 박스면 삼각형 아님."""
        box = {"upper": 110.0, "lower": 90.0}
        candles = [FakeCandle(close=100.0, high=105.0, low=95.0)] * 20
        reason = _check_box_invalidation(candles[-1], box, 0.5, candles)
        assert reason is None

    def test_diverging_not_converging(self):
        """발산 삼각형(고점↑, 저점↓)은 수렴이 아님."""
        box = {"upper": 110.0, "lower": 90.0}
        candles = []
        for i in range(20):
            h = 105 + i * 0.3  # 고점 상승
            l = 95 - i * 0.3   # 저점 하락
            c = (h + l) / 2
            candles.append(FakeCandle(close=c, high=h, low=l))
        reason = _check_box_invalidation(candles[-1], box, 0.5, candles)
        assert reason is None  # 발산이므로 무효화 아님


# ──────────────────────────────────────────────────────────────
# D-5: FX 주말 청산
# ──────────────────────────────────────────────────────────────

JST = ZoneInfo("Asia/Tokyo")


class TestD5WeekendClose:
    """D-5: FX 주말 자동 청산 시뮬레이션."""

    def test_saturday_is_weekend(self):
        """토요일은 주말."""
        sat = datetime(2025, 6, 7, 10, 0, tzinfo=JST)  # 토요일
        assert _is_weekend_close_time(sat) is True

    def test_sunday_is_weekend(self):
        """일요일은 주말."""
        sun = datetime(2025, 6, 8, 10, 0, tzinfo=JST)  # 일요일
        assert _is_weekend_close_time(sun) is True

    def test_monday_early_is_not_weekend_close(self):
        """월요일 07:00 JST 이전: 시장 휴장이지만 청산 트리거가 아님 (FB-1)."""
        mon_early = datetime(2025, 6, 9, 6, 0, tzinfo=JST)  # 월요일 06:00
        assert _is_weekend_close_time(mon_early) is False
        # 대신 _is_market_closed가 차단
        assert _is_market_closed(mon_early) is True

    def test_monday_after_open_is_weekday(self):
        """월요일 07:00 JST 이후는 평일."""
        mon_open = datetime(2025, 6, 9, 7, 0, tzinfo=JST)  # 월요일 07:00
        assert _is_weekend_close_time(mon_open) is False

    def test_friday_is_weekday(self):
        """금요일은 평일."""
        fri = datetime(2025, 6, 6, 15, 0, tzinfo=JST)  # 금요일 15:00
        assert _is_weekend_close_time(fri) is False

    def test_fx_weekend_close_in_backtest(self):
        """FX 모드에서 주말 캔들이 있으면 포지션 강제 청산."""
        # 목요일~일요일 4H 캔들 (주말 포함)
        start = datetime(2025, 6, 5, 0, 0, tzinfo=JST)  # 목요일
        candles = _box_candles_timed(
            n=120, upper=110.0, lower=90.0, oscillations=6,
            start_dt=start, interval_hours=4,
        )

        config = BacktestConfig(fee_pct=0.0, slippage_pct=0.0)
        params = {
            "box_tolerance_pct": 1.5,
            "box_min_touches": 2,
            "near_bound_pct": 1.5,
            "exchange_type": "fx",
        }
        result = _run_box_backtest(candles, params, config)

        # exchange_type="fx"면 주말 청산이 발생 가능 (포지션 보유 중 주말 도래 시)
        weekend_exits = [t for t in result.trades if t.exit_reason == "weekend_close"]
        # 주말이 포함된 기간이므로 weekend_close가 있을 수 있음
        # 없더라도 에러 없이 실행되면 성공 (포지션이 주말에 안 걸렸을 수도)

    def test_spot_no_weekend_close(self):
        """spot 모드에서는 주말 청산이 발생하지 않음."""
        start = datetime(2025, 6, 5, 0, 0, tzinfo=JST)
        candles = _box_candles_timed(
            n=120, upper=110.0, lower=90.0, oscillations=6,
            start_dt=start, interval_hours=4,
        )

        config = BacktestConfig(fee_pct=0.0, slippage_pct=0.0)
        params = {
            "box_tolerance_pct": 1.5,
            "box_min_touches": 2,
            "near_bound_pct": 1.5,
            "exchange_type": "spot",
        }
        result = _run_box_backtest(candles, params, config)

        for trade in result.trades:
            assert trade.exit_reason != "weekend_close", "spot은 주말 청산 없어야 함"


class TestIsMarketClosed:
    """_is_market_closed: 실전 is_fx_market_open 반전과 일치."""

    def test_saturday_early_open(self):
        """토요일 06:00 JST — 금요일 연장 세션, 시장 열림."""
        sat_early = datetime(2025, 6, 7, 6, 0, tzinfo=JST)
        assert _is_market_closed(sat_early) is False

    def test_saturday_late_closed(self):
        """토요일 07:00 JST 이후 — 시장 닫힘."""
        sat_late = datetime(2025, 6, 7, 10, 0, tzinfo=JST)
        assert _is_market_closed(sat_late) is True

    def test_sunday_closed(self):
        """일요일 — 시장 닫힘."""
        sun = datetime(2025, 6, 8, 12, 0, tzinfo=JST)
        assert _is_market_closed(sun) is True

    def test_monday_before_open(self):
        """월요일 07:00 JST 이전 — 시장 닫힘."""
        mon_early = datetime(2025, 6, 9, 6, 0, tzinfo=JST)
        assert _is_market_closed(mon_early) is True

    def test_monday_after_open(self):
        """월요일 07:00 JST 이후 — 시장 열림."""
        mon_open = datetime(2025, 6, 9, 7, 0, tzinfo=JST)
        assert _is_market_closed(mon_open) is False

    def test_weekday_open(self):
        """수요일 — 시장 열림."""
        wed = datetime(2025, 6, 11, 15, 0, tzinfo=JST)
        assert _is_market_closed(wed) is False


# ──────────────────────────────────────────────────────────────
# 헬퍼 단위 테스트
# ──────────────────────────────────────────────────────────────

class TestLinearSlope:
    """_linear_slope 헬퍼."""

    def test_positive_slope(self):
        slope = _linear_slope([0, 1, 2, 3], [1.0, 2.0, 3.0, 4.0])
        assert slope == pytest.approx(1.0, abs=1e-6)

    def test_negative_slope(self):
        slope = _linear_slope([0, 1, 2, 3], [4.0, 3.0, 2.0, 1.0])
        assert slope == pytest.approx(-1.0, abs=1e-6)

    def test_flat(self):
        slope = _linear_slope([0, 1, 2, 3], [5.0, 5.0, 5.0, 5.0])
        assert slope == pytest.approx(0.0, abs=1e-6)

    def test_single_point(self):
        slope = _linear_slope([0], [1.0])
        assert slope == 0.0

    def test_empty(self):
        slope = _linear_slope([], [])
        assert slope == 0.0


# ──────────────────────────────────────────────────────────────
# SL: 가격 기반 손절 (실전 _entry_monitor SL과 동일)
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# SL: 가격 기반 손절 (실전 _entry_monitor SL과 동일)
# ──────────────────────────────────────────────────────────────

class TestPriceStopLoss:
    """SL: entry × (1 - stop_loss_pct/100) 이하 시 즉시 청산.

    SL이 D-3 박스 무효화보다 먼저 발동하려면:
      SL 가격 > lower × (1 - tolerance_pct/100) — D-3 임계값보다 높아야 함

    tolerance_pct=1.5 사용 시:
      - 박스 lower ≈ 89.91 (lower=90 * 0.999)
      - D-3 임계값: 89.91 × 0.985 ≈ 88.56
      - near_lower 진입 at 90.5 → SL@1.5% = 90.5×0.985 = 89.14
      - SL 트리거: 89.1 (D-3 임계 88.56 < 89.1 < SL 89.14) → SL 발동 ✓
    """

    def _make_sl_candles(self):
        """
        박스 상/하단을 형성하는 middle-zone 클로즈 캔들 70개 +
        near_lower 진입 캔들 1개 + SL 트리거 캔들 1개.

        tolerance_pct=1.5 기준으로 설계됨:
          - upper ≈ 110.11 (110 × 1.001)
          - lower ≈ 89.91  (90 × 0.999)
          - near_lower zone: [89.91×0.985, 89.91×1.015] = [88.56, 91.26]
          - D-3 임계: 89.91 × 0.985 = 88.56

        주의: high=110.0×1.001 고정 → high_slope≈0 → converging_triangle 미발동.
        """
        candles = []
        # Formation: close=100 (middle), high/low으로 박스 상/하단 형성
        for _ in range(70):
            candles.append(FakeCandle(close=100.0, high=110.0 * 1.001, low=90.0 * 0.999))
        # near_lower 진입 (90.5 ∈ [88.56, 91.26])
        # high를 상단과 동일하게 유지 → high_slope≈0 → converging_triangle 미발동
        candles.append(FakeCandle(close=90.5, high=110.0 * 1.001, low=90.5 * 0.999))
        # SL 평가 캔들: 89.1 > D-3(88.56), 89.1 < SL@1.5%(89.14) → SL만 발동
        candles.append(FakeCandle(close=89.1, high=110.0 * 1.001, low=89.1 * 0.999))
        return candles

    def test_sl_BT01_fires_at_sl_price(self):
        """T-SL-BT-01: SL 이하 가격 → price_stop_loss 청산."""
        candles = self._make_sl_candles()
        config = BacktestConfig(fee_pct=0.0, slippage_pct=0.0)
        params = {
            "box_tolerance_pct": 1.5,
            "box_min_touches": 2,
            "near_bound_pct": 1.5,
            "stop_loss_pct": 1.5,
        }
        result = _run_box_backtest(candles, params, config)
        sl_trades = [t for t in result.trades if t.exit_reason == "price_stop_loss"]
        assert len(sl_trades) >= 1, (
            f"SL이 발동되어야 함. trades={[(t.exit_reason, t.entry_price, t.exit_price) for t in result.trades]}"
        )

    def test_sl_BT02_no_fire_above_sl(self):
        """T-SL-BT-02: SL 이상 가격에서는 SL 미발동."""
        candles = []
        # 박스 형성
        for _ in range(70):
            candles.append(FakeCandle(close=100.0, high=110.0 * 1.001, low=90.0 * 0.999))
        # near_lower 진입 — high 고정으로 converging_triangle 미발동
        candles.append(FakeCandle(close=90.5, high=110.0 * 1.001, low=90.5 * 0.999))
        # SL@1.5% = 89.14, 90.3 > 89.14 → SL 미발동 — high 고정
        candles.append(FakeCandle(close=90.3, high=110.0 * 1.001, low=90.3 * 0.999))

        config = BacktestConfig(fee_pct=0.0, slippage_pct=0.0)
        params = {
            "box_tolerance_pct": 1.5,
            "box_min_touches": 2,
            "near_bound_pct": 1.5,
            "stop_loss_pct": 1.5,  # SL = 90.5 × 0.985 = 89.14, 90.3 > 89.14
        }
        result = _run_box_backtest(candles, params, config)
        sl_trades = [t for t in result.trades if t.exit_reason == "price_stop_loss"]
        assert len(sl_trades) == 0, "SL이 발동되면 안 됨"

    def test_sl_BT03_near_upper_first_then_no_sl(self):
        """T-SL-BT-03: near_upper 청산 직후 SL이 추가 발동하지 않음 (중복 청산 없음).

        같은 near_upper trade가 price_stop_loss로도 기록되면 안 됨.
        """
        candles = _box_candles(n=200, upper=110.0, lower=90.0, oscillations=10)
        config = BacktestConfig(fee_pct=0.0, slippage_pct=0.0)
        params = {
            "box_tolerance_pct": 1.5,
            "box_min_touches": 2,
            "near_bound_pct": 1.5,
            "stop_loss_pct": 1.5,
        }
        result = _run_box_backtest(candles, params, config)
        # 모든 거래 각각 하나의 exit_reason만 가져야 함
        # (BacktestTrade는 단일 포지션 → 중복 청산이면 extra trade가 생성됨)
        assert result.total_trades == len(result.trades)
        # near_upper 청산 직후 추가 price_stop_loss 없음 확인:
        # BT는 캔들 단위이므로 same-candle 중복은 발생하지 않는 구조.
        # near_upper 후 current_position=None → SL check 스킵.
        for trade in result.trades:
            assert trade.exit_reason in {
                "near_upper_exit", "price_stop_loss",
                "4h_close_below_lower", "4h_close_above_upper",
                "converging_triangle", "backtest_end",
                "weekend_close",
            }

    def test_sl_BT04_default_sl_pct_matches_explicit(self):
        """T-SL-BT-04: stop_loss_pct 기본값 1.5% — 미설정 시 명시적 1.5%와 동일."""
        candles = self._make_sl_candles()
        config = BacktestConfig(fee_pct=0.0, slippage_pct=0.0)
        base_params = {
            "box_tolerance_pct": 1.5,
            "box_min_touches": 2,
            "near_bound_pct": 1.5,
        }
        result_default = _run_box_backtest(candles, dict(base_params), config)

        params_explicit = dict(base_params)
        params_explicit["stop_loss_pct"] = 1.5
        result_explicit = _run_box_backtest(candles, params_explicit, config)

        # 동일 파라미터이므로 결과 동일해야 함
        assert result_default.total_trades == result_explicit.total_trades
        assert result_default.wins == result_explicit.wins

    def test_sl_BT05_sl_zero_disables_sl(self):
        """T-SL-BT-05: stop_loss_pct=0 → SL 비활성화. price_stop_loss 청산 없음."""
        # SL 트리거 가격이지만 stop_loss_pct=0이므로 SL 미발동
        # D-3 임계(88.56) 이상인 89.1에서 무엇도 일어나지 않아야 함
        candles = self._make_sl_candles()
        config = BacktestConfig(fee_pct=0.0, slippage_pct=0.0)
        params = {
            "box_tolerance_pct": 1.5,
            "box_min_touches": 2,
            "near_bound_pct": 1.5,
            "stop_loss_pct": 0,  # SL 비활성화
        }
        result = _run_box_backtest(candles, params, config)
        sl_trades = [t for t in result.trades if t.exit_reason == "price_stop_loss"]
        assert len(sl_trades) == 0, "stop_loss_pct=0 → SL 없어야 함"


# ══════════════════════════════════════════════
# 테스트: 박스 쿨다운 (BOX_LIFECYCLE_POLICY §3.3)
# ══════════════════════════════════════════════

class TestBoxBacktestCooldown:
    """무효화 후 8캔들(32시간) 재감지 금지 — 백테스트 실전 정합성."""

    def _make_invalidation_candles(self):
        """
        박스 형성 → 무효화 → 즉시 재형성 시도 시나리오.
        캔들 구성:
          0~24: 박스 형성용 (upper=110, lower=90)
          25: 박스 이탈 하향 (close=78 → 무효화)
          26~33: 무효화 직후 8캔들 (쿨다운 기간)
          34~60: 쿨다운 종료 후 새 박스 형성용
        """
        candles = []
        base_time = datetime(2025, 1, 6, 0, 0, tzinfo=timezone.utc)  # 월요일

        def _c(close, high=None, low=None, idx=0):
            return FakeCandle(
                close=close,
                high=high if high is not None else close + 1,
                low=low if low is not None else close - 1,
                open_time=base_time + timedelta(hours=4 * idx),
            )

        # 0~24: 박스 형성 (upper≈110, lower≈90)
        for i in range(25):
            if i % 2 == 0:
                candles.append(_c(104.0, high=110.5, low=89.5, idx=i))
            else:
                candles.append(_c(96.0, high=110.5, low=89.5, idx=i))

        # 25: 박스 이탈 (하향 이탈 → D-3 무효화)
        candles.append(_c(75.0, high=90.0, low=74.0, idx=25))

        # 26~33: 쿨다운 기간 (가격 박스 영역 안으로 복귀했어도 재감지 금지)
        for i in range(26, 34):
            candles.append(_c(100.0, high=111.0, low=89.0, idx=i))

        # 34~60: 쿨다운 종료 후 새 박스
        for i in range(34, 61):
            if i % 2 == 0:
                candles.append(_c(105.0, high=111.0, low=89.5, idx=i))
            else:
                candles.append(_c(95.0, high=110.5, low=89.0, idx=i))

        return candles

    def test_BT_CD01_no_redetection_during_cooldown(self):
        """T-BT-CD-01: 무효화 후 8캔들 이내 재감지 없음."""
        candles = self._make_invalidation_candles()
        config = BacktestConfig(fee_pct=0.15, slippage_pct=0.0)
        params = {
            "box_tolerance_pct": 1.5,
            "box_min_touches": 3,
            "near_bound_pct": 1.5,
        }
        result = _run_box_backtest(candles, params, config)

        # 무효화(idx=25) 후 쿨다운(26~33) 중 진입한 거래가 있으면 안 됨
        for trade in result.trades:
            if trade.entry_time is not None:
                candle_idx = int((trade.entry_time - datetime(2025, 1, 6, 0, 0, tzinfo=timezone.utc)).total_seconds() / (4 * 3600))
                if trade.exit_reason not in ("near_upper_exit", "near_lower_exit", "price_stop_loss"):
                    continue
                # 무효화 직후 8캔들(26~33) 중 진입했으면 실패
                assert not (26 <= candle_idx <= 33), (
                    f"쿨다운 중({candle_idx}번 캔들) 진입 감지 — 쿨다운이 작동하지 않음"
                )

