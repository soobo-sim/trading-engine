"""
Performance Service — 성과 메트릭 + 백테스트 비즈니스 로직.

라우트(api/routes/performance.py)에서 추출.
"""
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState
from core.backtest.engine import BacktestConfig, run_backtest, run_grid_search

logger = logging.getLogger(__name__)

PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "180d": 180, "365d": 365, "all": 3650}
MAX_GRID_COMBINATIONS = 500


# ──────────────────────────────────────────────────────────────
# 메트릭 계산
# ──────────────────────────────────────────────────────────────

def compute_metrics(positions: list) -> dict:
    """
    종료 포지션 목록에서 종합 성과 메트릭 산출.

    Returns dict:
      total_trades, valid_trades, wins, losses, unknown
      win_rate, total_pnl_jpy, total_return_pct
      avg_win_pct, avg_loss_pct, expected_value_pct
      sharpe_ratio, max_drawdown_pct, max_consecutive_losses
      avg_holding_hours, monthly
    """
    if not positions:
        return empty_metrics()

    valid = [p for p in positions if p.realized_pnl_jpy is not None]
    wins = [p for p in valid if float(p.realized_pnl_jpy) > 0]
    losses = [p for p in valid if float(p.realized_pnl_jpy) <= 0]
    unknown_count = len(positions) - len(valid)

    win_rate = len(wins) / len(valid) if valid else None

    pnl_jpys = [float(p.realized_pnl_jpy) for p in valid]
    pnl_pcts = [
        float(p.realized_pnl_pct) for p in valid
        if p.realized_pnl_pct is not None
    ]
    total_pnl_jpy = sum(pnl_jpys)
    total_return_pct = sum(pnl_pcts) if pnl_pcts else None

    avg_win_pct = (
        sum(float(p.realized_pnl_pct) for p in wins if p.realized_pnl_pct) / len(wins)
        if wins else None
    )
    avg_loss_pct = (
        sum(float(p.realized_pnl_pct) for p in losses if p.realized_pnl_pct) / len(losses)
        if losses else None
    )

    ev = None
    if win_rate is not None and avg_win_pct is not None and avg_loss_pct is not None:
        ev = round(win_rate * avg_win_pct + (1 - win_rate) * avg_loss_pct, 4)

    sharpe = None
    if len(pnl_pcts) >= 2:
        mean_pct = sum(pnl_pcts) / len(pnl_pcts)
        variance = sum((x - mean_pct) ** 2 for x in pnl_pcts) / (len(pnl_pcts) - 1)
        std = math.sqrt(variance)
        if std > 0:
            sharpe = round(mean_pct / std, 2)

    max_drawdown_pct = _compute_max_drawdown(pnl_pcts)

    max_consec_loss = 0
    cur_consec = 0
    for p in positions:
        if p.realized_pnl_jpy is not None and float(p.realized_pnl_jpy) <= 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        elif p.realized_pnl_jpy is not None:
            cur_consec = 0

    holding_hours_list = []
    for p in valid:
        if p.created_at and p.closed_at:
            diff = (p.closed_at - p.created_at).total_seconds() / 3600
            if diff > 0:
                holding_hours_list.append(diff)
    avg_holding_hours = (
        round(sum(holding_hours_list) / len(holding_hours_list), 1)
        if holding_hours_list else None
    )

    monthly = _compute_monthly(valid)

    return {
        "total_trades": len(positions),
        "valid_trades": len(valid),
        "wins": len(wins),
        "losses": len(losses),
        "unknown": unknown_count,
        "win_rate": round(win_rate * 100, 1) if win_rate is not None else None,
        "total_pnl_jpy": round(total_pnl_jpy, 2),
        "total_return_pct": round(total_return_pct, 2) if total_return_pct is not None else None,
        "avg_win_pct": round(avg_win_pct, 4) if avg_win_pct is not None else None,
        "avg_loss_pct": round(avg_loss_pct, 4) if avg_loss_pct is not None else None,
        "expected_value_pct": ev,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": max_drawdown_pct,
        "max_consecutive_losses": max_consec_loss,
        "avg_holding_hours": avg_holding_hours,
        "monthly": monthly,
    }


def empty_metrics() -> dict:
    return {
        "total_trades": 0, "valid_trades": 0, "wins": 0,
        "losses": 0, "unknown": 0, "win_rate": None,
        "total_pnl_jpy": 0.0, "total_return_pct": None,
        "avg_win_pct": None, "avg_loss_pct": None,
        "expected_value_pct": None, "sharpe_ratio": None,
        "max_drawdown_pct": None, "max_consecutive_losses": 0,
        "avg_holding_hours": None, "monthly": [],
    }


