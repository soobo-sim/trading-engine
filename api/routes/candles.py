"""
Candles API — RSI 등 기술적 지표 조회.

GET /api/candles/{pair}/{timeframe}/rsi  — RSI 조회
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
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
