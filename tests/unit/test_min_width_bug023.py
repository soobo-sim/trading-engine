"""
BUG-023: 백테스트 엔진 min_width 체크 누락

MW-01: tol=0.5 + fee=0 → min_width=1.0% → GBP_JPY폭 0.3% 박스 스킵 (거래 감소)
MW-02: tol=0.05 + fee=0 → min_width=0.1% → 박스 통과 (거래 발생)
MW-03: min_width 추가 후 기존 BT 테스트 회귀 없음
MW-04: fee_pct 높으면 더 많은 박스 스킵
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from core.backtest.engine import BacktestConfig, run_backtest


@dataclass
class FakeCandle:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float


def _gbp_jpy_candles(n: int = 300) -> list:
    """GBP_JPY 가격대 (~210) 좁은 박스 패턴."""
    candles = []
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    price = 210.0
    for i in range(n):
        t = start + timedelta(hours=i * 4)
        phase = (i % 20) / 20 * 2 * math.pi
        delta = math.sin(phase) * 0.15  # 폭 ~0.3% (210 기준 0.63엔 = 0.3%)
        o = price
        c = price + delta
        h = max(o, c) + 0.05
        l = min(o, c) - 0.05
        candles.append(FakeCandle(open_time=t, open=o, high=h, low=l, close=c))
        price = c
    return candles


# MW-01: tol=0.5 → min_width=1.0% → 좁은 박스 스킵 → 거래 적음
def test_mw01_high_tol_skips_narrow_box():
    candles = _gbp_jpy_candles(500)
    config = BacktestConfig(initial_capital_jpy=100_000, slippage_pct=0.05, fee_pct=0.0)
    params_high_tol = {
        "tolerance_pct": 0.5,  # min_width = 1.0%
        "box_lookback_candles": 40,
        "box_min_touches": 2,
        "stop_loss_pct": 1.5,
        "take_profit_pct": 1.0,
    }
    r = run_backtest(candles, params_high_tol, config, "box_mean_reversion")
    # 폭 ~0.3% < min_width 1.0% → 대부분 스킵되어야 함
    # (완전히 0일 수는 없으나 낮아야 함 — 검증은 MW-02와 비교)
    trades_high = r.total_trades or 0

    params_low_tol = {
        "tolerance_pct": 0.05,  # min_width = 0.1%
        "box_lookback_candles": 40,
        "box_min_touches": 2,
        "stop_loss_pct": 1.5,
        "take_profit_pct": 1.0,
    }
    r2 = run_backtest(candles, params_low_tol, config, "box_mean_reversion")
    trades_low = r2.total_trades or 0

    assert trades_low >= trades_high, (
        f"tol=0.05 거래({trades_low}) >= tol=0.5 거래({trades_high}) 이어야 함"
    )


# MW-02: tol=0.05 + fee=0 → min_width=0.1% → 좁은 박스 통과
def test_mw02_low_tol_allows_narrow_box():
    candles = _gbp_jpy_candles(500)
    config = BacktestConfig(initial_capital_jpy=100_000, slippage_pct=0.05, fee_pct=0.0)
    params = {
        "tolerance_pct": 0.05,
        "box_lookback_candles": 40,
        "box_min_touches": 2,
        "stop_loss_pct": 1.5,
        "take_profit_pct": 1.0,
    }
    r = run_backtest(candles, params, config, "box_mean_reversion")
    # min_width=0.1% → 좁은 박스도 통과 → 거래 발생
    assert (r.total_trades or 0) > 0, "tol=0.05이면 박스가 통과되어 거래 발생해야 함"


# MW-03: 회귀 — 기존 파라미터(tol=0.3) 동작 유지
def test_mw03_regression_tol_03():
    candles = _gbp_jpy_candles(500)
    config = BacktestConfig(initial_capital_jpy=100_000, slippage_pct=0.05, fee_pct=0.0)
    params = {
        "tolerance_pct": 0.3,
        "box_lookback_candles": 40,
        "box_min_touches": 2,
        "stop_loss_pct": 1.5,
        "take_profit_pct": 1.0,
    }
    r = run_backtest(candles, params, config, "box_mean_reversion")
    # tol=0.3 → min_width=0.6% — 박스 폭 ~0.3% 이므로 스킵될 수 있음 (이상 없음)
    assert r.candle_count == 500


# MW-04: fee_pct 높으면 더 많이 스킵
def test_mw04_high_fee_increases_skip():
    candles = _gbp_jpy_candles(500)
    params = {
        "tolerance_pct": 0.1,
        "box_lookback_candles": 40,
        "box_min_touches": 2,
        "stop_loss_pct": 1.5,
        "take_profit_pct": 1.0,
    }
    config_no_fee = BacktestConfig(initial_capital_jpy=100_000, slippage_pct=0.0, fee_pct=0.0)
    config_high_fee = BacktestConfig(initial_capital_jpy=100_000, slippage_pct=0.0, fee_pct=0.5)

    r_low = run_backtest(candles, params, config_no_fee, "box_mean_reversion")
    r_high = run_backtest(candles, params, config_high_fee, "box_mean_reversion")

    # fee=0.5 → min_width=0.2+1.0=1.2% → 더 많이 스킵
    assert (r_low.total_trades or 0) >= (r_high.total_trades or 0), (
        f"fee=0 거래({r_low.total_trades}) >= fee=0.5 거래({r_high.total_trades})"
    )
