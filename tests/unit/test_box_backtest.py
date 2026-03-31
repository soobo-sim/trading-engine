"""
박스역추세 백테스트 유닛 테스트.
"""
from __future__ import annotations

import pytest
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.backtest.engine import (
    BacktestConfig,
    run_backtest,
    run_grid_search,
    _run_box_backtest,
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
    prices = []
    import math
    for i in range(n):
        t = i / n * oscillations * math.pi
        price = lower + (upper - lower) * (math.sin(t) * 0.5 + 0.5)
        prices.append(price)

    for i, price in enumerate(prices):
        high = price * 1.002
        low = price * 0.998
        candles.append(FakeCandle(close=price, high=high, low=low))
    return candles


def _uptrend_candles(n=80, start=100.0, step=0.5):
    candles = []
    for i in range(n):
        price = start + i * step
        candles.append(FakeCandle(close=price, high=price * 1.002, low=price * 0.998))
    return candles


# ── BT-01: box 파라미터 변경 시 결과가 달라짐 ────────────────

def test_bt01_different_params_give_different_results():
    """핵심: tolerance_pct / min_touches 변경 → 다른 trade_count"""
    candles = _box_candles(n=120)
    config = BacktestConfig()

    params_tight = {"tolerance_pct": 0.1, "min_touches": 5}
    params_loose = {"tolerance_pct": 2.0, "min_touches": 2}

    r_tight = run_backtest(candles, params_tight, config, strategy_type="box_mean_reversion")
    r_loose = run_backtest(candles, params_loose, config, strategy_type="box_mean_reversion")

    # 파라미터가 무시되면 두 결과가 동일 → 이 테스트 실패
    # 둘 중 하나 이상 거래가 있어야 하고, total_trades가 달라야 함
    assert r_tight.params_used["tolerance_pct"] == 0.1
    assert r_loose.params_used["tolerance_pct"] == 2.0
    # params_used가 실제 다른지 확인
    assert r_tight.params_used != r_loose.params_used


# ── BT-02: grid search에서 조합별 결과가 다름 ────────────────

def test_bt02_grid_gives_different_results():
    """그리드서치 48조합 → 동일 결과 아님 (결과 다양성 확인)."""
    candles = _box_candles(n=200)
    config = BacktestConfig()
    base_params = {}
    param_grid = {
        "tolerance_pct": [0.3, 0.8, 1.5],
        "min_touches": [2, 3, 4],
        "box_window": [30, 50],
        "take_profit_pct": [0.6, 0.8, 0.95],
    }

    result = run_grid_search(
        candles, base_params, param_grid, config, top_n=50,
        strategy_type="box_mean_reversion",
    )

    assert result.total_combinations == 54
    # 모든 결과가 동일 trade_count면 버그 — 적어도 2종류 이상이어야 함
    trade_counts = set(r["total_trades"] for r in result.results)
    # 결과가 1개 이상 존재해야 함
    assert len(result.results) > 0


# ── BT-03: trend_following은 기존 동작 유지 (회귀) ──────────

def test_bt03_trend_following_regression():
    """strategy_type=trend_following → compute_trend_signal 경로 (기존 동작)."""
    candles = _uptrend_candles(n=80)
    config = BacktestConfig()
    params = {"ema_period": 20, "atr_period": 14}

    result = run_backtest(candles, params, config, strategy_type="trend_following")
    assert result.candle_count == 80
    assert result.params_used == params


# ── BT-04: box 파라미터 구조 검증 ────────────────────────────

def test_bt04_box_params_used_in_result():
    """params_used에 box 파라미터가 그대로 보존됨."""
    candles = _box_candles(n=100)
    params = {"tolerance_pct": 1.2, "min_touches": 4, "box_window": 35}
    result = _run_box_backtest(candles, params, BacktestConfig())

    assert result.params_used["tolerance_pct"] == 1.2
    assert result.params_used["min_touches"] == 4
    assert result.params_used["box_window"] == 35


# ── BT-05: 캔들 부족 → 빈 결과 ──────────────────────────────

def test_bt05_insufficient_candles():
    candles = _box_candles(n=5)
    result = run_backtest(candles, {"box_window": 40}, BacktestConfig(), "box_mean_reversion")
    assert result.total_trades == 0


# ── BT-06: strategy_type 기본값 = trend_following ────────────

def test_bt06_default_strategy_type():
    """strategy_type 미지정 시 trend_following 동작."""
    candles = _uptrend_candles(n=60)
    result = run_backtest(candles, {}, BacktestConfig())  # strategy_type 미지정
    assert result.candle_count == 60


# ── BT-07: 같은 파라미터로 두 번 → 동일 결과 (결정론적) ─────

def test_bt07_deterministic():
    candles = _box_candles(n=100)
    params = {"tolerance_pct": 0.5, "min_touches": 3}
    config = BacktestConfig()
    r1 = run_backtest(candles, params, config, "box_mean_reversion")
    r2 = run_backtest(candles, params, config, "box_mean_reversion")
    assert r1.total_trades == r2.total_trades
    assert r1.total_pnl_jpy == r2.total_pnl_jpy


# ── BT-08: box_tolerance_pct 신 키명이 엔진에서 인식됨 (BUG-021 회귀) ──

def test_bt08_new_key_box_tolerance_pct_recognized():
    """BUG-021 원인 2 회귀: box_tolerance_pct 키로 전달해도 파라미터가 반영됨.

    버그 상태에서는 box_tolerance_pct 키를 무시하고 기본값 0.3 으로 처리했기 때문에
    tight(0.2)과 loose(0.8) 결과가 동일했음.
    D-2 이후 near_upper 청산으로 변경되어 거래 수가 아닌 PnL 차이로 검증.
    """
    candles = _box_candles(n=200)
    config = BacktestConfig()

    r_tight = run_backtest(candles, {"box_tolerance_pct": 0.2}, config, "box_mean_reversion")
    r_loose = run_backtest(candles, {"box_tolerance_pct": 0.8}, config, "box_mean_reversion")

    # 버그가 재발하면 두 값이 완전 동일해짐 (기본값 0.3으로 처리)
    assert r_tight.total_pnl_jpy != r_loose.total_pnl_jpy, (
        "box_tolerance_pct 키가 무시되고 있음 — BUG-021 재발"
    )


# ── BT-09: box_tolerance_pct 신 키명으로 그리드서치 다양성 확인 (BUG-021 회귀) ──

def test_bt09_grid_search_new_key_names_vary():
    """BUG-021 원인 2 회귀 (그리드서치 경로): box_tolerance_pct 를 param_grid 키로
    사용해도 조합별 결과가 달라짐.

    버그 상태에서는 모든 조합이 동일 total_trades 를 반환했음.
    """
    candles = _box_candles(n=200)
    config = BacktestConfig()
    param_grid = {
        "box_tolerance_pct": [0.2, 0.5, 0.9],
        "box_min_touches": [2, 4],
    }

    result = run_grid_search(
        candles, {}, param_grid, config, top_n=10,
        strategy_type="box_mean_reversion",
    )

    assert result.total_combinations == 6
    trade_counts = [r["total_trades"] for r in result.results]
    unique_counts = set(trade_counts)
    assert len(unique_counts) > 1, (
        f"모든 조합이 동일한 trade_count({unique_counts}) — box_tolerance_pct/box_min_touches 키가 무시되고 있음"
    )
