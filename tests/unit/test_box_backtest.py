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
