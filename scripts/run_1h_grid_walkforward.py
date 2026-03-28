"""
T-03 + T-04: GMO FX USD/JPY 1H 2단계 그리드 서치 + Walk-forward 비교.

실행: docker exec gmofx-trader python3 scripts/run_1h_grid_walkforward.py
(또는 bitflyer-trader 컨테이너에서도 가능 — DB 공유)

Phase:
  Stage 1: 핵심 3변수 (atr_stop, trail_init, ema_period) × 48조합 → top 5
  Stage 2: 부변수 (entry_rsi_max, slope_entry_min) × top5 × 9 = 45조합 → top 3
  Walk-forward: train 120일 / validation 60일, 4H vs 1H 단독 비교
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, and_

# ── 프로젝트 루트를 path에 추가 ──
sys.path.insert(0, "/app")

from adapters.database.session import create_db_engine, create_session_factory  # noqa: E402
from adapters.database.models import create_candle_model  # noqa: E402
from core.backtest.engine import (  # noqa: E402
    BacktestConfig,
    run_backtest,
    run_grid_search,
)

JST = timezone(timedelta(hours=9))

# ── 설정 ──────────────────────────────────────────────────
PAIR = "usd_jpy"
TOTAL_DAYS = 180
TRAIN_DAYS = 120
VALID_DAYS = 60
OVERFIT_THRESHOLD_PCT = 30  # 훈련↔검증 괴리 > 30% → 과적합

# GMO FX 기본 파라미터 (4H 기준 — BF v4 참고 + FX regime 임계값 보정)
BASE_PARAMS_4H = {
    "pair": PAIR,
    "trading_style": "trend_following",
    "basis_timeframe": "4h",
    "ema_period": 20,
    "entry_rsi_min": 40.0,
    "entry_rsi_max": 65.0,
    "atr_multiplier_stop": 2.0,
    "trailing_stop_atr_initial": 2.0,
    "trailing_stop_atr_mature": 1.2,
    "tighten_stop_atr": 1.0,
    "rsi_overbought": 75,
    "rsi_extreme": 80,
    "rsi_breakdown": 40,
    "ema_slope_weak_threshold": 0.05,
    "ema_slope_entry_min": -0.05,
    "position_size_pct": 100.0,
    # FX regime 임계값 (분포 분석: 1H BB P75=0.76%, range P75=1.47%)
    "bb_width_trending_min": 0.8,
    "range_pct_trending_min": 1.5,
    "bb_width_ranging_max": 0.35,
    "range_pct_ranging_max": 0.9,
}

# 1H 기본 파라미터 (4H와 동일 시작 — 그리드로 최적화)
BASE_PARAMS_1H = {**BASE_PARAMS_4H, "basis_timeframe": "1h"}

# Stage 1: 핵심 3변수
STAGE1_GRID = {
    "atr_multiplier_stop": [2.0, 2.5, 3.0, 3.5],
    "trailing_stop_atr_initial": [2.0, 2.5, 3.0, 3.5],
    "ema_period": [15, 20, 25],
}

# Stage 2: 부변수
STAGE2_GRID = {
    "entry_rsi_max": [60, 65, 70],
    "ema_slope_entry_min": [-0.05, 0.0, 0.05],
}

CONFIG = BacktestConfig(
    initial_capital_jpy=100_000.0,
    slippage_pct=0.05,
    fee_pct=0.0,  # GMO FX 수수료 무료 (스프레드에 포함)
)


# ── DB 캔들 로드 ──────────────────────────────────────────
GmoCandle = create_candle_model("gmo", pair_column="pair")


async def load_candles(timeframe: str, days: int, end_date=None):
    """DB에서 캔들 로드."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise ValueError("DATABASE_URL 환경변수 필수")
    engine = create_db_engine(database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        if end_date is None:
            end_date = datetime.now(JST)
        start_date = end_date - timedelta(days=days)
        stmt = (
            select(GmoCandle)
            .where(
                and_(
                    GmoCandle.pair == PAIR,
                    GmoCandle.timeframe == timeframe,
                    GmoCandle.is_complete == True,  # noqa: E712
                    GmoCandle.open_time >= start_date,
                    GmoCandle.open_time <= end_date,
                )
            )
            .order_by(GmoCandle.open_time)
        )
        result = await session.execute(stmt)
        candles = result.scalars().all()
    await engine.dispose()
    return candles


def split_candles(candles, train_ratio=0.667):
    """캔들을 train/validation으로 분할."""
    split_idx = int(len(candles) * train_ratio)
    return candles[:split_idx], candles[split_idx:]


def fmt(val, suffix=""):
    """포맷팅 헬퍼."""
    if val is None:
        return "N/A"
    return f"{val}{suffix}"


def result_row(label, r):
    """비교표 한 행."""
    return {
        "label": label,
        "trades": r.total_trades,
        "win_rate": r.win_rate,
        "total_return_pct": r.total_return_pct,
        "sharpe_ratio": r.sharpe_ratio,
        "max_drawdown_pct": r.max_drawdown_pct,
        "avg_holding_hours": r.avg_holding_hours,
        "total_pnl_jpy": r.total_pnl_jpy,
    }


def check_overfit(train_result, valid_result):
    """훈련↔검증 괴리 확인."""
    if train_result.sharpe_ratio is None or valid_result.sharpe_ratio is None:
        return None, False
    if train_result.sharpe_ratio == 0:
        return None, False
    gap_pct = abs(train_result.sharpe_ratio - valid_result.sharpe_ratio) / abs(train_result.sharpe_ratio) * 100
    return round(gap_pct, 1), gap_pct > OVERFIT_THRESHOLD_PCT


# ── 메인 실행 ─────────────────────────────────────────────
async def main():
    print("=" * 72)
    print("GMO FX USD/JPY — 1H 2단계 그리드 서치 + Walk-forward 비교")
    print(f"실행 시각: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")
    print("=" * 72)

    # ── 데이터 로드 ──
    print("\n[1] 데이터 로드...")
    candles_1h = await load_candles("1h", TOTAL_DAYS)
    candles_4h = await load_candles("4h", TOTAL_DAYS)
    print(f"  1H 캔들: {len(candles_1h)}개 ({candles_1h[0].open_time.date()} ~ {candles_1h[-1].open_time.date()})" if candles_1h else "  1H 캔들: 0개")
    print(f"  4H 캔들: {len(candles_4h)}개 ({candles_4h[0].open_time.date()} ~ {candles_4h[-1].open_time.date()})" if candles_4h else "  4H 캔들: 0개")

    if len(candles_1h) < 100:
        print("\n❌ 1H 캔들 부족 (최소 100개 필요). 중단.")
        return

    # ── Stage 1: 핵심 3변수 ──
    print(f"\n[2] Stage 1: 핵심 3변수 그리드 서치 ({4*4*3}=48 조합)...")
    stage1_result = run_grid_search(
        candles_1h, BASE_PARAMS_1H, STAGE1_GRID,
        config=CONFIG, top_n=5, strategy_type="trend_following",
    )
    print(f"  총 조합: {stage1_result.total_combinations}")
    print(f"  Best Sharpe: {stage1_result.best_sharpe}")
    print(f"  Best Params: {stage1_result.best_params}")
    print("\n  Top 5:")
    for i, r in enumerate(stage1_result.results[:5], 1):
        print(f"    #{i} Sharpe={fmt(r['sharpe_ratio'])} Return={fmt(r['total_return_pct'],'%')}"
              f" MDD={fmt(r['max_drawdown_pct'],'%')} Trades={r['total_trades']}"
              f" WR={fmt(r['win_rate'],'%')} | {r['params']}")

    if not stage1_result.results:
        print("\n❌ Stage 1 결과 없음. 중단.")
        return

    # ── Stage 2: 부변수 미세 조정 ──
    print(f"\n[3] Stage 2: 부변수 미세 조정 (top 5 × {3*3}=9 = 45 조합)...")
    stage2_all = []
    for rank, s1 in enumerate(stage1_result.results[:5], 1):
        s1_base = {**BASE_PARAMS_1H, **s1["params"]}
        s2_result = run_grid_search(
            candles_1h, s1_base, STAGE2_GRID,
            config=CONFIG, top_n=3, strategy_type="trend_following",
        )
        for r in s2_result.results[:3]:
            merged = {**s1["params"], **r["params"]}
            stage2_all.append({**r, "params": merged, "stage1_rank": rank})

    # Sharpe 기준 재정렬
    stage2_all.sort(key=lambda x: x["sharpe_ratio"] if x["sharpe_ratio"] is not None else -999, reverse=True)
    top3 = stage2_all[:3]

    print("\n  Final Top 3 (1H):")
    for i, r in enumerate(top3, 1):
        print(f"    #{i} Sharpe={fmt(r['sharpe_ratio'])} Return={fmt(r['total_return_pct'],'%')}"
              f" MDD={fmt(r['max_drawdown_pct'],'%')} Trades={r['total_trades']}"
              f" WR={fmt(r['win_rate'],'%')} | {r['params']}")

    if not top3:
        print("\n❌ Stage 2 결과 없음. 중단.")
        return

    # ── Walk-forward 비교 ──
    print(f"\n[4] Walk-forward 비교 (Train {TRAIN_DAYS}일 / Validation {VALID_DAYS}일)...")

    # 캔들 분할
    train_1h, valid_1h = split_candles(candles_1h)
    train_4h, valid_4h = split_candles(candles_4h)
    print(f"  1H Train: {len(train_1h)}개, Validation: {len(valid_1h)}개")
    print(f"  4H Train: {len(train_4h)}개, Validation: {len(valid_4h)}개")

    # 4H 베이스라인 (검증 기간)
    print("\n  [4a] 4H 베이스라인 (검증 기간)...")
    baseline_4h_valid = run_backtest(valid_4h, BASE_PARAMS_4H, CONFIG)
    baseline_4h_train = run_backtest(train_4h, BASE_PARAMS_4H, CONFIG)
    gap_4h, overfit_4h = check_overfit(baseline_4h_train, baseline_4h_valid)
    print(f"    4H Train:  Sharpe={fmt(baseline_4h_train.sharpe_ratio)} Return={fmt(baseline_4h_train.total_return_pct,'%')}")
    print(f"    4H Valid:  Sharpe={fmt(baseline_4h_valid.sharpe_ratio)} Return={fmt(baseline_4h_valid.total_return_pct,'%')}")
    print(f"    4H 괴리:   {fmt(gap_4h,'%')} {'⚠️ 과적합' if overfit_4h else '✅ OK'}")

    # 1H Top 3 각각 Walk-forward
    wf_results = []
    for i, top in enumerate(top3, 1):
        full_params = {**BASE_PARAMS_1H, **top["params"]}
        print(f"\n  [4b-{i}] 1H #{i} Walk-forward...")

        # 훈련 기간에서 그리드 서치 재실행 (in-sample)
        train_result = run_backtest(train_1h, full_params, CONFIG)
        # 검증 기간 (out-of-sample)
        valid_result = run_backtest(valid_1h, full_params, CONFIG)

        gap_pct, is_overfit = check_overfit(train_result, valid_result)

        print(f"    Train: Sharpe={fmt(train_result.sharpe_ratio)} Return={fmt(train_result.total_return_pct,'%')} Trades={train_result.total_trades}")
        print(f"    Valid: Sharpe={fmt(valid_result.sharpe_ratio)} Return={fmt(valid_result.total_return_pct,'%')} Trades={valid_result.total_trades}")
        print(f"    괴리:  {fmt(gap_pct,'%')} {'⚠️ 과적합 → 탈락' if is_overfit else '✅ OK'}")

        # Kill 조건: 일평균 거래 > 3
        if valid_result.total_trades > 0 and valid_1h:
            valid_days_actual = (valid_1h[-1].open_time - valid_1h[0].open_time).days or 1
            daily_avg = valid_result.total_trades / valid_days_actual
        else:
            daily_avg = 0

        kill_triggered = daily_avg > 3
        if kill_triggered:
            print(f"    ⚠️ Kill: 일평균 {daily_avg:.1f}거래 > 3 → 과매매")

        wf_results.append({
            "rank": i,
            "params": top["params"],
            "full_period": {
                "sharpe": top["sharpe_ratio"],
                "return_pct": top["total_return_pct"],
                "trades": top["total_trades"],
            },
            "train": result_row("train", train_result),
            "validation": result_row("validation", valid_result),
            "gap_pct": gap_pct,
            "is_overfit": is_overfit,
            "daily_avg_trades": round(daily_avg, 2),
            "kill_triggered": kill_triggered,
            "passed": not is_overfit and not kill_triggered,
        })

    # ── 비교표 출력 ──
    print("\n" + "=" * 72)
    print("최종 비교표")
    print("=" * 72)
    header = f"{'':20} {'Sharpe':>8} {'Return%':>9} {'MDD%':>7} {'Trades':>7} {'WR%':>6} {'Avg H':>7}"
    print(header)
    print("-" * 72)

    def print_row(label, r):
        print(f"{label:20} {fmt(r.sharpe_ratio):>8} {fmt(r.total_return_pct):>8}% "
              f"{fmt(r.max_drawdown_pct):>6}% {r.total_trades:>7} {fmt(r.win_rate):>5}% "
              f"{fmt(r.avg_holding_hours):>6}h")

    print_row("4H Baseline (valid)", baseline_4h_valid)
    for wf in wf_results:
        status = "✅" if wf["passed"] else "❌"
        label = f"1H #{wf['rank']} (valid) {status}"
        vr = wf["validation"]
        print(f"{label:20} {fmt(vr['sharpe_ratio']):>8} {fmt(vr['total_return_pct']):>8}% "
              f"{fmt(vr['max_drawdown_pct']):>6}% {vr['trades']:>7} {fmt(vr['win_rate']):>5}% "
              f"{fmt(vr['avg_holding_hours']):>6}h")

    # ── 판단 ──
    print("\n" + "-" * 72)
    passed = [w for w in wf_results if w["passed"]]
    if passed:
        best = max(passed, key=lambda w: w["validation"]["sharpe_ratio"] or -999)
        best_valid_sharpe = best["validation"]["sharpe_ratio"]
        baseline_sharpe = baseline_4h_valid.sharpe_ratio

        if best_valid_sharpe is not None and baseline_sharpe is not None:
            if best_valid_sharpe >= baseline_sharpe:
                print(f"✅ 판정: 1H #{best['rank']} 채택 추천 (검증 Sharpe {best_valid_sharpe} >= 4H {baseline_sharpe})")
            elif best_valid_sharpe >= baseline_sharpe * 0.9:
                print(f"✅ 판정: 1H #{best['rank']} 채택 추천 (검증 Sharpe 동등 + 거래빈도 이점)")
            else:
                print(f"⚠️ 판정: 4H 현행 유지 (1H 검증 Sharpe {best_valid_sharpe} < 4H {baseline_sharpe})")
        else:
            print("⚠️ 판정: Sharpe 비교 불가 (거래 수 부족). 데이터 축적 후 재검증 필요")

        print(f"\n  최적 1H 파라미터:")
        print(f"  {json.dumps(best['params'], indent=2)}")
    else:
        print("❌ 판정: 모든 1H 후보 탈락 (과적합 또는 과매매). 4H 현행 유지.")

    # ── JSON 결과 저장 ──
    output = {
        "timestamp": datetime.now(JST).isoformat(),
        "data": {
            "candles_1h": len(candles_1h),
            "candles_4h": len(candles_4h),
        },
        "stage1_top5": stage1_result.results[:5],
        "stage2_top3": [r for r in top3],
        "walkforward": {
            "baseline_4h": {
                "train": result_row("4h_train", baseline_4h_train),
                "validation": result_row("4h_valid", baseline_4h_valid),
                "gap_pct": gap_4h,
            },
            "candidates_1h": wf_results,
        },
        "verdict": "1h_recommended" if passed else "4h_maintain",
    }

    result_path = "/app/scripts/grid_walkforward_result.json"
    with open(result_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n결과 저장: {result_path}")


if __name__ == "__main__":
    asyncio.run(main())
