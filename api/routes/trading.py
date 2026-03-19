"""
Trading API — 주문 생성/취소/조회 + 제약조건.

POST   /api/exchange/orders          — 주문 생성
DELETE /api/exchange/orders/{id}      — 주문 취소
GET    /api/exchange/constraints      — 거래소 제약
GET    /api/exchange/orders/rate      — 환율 조회
GET    /api/exchange/orders/opens     — 미체결 주문
GET    /api/exchange/orders/{id}      — 주문 상세
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.dependencies import AppState, get_state
from core.exchange.types import OrderType

router = APIRouter(prefix="/api/exchange", tags=["Trading"])


# ── Schemas ──────────────────────────────────────────────────

class OrderRequest(BaseModel):
    pair: str
    order_type: str
    amount: float
    price: float | None = None
    reasoning: str = Field(..., min_length=20)


class CancelRequest(BaseModel):
    pair: str = ""


# ── 제약조건 ─────────────────────────────────────────────────

@router.get("/constraints")
async def get_constraints(state: AppState = Depends(get_state)):
    """거래소 제약 (최소 주문, 레이트 리밋 등)."""
    c = state.adapter.constraints
    return {
        "exchange": state.adapter.exchange_name,
        "min_order_sizes": c.min_order_sizes,
        "rate_limit": {"calls": c.rate_limit[0], "seconds": c.rate_limit[1]},
    }


# ── 주문 생성 ────────────────────────────────────────────────

@router.post("/orders")
async def create_order(
    body: OrderRequest,
    state: AppState = Depends(get_state),
):
    """주문 실행. ExchangeAdapter를 통해 거래소에 전송."""
    try:
        order_type = OrderType(body.order_type)
    except ValueError:
        raise HTTPException(400, f"Invalid order_type: {body.order_type}")

    order = await state.adapter.place_order(
        order_type=order_type,
        pair=body.pair,
        amount=body.amount,
        price=body.price,
    )
    return {
        "order_id": order.order_id,
        "pair": order.pair,
        "order_type": order.order_type,
        "amount": order.amount,
        "price": order.price,
        "status": order.status,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


# ── 주문 취소 ────────────────────────────────────────────────

@router.delete("/orders/{order_id}")
async def cancel_order(
    order_id: str,
    pair: str = Query(""),
    state: AppState = Depends(get_state),
):
    """주문 취소."""
    success = await state.adapter.cancel_order(order_id, pair)
    if not success:
        raise HTTPException(400, "주문 취소 실패")
    return {"cancelled": True, "order_id": order_id}


# ── 미체결 주문 ──────────────────────────────────────────────

@router.get("/orders/opens")
async def get_open_orders(
    pair: str = Query(""),
    state: AppState = Depends(get_state),
):
    """미체결 주문 목록."""
    orders = await state.adapter.get_open_orders(pair)
    return {
        "orders": [
            {
                "order_id": o.order_id,
                "pair": o.pair,
                "order_type": o.order_type,
                "amount": o.amount,
                "price": o.price,
                "status": o.status,
            }
            for o in orders
        ]
    }


# ── 주문 상세 ────────────────────────────────────────────────

@router.get("/orders/{order_id}")
async def get_order_detail(
    order_id: str,
    pair: str = Query(""),
    state: AppState = Depends(get_state),
):
    """단일 주문 상세 조회."""
    order = await state.adapter.get_order(order_id, pair)
    if not order:
        raise HTTPException(404, "주문 없음")
    return {
        "order_id": order.order_id,
        "pair": order.pair,
        "order_type": order.order_type,
        "amount": order.amount,
        "price": order.price,
        "status": order.status,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }
