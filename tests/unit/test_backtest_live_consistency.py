"""
A-3: 백테스트-실전 판정 로직 일치 검증 (CI 자동 테스트).

두 코드 경로가 동일한 순수 함수를 공유하는지 확인하고,
같은 입력에 동일한 판정 결과를 내는지 검증한다.
"""
import pytest
from core.strategy.box_signals import (
    classify_price_in_box,
    check_box_invalidation,
    linear_slope,
)


# ──────────────────────────────────────────────────────────────
# 1. 동일 함수 참조 검증 (identity check)
# ──────────────────────────────────────────────────────────────

class TestSharedFunctionIdentity:
    """백테스트 engine와 실전 BoxManager가 같은 함수를 import하는지 확인."""

    def test_classify_price_identity(self):
        from core.backtest import engine as bt
        from core.strategy.plugins.box_mean_reversion import manager as live

        assert bt.classify_price_in_box is classify_price_in_box
        assert live.classify_price_in_box is classify_price_in_box

    def test_check_box_invalidation_identity(self):
        from core.backtest import engine as bt

        assert bt.check_box_invalidation is check_box_invalidation

    def test_linear_slope_identity(self):
        from core.backtest import engine as bt
        from core.strategy.plugins.box_mean_reversion import manager as live

        assert bt.linear_slope is linear_slope
        assert live.linear_slope is linear_slope

    def test_session_guards_identity(self):
        """FX 주말 판정도 동일 함수인지 확인."""
        from core.exchange.session import should_close_for_weekend, is_fx_market_open
        from core.backtest import engine as bt

        assert bt.should_close_for_weekend is should_close_for_weekend
        assert bt.is_fx_market_open is is_fx_market_open


# ──────────────────────────────────────────────────────────────
# 2. 진입/청산 판정 시나리오 — 동일 가격 시퀀스 → 동일 결과
# ──────────────────────────────────────────────────────────────

BOX_UPPER = 150.0
BOX_LOWER = 148.0
NEAR_PCT = 0.3
TOL_PCT = 0.3


class TestBoxEntryExitConsistency:
    """
    classify_price_in_box + prev_state 전이 로직이
    백테스트·실전 양쪽에서 동일한 진입/청산 판정을 내는지 검증.
    """

    @staticmethod
    def _simulate_state_transitions(prices: list[float]) -> list[dict]:
        """
        순수 함수만으로 진입/청산 판정을 시뮬레이션.
        백테스트 _run_box_backtest의 D-1/D-2 판정 로직과 동형.
        """
        prev_state = None
        has_position = False
        events = []

        for price in prices:
            box_state = classify_price_in_box(price, BOX_UPPER, BOX_LOWER, NEAR_PCT)

            if not has_position and box_state == "near_lower" and prev_state != "near_lower":
                events.append({"type": "entry", "price": price, "state": box_state})
                has_position = True
            elif has_position and box_state == "near_upper" and prev_state != "near_upper":
                events.append({"type": "exit", "price": price, "state": box_state})
                has_position = False

            prev_state = box_state

        return events

    def test_basic_entry_exit_cycle(self):
        prices = [149.0, 148.1, 148.0, 149.0, 149.9, 150.0]
        events = self._simulate_state_transitions(prices)

        assert len(events) == 2
        assert events[0]["type"] == "entry"
        assert events[0]["state"] == "near_lower"
        assert events[1]["type"] == "exit"
        assert events[1]["state"] == "near_upper"

    def test_no_entry_when_staying_near_lower(self):
        """near_lower에 이미 있었으면 재진입하지 않는다 (prev_state 체크)."""
        prices = [148.0, 148.05, 148.1]  # 모두 near_lower
        events = self._simulate_state_transitions(prices)

        assert len(events) == 1  # 첫 번째만 entry

    def test_middle_zone_no_action(self):
        prices = [149.0, 149.1, 149.2]  # 모두 middle
        events = self._simulate_state_transitions(prices)
        assert len(events) == 0

    def test_outside_zone_no_action(self):
        prices = [147.0, 151.0, 147.5]  # 모두 outside
        events = self._simulate_state_transitions(prices)
        assert len(events) == 0

    def test_reentry_after_return_to_middle(self):
        """middle → near_lower → middle → near_lower → entry 재발생."""
        prices = [
            149.0,  # middle → prev=middle
            148.0,  # near_lower → entry
            149.0,  # middle (exit 아님 - 포지션 보유 중)
            149.9,  # near_upper → exit
            149.0,  # middle
            148.0,  # near_lower → reentry
        ]
        events = self._simulate_state_transitions(prices)
        assert len(events) == 3  # entry, exit, reentry (exit 없이 시퀀스 종료)
        assert events[0]["type"] == "entry"
        assert events[1]["type"] == "exit"
        assert events[2]["type"] == "entry"


