"""
Analysis API — Rachel (전략 분석 에이전트) 전용 읽기 엔드포인트.

GET /api/analysis/box-history    — 박스 이력 + 포지션 성과 집계
GET /api/analysis/trade-stats    — 기간별 거래 통계 (승률, 기대값)
GET /api/analysis/regime         — 시장 체제 판단 (횡보/추세)
GET /api/analysis/trend-signal   — 추세추종 진입/청산 시그널 종합 판단
GET /api/analysis/box-detect     — 박스권 독립 감지 (전략 무관)

모든 엔드포인트는 읽기 전용. 트레이딩 로직에 영향 없음.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState, get_db, get_state
from api.services import analysis_service as svc
from core.analysis.box_detector import detect_box

router = APIRouter(prefix="/api/analysis", tags=["Analysis (Rachel)"])


@router.get("/box-history", summary="박스 이력 + 포지션 성과 집계")
async def get_box_history(
    pair: str = Query(..., description="페어 (e.g. xrp_jpy / XRP_JPY)"),
    days: int = Query(30, ge=1, le=365, description="조회 기간 (일)"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """박스 이력 + 각 박스 포지션 성과 + 추세추종 포지션 별도 집계."""
    return await svc.get_box_history(pair, days, state, db)


@router.get("/trade-stats", summary="기간별 거래 통계")
async def get_trade_stats(
    pair: str = Query(..., description="페어 (e.g. xrp_jpy / XRP_JPY)"),
    days: int = Query(30, ge=1, le=365, description="조회 기간 (일)"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """기간별 거래 통계 (승률, 기대값, 연속 손실 등)."""
    return await svc.get_trade_stats(pair, days, state, db)


@router.get("/regime", summary="시장 체제 판단")
async def get_market_regime(
    pair: str = Query(..., description="페어 (e.g. xrp_jpy / XRP_JPY)"),
    timeframe: str = Query("4h", description="캔들 타임프레임: 1h | 4h"),
    lookback: int = Query(50, ge=10, le=200, description="분석할 캔들 개수"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """시장 체제 판단 (횡보/추세)."""
    if timeframe not in ("1h", "4h"):
        raise HTTPException(400, {"blocked_code": "INVALID_TIMEFRAME"})
    return await svc.get_market_regime(pair, timeframe, lookback, state, db)


@router.get("/trend-signal", summary="추세추종 진입/청산 시그널")
async def get_trend_signal(
    pair: str = Query(..., description="페어 (e.g. xrp_jpy / XRP_JPY)"),
    timeframe: str = Query("4h", description="캔들 타임프레임: 1h | 4h"),
    ema_period: int = Query(20, ge=5, le=200, description="EMA 기간"),
    atr_period: int = Query(14, ge=5, le=100, description="ATR 기간"),
    rsi_entry_low: float = Query(40.0, ge=20.0, le=60.0, description="RSI 진입 하한"),
    rsi_entry_high: float = Query(65.0, ge=50.0, le=80.0, description="RSI 진입 상한"),
    ema_slope_entry_min: float = Query(0.0, ge=-0.5, le=0.5, description="EMA slope 진입 최소 임곗값(%)"),
    entry_price: Optional[float] = Query(None, description="현재 포지션 진입가 (청산 시그널 판단용)"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """추세추종 전략 진입/청산 시그널 종합 판단."""
    if timeframe not in ("1h", "4h"):
        raise HTTPException(400, {"blocked_code": "INVALID_TIMEFRAME"})
    return await svc.get_trend_signal(
        pair, timeframe, ema_period, atr_period,
        rsi_entry_low, rsi_entry_high, ema_slope_entry_min,
        entry_price, state, db,
    )


@router.get("/box-detect", summary="박스권 독립 감지 (전략 무관)")
async def get_box_detect(
    pair: str = Query(..., description="페어 (e.g. BTC_JPY)"),
    timeframe: str = Query("4h", description="캔들 타임프레임"),
    lookback: int = Query(60, ge=6, le=500, description="캔들 수"),
    tolerance_pct: float = Query(0.5, ge=0.0, le=10.0, description="클러스터 허용 오차 (%)"),
    min_touches: int = Query(3, ge=2, le=20, description="최소 터치 횟수"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """박스권 독립 감지. 활성 전략 없어도 분석 가능."""
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
        .limit(lookback)
    )
    result = await db.execute(stmt)
    candles = list(reversed(result.scalars().all()))

    if not candles:
        raise HTTPException(400, f"캔들 없음: pair={pair}, timeframe={timeframe}")

    if len(candles) < min_touches * 2:
        raise HTTPException(
            400,
            f"캔들 부족: {len(candles)}개 (min_touches={min_touches} 기준 최소 {min_touches * 2}개 필요)",
        )

    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]

    det = detect_box(highs, lows, tolerance_pct=tolerance_pct, min_touches=min_touches)

    # 현재가 계산용: 최신 캔들 종가
    current_price = float(candles[-1].close)

    candle_range = {
        "from": candles[0].open_time.isoformat() if candles[0].open_time else None,
        "to": candles[-1].open_time.isoformat() if candles[-1].open_time else None,
    }

    params_used = {
        "pair": pair,
        "timeframe": timeframe,
        "lookback": lookback,
        "tolerance_pct": tolerance_pct,
        "min_touches": min_touches,
        "candles_fetched": len(candles),
    }

    if not det.box_detected:
        return {
            "success": True,
            "pair": pair,
            "box_detected": False,
            "box": None,
            "reason": det.reason,
            "params_used": params_used,
            "candle_range": candle_range,
        }

    upper = det.upper_bound
    lower = det.lower_bound
    width_pct = det.width_pct

    # price_position
    zone_pct = width_pct * 0.15 if width_pct else 0
    upper_zone = upper * (1 - zone_pct / 100)
    lower_zone = lower * (1 + zone_pct / 100)

    if current_price > upper:
        price_position = "above"
    elif current_price >= upper_zone:
        price_position = "upper_zone"
    elif current_price <= lower:
        price_position = "below"
    elif current_price <= lower_zone:
        price_position = "lower_zone"
    else:
        price_position = "mid"

    distance_to_upper_pct = round((upper - current_price) / current_price * 100, 4) if upper else None
    distance_to_lower_pct = round((current_price - lower) / current_price * 100, 4) if lower else None

    return {
        "success": True,
        "pair": pair,
        "box_detected": True,
        "box": {
            "upper_bound": round(upper, 2),
            "lower_bound": round(lower, 2),
            "upper_touch_count": det.upper_touch_count,
            "lower_touch_count": det.lower_touch_count,
            "width_pct": width_pct,
            "current_price": current_price,
            "price_position": price_position,
            "distance_to_upper_pct": distance_to_upper_pct,
            "distance_to_lower_pct": distance_to_lower_pct,
        },
        "params_used": params_used,
        "candle_range": candle_range,
    }
