"""
USD_JPY 박스역추세 Rolling Walk-forward 검증.

목적:
  활성화된 USD_JPY 박스역추세 전략 (#4) 파라미터를
  Rolling Walk-forward로 OOS 검증.
  Phase 1 확대 전 판단 기준으로 활용.

실행: docker exec bitflyer-trader python3 scripts/run_box_wf.py

Stage 1: 박스 구성 파라미터 (tolerance × touches × lookback × near) — 54조합
Stage 2: 리스크 파라미터 (stop_loss × take_profit) — top5×9=45조합
Walk-forward: Rolling — 훈련 240일 / 검증 60일 / 스텝 30일

현재 활성 파라미터 (전략 #4):
  tol=0.4, touches=2, lb=40, near=0.5, SL=2.5%, TP=1.5%, size=20%, lever=3
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

# ── 설정 ──────────────────────────────────────────────────────
PAIR = "usd_jpy"
TIMEFRAME = "4h"
TOTAL_DAYS = 450

# Rolling Walk-forward
TRAIN_DAYS = 240
VALID_DAYS = 60
STEP_DAYS = 30
OVERFIT_THRESHOLD_PCT = 40

# 현재 활성 파라미터
ACTIVE_PARAMS = {
    "box_tolerance_pct": 0.4,
    "min_touches": 2,
    "box_lookback_candles": 40,
    "near_bound_pct": 0.5,
    "stop_loss_pct": 2.5,
    "take_profit_pct": 1.5,
    "position_size_pct": 20.0,
}

# Stage 1: 박스 구성 — 3 × 2 × 3 × 3 = 54조합
STAGE1_GRID = {
    "box_tolerance_pct": [0.3, 0.4, 0.5],
    "min_touches": [2, 3],
    "box_lookback_candles": [30, 40, 60],
    "near_bound_pct": [0.3, 0.5, 0.8],
}

# Stage 2: 리스크 — 3 × 3 = 9조합
STAGE2_GRID = {
    "stop_loss_pct": [2.0, 2.5, 3.0],
    "take_profit_pct": [1.0, 1.5, 2.0],
}

CONFIG = BacktestConfig(
    initial_capital_jpy=100_000.0,
    slippage_pct=0.05,
    fee_pct=0.0,  # GMO FX 트라이얼 기간 수수료 0 (2026-04-30까지)
)

# ── DB ────────────────────────────────────────────────────────
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


async def load_candles(days: int):
    sf = await get_session_factory()
    async with sf() as session:
        end_date = datetime.now(JST)
        start_date = end_date - timedelta(days=days)
        stmt = (
            select(GmoCandle)
            .where(
                and_(
                    GmoCandle.pair == PAIR,
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
    gap_pct = (
        abs(train_result.sharpe_ratio - valid_result.sharpe_ratio)
        / abs(train_result.sharpe_ratio) * 100
    )
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


async def verify_active_params(candles, windows):
    """현재 활성 파라미터를 WF에 직접 넣어 검증."""
    print("\n── 현재 활성 파라미터 WF 검증 ──")
    print(f"  파라미터: {ACTIVE_PARAMS}")

    validations = []
    for wi, (train_start, train_end, valid_end) in enumerate(windows, 1):
        train_candles, valid_candles = split_by_date(candles, train_start, train_end, valid_end)
        if len(valid_candles) < 10:
            continue
        train_res = run_backtest(train_candles, ACTIVE_PARAMS, CONFIG, "box_mean_reversion")
        valid_res = run_backtest(valid_candles, ACTIVE_PARAMS, CONFIG, "box_mean_reversion")
        gap_pct, is_overfit = check_overfit(train_res, valid_res)
        validations.append({
            "window": wi,
            "valid_period": f"{train_end.date()}~{valid_end.date()}",
            "trades": valid_res.total_trades,
            "wins": valid_res.wins,
            "losses": valid_res.losses,
            "return_pct": valid_res.total_return_pct,
            "sharpe": valid_res.sharpe_ratio,
            "pnl_jpy": valid_res.total_pnl_jpy,
            "max_dd_pct": valid_res.max_drawdown_pct,
            "train_sharpe": train_res.sharpe_ratio,
            "gap_pct": gap_pct,
            "overfit": is_overfit,
        })
        flag = "⚠️" if is_overfit else "✅"
        print(
            f"  W{wi:02d} [{train_end.date()}~{valid_end.date()}]"
            f"  Trades={valid_res.total_trades:>3}"
            f"  Ret={fmt(valid_res.total_return_pct):>7}%"
            f"  Sharpe={fmt(valid_res.sharpe_ratio):>6}"
            f"  MDD={fmt(valid_res.max_drawdown_pct):>5}%"
            f"  {flag}"
        )

    if validations:
        agg = aggregate_walkforward(validations)
        print(
            f"\n  집계: Ret={fmt(agg['cumulative_return_pct'])}%"
            f"  Trades={agg['total_trades']}"
            f"  WR={fmt(agg['win_rate'])}%"
            f"  MDD={fmt(agg['max_drawdown_pct'])}%"
            f"  Windows={agg['windows_count']}"
        )
        return agg, validations
    return None, []


async def run_grid_walkforward(candles, windows):
    """2단계 그리드서치 + WF. 최적 파라미터 후보 탐색."""
    print("\n── Stage 1: 박스 구성 파라미터 그리드서치 ──")
    s1_combos = len(list(product(*STAGE1_GRID.values())))
    s1 = run_grid_search(
        candles, ACTIVE_PARAMS, STAGE1_GRID,
        config=CONFIG, top_n=10, strategy_type="box_mean_reversion",
    )
    print(f"  {s1_combos}조합 → Best Sharpe={fmt(s1.best_sharpe)}")
    for i, r in enumerate(s1.results[:3], 1):
        print(
            f"    #{i} Sharpe={fmt(r['sharpe_ratio']):>7}"
            f"  Ret={fmt(r['total_return_pct']):>7}%"
            f"  MDD={fmt(r['max_drawdown_pct']):>6}%"
            f"  Trades={r['total_trades']:>3}"
            f"  | {r['params']}"
        )

    if not s1.results:
        print("  Stage 1 결과 없음 — 종료")
        return None, None

    print("\n── Stage 2: 리스크 파라미터 그리드서치 ──")
    stage2_all = []
    for rank, s1r in enumerate(s1.results[:5], 1):
        s1_base = {**ACTIVE_PARAMS, **s1r["params"]}
        s2 = run_grid_search(
            candles, s1_base, STAGE2_GRID,
            config=CONFIG, top_n=5, strategy_type="box_mean_reversion",
        )
        for r2 in s2.results[:5]:
            merged = {**s1r["params"], **r2["params"]}
            stage2_all.append({**r2, "params": merged, "stage1_rank": rank})

    stage2_all.sort(
        key=lambda x: x["sharpe_ratio"] if x["sharpe_ratio"] is not None else -999,
        reverse=True,
    )
    top5 = stage2_all[:5]
    print(f"  top5×9조합 → Best Sharpe={fmt(top5[0]['sharpe_ratio']) if top5 else 'N/A'}")
    for i, r in enumerate(top5[:3], 1):
        print(
            f"    #{i} Sharpe={fmt(r['sharpe_ratio']):>7}"
            f"  Ret={fmt(r['total_return_pct']):>7}%"
            f"  MDD={fmt(r['max_drawdown_pct']):>6}%"
            f"  Trades={r['total_trades']:>3}"
        )

    if not top5:
        return None, None

    print(f"\n── Rolling Walk-forward ({len(windows)} 윈도우) ──")
    wf_candidates = {}
    for ci, top in enumerate(top5):
        full_params = {**ACTIVE_PARAMS, **top["params"]}
        wf_candidates[ci] = {
            "rank": ci + 1,
            "params": top["params"],
            "full_params": full_params,
            "validations": [],
            "overfits": 0,
        }

    for wi, (train_start, train_end, valid_end) in enumerate(windows, 1):
        train_candles, valid_candles = split_by_date(candles, train_start, train_end, valid_end)
        if len(train_candles) < 40 or len(valid_candles) < 10:
            continue
        for ci, cand in wf_candidates.items():
            train_res = run_backtest(train_candles, cand["full_params"], CONFIG, "box_mean_reversion")
            valid_res = run_backtest(valid_candles, cand["full_params"], CONFIG, "box_mean_reversion")
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
    viable = [
        c for c in ranked
        if c["aggregate"]["cumulative_return_pct"] > 0
        and c["aggregate"]["total_trades"] > 0
        and c["overfits"] <= len(windows) * 0.6
    ]

    if viable:
        best = viable[0]
        agg = best["aggregate"]
        print(
            f"\n  ✅ 최적: Ret={fmt(agg['cumulative_return_pct'])}%"
            f"  Trades={agg['total_trades']}"
            f"  WR={fmt(agg['win_rate'])}%"
            f"  MDD={fmt(agg['max_drawdown_pct'])}%"
            f"  Overfits={best['overfits']}/{agg['windows_count']}"
        )
        print(f"  파라미터: {best['params']}")
        return best["params"], agg
    else:
        if ranked:
            ref = ranked[0]
            agg = ref["aggregate"]
            print(
                f"\n  ❌ viable 없음 (참고 — Ret={fmt(agg['cumulative_return_pct'])}%"
                f"  Trades={agg['total_trades']})"
            )
        else:
            print("  ❌ Walk-forward 결과 없음")
        return None, None


async def main():
    print("=" * 72)
    print("USD_JPY 박스역추세 Rolling Walk-forward 검증")
    print(f"실행: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")
    print(f"설정: Train={TRAIN_DAYS}d / Valid={VALID_DAYS}d / Step={STEP_DAYS}d")
    print("=" * 72)

    # ── 캔들 로드 ──
    print(f"\n캔들 로드 중... ({PAIR.upper()} {TIMEFRAME}, 최대 {TOTAL_DAYS}일)")
    candles = await load_candles(TOTAL_DAYS)
    if len(candles) < 100:
        print(f"캔들 부족: {len(candles)}개. 종료.")
        return

    date_range = f"{candles[0].open_time.date()} ~ {candles[-1].open_time.date()}"
    print(f"로드 완료: {len(candles)}개 ({date_range})")

    # ── WF 윈도우 생성 ──
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
    print(f"WF 윈도우: {len(windows)}개")

    # ── 현재 활성 파라미터 WF 검증 ──
    active_agg, active_validations = await verify_active_params(candles, windows)

    # ── 그리드서치 + 최적화 ──
    print("\n" + "─" * 72)
    best_params, best_agg = await run_grid_walkforward(candles, windows)

    # ── 최종 비교 요약 ──
    print("\n" + "=" * 72)
    print("최종 요약")
    print("=" * 72)

    if active_agg:
        print(
            f"  현재 활성 (#4): Ret={fmt(active_agg['cumulative_return_pct'])}%"
            f"  Trades={active_agg['total_trades']}"
            f"  WR={fmt(active_agg['win_rate'])}%"
            f"  MDD={fmt(active_agg['max_drawdown_pct'])}%"
        )
    else:
        print("  현재 활성 (#4): 검증 데이터 없음")

    if best_params and best_agg:
        print(
            f"  그리드 최적: Ret={fmt(best_agg['cumulative_return_pct'])}%"
            f"  Trades={best_agg['total_trades']}"
            f"  WR={fmt(best_agg['win_rate'])}%"
            f"  MDD={fmt(best_agg['max_drawdown_pct'])}%"
        )
        print(f"  최적 파라미터: {best_params}")

        # 활성 파라미터 대비 그리드 최적이 유의미하게 나은지 판단 (수익률 +2% 이상)
        if active_agg:
            diff = best_agg["cumulative_return_pct"] - active_agg["cumulative_return_pct"]
            if diff >= 2.0:
                print(f"\n  ⚠️  그리드 최적이 +{fmt(diff)}% 우위. 파라미터 갱신 검토 권장.")
            else:
                print(f"\n  ✅ 현재 파라미터 유지 적절 (차이 {fmt(diff)}%).")
    else:
        print("  그리드 최적: viable 없음")

    # JSON 결과 파일
    output = {
        "run_at": datetime.now(JST).isoformat(),
        "pair": PAIR,
        "active_params": ACTIVE_PARAMS,
        "active_wf": active_agg,
        "active_validations": active_validations,
        "grid_best_params": best_params,
        "grid_best_wf": best_agg,
    }
    out_path = "/tmp/box_wf_result.json"
    with open(out_path, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n  결과 저장: {out_path}")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
