"""
Performance API — 성과 메트릭 + 백테스트 vs 실전 괴리 비교.

Phase 1-B: 성과 메트릭 인프라
  GET /api/performance           — 종합 성과 메트릭 (수익률, 샤프, 드로다운 등)
  GET /api/performance/compare   — 백테스트 vs 실전 괴리 비교

Phase 1-C: 백테스트
  POST /api/backtest/run         — 백테스트 실행
  POST /api/backtest/grid        — 파라미터 그리드 서치
"""
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState, get_db, get_state
from core.backtest.engine import BacktestConfig, run_backtest, run_grid_search

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Performance"])


# ──────────────────────────────────────────────────────────────
# 헬퍼: 포지션 → 성과 메트릭 계산
# ──────────────────────────────────────────────────────────────

def _compute_metrics(positions: list) -> dict:
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
        return _empty_metrics()

    valid = [p for p in positions if p.realized_pnl_jpy is not None]
    wins = [p for p in valid if float(p.realized_pnl_jpy) > 0]
    losses = [p for p in valid if float(p.realized_pnl_jpy) <= 0]
    unknown_count = len(positions) - len(valid)

    win_rate = len(wins) / len(valid) if valid else None

    # PnL 집계
    pnl_jpys = [float(p.realized_pnl_jpy) for p in valid]
    pnl_pcts = [
        float(p.realized_pnl_pct) for p in valid
        if p.realized_pnl_pct is not None
    ]
    total_pnl_jpy = sum(pnl_jpys)

    # 수익률 합산 (각 트레이드의 % 수익의 합)
    total_return_pct = sum(pnl_pcts) if pnl_pcts else None

    avg_win_pct = (
        sum(float(p.realized_pnl_pct) for p in wins if p.realized_pnl_pct) / len(wins)
        if wins else None
    )
    avg_loss_pct = (
        sum(float(p.realized_pnl_pct) for p in losses if p.realized_pnl_pct) / len(losses)
        if losses else None
    )

    # Expected value (기대값)
    ev = None
    if win_rate is not None and avg_win_pct is not None and avg_loss_pct is not None:
        ev = round(win_rate * avg_win_pct + (1 - win_rate) * avg_loss_pct, 4)

    # Sharpe ratio (트레이드 단위) = mean(pnl_pct) / std(pnl_pct)
    sharpe = None
    if len(pnl_pcts) >= 2:
        mean_pct = sum(pnl_pcts) / len(pnl_pcts)
        variance = sum((x - mean_pct) ** 2 for x in pnl_pcts) / (len(pnl_pcts) - 1)
        std = math.sqrt(variance)
        if std > 0:
            sharpe = round(mean_pct / std, 2)

    # Max drawdown (누적 PnL 기준)
    max_drawdown_pct = _compute_max_drawdown(pnl_pcts)

    # 연속 손실
    max_consec_loss = 0
    cur_consec = 0
    for p in positions:
        if p.realized_pnl_jpy is not None and float(p.realized_pnl_jpy) <= 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        elif p.realized_pnl_jpy is not None:
            cur_consec = 0

    # 평균 보유 시간
    holding_hours_list = []
    for p in valid:
        if p.created_at and p.closed_at:
            created = p.created_at
            closed = p.closed_at
            diff = (closed - created).total_seconds() / 3600
            if diff > 0:
                holding_hours_list.append(diff)
    avg_holding_hours = (
        round(sum(holding_hours_list) / len(holding_hours_list), 1)
        if holding_hours_list else None
    )

    # 월별 수익
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


def _empty_metrics() -> dict:
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


async def _fetch_closed_positions(
    db: AsyncSession,
    state: AppState,
    pair: str,
    since: datetime,
    strategy_type: Optional[str] = None,
) -> Tuple[list, list]:
    """
    기간 내 종료 포지션을 조회.

    Returns: (trend_positions, box_positions)
    """
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


# ──────────────────────────────────────────────────────────────
# GET /api/performance — 종합 성과 메트릭
# ──────────────────────────────────────────────────────────────

PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "180d": 180, "365d": 365, "all": 3650}


