"""
T-1~T-6: Percentile-Cluster 하이브리드 박스 감지 테스트 (PERCENTILE_CLUSTER_DESIGN.md)
"""
from __future__ import annotations
import math, random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from core.analysis.box_detector import find_cluster, find_cluster_percentile, detect_box
from core.backtest.engine import BacktestConfig, run_backtest


# ─── 픽스처 ───────────────────────────────────────────────────────────────────

def _make_fx_prices(n=200, center=158.0, amp=1.5, seed=42):
    """FX 가격대 (USD_JPY 기준) 좁은 range."""
    rng = random.Random(seed)
    return [center + rng.uniform(-amp, amp) for _ in range(n)]


def _make_gbp_prices(n=200, center=210.0, amp=2.0, seed=7):
    rng = random.Random(seed)
    return [center + rng.uniform(-amp, amp) for _ in range(n)]


def _make_eur_prices(n=200, center=168.0, amp=1.8, seed=13):
    rng = random.Random(seed)
    return [center + rng.uniform(-amp, amp) for _ in range(n)]


@dataclass
class FakeCandle:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float


def _candles_from_prices(prices, seed=0):
    rng = random.Random(seed)
    candles = []
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i, p in enumerate(prices):
        spread = abs(p) * 0.002
        h = p + rng.uniform(0, spread)
        l = p - rng.uniform(0, spread)
        candles.append(FakeCandle(
            open_time=start + timedelta(hours=i * 4),
            open=p, high=h, low=l, close=p,
        ))
    return candles


# ─── T-1: percentile=100 → find_cluster와 완전 동일 ─────────────────────────

def test_t1_percentile_100_identical_to_find_cluster():
    """T-1: percentile=100은 find_cluster와 비트 단위 동일."""
    prices = _make_fx_prices(200)
    for mode in ("high", "low"):
        r1 = find_cluster(prices, 0.5, 3, mode)
        r2 = find_cluster_percentile(prices, 0.5, 3, mode, 100.0)
        assert r1 == r2, f"mode={mode}: {r1} != {r2}"

    # detect_box도 동일
    highs = [p + 0.1 for p in prices]
    lows = [p - 0.1 for p in prices]
    r1 = detect_box(highs, lows, 0.5, 3, 100.0)
    r2 = detect_box(highs, lows, 0.5, 3)  # 기본값 100
    assert r1 == r2


# ─── T-2: 3페어 × percentile {10,25,50,75,100} 감지 비교 ────────────────────

def test_t2_three_pairs_percentile_comparison(capsys):
    """T-2: USD/GBP/EUR × percentile {10,25,50,75,100} 박스 폭/터치 수 출력."""
    pairs = {
        "USD_JPY": _make_fx_prices(),
        "GBP_JPY": _make_gbp_prices(),
        "EUR_JPY": _make_eur_prices(),
    }
    percentiles = [10, 25, 50, 75, 100]
    tol = 0.3
    min_t = 3

    print("\n=== T-2: percentile × pair 박스 감지 결과 ===")
    print(f"{'pair':<12} {'pct':>5} {'upper':>12} {'lower':>12} {'width%':>8} {'u_cnt':>6} {'l_cnt':>6}")
    for pair_name, prices in pairs.items():
        highs = [p + abs(p) * 0.001 for p in prices]
        lows = [p - abs(p) * 0.001 for p in prices]
        for pct in percentiles:
            r = detect_box(highs, lows, tol, min_t, pct)
            if r.box_detected:
                print(
                    f"{pair_name:<12} {pct:>5} {r.upper_bound:>12.4f} {r.lower_bound:>12.4f}"
                    f" {r.width_pct:>8.4f} {r.upper_touch_count:>6} {r.lower_touch_count:>6}"
                )
            else:
                print(f"{pair_name:<12} {pct:>5} {'미형성':<40} ({r.reason})")

    # 검증: percentile=100은 항상 결과 반환되거나 동일 동작
    for pair_name, prices in pairs.items():
        highs = [p + abs(p) * 0.001 for p in prices]
        lows = [p - abs(p) * 0.001 for p in prices]
        r100 = detect_box(highs, lows, tol, min_t, 100)
        r_def = detect_box(highs, lows, tol, min_t)
        assert r100 == r_def, f"{pair_name}: percentile=100 결과가 기본값과 다름"


# ─── T-4: 실전-백테스트 동일 캔들 → 동일 박스 ───────────────────────────────

def test_t4_production_backtest_same_box():
    """T-4: detect_box(engine) vs find_cluster_percentile(manager) 동일 결과."""
    prices = _make_fx_prices(200)
    highs = [p + 0.05 for p in prices]
    lows = [p - 0.05 for p in prices]
    tol, min_t, pct = 0.3, 3, 50.0

    # 백테스트 경로 (detect_box)
    bt_result = detect_box(highs, lows, tol, min_t, pct)

    # 실전 경로 (find_cluster_percentile 직접)
    upper_prod, u_cnt = find_cluster_percentile(highs, tol, min_t, "high", pct)
    lower_prod, l_cnt = find_cluster_percentile(lows, tol, min_t, "low", pct)

    if bt_result.box_detected:
        assert bt_result.upper_bound == upper_prod
        assert bt_result.lower_bound == lower_prod
        assert bt_result.upper_touch_count == u_cnt
        assert bt_result.lower_touch_count == l_cnt
    else:
        assert upper_prod is None or lower_prod is None or upper_prod <= lower_prod


# ─── T-5: 경계값 (0, 1, 99) ─────────────────────────────────────────────────

def test_t5_boundary_percentiles():
    """T-5: 극단 percentile 에러 없이 처리."""
    prices = _make_fx_prices(200)
    highs = [p + 0.05 for p in prices]
    lows = [p - 0.05 for p in prices]

    for pct in (0.0, 1.0, 99.0):
        # 에러 없이 실행되어야 함
        r = detect_box(highs, lows, 0.3, 3, pct)
        assert r is not None

    # percentile=0 → n=max(1,...) → 1개만 사용 → 에러 없음
    center, cnt = find_cluster_percentile(prices, 0.5, 3, "high", 0.0)
    # 결과가 None이거나 값이면 됨 (에러만 없으면 OK)


# ─── T-6: BTC_JPY percentile=100 WF 회귀 ────────────────────────────────────

def test_t6_btc_wf_regression():
    """T-6: box_cluster_percentile 미설정 → 기본값 100 → 기존 결과 동일."""
    rng = random.Random(99)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles = []
    price = 11_000_000.0
    for i in range(500):
        delta = rng.uniform(-50000, 50000)
        o = price
        c = price + delta
        h = max(o, c) + rng.uniform(0, 30000)
        l = min(o, c) - rng.uniform(0, 30000)
        candles.append(FakeCandle(open_time=start + timedelta(hours=i*4),
                                   open=o, high=h, low=l, close=c))
        price = c

    config = BacktestConfig(initial_capital_jpy=100_000, slippage_pct=0.05, fee_pct=0.0)
    params_no_pct = {"tolerance_pct": 0.3, "box_lookback_candles": 40, "box_min_touches": 2,
                     "stop_loss_pct": 1.5, "take_profit_pct": 2.0}
    params_100 = {**params_no_pct, "box_cluster_percentile": 100.0}

    r1 = run_backtest(candles, params_no_pct, config, "box_mean_reversion")
    r2 = run_backtest(candles, params_100, config, "box_mean_reversion")

    assert r1.total_trades == r2.total_trades, (
        f"percentile=미설정({r1.total_trades}) vs percentile=100({r2.total_trades}) 다름"
    )
    assert r1.total_return_pct == r2.total_return_pct
