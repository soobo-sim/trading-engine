"""BitFlyer API 응답 → Order DTO 파서."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from core.exchange.types import Order, OrderSide, OrderStatus, OrderType


def parse_order(data: dict, pair: str = "") -> Order:
    """BitFlyer 주문 응답 → Order DTO."""
    side_str = data.get("side", "BUY").upper()
    side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL

    child_order_type = data.get("child_order_type", "LIMIT").upper()
    if side == OrderSide.BUY:
        order_type = OrderType.BUY if child_order_type == "LIMIT" else OrderType.MARKET_BUY
    else:
        order_type = OrderType.SELL if child_order_type == "LIMIT" else OrderType.MARKET_SELL

    price: Optional[float] = None
    raw_price = data.get("price")
    if raw_price is not None:
        try:
            price = float(raw_price)
        except (ValueError, TypeError):
            pass

    raw_size = data.get("size", data.get("outstanding_size", 0))
    try:
        amount = float(raw_size)
    except (ValueError, TypeError):
        amount = 0.0

    # product_code → pair 역변환 (XRP_JPY → xrp_jpy)
    product_code = data.get("product_code", "")
    resolved_pair = pair or product_code.lower() if product_code else pair

    order_state = data.get("child_order_state", "ACTIVE")
    if order_state == "COMPLETED":
        status = OrderStatus.COMPLETED
    elif order_state == "CANCELED":
        status = OrderStatus.CANCELLED
    else:
        status = OrderStatus.OPEN

    created_at: Optional[datetime] = None
    raw_ts = data.get("child_order_date")
    if raw_ts:
        try:
            created_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    order_id = str(
        data.get("child_order_acceptance_id") or data.get("child_order_id", "")
    )

    return Order(
        order_id=order_id,
        pair=resolved_pair,
        order_type=order_type,
        side=side,
        price=price,
        amount=amount,
        status=status,
        created_at=created_at,
        raw=data,
    )