def _compute_max_drawdown(pnl_pcts: List[float]) -> Optional[float]:
    """누적 PnL% 기준 최대 드로다운 계산."""
    if not pnl_pcts:
        return None
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pct in pnl_pcts:
        cumulative += pct
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2) if max_dd > 0 else 0.0


def _compute_monthly(positions: list) -> List[dict]:
    """월별 성과 집계."""
    monthly_map: Dict[str, list] = {}
    for p in positions:
        if not p.closed_at or p.realized_pnl_pct is None:
            continue
        key = p.closed_at.strftime("%Y-%m")
        monthly_map.setdefault(key, []).append(float(p.realized_pnl_pct))

    result = []
    for month in sorted(monthly_map.keys()):
        pcts = monthly_map[month]
        result.append({
            "month": month,
            "trades": len(pcts),
            "return_pct": round(sum(pcts), 2),
            "avg_pct": round(sum(pcts) / len(pcts), 4),
        })
    return result


# ──────────────────────────────────────────────────────────────
# DB 조회 헬퍼
# ──────────────────────────────────────────────────────────────

async def fetch_closed_positions(
    db: AsyncSession,
    state: AppState,
    pair: str,
    since: datetime,
    strategy_type: Optional[str] = None,
) -> Tuple[list, list]:
    """기간 내 종료 포지션 조회. Returns: (trend_positions, box_positions)"""
    TrendPos = state.models.trend_position
    BoxPos = state.models.box_position
    bp_pair_col = getattr(BoxPos, state.pair_column)

    trend_positions = []
    box_positions = []

    if strategy_type is None or strategy_type == "trend_following":
        trend_result = await db.execute(
            select(TrendPos)
            .where(and_(
                TrendPos.pair == pair,
                TrendPos.status == "closed",
                TrendPos.closed_at >= since,
            ))
            .order_by(TrendPos.closed_at)
        )
        trend_positions = list(trend_result.scalars().all())

    if strategy_type is None or strategy_type == "box_mean_reversion":
        box_result = await db.execute(
            select(BoxPos)
            .where(and_(
                bp_pair_col == pair,
                BoxPos.status == "closed",
                BoxPos.closed_at >= since,
            ))
            .order_by(BoxPos.closed_at)
        )
        box_positions = list(box_result.scalars().all())

    return trend_positions, box_positions


async def fetch_candles(
    db: AsyncSession,
    state: AppState,
    pair: str,
    timeframe: str,
    days: int,
) -> list:
    """DB에서 완성 캔들 조회 (시간순)."""
    CandleModel = state.models.candle
    pair_col = getattr(CandleModel, state.pair_column)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    result = await db.execute(
        select(CandleModel)
        .where(and_(
            pair_col == pair,
            CandleModel.timeframe == timeframe,
            CandleModel.is_complete == True,
            CandleModel.open_time >= since,
        ))
        .order_by(CandleModel.open_time)
    )
    return list(result.scalars().all())


# ──────────────────────────────────────────────────────────────
# 종합 성과 메트릭
# ──────────────────────────────────────────────────────────────

async def get_performance(
    pair: str, period: str, strategy_type: Optional[str],
    state: AppState, db: AsyncSession,
) -> dict:
    """실전 거래 성과 메트릭."""
    since = datetime.now(timezone.utc) - timedelta(days=PERIOD_DAYS[period])

    trend_positions, box_positions = await fetch_closed_positions(
        db, state, pair, since, strategy_type
    )

    all_positions = sorted(
        trend_positions + box_positions,
        key=lambda p: p.closed_at or datetime.min.replace(tzinfo=timezone.utc),
    )

    metrics = compute_metrics(all_positions)

    by_strategy = {}
    if trend_positions:
        by_strategy["trend_following"] = compute_metrics(trend_positions)
    if box_positions:
        by_strategy["box_mean_reversion"] = compute_metrics(box_positions)

    return {
        "success": True,
        "pair": pair,
        "period": period,
        "since": since.isoformat(),
        "metrics": metrics,
        "by_strategy": by_strategy,
    }


# ──────────────────────────────────────────────────────────────
# 성과 카드 생성 (archive 시 호출)
# ──────────────────────────────────────────────────────────────

