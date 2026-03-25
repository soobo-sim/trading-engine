"""
Analysis API — Rachel (전략 분석 에이전트) 전용 읽기 엔드포인트.

GET /api/analysis/box-history    — 박스 이력 + 포지션 성과 집계
GET /api/analysis/trade-stats    — 기간별 거래 통계 (승률, 기대값)
GET /api/analysis/regime         — 시장 체제 판단 (횡보/추세)
GET /api/analysis/trend-signal   — 추세추종 진입/청산 시그널 종합 판단

모든 엔드포인트는 읽기 전용. 트레이딩 로직에 영향 없음.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState, get_db, get_state
from api.services import analysis_service as svc

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