@router.get("/api/performance", summary="종합 성과 메트릭")
async def get_performance(
    pair: str = Query(..., description="페어 (e.g. xrp_jpy)"),
    period: str = Query("30d", description="기간: 7d|30d|90d|180d|365d|all"),
    strategy_type: Optional[str] = Query(
        None, description="전략 필터: trend_following|box_mean_reversion (없으면 전체)"
    ),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """
    실전 거래 성과 메트릭.

    응답:
      수익률%, EV, 샤프 비율, 최대 드로다운, 거래 수, 월별 분해.
    """
    if period not in PERIOD_DAYS:
        raise HTTPException(400, {"blocked_code": "INVALID_PERIOD", "valid": list(PERIOD_DAYS.keys())})
    if strategy_type and strategy_type not in ("trend_following", "box_mean_reversion"):
        raise HTTPException(400, {"blocked_code": "INVALID_STRATEGY_TYPE"})

    since = datetime.now(timezone.utc) - timedelta(days=PERIOD_DAYS[period])

    trend_positions, box_positions = await _fetch_closed_positions(
        db, state, pair, since, strategy_type
    )

    # 전체 통합 (closed_at 기준 정렬)
    all_positions = sorted(
        trend_positions + box_positions,
        key=lambda p: p.closed_at or datetime.min.replace(tzinfo=timezone.utc),
    )

    metrics = _compute_metrics(all_positions)

    # 전략별 내역
    by_strategy = {}
    if trend_positions:
        by_strategy["trend_following"] = _compute_metrics(trend_positions)
    if box_positions:
        by_strategy["box_mean_reversion"] = _compute_metrics(box_positions)

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
    """
    전략 archive 시 성과 카드 자동 생성.

    activated_at ~ archived_at 기간의 실전 성과를 집계하여
    strategy.performance_summary JSON 필드에 저장.
    """
    since = activated_at or (datetime.now(timezone.utc) - timedelta(days=3650))
    until = archived_at or datetime.now(timezone.utc)

    TrendPos = state.models.trend_position
    BoxPos = state.models.box_position
    bp_pair_col = getattr(BoxPos, state.pair_column)

    # 추세추종 (strategy_id로 필터 — 해당 전략에서 발생한 포지션만)
    trend_result = await db.execute(
        select(TrendPos).where(and_(
            TrendPos.pair == pair,
            TrendPos.strategy_id == strategy_id,
            TrendPos.status == "closed",
        ))
    )
    trend_positions = list(trend_result.scalars().all())

    # 박스 (박스 포지션은 strategy_id 없음 → 기간 필터)
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

    metrics = _compute_metrics(all_positions)
    metrics["strategy_id"] = strategy_id
    metrics["pair"] = pair
    metrics["period_start"] = since.isoformat()
    metrics["period_end"] = until.isoformat()
    return metrics


# ──────────────────────────────────────────────────────────────
# POST /api/backtest/run — 백테스트 실행
# ──────────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    pair: str = Field(..., description="페어 (e.g. xrp_jpy)")
    params: dict = Field(..., description="전략 파라미터")
    days: int = Field(90, ge=7, le=365, description="백테스트 기간 (일)")
    timeframe: str = Field("4h", description="캔들 타임프레임: 1h | 4h")
    initial_capital_jpy: float = Field(100_000.0, ge=1000, description="초기 자본 (JPY)")
    slippage_pct: float = Field(0.05, ge=0, le=1.0, description="슬리피지 (%)")
    fee_pct: float = Field(0.15, ge=0, le=1.0, description="수수료 편도 (%)")


@router.post("/api/backtest/run", summary="백테스트 실행")
async def run_backtest_api(
    body: BacktestRequest,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """
    캔들 리플레이 백테스트.

    실전과 동일한 signals.py로 가상 매매 시뮬레이션.
    슬리피지 + 수수료 포함.
    """
    if body.timeframe not in ("1h", "4h"):
        raise HTTPException(400, {"blocked_code": "INVALID_TIMEFRAME"})

    candles = await _fetch_candles(db, state, body.pair, body.timeframe, body.days)
    if len(candles) < 25:
        raise HTTPException(400, {
            "blocked_code": "INSUFFICIENT_CANDLES",
            "detail": f"캔들 {len(candles)}개 — 최소 25개 필요",
        })

    config = BacktestConfig(
        initial_capital_jpy=body.initial_capital_jpy,
        slippage_pct=body.slippage_pct,
        fee_pct=body.fee_pct,
    )

    result = run_backtest(candles, body.params, config)

    return {
        "success": True,
        "pair": body.pair,
        "timeframe": body.timeframe,
        "days": body.days,
        "result": result.to_dict(),
    }


# ──────────────────────────────────────────────────────────────
# POST /api/backtest/grid — 파라미터 그리드 서치
# ──────────────────────────────────────────────────────────────

MAX_GRID_COMBINATIONS = 500  # 조합 수 상한


class GridSearchRequest(BaseModel):
    pair: str = Field(..., description="페어")
    base_params: dict = Field(..., description="기본 전략 파라미터")
    param_grid: dict = Field(..., description="그리드 서치 파라미터 (키: 파라미터명, 값: 후보 리스트)")
    days: int = Field(90, ge=7, le=365, description="백테스트 기간 (일)")
    timeframe: str = Field("4h", description="캔들 타임프레임")
    top_n: int = Field(10, ge=1, le=50, description="상위 N개 결과")
    initial_capital_jpy: float = Field(100_000.0, ge=1000)
    slippage_pct: float = Field(0.05, ge=0, le=1.0)
    fee_pct: float = Field(0.15, ge=0, le=1.0)


@router.post("/api/backtest/grid", summary="파라미터 그리드 서치")
async def grid_search_api(
    body: GridSearchRequest,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """
    파라미터 조합 자동 비교.

    param_grid의 모든 조합으로 백테스트 실행 후 Sharpe ratio 기준 정렬.
    """
    if body.timeframe not in ("1h", "4h"):
        raise HTTPException(400, {"blocked_code": "INVALID_TIMEFRAME"})

    # 조합 수 검증
    total = 1
    for vals in body.param_grid.values():
        if not isinstance(vals, list):
            raise HTTPException(400, {"blocked_code": "INVALID_PARAM_GRID", "detail": "값은 리스트여야 합니다"})
        total *= len(vals)
    if total > MAX_GRID_COMBINATIONS:
        raise HTTPException(400, {
            "blocked_code": "TOO_MANY_COMBINATIONS",
            "detail": f"조합 {total}개 > 상한 {MAX_GRID_COMBINATIONS}개",
        })

    candles = await _fetch_candles(db, state, body.pair, body.timeframe, body.days)
    if len(candles) < 25:
        raise HTTPException(400, {
            "blocked_code": "INSUFFICIENT_CANDLES",
            "detail": f"캔들 {len(candles)}개",
        })

    config = BacktestConfig(
        initial_capital_jpy=body.initial_capital_jpy,
        slippage_pct=body.slippage_pct,
        fee_pct=body.fee_pct,
    )

    result = run_grid_search(
        candles, body.base_params, body.param_grid, config, body.top_n,
    )

    return {
        "success": True,
        "pair": body.pair,
        "timeframe": body.timeframe,
        "days": body.days,
        "grid_search": result.to_dict(),
    }


# ──────────────────────────────────────────────────────────────
# GET /api/performance/compare — 백테스트 vs 실전 괴리 비교
# ──────────────────────────────────────────────────────────────

@router.get("/api/performance/compare", summary="백테스트 vs 실전 괴리 비교")
async def compare_performance(
    pair: str = Query(..., description="페어"),
    period: str = Query("90d", description="기간: 7d|30d|90d|180d|365d|all"),
    strategy_type: Optional[str] = Query(
        None, description="전략 필터: trend_following|box_mean_reversion"
    ),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """
    동일 기간 백테스트 vs 실전 성과 괴리 비교.

    1. 실전 성과 집계
    2. 동일 기간 캔들로 백테스트 실행 (활성 전략 파라미터 사용)
    3. 괴리 계산 + 신뢰도 점수
    """
    if period not in PERIOD_DAYS:
        raise HTTPException(400, {"blocked_code": "INVALID_PERIOD"})

    days = PERIOD_DAYS[period]
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # 1. 활성 전략 파라미터 가져오기 (최신 active or archived)
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
        raise HTTPException(404, {
            "blocked_code": "NO_STRATEGY_FOUND",
            "detail": f"pair={pair}에 해당하는 전략 없음",
        })

    params = target_strategy.parameters or {}

    # 2. 실전 성과
    trend_positions, box_positions = await _fetch_closed_positions(
        db, state, pair, since, strategy_type
    )
    all_positions = sorted(
        trend_positions + box_positions,
        key=lambda p: p.closed_at or datetime.min.replace(tzinfo=timezone.utc),
    )
    live_metrics = _compute_metrics(all_positions)

    # 3. 백테스트 (동일 기간 캔들)
    timeframe = params.get("timeframe", "4h")
    candles = await _fetch_candles(db, state, pair, timeframe, days)

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
        # trades 상세는 compare에서 불필요
        bt_metrics.pop("trades", None)

        # 4. 괴리 계산
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
            "drawdown_gap": (
                round((live_dd or 0) - (bt_dd or 0), 2)
            ),
        }

        # 신뢰도: 수익률 괴리가 작을수록 높음
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
# 캔들 조회 헬퍼
# ──────────────────────────────────────────────────────────────

async def _fetch_candles(
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
