"""
Candles API — RSI 등 기술적 지표 조회.

GET /api/candles/{pair}/{timeframe}/rsi   — RSI 조회
GET /api/candles/{pair}/{timeframe}       — 캔들 목록 (from/to 필터 지원)
"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState, get_db, get_state

router = APIRouter(prefix="/api/candles", tags=["Candles"])


@router.get("/{pair}/{timeframe}/rsi")
async def get_candle_rsi(
    pair: str,
    timeframe: str,
    period: int = Query(14, ge=2, le=100),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """완성 캔들 기반 Wilder RSI 계산."""
    CandleModel = state.models.candle
    pair_col = getattr(CandleModel, state.pair_column)

    stmt = (
        select(CandleModel)
        .where(
            pair_col == pair,
            CandleModel.timeframe == timeframe,
            CandleModel.is_complete == True,  # noqa: E712
        )
        .order_by(desc(CandleModel.open_time))
        .limit(period + 1)
    )
    result = await db.execute(stmt)
    candles = list(reversed(result.scalars().all()))

    if len(candles) < period + 1:
        raise HTTPException(400, f"캔들 부족: {len(candles)}개 (최소 {period + 1}개 필요)")

    # Wilder's RSI 계산
    closes = [float(c.close) for c in candles]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    latest = candles[-1]
    return {
        "pair": pair,
        "timeframe": timeframe,
        "period": period,
        "rsi": round(rsi, 2),
        "candle_count": len(candles),
        "latest_close": float(latest.close),
        "latest_open_time": latest.open_time.isoformat() if latest.open_time else None,
    }


@router.get("/{pair}/{timeframe}")
async def get_candles(
    pair: str,
    timeframe: str,
    limit: int = Query(100, ge=1, le=1000),
    from_: str | None = Query(None, alias="from", description="ISO8601 시작 시간 (포함)"),
    to: str | None = Query(None, description="ISO8601 종료 시간 (포함)"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """캔들 목록 조회. from/to 파라미터로 시점 범위 필터 가능 (Alice 사후 분석용)."""
    CandleModel = state.models.candle
    pair_col = getattr(CandleModel, state.pair_column)

    conditions = [
        pair_col == pair,
        CandleModel.timeframe == timeframe,
        CandleModel.is_complete == True,  # noqa: E712
    ]

    if from_:
        try:
            from_dt = datetime.fromisoformat(from_)
            conditions.append(CandleModel.open_time >= from_dt)
        except ValueError:
            raise HTTPException(400, f"from 파라미터 형식 오류: {from_}")

    if to:
        try:
            to_dt = datetime.fromisoformat(to)
            conditions.append(CandleModel.open_time <= to_dt)
        except ValueError:
            raise HTTPException(400, f"to 파라미터 형식 오류: {to}")

    stmt = (
        select(CandleModel)
        .where(*conditions)
        .order_by(asc(CandleModel.open_time) if (from_ or to) else desc(CandleModel.open_time))
        .limit(limit)
    )
    result = await db.execute(stmt)
    candles = result.scalars().all()

    return {
        "pair": pair,
        "timeframe": timeframe,
        "count": len(candles),
        "candles": [
            {
                "open_time": c.open_time.isoformat() if c.open_time else None,
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": float(c.volume) if c.volume else None,
            }
            for c in candles
        ],
    }
