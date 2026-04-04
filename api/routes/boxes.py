"""
Boxes API — 박스권 조회 + 포지션 관리.

GET /api/boxes/{pair}                   — 활성 박스
GET /api/boxes/{pair}/history           — 박스 이력
GET /api/boxes/{pair}/position          — 현재가 박스 내 위치
GET /api/boxes/{pair}/active-position   — 활성 포지션 (trend or box)
GET /api/boxes/{pair}/positions/history — 포지션 이력
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from api.dependencies import AppState, get_db, get_state

router = APIRouter(prefix="/api/boxes", tags=["Boxes"])


@router.get("/{pair}")
async def get_active_box(
    pair: str,
    strategy_id: Optional[int] = Query(None, description="strategy_id 지정 시 해당 paper 박스만 반환. 미지정 시 active 전략 박스(strategy_id=NULL)만 반환."),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """활성 박스 조회. strategy_id 미지정 = active 전략 박스(후방 호환)."""
    pair = state.normalize_pair(pair)
    BoxModel = state.models.box
    pair_col = getattr(BoxModel, state.pair_column)
    if strategy_id is None:
        sid_filter = BoxModel.strategy_id.is_(None)
    else:
        sid_filter = BoxModel.strategy_id == strategy_id
    stmt = (
        select(BoxModel)
        .where(pair_col == pair, BoxModel.status == "active", sid_filter)
        .order_by(BoxModel.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    box = result.scalar_one_or_none()
    if not box:
        return {"box": None, "pair": pair}
    return {"box": _box_to_dict(box, state.pair_column), "pair": pair}


@router.get("/{pair}/history")
async def get_box_history(
    pair: str,
    limit: int = Query(10, ge=1, le=50),
    strategy_id: Optional[int] = Query(None, description="strategy_id 지정 시 해당 전략 박스만 반환. 미지정 시 active 전략 박스(strategy_id=NULL)만 반환."),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """박스 이력 (active + invalidated). strategy_id 미지정 = active 전략 박스(후방 호환)."""
    pair = state.normalize_pair(pair)
    BoxModel = state.models.box
    pair_col = getattr(BoxModel, state.pair_column)
    if strategy_id is None:
        sid_filter = BoxModel.strategy_id.is_(None)
    else:
        sid_filter = BoxModel.strategy_id == strategy_id
    stmt = (
        select(BoxModel)
        .where(pair_col == pair, sid_filter)
        .order_by(BoxModel.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return {"boxes": [_box_to_dict(r, state.pair_column) for r in rows]}


@router.get("/{pair}/position")
async def get_price_position(
    pair: str,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """현재가의 박스 내 위치 (near_lower/near_upper/middle/outside/no_box)."""
    pair = state.normalize_pair(pair)
    ticker = await state.adapter.get_ticker(pair)
    price = ticker.last

    BoxModel = state.models.box
    pair_col = getattr(BoxModel, state.pair_column)
    stmt = (
        select(BoxModel)
        .where(pair_col == pair, BoxModel.status == "active", BoxModel.strategy_id.is_(None))
        .limit(1)
    )
    result = await db.execute(stmt)
    box = result.scalar_one_or_none()

    if not box:
        # 박스 미형성 시 형성 진행 상황 계산
        progress_data = None
        try:
            from core.analysis.box_detector import detect_box_progress
            from sqlalchemy import and_

            # 활성 전략에서 파라미터 가져오기
            StratModel = state.models.strategy
            strat_result = await db.execute(
                select(StratModel).where(StratModel.status == "active")
            )
            strat = strat_result.scalar_one_or_none()
            params = (strat.parameters or {}) if strat else {}

            lookback = int(params.get("lookback_candles", 40))
            tol = float(params.get("box_tolerance_pct", 0.5))
            min_t = int(params.get("min_touches", 3))
            basis_tf = params.get("basis_timeframe", "4h")
            CandleModel = state.models.candle
            candle_pair_col = getattr(CandleModel, state.pair_column)
            candle_result = await db.execute(
                select(CandleModel)
                .where(
                    and_(
                        candle_pair_col == pair,
                        CandleModel.timeframe == basis_tf,
                        CandleModel.is_complete == True,
                    )
                )
                .order_by(CandleModel.open_time.desc())
                .limit(lookback)
            )
            candles = list(reversed(candle_result.scalars().all()))
            if candles:
                highs = [max(float(c.open), float(c.close)) for c in candles]
                lows = [min(float(c.open), float(c.close)) for c in candles]
                progress = detect_box_progress(highs, lows, tol, min_t)
                progress_data = {
                    "upper_touches": progress.upper_touches,
                    "lower_touches": progress.lower_touches,
                    "min_touches": progress.min_touches,
                    "candles_remaining": progress.candles_remaining,
                    "upper_center": progress.upper_center,
                    "lower_center": progress.lower_center,
                    "basis_timeframe": basis_tf,
                }
        except Exception:
            pass

        return {"pair": pair, "price": price, "position": "no_box", "formation_progress": progress_data}

    upper = float(box.upper_bound)
    lower = float(box.lower_bound)
    tol = float(box.tolerance_pct) / 100.0
    box_range = upper - lower

    if price < lower * (1 - tol) or price > upper * (1 + tol):
        pos = "outside"
    elif abs(price - lower) <= box_range * 0.2:
        pos = "near_lower"
    elif abs(price - upper) <= box_range * 0.2:
        pos = "near_upper"
    else:
        pos = "middle"

    return {
        "pair": pair,
        "price": price,
        "position": pos,
        "box": _box_to_dict(box, state.pair_column),
    }


@router.get("/{pair}/active-position")
async def get_active_position(
    pair: str,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """활성 포지션 조회 (trend_following 또는 box_mean_reversion)."""
    pair = state.normalize_pair(pair)
    # 활성 전략의 trading_style 확인
    StrategyModel = state.models.strategy
    stmt = select(StrategyModel).where(StrategyModel.status == "active")
    result = await db.execute(stmt)
    active_strategies = result.scalars().all()

    trading_style = None
    for s in active_strategies:
        s_pair = state.normalize_pair((s.parameters or {}).get("pair", ""))
        if s_pair == pair:
            trading_style = (s.parameters or {}).get("trading_style")
            break

    # 트렌드 계열은 TrendPosition, 박스는 BoxPosition 조회
    _TREND_STYLES = {"trend_following", "cfd_trend_following"}
    if trading_style in _TREND_STYLES:
        return await _get_trend_position(pair, state, db)
    else:
        return await _get_box_position(pair, state, db)


@router.get("/positions/{position_id}")
async def get_position_by_id(
    position_id: int,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """포지션 단건 조회 (Alice 사후 분석용). GET /api/boxes/positions/{position_id}"""
    TrendPos = state.models.trend_position
    stmt = select(TrendPos).where(TrendPos.id == position_id)
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"position {position_id} not found")
    return {"position": _trend_pos_to_dict(row), "type": "trend_following"}


@router.get("/{pair}/positions/history")
async def get_position_history(
    pair: str,
    limit: int = Query(20, ge=1, le=100),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """포지션 이력 (closed)."""
    pair = state.normalize_pair(pair)
    # 활성 전략의 trading_style 확인
    StrategyModel = state.models.strategy
    stmt = select(StrategyModel).where(StrategyModel.status == "active")
    result = await db.execute(stmt)
    active_strategies = result.scalars().all()

    trading_style = None
    for s in active_strategies:
        s_pair = state.normalize_pair((s.parameters or {}).get("pair", ""))
        if s_pair == pair:
            trading_style = (s.parameters or {}).get("trading_style")
            break

    _TREND_STYLES = {"trend_following", "cfd_trend_following"}
    if trading_style in _TREND_STYLES:
        TrendPos = state.models.trend_position
        stmt = (
            select(TrendPos)
            .where(TrendPos.pair == pair, TrendPos.status == "closed")
            .order_by(TrendPos.closed_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        return {"positions": [_trend_pos_to_dict(r) for r in rows]}
    else:
        BoxPos = state.models.box_position
        pair_col = getattr(BoxPos, state.pair_column)
        stmt = (
            select(BoxPos)
            .where(pair_col == pair, BoxPos.status == "closed")
            .order_by(BoxPos.closed_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        return {"positions": [_box_pos_to_dict(r, state.pair_column) for r in rows]}


# ── 내부 조회 ────────────────────────────────────────────────

async def _get_trend_position(pair: str, state: AppState, db: AsyncSession) -> dict:
    TrendPos = state.models.trend_position
    stmt = (
        select(TrendPos)
        .where(TrendPos.pair == pair, TrendPos.status == "open")
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if not row:
        return {"position": None, "pair": pair, "type": "trend_following"}

    # 미실현 손익 계산
    current_price = None
    try:
        ticker = await state.adapter.get_ticker(pair)
        current_price = ticker.last
    except Exception:
        pass

    return {"position": _trend_pos_to_dict(row, current_price), "pair": pair, "type": "trend_following"}


async def _get_box_position(pair: str, state: AppState, db: AsyncSession) -> dict:
    BoxPos = state.models.box_position
    pair_col = getattr(BoxPos, state.pair_column)
    stmt = (
        select(BoxPos)
        .where(pair_col == pair, BoxPos.status == "open")
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if not row:
        return {"position": None, "pair": pair, "type": "box_mean_reversion"}

    # 미실현 손익 계산
    current_price = None
    try:
        ticker = await state.adapter.get_ticker(pair)
        current_price = ticker.last
    except Exception:
        pass

    return {
        "position": _box_pos_to_dict(row, state.pair_column, current_price),
        "pair": pair,
        "type": "box_mean_reversion",
    }


# ── 변환 ─────────────────────────────────────────────────────

def _box_to_dict(box, pair_column: str) -> dict:
    return {
        "id": box.id,
        "pair": getattr(box, pair_column),
        "strategy_id": box.strategy_id,
        "upper_bound": float(box.upper_bound),
        "lower_bound": float(box.lower_bound),
        "upper_touch_count": box.upper_touch_count,
        "lower_touch_count": box.lower_touch_count,
        "tolerance_pct": float(box.tolerance_pct),
        "status": box.status,
        "invalidation_reason": box.invalidation_reason,
        "created_at": box.created_at.isoformat() if box.created_at else None,
    }


def _trend_pos_to_dict(row, current_price: float | None = None) -> dict:
    entry_price = float(row.entry_price)
    entry_amount = float(row.entry_amount)
    quantity = entry_amount / entry_price if entry_price else None

    unrealized_pnl_jpy = None
    unrealized_pnl_pct = None
    if current_price and entry_price and quantity:
        unrealized_pnl_jpy = round((current_price - entry_price) * quantity, 0)
        unrealized_pnl_pct = round((current_price - entry_price) / entry_price * 100, 2)

    return {
        "id": row.id,
        "pair": row.pair,
        "entry_price": entry_price,
        "entry_amount": entry_amount,
        "quantity": quantity,
        "stop_loss_price": float(row.stop_loss_price) if row.stop_loss_price else None,
        "exit_price": float(row.exit_price) if row.exit_price else None,
        "exit_reason": row.exit_reason,
        "current_price": current_price,
        "unrealized_pnl_jpy": unrealized_pnl_jpy,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "realized_pnl_jpy": float(row.realized_pnl_jpy) if row.realized_pnl_jpy else None,
        "realized_pnl_pct": float(row.realized_pnl_pct) if row.realized_pnl_pct else None,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
        # 진입 시그널 스냅샷 (Alice 사후 분석용)
        "entry_rsi": float(row.entry_rsi) if getattr(row, "entry_rsi", None) is not None else None,
        "entry_ema_slope": float(row.entry_ema_slope) if getattr(row, "entry_ema_slope", None) is not None else None,
        "entry_atr": float(row.entry_atr) if getattr(row, "entry_atr", None) is not None else None,
        "entry_regime": getattr(row, "entry_regime", None),
        "entry_bb_width": float(row.entry_bb_width) if getattr(row, "entry_bb_width", None) is not None else None,
    }


def _box_pos_to_dict(row, pair_column: str, current_price: float | None = None) -> dict:
    entry_price = float(row.entry_price)
    entry_amount = float(row.entry_amount)
    quantity = entry_amount / entry_price if entry_price else None

    unrealized_pnl_jpy = None
    unrealized_pnl_pct = None
    if current_price and entry_price and quantity:
        unrealized_pnl_jpy = round((current_price - entry_price) * quantity, 0)
        unrealized_pnl_pct = round((current_price - entry_price) / entry_price * 100, 2)

    return {
        "id": row.id,
        "pair": getattr(row, pair_column),
        "box_id": row.box_id,
        "entry_price": entry_price,
        "entry_amount": entry_amount,
        "quantity": quantity,
        "exit_price": float(row.exit_price) if row.exit_price else None,
        "exit_reason": row.exit_reason,
        "current_price": current_price,
        "unrealized_pnl_jpy": unrealized_pnl_jpy,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "realized_pnl_jpy": float(row.realized_pnl_jpy) if row.realized_pnl_jpy else None,
        "realized_pnl_pct": float(row.realized_pnl_pct) if row.realized_pnl_pct else None,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
    }
