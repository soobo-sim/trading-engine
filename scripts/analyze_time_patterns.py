"""
F-03: 요일·시간 패턴 분석 스크립트.

백테스트(box_mean_reversion)로 생성된 per-trade 데이터를 사용해
요일·시간대별 성과 히트맵과 통계 검정을 출력한다.

별도 데이터 소스 불필요 — 기존 gmo_candles / bf_candles 활용.

사용:
    cd trading-engine
    DATABASE_URL=postgresql+asyncpg://trader:trader@localhost:5432/trader_db \\
        python scripts/analyze_time_patterns.py

    # 특정 페어만
    DATABASE_URL=... python scripts/analyze_time_patterns.py --pair usd_jpy

    # 실전 포지션도 포함
    DATABASE_URL=... python scripts/analyze_time_patterns.py --include-live
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# trading-engine 루트를 path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.backtest.engine import BacktestConfig, BacktestTrade, _run_box_backtest


# ── 기본 백테스트 파라미터 (WF 최적화 확정값) ────────────────────
DEFAULT_PARAMS = {
    "exchange_type": "fx",
    "box_tolerance_pct": 0.4,
    "box_min_touches": 3,
    "box_lookback_candles": 60,
    "near_bound_pct": 0.3,
    "position_size_pct": 50.0,
    "direction_mode": "long_only",
}

BACKTEST_CONFIG = BacktestConfig(
    initial_capital_jpy=100_000.0,
    slippage_pct=0.05,
    fee_pct=0.05,   # GMO FX 트라이얼 구간 0 → 보수적 0.05
    position_size_pct=50.0,
)

WEEKDAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]
HOUR_BLOCKS = list(range(0, 24, 4))   # 0, 4, 8, 12, 16, 20


# ─────────────────────────────────────────────────────────────────
# 캔들 Proxy — BacktestTrade가 open_time 속성을 필요로 함
# ─────────────────────────────────────────────────────────────────
class _CandleProxy:
    __slots__ = ("open_time", "close_time", "open", "high", "low", "close", "volume")

    def __init__(self, row):
        self.open_time  = row.open_time
        self.close_time = row.close_time
        self.open       = float(row.open)
        self.high       = float(row.high)
        self.low        = float(row.low)
        self.close      = float(row.close)
        self.volume     = float(row.volume)


# ─────────────────────────────────────────────────────────────────
# DB에서 캔들 조회
# ─────────────────────────────────────────────────────────────────
async def fetch_candles(
    session: AsyncSession,
    table: str,
    pair_col: str,
    pair: str,
    timeframe: str = "4h",
) -> list:
    sql = text(
        f"SELECT open_time, close_time, open, high, low, close, volume "
        f"FROM {table} "
        f"WHERE {pair_col} = :pair AND timeframe = :tf AND is_complete = true "
        f"ORDER BY open_time ASC"
    )
    result = await session.execute(sql, {"pair": pair, "tf": timeframe})
    return [_CandleProxy(row) for row in result.fetchall()]


# ─────────────────────────────────────────────────────────────────
# 실전 포지션 조회 (완결된 것만)
# ─────────────────────────────────────────────────────────────────
async def fetch_live_trades(session: AsyncSession) -> list[BacktestTrade]:
    """gmo_box_positions + bf_box_positions + bf_trend_positions 완결 거래."""
    trades = []

    queries = [
        ("gmo_box_positions", "created_at", "closed_at", "realized_pnl_pct"),
        ("bf_box_positions",  "created_at", "closed_at", "realized_pnl_pct"),
        ("bf_trend_positions","created_at", "closed_at", "realized_pnl_pct"),
    ]
    for table, entry_col, exit_col, pnl_col in queries:
        try:
            sql = text(
                f"SELECT {entry_col}, {exit_col}, {pnl_col}, exit_reason "
                f"FROM {table} WHERE status = 'closed' AND {pnl_col} IS NOT NULL"
            )
            result = await session.execute(sql)
            for row in result.fetchall():
                t = BacktestTrade(
                    entry_time=row[0].astimezone(timezone.utc).replace(tzinfo=None)
                    if row[0] else None,
                    entry_price=0,
                    exit_time=row[1],
                    pnl_pct=float(row[2]) if row[2] else None,
                    exit_reason=row[3],
                )
                if t.entry_time and t.pnl_pct is not None:
                    trades.append(t)
        except Exception:
            continue

    return trades


# ─────────────────────────────────────────────────────────────────
# 분석 핵심: 요일 × 시간대 집계
# ─────────────────────────────────────────────────────────────────
def _hour_block(dt) -> int:
    return (dt.hour // 4) * 4


def aggregate(trades: list[BacktestTrade]) -> tuple[dict, dict]:
    """
    Returns:
        by_day:  {weekday_name: [pnl_pct, ...]}
        by_hour: {hour_block:   [pnl_pct, ...]}
    """
    by_day: dict[str, list] = defaultdict(list)
    by_hour: dict[int, list] = defaultdict(list)

    for t in trades:
        if t.entry_time is None or t.pnl_pct is None:
            continue
        wd = t.entry_time.strftime("%A")
        hb = _hour_block(t.entry_time)
        by_day[wd].append(t.pnl_pct)
        by_hour[hb].append(t.pnl_pct)

    return dict(by_day), dict(by_hour)


def _mean(lst): return sum(lst) / len(lst) if lst else 0.0
def _wr(lst): return sum(1 for x in lst if x > 0) / len(lst) * 100 if lst else 0.0


def print_table(title: str, data: dict, key_order: list, key_fmt=None) -> None:
    if key_fmt is None:
        key_fmt = str
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    print(f"{'Key':<14}  {'Count':>5}  {'WinRate':>8}  {'MeanPnL%':>10}  {'TotalPnL%':>11}")
    print("-" * 60)
    for k in key_order:
        vals = data.get(k)
        if vals is None:
            continue
        label = key_fmt(k) if callable(key_fmt) else key_fmt.format(k)
        print(
            f"{label:<14}  {len(vals):>5}  {_wr(vals):>7.1f}%  "
            f"{_mean(vals):>+9.3f}%  {sum(vals):>+10.3f}%"
        )


def _anova_pvalue(groups: list[list]) -> float | None:
    """Kruskal-Wallis p-value (no scipy dependency)."""
    # 그룹이 2개 이상이고 각 그룹 크기 >= 2여야 의미 있음
    valid = [g for g in groups if len(g) >= 2]
    if len(valid) < 2:
        return None
    # 간단한 Kruskal-Wallis H 통계량 계산
    all_vals = [v for g in valid for v in g]
    n = len(all_vals)
    sorted_all = sorted(enumerate(all_vals), key=lambda x: x[1])
    ranks = [0.0] * n
    for rank, (orig_idx, _) in enumerate(sorted_all, 1):
        ranks[orig_idx] = rank

    # 각 그룹의 순위 합
    offset = 0
    H = 0.0
    for g in valid:
        ng = len(g)
        ri = sum(ranks[offset + j] for j in range(ng))
        H += (ri ** 2) / ng
        offset += ng
    H = (12 / (n * (n + 1))) * H - 3 * (n + 1)

    # chi-square 근사 (df = len(valid) - 1)
    df = len(valid) - 1
    # chi2 p-value approximation (table-based for df<=6)
    # 실용적으로 H > critical_value(0.05) 여부만 판단
    # df=1: 3.84, df=2: 5.99, df=3: 7.81, df=4: 9.49, df=5: 11.07, df=6: 12.59
    thresholds = {1: 3.84, 2: 5.99, 3: 7.81, 4: 9.49, 5: 11.07, 6: 12.59}
    sig_threshold = thresholds.get(df, 12.59)

    return 0.01 if H > sig_threshold else 0.99   # 간략 유의성 플래그


def print_pivot(trades: list[BacktestTrade]) -> None:
    """요일 × 4H시간 교차표 (평균 PnL%)."""
    grid: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for t in trades:
        if t.entry_time is None or t.pnl_pct is None:
            continue
        wd = t.entry_time.strftime("%A")
        hb = _hour_block(t.entry_time)
        grid[wd][hb].append(t.pnl_pct)

    # 헤더
    print(f"\n{'='*60}")
    print("  교차표: 평균 PnL% (요일 × 4H시간대, UTC)")
    print(f"{'='*60}")
    col_w = 8
    header = f"{'Weekday':<12}" + "".join(f" {f'{h:02d}h':>{col_w}}" for h in HOUR_BLOCKS)
    print(header)
    print("-" * len(header))

    for wd in WEEKDAY_ORDER:
        if wd not in grid:
            continue
        row = f"{wd:<12}"
        for h in HOUR_BLOCKS:
            vals = grid[wd].get(h, [])
            if vals:
                row += f" {_mean(vals):>+{col_w}.2f}"
            else:
                row += f" {'--':>{col_w}}"
        print(row)


def print_seasonal(trades: list[BacktestTrade]) -> None:
    """계절성 분석: 월별 + 분기별 집계."""
    by_month: dict[int, list] = defaultdict(list)
    for t in trades:
        if t.entry_time is None or t.pnl_pct is None:
            continue
        by_month[t.entry_time.month].append(t.pnl_pct)

    print(f"\n{'='*60}")
    print("  월별 성과 (계절성)")
    print(f"{'='*60}")
    print(f"{'Month':<10}  {'Count':>5}  {'WinRate':>8}  {'MeanPnL%':>10}")
    print("-" * 40)
    for m in range(1, 13):
        vals = by_month.get(m, [])
        if not vals:
            continue
        print(f"{'M{:02d}'.format(m):<10}  {len(vals):>5}  {_wr(vals):>7.1f}%  {_mean(vals):>+9.3f}%")

    # 특별 주목 구간
    mar4 = [t.pnl_pct for t in trades
            if t.entry_time and t.entry_time.month == 3
            and t.entry_time.day >= 22 and t.pnl_pct is not None]
    if mar4:
        print(f"\n[주목] 3월 4주차 (리패트리에이션 가설): "
              f"n={len(mar4)}, WR={_wr(mar4):.1f}%, mean={_mean(mar4):+.3f}%")


# ─────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────
async def main(args: argparse.Namespace) -> None:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL 환경변수가 없습니다.")
        sys.exit(1)

    # asyncpg 드라이버 필요
    if "postgresql://" in db_url and "+asyncpg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")

    engine = create_async_engine(db_url, echo=False)
    SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    all_trades: list[BacktestTrade] = []

    async with SessionLocal() as session:
        # ── 백테스트 캔들 소스 목록 ──
        sources = []
        if args.pair:
            # 지정 페어만
            for table, pair_col in [("gmo_candles", "pair"), ("bf_candles", "product_code")]:
                sources.append((table, pair_col, args.pair))
        else:
            for pair in ["usd_jpy", "gbp_jpy", "eur_jpy"]:
                sources.append(("gmo_candles", "pair", pair))

        for table, pair_col, pair in sources:
            print(f"[캔들 조회] {table} / {pair} ...")
            candles = await fetch_candles(session, table, pair_col, pair)
            if len(candles) < 100:
                print(f"  → 캔들 부족 ({len(candles)}개), 스킵")
                continue
            print(f"  → {len(candles)}개 로드. 백테스트 실행 중...")

            result = _run_box_backtest(candles, DEFAULT_PARAMS, BACKTEST_CONFIG)
            print(
                f"  → 거래 {result.total_trades}건, "
                f"WR={result.win_rate}%, "
                f"return={result.total_return_pct}%"
            )
            all_trades.extend(result.trades)

        # ── 실전 포지션 포함 (옵션) ──
        if args.include_live:
            live = await fetch_live_trades(session)
            print(f"\n[실전 포지션] {len(live)}건 추가")
            all_trades.extend(live)

    await engine.dispose()

    # ── 분석 대상 거래 필터링 ──
    valid = [t for t in all_trades if t.entry_time is not None and t.pnl_pct is not None]
    print(f"\n분석 대상 거래: {len(valid)}건")

    if len(valid) < 20:
        print("\n⚠️  데이터 부족 (< 20건). 통계 검정 신뢰도가 낮습니다.")
        print("   실거래 누적 후 재실행을 권장합니다.")
        if not valid:
            return

    # ── 요일별 분석 ──
    by_day, by_hour = aggregate(valid)
    print_table(
        "요일별 성과",
        by_day,
        WEEKDAY_ORDER,
        key_fmt="{}",
    )

    # ── 4H 시간대별 분석 (UTC) ──
    hour_labels = {h: f"UTC {h:02d}~{h+4:02d}" for h in HOUR_BLOCKS}
    print_table(
        "4H 시간대별 성과 (UTC)",
        by_hour,
        HOUR_BLOCKS,
        key_fmt=lambda k: hour_labels[k],
    )

    # ── 교차표 ──
    print_pivot(valid)

    # ── 계절성 ──
    print_seasonal(valid)

    # ── 통계 검정 ──
    print(f"\n{'='*60}")
    print("  통계 검정 (Kruskal-Wallis)")
    print(f"{'='*60}")

    day_groups = [by_day.get(d, []) for d in WEEKDAY_ORDER]
    p_day = _anova_pvalue([g for g in day_groups if g])
    if p_day is not None:
        sig_day = "유의 (< 0.05)" if p_day < 0.05 else "비유의 (>= 0.05)"
        print(f"요일별 PnL 차이: {sig_day}")
    else:
        print("요일별: 데이터 부족 (검정 불가)")

    hour_groups = [by_hour.get(h, []) for h in HOUR_BLOCKS]
    p_hour = _anova_pvalue([g for g in hour_groups if g])
    if p_hour is not None:
        sig_hour = "유의 (< 0.05)" if p_hour < 0.05 else "비유의 (>= 0.05)"
        print(f"시간대별 PnL 차이: {sig_hour}")
    else:
        print("시간대별: 데이터 부족 (검정 불가)")

    # ── 권고 ──
    print(f"\n{'='*60}")
    print("  판단 기준")
    print(f"{'='*60}")
    print("  유의 + 실질 효과크기(평균 차이 > 0.2%) → 파라미터 추가 후 전략 연동")
    print("  비유의 또는 효과크기 미미 → 구현 생략 (현 결론)")

    # ── 특정 조건 강조 ──
    worst_day = min(by_day, key=lambda d: _mean(by_day[d])) if by_day else None
    best_day  = max(by_day, key=lambda d: _mean(by_day[d])) if by_day else None
    if worst_day and best_day:
        print(f"\n  최악 요일: {worst_day} (mean={_mean(by_day[worst_day]):+.3f}%)")
        print(f"  최선 요일: {best_day} (mean={_mean(by_day[best_day]):+.3f}%)")
        diff = _mean(by_day[best_day]) - _mean(by_day[worst_day])
        if diff > 0.2:
            print(f"  → 요일 간 차이 {diff:.3f}% — 차단/조절 검토 가능")
        else:
            print(f"  → 요일 간 차이 {diff:.3f}% — 미미. 구현 불필요.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="F-03: 요일·시간 패턴 분석")
    parser.add_argument("--pair", default="", help="분석 페어 (e.g. usd_jpy). 미지정 시 전체")
    parser.add_argument(
        "--include-live", action="store_true",
        help="실전 포지션 데이터도 포함"
    )
    args = parser.parse_args()
    asyncio.run(main(args))