async def compute_performance_summary(
    db: AsyncSession,
    state: AppState,
    strategy_id: int,
    pair: str,
    activated_at: Optional[datetime],
    archived_at: Optional[datetime],
) -> dict:
    """전략 archive 시 성과 카드 자동 생성."""
    since = activated_at or (datetime.now(timezone.utc) - timedelta(days=3650))
    until = archived_at or datetime.now(timezone.utc)

    TrendPos = state.models.trend_position
    BoxPos = state.models.box_position
    bp_pair_col = getattr(BoxPos, state.pair_column)

    trend_result = await db.execute(
        select(TrendPos).where(and_(
            TrendPos.pair == pair,
            TrendPos.strategy_id == strategy_id,
            TrendPos.status == "closed",
        ))
    )
    trend_positions = list(trend_result.scalars().all())

    box_result = await db.execute(
        select(BoxPos).where(and_(
            bp_pair_col == pair,
            BoxPos.status == "closed",
            BoxPos.closed_at >= since,
            BoxPos.closed_at <= until,
        ))
    )
    box_positions = list(box_result.scalars().all())

    all_positions = sorted(
        trend_positions + box_positions,
        key=lambda p: p.closed_at or datetime.min.replace(tzinfo=timezone.utc),
    )

    metrics = compute_metrics(all_positions)
    metrics["strategy_id"] = strategy_id
    metrics["pair"] = pair
    metrics["period_start"] = since.isoformat()
    metrics["period_end"] = until.isoformat()
    return metrics


# ──────────────────────────────────────────────────────────────
# 백테스트
# ──────────────────────────────────────────────────────────────

async def run_backtest_api(
    pair: str, params: dict, days: int, timeframe: str,
    initial_capital_jpy: float, slippage_pct: float, fee_pct: float,
    state: AppState, db: AsyncSession,
) -> dict:
    """캔들 리플레이 백테스트."""
    candles = await fetch_candles(db, state, pair, timeframe, days)
    if len(candles) < 25:
        return {"error": "INSUFFICIENT_CANDLES", "count": len(candles)}

    config = BacktestConfig(
        initial_capital_jpy=initial_capital_jpy,
        slippage_pct=slippage_pct,
        fee_pct=fee_pct,
    )
    result = run_backtest(candles, params, config)

    return {
        "success": True,
        "pair": pair,
        "timeframe": timeframe,
        "days": days,
        "result": result.to_dict(),
    }


async def run_grid_search_api(
    pair: str, base_params: dict, param_grid: dict,
    days: int, timeframe: str, top_n: int,
    initial_capital_jpy: float, slippage_pct: float, fee_pct: float,
    state: AppState, db: AsyncSession,
) -> dict:
    """파라미터 조합 자동 비교."""
    candles = await fetch_candles(db, state, pair, timeframe, days)
    if len(candles) < 25:
        return {"error": "INSUFFICIENT_CANDLES", "count": len(candles)}

    config = BacktestConfig(
        initial_capital_jpy=initial_capital_jpy,
        slippage_pct=slippage_pct,
        fee_pct=fee_pct,
    )
    result = run_grid_search(
        candles, base_params, param_grid, config, top_n,
    )

    return {
        "success": True,
        "pair": pair,
        "timeframe": timeframe,
        "days": days,
        "grid_search": result.to_dict(),
    }


# ──────────────────────────────────────────────────────────────
# 백테스트 vs 실전 괴리 비교
# ──────────────────────────────────────────────────────────────