# ──────────────────────────────────────────────────────────────
# 3. box_invalidation 일관성
# ──────────────────────────────────────────────────────────────

class TestBoxInvalidationConsistency:
    """check_box_invalidation이 4H 종가 이탈/수렴 삼각형을 올바르게 감지."""

    def test_close_below_lower_invalidates(self):
        close = BOX_LOWER * 0.995  # tolerance 이하
        result = check_box_invalidation(
            close=close,
            candle_highs=[BOX_UPPER] * 10,
            candle_lows=[BOX_LOWER] * 10,
            upper=BOX_UPPER,
            lower=BOX_LOWER,
            tolerance_pct=TOL_PCT,
        )
        assert result == "4h_close_below_lower"

    def test_close_above_upper_invalidates(self):
        close = BOX_UPPER * 1.005
        result = check_box_invalidation(
            close=close,
            candle_highs=[BOX_UPPER] * 10,
            candle_lows=[BOX_LOWER] * 10,
            upper=BOX_UPPER,
            lower=BOX_LOWER,
            tolerance_pct=TOL_PCT,
        )
        assert result == "4h_close_above_upper"

    def test_close_within_box_no_invalidation(self):
        close = 149.0
        result = check_box_invalidation(
            close=close,
            candle_highs=[BOX_UPPER] * 10,
            candle_lows=[BOX_LOWER] * 10,
            upper=BOX_UPPER,
            lower=BOX_LOWER,
            tolerance_pct=TOL_PCT,
        )
        assert result is None

    def test_converging_triangle_invalidates(self):
        n = 20
        highs = [150.0 - i * 0.05 for i in range(n)]  # 하락
        lows = [148.0 + i * 0.03 for i in range(n)]   # 상승 → 수렴
        close = 149.0

        result = check_box_invalidation(
            close=close,
            candle_highs=highs,
            candle_lows=lows,
            upper=BOX_UPPER,
            lower=BOX_LOWER,
            tolerance_pct=TOL_PCT,
        )
        assert result == "converging_triangle"

    def test_diverging_no_invalidation(self):
        n = 20
        highs = [150.0 + i * 0.05 for i in range(n)]  # 발산
        lows = [148.0 - i * 0.03 for i in range(n)]
        close = 149.0

        result = check_box_invalidation(
            close=close,
            candle_highs=highs,
            candle_lows=lows,
            upper=BOX_UPPER,
            lower=BOX_LOWER,
            tolerance_pct=TOL_PCT,
        )
        assert result is None


# ──────────────────────────────────────────────────────────────
# 4. linear_slope 일관성
# ──────────────────────────────────────────────────────────────

class TestLinearSlopeConsistency:
    def test_flat(self):
        assert linear_slope([0, 1, 2], [5.0, 5.0, 5.0]) == 0.0

    def test_positive_slope(self):
        assert linear_slope([0, 1, 2], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_negative_slope(self):
        assert linear_slope([0, 1, 2], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)

    def test_single_point(self):
        assert linear_slope([0], [5.0]) == 0.0
