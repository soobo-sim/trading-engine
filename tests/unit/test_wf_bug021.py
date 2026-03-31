"""
BUG-021 테스트: 박스역추세 WF + params 미반영.

WF-01: run_walk_forward 기본 동작 — windows 배열 반환
WF-02: box_mean_reversion WF — windows 반환
WF-03: tolerance 0.2 vs 0.6 결과 상이 (버그 A)
WF-04: SL 1.0% vs 2.0% 결과 상이
WF-05: 캔들 부족 → fail_reason 반환
WF-06: 윈도우 부족 → fail_reason 반환
WF-07: pass_fail 기준 — 양수 윈도우 ≥ 60% + 수익률 > 0 + 거래 ≥ 30
WF-08: strategy_type=trend_following WF 동작
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from core.backtest.engine import BacktestConfig, run_backtest
from core.backtest.walk_forward import (
    WFResult,
    run_walk_forward,
    WF_PASS_MIN_TRADES,
)


# ─── 공통 캔들 fixture ────────────────────────────────────────

@dataclass
class FakeCandle:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float


def _make_candles(n: int, base_price: float = 150.0, step_hours: int = 4) -> list:
    """n개 4H 캔들 생성 (단순 사인 패턴으로 박스 형성)."""
    candles = []
    start = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    price = base_price
    for i in range(n):
        t = start + timedelta(hours=i * step_hours)
        # 단순 사인 패턴으로 박스 구간 형성
        phase = (i % 20) / 20 * 2 * math.pi
        delta = math.sin(phase) * 0.5
        o = price
        c = price + delta
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        candles.append(FakeCandle(open_time=t, open=o, high=h, low=l, close=c))
        price = c
    return candles


def _make_trend_candles(n: int, step_hours: int = 4) -> list:
    """n개 4H 캔들 (강한 상승 추세)."""
    candles = []
    start = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    price = 10_000_000.0  # BTC 가격대
    for i in range(n):
        t = start + timedelta(hours=i * step_hours)
        # 점진적 상승
        price *= 1.0005
        delta = price * 0.002
        o = price
        c = price + delta * (1 if i % 3 != 0 else -0.5)
        h = max(o, c) + price * 0.001
        l = min(o, c) - price * 0.001
        candles.append(FakeCandle(open_time=t, open=o, high=h, low=l, close=c))
        price = c
    return candles


# ─── WF-01: 기본 동작 — windows 반환 ─────────────────────────
def test_wf01_returns_windows():
    """WF 실행 시 windows 배열 반환."""
    candles = _make_candles(1500)  # 250일
    params = {"tolerance_pct": 0.3, "box_lookback_candles": 30, "box_min_touches": 2,
              "stop_loss_pct": 1.5, "take_profit_pct": 1.0}
    result = run_walk_forward(
        candles, params, "box_mean_reversion",
        train_days=60, valid_days=30, step_days=15, min_windows=2,
    )
    assert isinstance(result.windows, list)
    assert len(result.windows) >= 2, f"윈도우 수: {len(result.windows)}, fail: {result.fail_reason}"
    # 각 윈도우에 필드 존재
    w = result.windows[0]
    assert w.oos_start < w.oos_end
    assert w.index == 1


# ─── WF-02: box_mean_reversion WF windows 있음 ───────────────
def test_wf02_box_wf_has_windows():
    candles = _make_candles(1800)  # 300일
    params = {"tolerance_pct": 0.3, "box_lookback_candles": 40, "box_min_touches": 2,
              "near_bound_pct": 1.0, "stop_loss_pct": 1.5, "take_profit_pct": 1.0}
    result = run_walk_forward(
        candles, params, "box_mean_reversion",
        train_days=90, valid_days=30, step_days=30, min_windows=2,
    )
    assert result.total_windows >= 2, f"fail: {result.fail_reason}"
    assert result.fail_reason != "캔들 없음"


# ─── WF-03: tolerance 0.2 vs 0.6 결과 상이 (버그 A 검증) ─────
def test_wf03_tolerance_affects_trades():
    """tolerance 변경 시 거래 수 달라야 함."""
    candles = _make_candles(500)
    base = {"box_lookback_candles": 40, "box_min_touches": 2,
            "stop_loss_pct": 1.5, "take_profit_pct": 1.0}

    config = BacktestConfig(initial_capital_jpy=100_000, slippage_pct=0.05, fee_pct=0.0)
    r1 = run_backtest(candles, {**base, "tolerance_pct": 0.2}, config, "box_mean_reversion")
    r2 = run_backtest(candles, {**base, "tolerance_pct": 0.6}, config, "box_mean_reversion")
    # tolerance가 다르면 거래수 또는 수익이 달라야 함
    assert (r1.total_trades, r1.total_pnl_jpy) != (r2.total_trades, r2.total_pnl_jpy), \
        f"tolerance 0.2={r1.total_trades}거래 vs 0.6={r2.total_trades}거래 — 같으면 버그"


# ─── WF-04: near_bound_pct 차이 → 결과 상이 ─────────────────
def test_wf04_near_bound_affects_result():
    """near_bound_pct가 다르면 trades 또는 pnl이 달라야 함 (D-2: SL/TP 제거됨)."""
    candles = _make_candles(500)
    base = {"tolerance_pct": 0.3, "box_lookback_candles": 40, "box_min_touches": 2}
    config = BacktestConfig(initial_capital_jpy=100_000, slippage_pct=0.05, fee_pct=0.0)
    # near_bound_pct가 다르면 진입/청산 밴드가 달라짐
    r1 = run_backtest(candles, {**base, "near_bound_pct": 0.1},
                      config, "box_mean_reversion")
    r2 = run_backtest(candles, {**base, "near_bound_pct": 2.0},
                      config, "box_mean_reversion")
    # 밴드 폭이 0.1% vs 2.0%이면 결과가 달라야 함
    assert (r1.total_trades, r1.total_pnl_jpy) != (r2.total_trades, r2.total_pnl_jpy), \
        f"near_bound 0.1={r1.total_trades}거래/{r1.total_pnl_jpy} vs 2.0={r2.total_trades}거래/{r2.total_pnl_jpy}"


# ─── WF-05: 캔들 부족 → fail_reason ─────────────────────────
def test_wf05_insufficient_candles_fail():
    candles = _make_candles(10)
    params = {"tolerance_pct": 0.3}
    result = run_walk_forward(
        candles, params, "box_mean_reversion",
        train_days=240, valid_days=60, step_days=30,
    )
    assert result.pass_fail is False
    assert result.fail_reason != ""


# ─── WF-06: 윈도우 부족 → fail_reason ───────────────────────
def test_wf06_insufficient_windows_fail():
    # 딱 1개 윈도우만 만들어질 만한 캔들 (90+30일 = 120일 = 720 4H 캔들)
    candles = _make_candles(720)
    params = {"tolerance_pct": 0.3}
    result = run_walk_forward(
        candles, params, "box_mean_reversion",
        train_days=90, valid_days=30, step_days=30,
        min_windows=5,  # 5개 요구
    )
    # 5개 윈도우가 나올 수 없으면 fail
    if result.total_windows < 5:
        assert result.pass_fail is False


# ─── WF-07: pass_fail 판정 ────────────────────────────────────
def test_wf07_pass_fail_criteria():
    """pass_fail=True이면 조건 충족 확인."""
    candles = _make_candles(600)
    params = {"tolerance_pct": 0.3, "box_lookback_candles": 30, "box_min_touches": 2,
              "near_bound_pct": 1.0}
    result = run_walk_forward(
        candles, params, "box_mean_reversion",
        train_days=60, valid_days=30, step_days=15, min_windows=2,
    )
    if result.pass_fail:
        assert result.total_windows > 0
        ratio = result.positive_windows / result.total_windows
        assert ratio >= 0.6
        assert result.total_return_pct > 0
        assert result.total_trades >= WF_PASS_MIN_TRADES
    # fail이어도 windows는 반환됨
    assert isinstance(result.windows, list)


# ─── WF-08: trend_following WF 동작 ─────────────────────────
def test_wf08_trend_following_wf():
    candles = _make_trend_candles(500)
    params = {"ema_period": 20, "position_size_pct": 100,
              "atr_multiplier_stop": 2.0, "trailing_stop_atr_initial": 2.0}
    result = run_walk_forward(
        candles, params, "trend_following",
        train_days=60, valid_days=30, step_days=15, min_windows=2,
    )
    assert isinstance(result.windows, list)
    # trend_following WF도 windows 반환
    if result.total_windows >= 2:
        assert result.windows[0].index == 1