async def compare_performance(
    pair: str, period: str, strategy_type: Optional[str],
    state: AppState, db: AsyncSession,
) -> dict:
    """동일 기간 백테스트 vs 실전 성과 괴리 비교."""
    days = PERIOD_DAYS[period]
    since = datetime.now(timezone.utc) - timedelta(days=days)

    StrategyModel = state.models.strategy
    stmt = (
        select(StrategyModel)
        .where(StrategyModel.status.in_(["active", "archived"]))
        .order_by(StrategyModel.created_at.desc())
    )
    result = await db.execute(stmt)
    strategies = result.scalars().all()

    target_strategy = None
    for s in strategies:
        s_pair = (s.parameters or {}).get("pair") or (s.parameters or {}).get("product_code")
        s_style = (s.parameters or {}).get("trading_style", "")
        if s_pair == pair:
            if strategy_type is None or s_style == strategy_type:
                target_strategy = s
                break

    if not target_strategy:
        return {"error": "NO_STRATEGY_FOUND", "pair": pair}

    params = target_strategy.parameters or {}

    trend_positions, box_positions = await fetch_closed_positions(
        db, state, pair, since, strategy_type
    )
    all_positions = sorted(
        trend_positions + box_positions,
        key=lambda p: p.closed_at or datetime.min.replace(tzinfo=timezone.utc),
    )
    live_metrics = compute_metrics(all_positions)

    timeframe = params.get("timeframe", "4h")
    candles = await fetch_candles(db, state, pair, timeframe, days)

    bt_metrics: Optional[dict] = None
    gap: Optional[dict] = None
    reliability_score: Optional[float] = None

    if len(candles) >= 25:
        bt_config = BacktestConfig(
            initial_capital_jpy=100_000.0,
            slippage_pct=0.05,
            fee_pct=0.15,
        )
        bt_result = run_backtest(candles, params, bt_config)
        bt_metrics = bt_result.to_dict()
        bt_metrics.pop("trades", None)

        bt_return = bt_result.total_return_pct
        live_return = live_metrics.get("total_return_pct")
        bt_sharpe = bt_result.sharpe_ratio
        live_sharpe = live_metrics.get("sharpe_ratio")
        bt_dd = bt_result.max_drawdown_pct
        live_dd = live_metrics.get("max_drawdown_pct")

        gap = {
            "return_gap": (
                round(live_return - bt_return, 2)
                if live_return is not None and bt_return is not None
                else None
            ),
            "sharpe_gap": (
                round(live_sharpe - bt_sharpe, 2)
                if live_sharpe is not None and bt_sharpe is not None
                else None
            ),
            "drawdown_gap": round((live_dd or 0) - (bt_dd or 0), 2),
        }

        if gap["return_gap"] is not None and bt_return is not None and bt_return != 0:
            abs_ratio = abs(gap["return_gap"] / bt_return) if bt_return != 0 else 1
            reliability_score = round(max(0, (1 - abs_ratio)) * 100, 0)
        elif live_return is not None and bt_return is not None:
            reliability_score = 100.0 if live_return == bt_return == 0 else 50.0

    return {
        "success": True,
        "pair": pair,
        "period": period,
        "strategy_id": target_strategy.id,
        "strategy_name": target_strategy.name,
        "backtest": bt_metrics,
        "live": live_metrics,
        "gap": gap,
        "reliability_score": reliability_score,
        "candle_count": len(candles),
    }


# ──────────────────────────────────────────────────────────────
# 전략별 성과 분해
# ──────────────────────────────────────────────────────────────

def _compute_grade(metrics: dict, trade_count: int) -> str:
    if trade_count < 10:
        return "insufficient"
    ev = metrics.get("expected_value")
    win_rate = metrics.get("win_rate")
    ev_positive = ev is not None and ev > 0
    wr_ok = win_rate is not None and win_rate >= 50.0
    if ev_positive and wr_ok:
        return "A"
    if ev_positive or wr_ok:
        return "B"
    return "C"


def _compute_metrics_for_strategy(positions: list) -> dict:
    """strategy_id 단위 성과 메트릭. NULL pnl 제외 + excluded_count 포함."""
    valid = [p for p in positions if p.realized_pnl_jpy is not None]
    excluded = len(positions) - len(valid)

    wins = [p for p in valid if float(p.realized_pnl_jpy) > 0]
    losses = [p for p in valid if float(p.realized_pnl_jpy) <= 0]

    total_pnl = sum(float(p.realized_pnl_jpy) for p in valid)
    avg_pnl = total_pnl / len(valid) if valid else 0.0
    max_loss = min((float(p.realized_pnl_jpy) for p in losses), default=None)
    max_win = max((float(p.realized_pnl_jpy) for p in wins), default=None)

    win_rate = round(len(wins) / len(valid) * 100, 2) if valid else 0.0

    # Sharpe (pnl_jpy 기준)
    sharpe = None
    if len(valid) >= 2:
        pnls = [float(p.realized_pnl_jpy) for p in valid]
        mean = sum(pnls) / len(pnls)
        variance = sum((x - mean) ** 2 for x in pnls) / (len(pnls) - 1)
        std = math.sqrt(variance)
        if std > 0:
            sharpe = round(mean / std, 4)

    # avg_holding_hours
    holding_hours_list = []
    for p in valid:
        if p.created_at and p.closed_at:
            ca, cl = p.created_at, p.closed_at
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            if cl.tzinfo is None:
                cl = cl.replace(tzinfo=timezone.utc)
            holding_hours_list.append((cl - ca).total_seconds() / 3600)
    avg_holding_hours = round(sum(holding_hours_list) / len(holding_hours_list), 2) if holding_hours_list else None

    # EV (JPY 기준 간이)
    ev = round(avg_pnl, 2) if valid else None

    return {
        "total_trades": len(positions),
        "excluded_null_pnl_count": excluded,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_pnl_jpy": round(total_pnl, 2),
        "avg_pnl_jpy": round(avg_pnl, 2),
        "max_single_loss_jpy": round(max_loss, 2) if max_loss is not None else None,
        "max_single_win_jpy": round(max_win, 2) if max_win is not None else None,
        "sharpe_ratio": sharpe,
        "avg_holding_hours": avg_holding_hours,
        "expected_value": ev,
    }


