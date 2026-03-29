"""
GMO FX USD/JPY 4H 그리드 서치 + Rolling Walk-forward 파라미터 최적화.

실행: docker exec bitflyer-trader python3 scripts/run_4h_grid_walkforward.py
(또는 gmofx-trader 컨테이너 — DB 공유)

목적:
  4H 타임프레임에서 최적 파라미터를 찾되, Rolling Walk-forward로
  과적합 검증하여 실전 적합 파라미터를 확정한다.

Phase:
  Stage 1: 핵심 4변수 (atr_stop, trail_init, trail_mature, ema_period)
  Stage 2: 부변수 (entry_rsi_max, slope_entry_min, tighten_stop_atr)
  Walk-forward: Rolling window — 240일 훈련 / 60일 검증, 30일 스텝
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from itertools import product

from sqlalchemy import select, and_

# ── 프로젝트 루트를 path에 추가 ──
sys.path.insert(0, "/app")

from adapters.database.session import create_db_engine, create_session_factory  # noqa: E402
from adapters.database.models import create_candle_model  # noqa: E402
from core.backtest.engine import (  # noqa: E402
    BacktestConfig,
    BacktestResult,
    run_backtest,
    run_grid_search,
)

JST = timezone(timedelta(hours=9))

# ── 설정 ──────────────────────────────────────────────────
PAIR = "usd_jpy"
TOTAL_DAYS = 450  # 15개월치 전부 사용
TIMEFRAME = "4h"

# Rolling Walk-forward 설정
TRAIN_DAYS = 240   # 훈련 윈도우  (~8개월)
VALID_DAYS = 60    # 검증 윈도우  (~2개월)
STEP_DAYS = 30     # 윈도우 이동 폭 (~1개월)
OVERFIT_THRESHOLD_PCT = 40  # 훈련↔검증 Sharpe 괴리 허용선

# GMO FX 4H 기본 파라미터 (FX regime 임계값 + ema_slope 보정 적용)
BASE_PARAMS = {
    "pair": PAIR,
    "trading_style": "trend_following",
    "basis_timeframe": TIMEFRAME,
    "ema_period": 20,
    "atr_period": 14,
    "rsi_period": 14,
    "entry_rsi_min": 40.0,
    "entry_rsi_max": 65.0,
    "atr_multiplier_stop": 2.5,
    "trailing_stop_atr_initial": 2.5,
    "trailing_stop_atr_mature": 1.5,
    "tighten_stop_atr": 1.2,
    "rsi_overbought": 75,
    "rsi_extreme": 80,
    "rsi_breakdown": 40,
    "ema_slope_weak_threshold": 0.005,   # FX 전용 (BTC는 0.03)
    "ema_slope_entry_min": -0.03,
    "position_size_pct": 100.0,
    # FX regime 임계값
    "bb_width_trending_min": 0.8,
    "range_pct_trending_min": 1.5,
    "bb_width_ranging_max": 0.35,
    "range_pct_ranging_max": 0.9,
}

# Stage 1: 핵심 4변수 — 4 × 4 × 3 × 3 = 144 조합
STAGE1_GRID = {
    "atr_multiplier_stop": [1.5, 2.0, 2.5, 3.0],
    "trailing_stop_atr_initial": [1.5, 2.0, 2.5, 3.0],
    "trailing_stop_atr_mature": [0.8, 1.0, 1.2],
    "ema_period": [15, 20, 25],
}

# Stage 2: 부변수 — 3 × 3 × 3 = 27 조합 (× top5 = 135)
STAGE2_GRID = {
    "entry_rsi_max": [60, 65, 70],
    "ema_slope_entry_min": [-0.05, -0.03, 0.0],
    "tighten_stop_atr": [0.8, 1.0, 1.2],
}

CONFIG = BacktestConfig(
    initial_capital_jpy=100_000.0,
    slippage_pct=0.05,
    fee_pct=0.0,  # GMO FX 수수료 무료 (스프레드에 포함)
)


# ── DB 캔들 로드 ──────────────────────────────────────────
GmoCandle = create_candle_model("gmo", pair_column="pair")


async def load_candles(days: int, end_date=None):
    """DB에서 4H 캔들 로드."""
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
                    GmoCandle.timeframe == TIMEFRAME,
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


def split_by_date(candles, train_start, train_end, valid_end):
    """날짜 기준으로 캔들을 train/validation 분할."""
    train = [c for c in candles if train_start <= c.open_time < train_end]
    valid = [c for c in candles if train_end <= c.open_time < valid_end]
    return train, valid


def fmt(val, suffix=""):
    """포맷팅 헬퍼."""
    if val is None:
        return "N/A"
    if isinstance(val, float):
        return f"{val:.2f}{suffix}"
    return f"{val}{suffix}"


def check_overfit(train_result, valid_result):
    """훈련↔검증 Sharpe 괴리 확인."""
    if train_result.sharpe_ratio is None or valid_result.sharpe_ratio is None:
        return None, False
    if train_result.sharpe_ratio == 0:
        return None, False
    gap_pct = abs(train_result.sharpe_ratio - valid_result.sharpe_ratio) / abs(train_result.sharpe_ratio) * 100
    return round(gap_pct, 1), gap_pct > OVERFIT_THRESHOLD_PCT


def aggregate_walkforward(wf_validations):
    """Walk-forward 검증 기간들의 성과를 합산."""
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_pnl = 0.0
    all_returns = []
    max_dd = 0.0

    for v in wf_validations:
        total_trades += v["trades"]
        total_wins += v["wins"]
        total_losses += v["losses"]
        total_pnl += v["pnl_jpy"]
        if v["return_pct"] is not None:
            all_returns.append(v["return_pct"])
        if v["max_dd_pct"] is not None and v["max_dd_pct"] > max_dd:
            max_dd = v["max_dd_pct"]

    win_rate = (total_wins / total_trades * 100) if total_trades > 0 else None
    avg_return = sum(all_returns) / len(all_returns) if all_returns else None
    cum_return = 1.0
    for r in all_returns:
        cum_return *= (1 + r / 100)
    cum_return_pct = (cum_return - 1) * 100

    return {
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "win_rate": round(win_rate, 1) if win_rate is not None else None,
        "total_pnl_jpy": round(total_pnl),
        "avg_return_per_window_pct": round(avg_return, 2) if avg_return is not None else None,
        "cumulative_return_pct": round(cum_return_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "windows_count": len(wf_validations),
    }


# ── 메인 실행 ─────────────────────────────────────────────
async def main():
    print("=" * 80)
    print("GMO FX USD/JPY — 4H Rolling Walk-forward 그리드 서치 파라미터 최적화")
    print(f"실행 시각: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")
    print("=" * 80)

    # ── 1. 데이터 로드 ──
    print("\n[1] 데이터 로드...")
    all_candles = await load_candles(TOTAL_DAYS)
    print(f"  4H 캔들: {len(all_candles)}개 ({all_candles[0].open_time.date()} ~ {all_candles[-1].open_time.date()})" if all_candles else "  4H 캔들: 0개")

    if len(all_candles) < 200:
        print("\n❌ 캔들 부족 (최소 200개 필요). 중단.")
        return

    # ── 2. Stage 1: 핵심 4변수 그리드 서치 (전체 데이터) ──
    total_combos = 1
    for vals in STAGE1_GRID.values():
        total_combos *= len(vals)
    print(f"\n[2] Stage 1: 핵심 4변수 그리드 서치 ({total_combos} 조합, 전체 데이터)...")

    stage1_result = run_grid_search(
        all_candles, BASE_PARAMS, STAGE1_GRID,
        config=CONFIG, top_n=10, strategy_type="trend_following",
    )
    print(f"  총 조합: {stage1_result.total_combinations}")
    print(f"  Best Sharpe: {fmt(stage1_result.best_sharpe)}")

    print("\n  Top 10:")
    for i, r in enumerate(stage1_result.results[:10], 1):
        print(f"    #{i:2d} Sharpe={fmt(r['sharpe_ratio']):>7} Return={fmt(r['total_return_pct']):>7}%"
              f" MDD={fmt(r['max_drawdown_pct']):>6}% Trades={r['total_trades']:>4}"
              f" WR={fmt(r['win_rate']):>5}% | {r['params']}")

    if not stage1_result.results:
        print("\n❌ Stage 1 결과 없음. 중단.")
        return

    # ── 3. Stage 2: 부변수 미세 조정 (top 5 × 27 = 135 조합) ──
    stage2_combos = 1
    for vals in STAGE2_GRID.values():
        stage2_combos *= len(vals)
    print(f"\n[3] Stage 2: 부변수 미세 조정 (top 5 × {stage2_combos} = {5 * stage2_combos} 조합)...")

    stage2_all = []
    for rank, s1 in enumerate(stage1_result.results[:5], 1):
        s1_base = {**BASE_PARAMS, **s1["params"]}
        s2_result = run_grid_search(
            all_candles, s1_base, STAGE2_GRID,
            config=CONFIG, top_n=5, strategy_type="trend_following",
        )
        for r in s2_result.results[:5]:
            merged = {**s1["params"], **r["params"]}
            stage2_all.append({**r, "params": merged, "stage1_rank": rank})

    stage2_all.sort(
        key=lambda x: x["sharpe_ratio"] if x["sharpe_ratio"] is not None else -999,
        reverse=True,
    )
    top5 = stage2_all[:5]

    print("\n  Final Top 5 (전체 데이터):")
    for i, r in enumerate(top5, 1):
        print(f"    #{i} Sharpe={fmt(r['sharpe_ratio']):>7} Return={fmt(r['total_return_pct']):>7}%"
              f" MDD={fmt(r['max_drawdown_pct']):>6}% Trades={r['total_trades']:>4}"
              f" WR={fmt(r['win_rate']):>5}% | {r['params']}")

    if not top5:
        print("\n❌ Stage 2 결과 없음. 중단.")
        return

    # ── 4. Rolling Walk-forward 검증 ──
    data_start = all_candles[0].open_time
    data_end = all_candles[-1].open_time

    # 윈도우 계산
    windows = []
    cursor = data_start
    while True:
        train_end = cursor + timedelta(days=TRAIN_DAYS)
        valid_end = train_end + timedelta(days=VALID_DAYS)
        if valid_end > data_end + timedelta(days=1):
            break
        windows.append((cursor, train_end, valid_end))
        cursor += timedelta(days=STEP_DAYS)

    print(f"\n[4] Rolling Walk-forward 검증 ({len(windows)} 윈도우)")
    print(f"    Train={TRAIN_DAYS}일 / Valid={VALID_DAYS}일 / Step={STEP_DAYS}일")

    wf_candidates = {}
    for ci, top in enumerate(top5):
        full_params = {**BASE_PARAMS, **top["params"]}
        wf_candidates[ci] = {
            "rank": ci + 1,
            "params": top["params"],
            "full_params": full_params,
            "validations": [],
            "overfits": 0,
        }

    for wi, (train_start, train_end, valid_end) in enumerate(windows, 1):
        print(f"\n  Window {wi}/{len(windows)}: "
              f"Train {train_start.date()}~{train_end.date()} | "
              f"Valid {train_end.date()}~{valid_end.date()}")

        train_candles, valid_candles = split_by_date(
            all_candles, train_start, train_end, valid_end
        )
        print(f"    Train: {len(train_candles)}개, Valid: {len(valid_candles)}개")

        if len(train_candles) < 60 or len(valid_candles) < 15:
            print(f"    ⚠️ 캔들 부족 — 스킵")
            continue

        for ci, cand in wf_candidates.items():
            # 훈련 기간 성과
            train_result = run_backtest(
                train_candles, cand["full_params"], CONFIG
            )
            # 검증 기간 성과 (out-of-sample)
            valid_result = run_backtest(
                valid_candles, cand["full_params"], CONFIG
            )
            gap_pct, is_overfit = check_overfit(train_result, valid_result)

            if is_overfit:
                cand["overfits"] += 1

            cand["validations"].append({
                "window": wi,
                "train_period": f"{train_start.date()}~{train_end.date()}",
                "valid_period": f"{train_end.date()}~{valid_end.date()}",
                "train_sharpe": train_result.sharpe_ratio,
                "train_return": train_result.total_return_pct,
                "train_trades": train_result.total_trades,
                "trades": valid_result.total_trades,
                "wins": valid_result.wins,
                "losses": valid_result.losses,
                "return_pct": valid_result.total_return_pct,
                "sharpe": valid_result.sharpe_ratio,
                "pnl_jpy": valid_result.total_pnl_jpy,
                "max_dd_pct": valid_result.max_drawdown_pct,
                "avg_holding_hours": valid_result.avg_holding_hours,
                "gap_pct": gap_pct,
                "overfit": is_overfit,
            })

            status = "⚠️ overfit" if is_overfit else "✅"
            print(f"    #{cand['rank']} {status}"
                  f" Train Sharpe={fmt(train_result.sharpe_ratio)} Return={fmt(train_result.total_return_pct)}%"
                  f" | Valid Sharpe={fmt(valid_result.sharpe_ratio)} Return={fmt(valid_result.total_return_pct)}%"
                  f" Trades={valid_result.total_trades}")

    # ── 5. 최종 비교표 ──
    print("\n" + "=" * 80)
    print("최종 Rolling Walk-forward 비교표")
    print("=" * 80)

    header = (f"{'Rank':>4} {'WF Return%':>10} {'WF Trades':>9} {'WinRate%':>9}"
              f" {'MaxDD%':>7} {'Overfits':>8} {'AvgRet/Win':>10} | Params")
    print(header)
    print("-" * 80)

    ranked = []
    for ci, cand in wf_candidates.items():
        agg = aggregate_walkforward(cand["validations"])
        cand["aggregate"] = agg
        ranked.append(cand)

    # 누적 수익률 기준 정렬
    ranked.sort(
        key=lambda c: c["aggregate"]["cumulative_return_pct"],
        reverse=True,
    )

    for cand in ranked:
        agg = cand["aggregate"]
        overfit_ratio = f"{cand['overfits']}/{agg['windows_count']}"
        print(f"  #{cand['rank']:>2} {fmt(agg['cumulative_return_pct']):>9}%"
              f" {agg['total_trades']:>9} {fmt(agg['win_rate']):>8}%"
              f" {fmt(agg['max_drawdown_pct']):>6}% {overfit_ratio:>8}"
              f" {fmt(agg['avg_return_per_window_pct']):>9}%"
              f" | {cand['params']}")

    # ── 6. 판정 ──
    print("\n" + "-" * 80)

    # 기준: 과적합 < 50%, 누적수익률 > 0%, 거래 발생
    viable = [c for c in ranked
              if c["aggregate"]["cumulative_return_pct"] > 0
              and c["aggregate"]["total_trades"] > 0
              and c["overfits"] <= len(windows) * 0.5]

    if viable:
        best = viable[0]
        agg = best["aggregate"]
        print(f"✅ 최적 파라미터 후보 #{best['rank']}:")
        print(f"   누적 수익률: {fmt(agg['cumulative_return_pct'])}%")
        print(f"   총 거래: {agg['total_trades']}건 (윈도우 {agg['windows_count']}개)")
        print(f"   승률: {fmt(agg['win_rate'])}%")
        print(f"   최대 낙폭: {fmt(agg['max_drawdown_pct'])}%")
        print(f"   과적합 윈도우: {best['overfits']}/{agg['windows_count']}")

        # gmo_strategies 등록용 최종 파라미터 출력
        final_params = {**BASE_PARAMS, **best["params"]}
        # position_size_pct는 실전에서 50%로 조정 (보수적)
        final_params["position_size_pct"] = 50.0
        final_params["leverage"] = 5

        print(f"\n   === gmo_strategies 등록용 최종 파라미터 ===")
        print(json.dumps(final_params, indent=2, ensure_ascii=False))

        # 윈도우별 상세
        print(f"\n   윈도우별 검증 결과:")
        for v in best["validations"]:
            status = "⚠️" if v["overfit"] else "✅"
            print(f"     {status} {v['valid_period']}: Return={fmt(v['return_pct'])}%"
                  f" Sharpe={fmt(v['sharpe'])} Trades={v['trades']}"
                  f" WR={fmt(v['wins']/(v['trades'] or 1)*100)}%"
                  f" Hold={fmt(v['avg_holding_hours'])}h")
    else:
        print("❌ 모든 파라미터 후보가 적합하지 않음.")
        print("   원인: 과적합 비율 과다, 또는 검증기간 수익률 < 0%")
        print("   → 전략 자체 재검토 또는 데이터 축적 후 재시도 필요")

        # 그래도 가장 나은 후보는 참고용으로 출력
        if ranked:
            ref = ranked[0]
            agg = ref["aggregate"]
            print(f"\n   (참고) 가장 나은 후보 #{ref['rank']}:")
            print(f"   누적 수익률: {fmt(agg['cumulative_return_pct'])}%")
            print(f"   총 거래: {agg['total_trades']}건")
            print(f"   승률: {fmt(agg['win_rate'])}%")
            ref_params = {**BASE_PARAMS, **ref["params"]}
            ref_params["position_size_pct"] = 50.0
            ref_params["leverage"] = 5
            print(f"\n   파라미터:")
            print(json.dumps(ref_params, indent=2, ensure_ascii=False))

    # ── JSON 결과 저장 ──
    output = {
        "timestamp": datetime.now(JST).isoformat(),
        "config": {
            "pair": PAIR,
            "timeframe": TIMEFRAME,
            "total_candles": len(all_candles),
            "train_days": TRAIN_DAYS,
            "valid_days": VALID_DAYS,
            "step_days": STEP_DAYS,
            "windows": len(windows),
        },
        "stage1_top10": stage1_result.results[:10],
        "stage2_top5": [{"params": t["params"], "sharpe": t["sharpe_ratio"],
                         "return_pct": t["total_return_pct"], "trades": t["total_trades"]}
                        for t in top5],
        "walkforward": {ci: {
            "rank": c["rank"],
            "params": c["params"],
            "aggregate": c["aggregate"],
            "overfits": c["overfits"],
            "validations": c["validations"],
        } for ci, c in wf_candidates.items()},
        "verdict": "optimal_found" if viable else "no_viable_candidate",
        "best_params": {**BASE_PARAMS, **(viable[0]["params"] if viable else {}),
                        "position_size_pct": 50.0, "leverage": 5} if viable else None,
    }

    result_path = "/app/scripts/grid_4h_walkforward_result.json"
    with open(result_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n결과 저장: {result_path}")

    print("\n" + "=" * 80)
    print("완료.")


if __name__ == "__main__":
    asyncio.run(main())
