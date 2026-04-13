"""
Account API — 잔고 조회.

GET /api/accounts/balance             — 현물 잔고 (거래소 API)
GET /api/accounts/collateral          — 증거금 상태 (거래소 API)
GET /api/accounts/positions           — 레버리지 열린 포지션 (거래소 API)
GET /api/accounts/positions/summary   — 포지션 요약 (거래소 API)
GET /api/accounts/executions          — 최신 약정 이력 (거래소 API)
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import AppState, get_state

router = APIRouter(prefix="/api/accounts", tags=["Account"])


@router.get("/balance")
async def get_balance(state: AppState = Depends(get_state)):
    """전체 잔고 조회."""
    balance = await state.adapter.get_balance()
    return {
        "exchange": state.adapter.exchange_name,
        "currencies": {
            code: {
                "currency": cb.currency,
                "amount": cb.amount,
                "available": cb.available,
            }
            for code, cb in balance.currencies.items()
        },
    }


@router.get("/collateral")
async def get_collateral(state: AppState = Depends(get_state)):
    """
    거래소 증거금 상태 직접 조회.
    CFD 매니저 없이도 호출 가능. 현물 전용 거래소는 501 반환.
    """
    if not hasattr(state.adapter, "get_collateral"):
        raise HTTPException(501, detail={"error": "이 거래소는 증거금 조회를 지원하지 않습니다"})

    c = await state.adapter.get_collateral()
    return {
        "exchange": state.adapter.exchange_name,
        "collateral": c.collateral,
        "open_position_pnl": c.open_position_pnl,
        "require_collateral": c.require_collateral,
        "keep_rate": c.keep_rate,
    }


@router.get("/positions/summary")
async def get_position_summary(
    symbol: str = Query("BTC_JPY", description="심볼 (BTC_JPY, USD_JPY 등)"),
    state: AppState = Depends(get_state),
):
    """
    거래소 포지션 요약 조회 (평균진입가, 합산수량 등).
    거래소 API 직접 호출 (DB 아님). 현물 전용 거래소는 501 반환.
    """
    if not hasattr(state.adapter, "get_position_summary"):
        raise HTTPException(501, detail={"error": "이 거래소는 포지션 요약을 지원하지 않습니다"})

    summary = await state.adapter.get_position_summary(symbol)
    return {
        "exchange": state.adapter.exchange_name,
        "symbol": symbol,
        "summary": summary,
    }


@router.get("/positions")
async def get_exchange_positions(
    symbol: str = Query("BTC_JPY", description="심볼 (BTC_JPY, FX_BTC_JPY, USD_JPY 등)"),
    state: AppState = Depends(get_state),
):
    """
    거래소 API에서 직접 열린 포지션(건옥) 목록을 조회한다.
    DB가 아닌 거래소 실제 상태를 반환한다. 현물 전용 거래소는 501 반환.
    """
    if not hasattr(state.adapter, "get_positions"):
        raise HTTPException(501, detail={"error": "이 거래소는 포지션 조회를 지원하지 않습니다"})

    positions = await state.adapter.get_positions(symbol)
    return {
        "exchange": state.adapter.exchange_name,
        "symbol": symbol,
        "count": len(positions),
        "positions": [
            {
                "position_id": p.position_id,
                "side": p.side,
                "price": p.price,
                "size": p.size,
                "pnl": p.pnl,
                "leverage": p.leverage,
                "open_date": p.open_date.isoformat() if p.open_date else None,
            }
            for p in positions
        ],
    }


@router.get("/executions")
async def get_latest_executions(
    symbol: str = Query("BTC_JPY", description="심볼"),
    count: int = Query(20, ge=1, le=100, description="조회 건수"),
    state: AppState = Depends(get_state),
):
    """
    거래소 API에서 최근 약정(체결) 이력을 직접 조회한다.
    orderId 없이 symbol 기준 최근 N건 반환. 현물 전용 거래소는 501 반환.
    """
    if not hasattr(state.adapter, "get_latest_executions"):
        raise HTTPException(501, detail={"error": "이 거래소는 약정 이력 조회를 지원하지 않습니다"})

    executions = await state.adapter.get_latest_executions(symbol, count)
    return {
        "exchange": state.adapter.exchange_name,
        "symbol": symbol,
        "count": len(executions),
        "executions": executions,
    }