async def get_performance_by_strategy(
    pair: str,
    period: str,
    status_filter: Optional[str],
    state: AppState,
    db: AsyncSession,
) -> dict:
    """전략별 성과 비교표."""
    since = datetime.now(timezone.utc) - timedelta(days=PERIOD_DAYS[period])

    TrendPos = state.models.trend_position
    StrategyModel = state.models.strategy

    # 전략 목록 조회
    strategy_conditions = []
    if status_filter:
        strategy_conditions.append(StrategyModel.status == status_filter)
    strat_result = await db.execute(
        select(StrategyModel).where(*strategy_conditions) if strategy_conditions
        else select(StrategyModel)
    )
    strategies = strat_result.scalars().all()

    # 해당 pair 전략만 (parameters.pair 기준)
    pair_strategies = []
    for s in strategies:
        params = s.parameters or {}
        s_pair = params.get("pair") or params.get("product_code") or ""
        if s_pair.upper() == pair.upper():
            pair_strategies.append(s)

    # 전략별 포지션 조회
    result_rows = []
    all_positions_flat = []

    for s in pair_strategies:
        pos_result = await db.execute(
            select(TrendPos).where(and_(
                TrendPos.pair == pair,
                TrendPos.strategy_id == s.id,
                TrendPos.status == "closed",
                TrendPos.closed_at >= since,
            )).order_by(TrendPos.closed_at)
        )
        positions = list(pos_result.scalars().all())
        all_positions_flat.extend(positions)

        metrics = _compute_metrics_for_strategy(positions)
        grade = _compute_grade(metrics, len(positions))

        # active_from / active_to (strategy created_at / updated_at)
        active_from = getattr(s, "created_at", None)
        active_to = getattr(s, "updated_at", None) if s.status != "active" else None

        result_rows.append({
            "strategy_id": s.id,
            "name": s.name,
            "status": s.status,
            **metrics,
            "grade": grade,
            "active_from": active_from.isoformat() if active_from else None,
            "active_to": active_to.isoformat() if active_to else None,
        })

    # totals
    total_pnl = sum(r["total_pnl_jpy"] for r in result_rows)
    total_trades = sum(r["total_trades"] for r in result_rows)
    best = max(result_rows, key=lambda r: r["total_pnl_jpy"], default=None)
    worst = min(result_rows, key=lambda r: r["total_pnl_jpy"], default=None)

    return {
        "success": True,
        "pair": pair,
        "period": period,
        "since": since.isoformat(),
        "strategies": result_rows,
        "totals": {
            "total_trades": total_trades,
            "total_pnl_jpy": round(total_pnl, 2),
            "best_strategy_id": best["strategy_id"] if best else None,
            "worst_strategy_id": worst["strategy_id"] if worst else None,
        },
    }


async def get_performance_by_strategy_id(
    pair: str,
    period: str,
    strategy_id: int,
    state: AppState,
    db: AsyncSession,
) -> dict:
    """특정 strategy_id 포지션만 집계."""
    since = datetime.now(timezone.utc) - timedelta(days=PERIOD_DAYS[period])
    TrendPos = state.models.trend_position

    pos_result = await db.execute(
        select(TrendPos).where(and_(
            TrendPos.pair == pair,
            TrendPos.strategy_id == strategy_id,
            TrendPos.status == "closed",
            TrendPos.closed_at >= since,
        )).order_by(TrendPos.closed_at)
    )
    positions = list(pos_result.scalars().all())
    metrics = _compute_metrics_for_strategy(positions)
    grade = _compute_grade(metrics, len(positions))

    return {
        "success": True,
        "pair": pair,
        "period": period,
        "strategy_id": strategy_id,
        "since": since.isoformat(),
        **metrics,
        "grade": grade,
    }
