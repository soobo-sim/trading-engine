"""
GMO FX 멀티페어 4H 그리드 서치 + Rolling Walk-forward 비교 분석.

실행: docker exec bitflyer-trader python3 scripts/run_multipair_4h_wf.py

목적:
  USD_JPY, GBP_JPY, EUR_JPY 세 페어에 대해 동일한 2단계 그리드 서치 +
  Rolling Walk-forward 검증을 수행하고, 최적 페어 + 파라미터를 추천한다.

Phase:
  Stage 1: 핵심 4변수 (atr_stop, trail_init, trail_mature, ema_period) — 144조합
  Stage 2: 부변수 (entry_rsi_max, slope_entry_min, tighten_stop_atr) — top5×27=135조합
  Walk-forward: Rolling window — 240일 훈련 / 60일 검증, 30일 스텝
  Cross-pair: 페어별 최적 후보 비교 → 최종 추천
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from itertools import product

from sqlalchemy import select, and_

sys.path.insert(0, "/app")

from adapters.database.session import create_db_engine, create_session_factory
from adapters.database.models import create_candle_model
from core.backtest.engine import (
    BacktestConfig,
    run_backtest,
    run_grid_search,
)

JST = timezone(timedelta(hours=9))

# ── 설정 ──────────────────────────────────────────────────
PAIRS = ["usd_jpy", "gbp_jpy", "eur_jpy"]
TOTAL_DAYS = 450
TIMEFRAME = "4h"

# Rolling Walk-forward 설정
TRAIN_DAYS = 240
VALID_DAYS = 60
STEP_DAYS = 30
OVERFIT_THRESHOLD_PCT = 40

# FX 공통 기본 파라미터
def make_base_params(pair: str) -> dict:
    return {
        "pair": pair,
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
        "ema_slope_weak_threshold": 0.005,   # FX 전용
        "ema_slope_entry_min": -0.03,
        "position_size_pct": 100.0,
        # FX regime 임계값 (USD_JPY 분포 기반 — FX 공통 적용)
        "bb_width_trending_min": 0.8,
        "range_pct_trending_min": 1.5,
        "bb_width_ranging_max": 0.35,
        "range_pct_ranging_max": 0.9,
        # 숏 진입
        "entry_rsi_min_short": 35.0,
        "entry_rsi_max_short": 60.0,
        "ema_slope_short_threshold": -0.05,
    }

# Stage 1: 핵심 4변수 — 4 × 4 × 3 × 3 = 144 조합
STAGE1_GRID = {
    "atr_multiplier_stop": [1.5, 2.0, 2.5, 3.0],
    "trailing_stop_atr_initial": [1.5, 2.0, 2.5, 3.0],
    "trailing_stop_atr_mature": [0.8, 1.0, 1.2],
    "ema_period": [15, 20, 25],
}

# Stage 2: 부변수 — 3 × 3 × 3 = 27 조합
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

# ── DB ──────────────────────────────────────────────────────
GmoCandle = create_candle_model("gmo", pair_column="pair")

_engine = None
_session_factory = None


async def get_session_factory():
    global _engine, _session_factory
    if _engine is None:
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            raise ValueError("DATABASE_URL 환경변수 필수")
        _engine = create_db_engine(database_url)
        _session_factory = create_session_factory(_engine)
    return _session_factory


async def load_candles(pair: str, days: int):
    """DB에서 4H 캔들 로드."""
    sf = await get_session_factory()
    async with sf() as session:
        end_date = datetime.now(JST)
        start_date = end_date - timedelta(days=days)
        stmt = (
            select(GmoCandle)
            .where(
                and_(
                    GmoCandle.pair == pair,
                    GmoCandle.timeframe == TIMEFRAME,
                    GmoCandle.is_complete == True,
                    GmoCandle.open_time >= start_date,
                    GmoCandle.open_time <= end_date,
                )
            )
            .order_by(GmoCandle.open_time)
        )
        result = await session.execute(stmt)
        return result.scalars().all()


def split_by_date(candles, train_start, train_end, valid_end):
    train = [c for c in candles if train_start <= c.open_time < train_end]
    valid = [c for c in candles if train_end <= c.open_time < valid_end]
    return train, valid


def fmt(val, suffix=""):
    if val is None:
        return "N/A"
    if isinstance(val, float):
        return f"{val:.2f}{suffix}"
    return f"{val}{suffix}"


def check_overfit(train_result, valid_result):
    if train_result.sharpe_ratio is None or valid_result.sharpe_ratio is None:
        return None, False
    if train_result.sharpe_ratio == 0:
        return None, False
    gap_pct = abs(train_result.sharpe_ratio - valid_result.sharpe_ratio) / abs(train_result.sharpe_ratio) * 100
    return round(gap_pct, 1), gap_pct > OVERFIT_THRESHOLD_PCT


def aggregate_walkforward(wf_validations):
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
        "cumulative_return_pct": round(cum_return_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "windows_count": len(wf_validations),
    }


async def run_pair_analysis(pair: str) -> dict:
    """단일 페어 완전 분석 (Stage1 → Stage2 → Walk-forward)."""
    base_params = make_base_params(pair)
    result = {
        "pair": pair,
        "candles": 0,
        "stage1_top": [],
        "stage2_top": [],
        "wf_best": None,
        "wf_aggregate": None,
        "viable": False,
        "error": None,
    }

    # ── 데이터 로드 ──
    candles = await load_candles(pair, TOTAL_DAYS)
    result["candles"] = len(candles)
    if len(candles) < 200:
        result["error"] = f"캔들 부족 ({len(candles)}개 < 200)"
        return result

    date_range = f"{candles[0].open_time.date()} ~ {candles[-1].open_time.date()}"
    print(f"\n  {pair.upper()}: {len(candles)}개 캔들 ({date_range})")

    # ── Stage 1 ──
    s1 = run_grid_search(
        candles, base_params, STAGE1_GRID,
        config=CONFIG, top_n=10, strategy_type="trend_following",
    )
    result["stage1_top"] = s1.results[:5]
    print(f"  Stage 1 ({s1.total_combinations} combos): Best Sharpe={fmt(s1.best_sharpe)}")
    for i, r in enumerate(s1.results[:3], 1):
        print(f"    #{i} Sharpe={fmt(r['sharpe_ratio']):>7} Ret={fmt(r['total_return_pct']):>7}%"
              f" MDD={fmt(r['max_drawdown_pct']):>6}% Trades={r['total_trades']:>3}"
              f" | {r['params']}")

    if not s1.results:
        result["error"] = "Stage 1 결과 없음"
        return result

    # ── Stage 2 ──
    stage2_all = []
    for rank, s1r in enumerate(s1.results[:5], 1):
        s1_base = {**base_params, **s1r["params"]}
        s2 = run_grid_search(
            candles, s1_base, STAGE2_GRID,
            config=CONFIG, top_n=5, strategy_type="trend_following",
        )
        for r2 in s2.results[:5]:
            merged = {**s1r["params"], **r2["params"]}
            stage2_all.append({**r2, "params": merged, "stage1_rank": rank})

    stage2_all.sort(
        key=lambda x: x["sharpe_ratio"] if x["sharpe_ratio"] is not None else -999,
        reverse=True,
    )
    top5 = stage2_all[:5]
    result["stage2_top"] = [{"params": t["params"], "sharpe": t["sharpe_ratio"],
                             "return_pct": t["total_return_pct"], "trades": t["total_trades"]}
                            for t in top5]

    print(f"  Stage 2 (top5×27): Best Sharpe={fmt(top5[0]['sharpe_ratio']) if top5 else 'N/A'}")
    for i, r in enumerate(top5[:3], 1):
        print(f"    #{i} Sharpe={fmt(r['sharpe_ratio']):>7} Ret={fmt(r['total_return_pct']):>7}%"
              f" MDD={fmt(r['max_drawdown_pct']):>6}% Trades={r['total_trades']:>3}")

    if not top5:
        result["error"] = "Stage 2 결과 없음"
        return result

    # ── Rolling Walk-forward ──
    data_start = candles[0].open_time
    data_end = candles[-1].open_time
    windows = []
    cursor = data_start
    while True:
        train_end = cursor + timedelta(days=TRAIN_DAYS)
        valid_end = train_end + timedelta(days=VALID_DAYS)
        if valid_end > data_end + timedelta(days=1):
            break
        windows.append((cursor, train_end, valid_end))
        cursor += timedelta(days=STEP_DAYS)

    print(f"  Walk-forward: {len(windows)} 윈도우 (Train={TRAIN_DAYS}d / Valid={VALID_DAYS}d)")

    wf_candidates = {}
    for ci, top in enumerate(top5):
        full_params = {**base_params, **top["params"]}
        wf_candidates[ci] = {
            "rank": ci + 1,
            "params": top["params"],
            "full_params": full_params,
            "validations": [],
            "overfits": 0,
        }

    for wi, (train_start, train_end, valid_end) in enumerate(windows, 1):
        train_candles, valid_candles = split_by_date(candles, train_start, train_end, valid_end)
        if len(train_candles) < 60 or len(valid_candles) < 15:
            continue
        for ci, cand in wf_candidates.items():
            train_res = run_backtest(train_candles, cand["full_params"], CONFIG)
            valid_res = run_backtest(valid_candles, cand["full_params"], CONFIG)
            gap_pct, is_overfit = check_overfit(train_res, valid_res)
            if is_overfit:
                cand["overfits"] += 1
            cand["validations"].append({
                "window": wi,
                "train_period": f"{train_start.date()}~{train_end.date()}",
                "valid_period": f"{train_end.date()}~{valid_end.date()}",
                "train_sharpe": train_res.sharpe_ratio,
                "train_return": train_res.total_return_pct,
                "trades": valid_res.total_trades,
                "wins": valid_res.wins,
                "losses": valid_res.losses,
                "return_pct": valid_res.total_return_pct,
                "sharpe": valid_res.sharpe_ratio,
                "pnl_jpy": valid_res.total_pnl_jpy,
                "max_dd_pct": valid_res.max_drawdown_pct,
                "gap_pct": gap_pct,
                "overfit": is_overfit,
            })

    # 최적 후보 선정
    ranked = []
    for ci, cand in wf_candidates.items():
        if cand["validations"]:
            agg = aggregate_walkforward(cand["validations"])
            cand["aggregate"] = agg
            ranked.append(cand)

    ranked.sort(
        key=lambda c: c["aggregate"]["cumulative_return_pct"],
        reverse=True,
    )

    viable = [c for c in ranked
              if c["aggregate"]["cumulative_return_pct"] > 0
              and c["aggregate"]["total_trades"] > 0
              and c["overfits"] <= len(windows) * 0.5]

    if viable:
        best = viable[0]
        agg = best["aggregate"]
        result["viable"] = True
        result["wf_best"] = best["params"]
        result["wf_aggregate"] = agg
        result["wf_overfits"] = best["overfits"]
        result["wf_windows"] = agg["windows_count"]
        result["wf_validations"] = best["validations"]
        print(f"  ✅ Best: Return={fmt(agg['cumulative_return_pct'])}%"
              f" Trades={agg['total_trades']} WR={fmt(agg['win_rate'])}%"
              f" MDD={fmt(agg['max_drawdown_pct'])}%"
              f" Overfits={best['overfits']}/{agg['windows_count']}")
    else:
        # 참고용 — 가장 나은 후보
        if ranked:
            ref = ranked[0]
            agg = ref["aggregate"]
            result["wf_best"] = ref["params"]
            result["wf_aggregate"] = agg
            result["wf_overfits"] = ref["overfits"]
            result["wf_windows"] = agg["windows_count"]
            result["wf_validations"] = ref["validations"]
            print(f"  ❌ No viable (ref: Return={fmt(agg['cumulative_return_pct'])}%"
                  f" Trades={agg['total_trades']})")
        else:
            print(f"  ❌ Walk-forward 결과 없음")

    return result


async def main():
    print("=" * 80)
    print("GMO FX 멀티페어 4H 그리드 서치 + Rolling Walk-forward 비교 분석")
    print(f"페어: {', '.join(p.upper() for p in PAIRS)}")
    print(f"실행 시각: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")
    print("=" * 80)

    # ── 페어별 분석 ──
    pair_results = {}
    for pair in PAIRS:
        print(f"\n{'─' * 80}")
        print(f"▶ {pair.upper()} 분석 시작")
        print(f"{'─' * 80}")
        pair_results[pair] = await run_pair_analysis(pair)

    # ── 크로스 페어 비교표 ──
    print("\n" + "=" * 80)
    print("크로스 페어 비교표 (Walk-forward 검증 기준)")
    print("=" * 80)
    header = (f"{'Pair':>10} {'WF Return%':>11} {'Trades':>7} {'WinRate%':>9}"
              f" {'MaxDD%':>7} {'Overfits':>10} {'Candles':>8} {'Viable':>7}")
    print(header)
    print("-" * 80)

    for pair in PAIRS:
        r = pair_results[pair]
        if r["wf_aggregate"]:
            agg = r["wf_aggregate"]
            of_str = f"{r.get('wf_overfits', '?')}/{r.get('wf_windows', '?')}"
            viable_str = "✅" if r["viable"] else "❌"
            print(f"{pair.upper():>10} {fmt(agg['cumulative_return_pct']):>10}%"
                  f" {agg['total_trades']:>7} {fmt(agg['win_rate']):>8}%"
                  f" {fmt(agg['max_drawdown_pct']):>6}%"
                  f" {of_str:>10} {r['candles']:>8} {viable_str:>7}")
        else:
            err = r.get("error", "no result")
            print(f"{pair.upper():>10} {'—':>11} {'—':>7} {'—':>9}"
                  f" {'—':>7} {'—':>10} {r['candles']:>8} {'❌':>7}  ({err})")

    # ── 윈도우별 상세 비교 ──
    viable_pairs = [p for p in PAIRS if pair_results[p]["viable"]]
    if viable_pairs:
        print(f"\n{'─' * 80}")
        print("윈도우별 수익률 비교 (Viable 페어)")
        print(f"{'─' * 80}")
        for pair in viable_pairs:
            r = pair_results[pair]
            vds = r.get("wf_validations", [])
            if vds:
                print(f"\n  {pair.upper()}:")
                for v in vds:
                    status = "⚠️" if v["overfit"] else "✅"
                    wr = round(v['wins'] / v['trades'] * 100, 1) if v['trades'] > 0 else 0
                    print(f"    {status} {v['valid_period']}: Ret={fmt(v['return_pct'])}%"
                          f" Sharpe={fmt(v['sharpe'])} Trades={v['trades']}"
                          f" WR={wr}% MDD={fmt(v['max_dd_pct'])}%")

    # ── 최종 판정 ──
    print(f"\n{'=' * 80}")
    print("최종 판정")
    print(f"{'=' * 80}")

    if not viable_pairs:
        print("❌ 모든 페어가 WF 검증 미통과. 전략 자체 재검토 필요.")
        # 참고용: 모든 페어의 최선 결과
        for pair in PAIRS:
            r = pair_results[pair]
            if r["wf_aggregate"]:
                agg = r["wf_aggregate"]
                print(f"  (참고) {pair.upper()}: Return={fmt(agg['cumulative_return_pct'])}%"
                      f" MDD={fmt(agg['max_drawdown_pct'])}%"
                      f" Params={r['wf_best']}")
    else:
        # Viable 중 누적 수익률 최고 → 최종 추천
        best_pair = max(viable_pairs,
                        key=lambda p: pair_results[p]["wf_aggregate"]["cumulative_return_pct"])
        best_r = pair_results[best_pair]
        agg = best_r["wf_aggregate"]

        print(f"✅ 최적 페어: {best_pair.upper()}")
        print(f"   누적 수익률: {fmt(agg['cumulative_return_pct'])}%")
        print(f"   총 거래: {agg['total_trades']}건 (윈도우 {agg['windows_count']}개)")
        print(f"   승률: {fmt(agg['win_rate'])}%")
        print(f"   최대 낙폭: {fmt(agg['max_drawdown_pct'])}%")
        print(f"   과적합 윈도우: {best_r.get('wf_overfits', '?')}/{agg['windows_count']}")

        final_params = {**make_base_params(best_pair), **best_r["wf_best"]}
        final_params["position_size_pct"] = 20.0   # Phase 0 보수적
        final_params["leverage"] = 3               # Phase 0 보수적
        print(f"\n   === proposed 전략 파라미터 ===")
        print(json.dumps(final_params, indent=2, ensure_ascii=False))

        # 차점 페어
        runner_up = [p for p in viable_pairs if p != best_pair]
        if runner_up:
            print(f"\n   차점 페어:")
            for p in runner_up:
                ra = pair_results[p]["wf_aggregate"]
                print(f"   {p.upper()}: Return={fmt(ra['cumulative_return_pct'])}%"
                      f" Trades={ra['total_trades']} WR={fmt(ra['win_rate'])}%")

    # ── JSON 저장 ──
    output = {
        "timestamp": datetime.now(JST).isoformat(),
        "config": {
            "pairs": PAIRS,
            "timeframe": TIMEFRAME,
            "total_days": TOTAL_DAYS,
            "train_days": TRAIN_DAYS,
            "valid_days": VALID_DAYS,
            "step_days": STEP_DAYS,
        },
        "pair_results": {},
    }
    for pair in PAIRS:
        r = pair_results[pair]
        entry = {
            "candles": r["candles"],
            "viable": r["viable"],
            "error": r["error"],
            "stage1_top3": r["stage1_top"][:3],
            "stage2_top3": r["stage2_top"][:3],
            "wf_best_params": r["wf_best"],
            "wf_aggregate": r["wf_aggregate"],
        }
        if r["viable"]:
            fp = {**make_base_params(pair), **r["wf_best"]}
            fp["position_size_pct"] = 20.0
            fp["leverage"] = 3
            entry["proposed_params"] = fp
        output["pair_results"][pair] = entry

    if viable_pairs:
        best_pair = max(viable_pairs,
                        key=lambda p: pair_results[p]["wf_aggregate"]["cumulative_return_pct"])
        fp = {**make_base_params(best_pair), **pair_results[best_pair]["wf_best"]}
        fp["position_size_pct"] = 20.0
        fp["leverage"] = 3
        output["recommendation"] = {
            "pair": best_pair,
            "params": fp,
            "aggregate": pair_results[best_pair]["wf_aggregate"],
        }
    else:
        output["recommendation"] = None

    result_path = "/app/scripts/multipair_4h_wf_result.json"
    with open(result_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n결과 저장: {result_path}")
    print("\n" + "=" * 80)
    print("완료.")

    # DB 엔진 정리
    global _engine
    if _engine:
        await _engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
