"""
Account API — 잔고 조회.

GET /api/accounts/balance
"""
from fastapi import APIRouter, Depends

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
