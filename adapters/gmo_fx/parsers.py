"""GMO FX API 응답 → Order DTO 파서."""
from __future__ import annotations

from datetime import datetime, timezone

from core.exchange.types import Order, OrderSide, OrderStatus, OrderType


def parse_order(data: dict, pair: str = "") -> Order:
    """GMO FX 주문 응답 → Order DTO."""
    side_str = data.get("side", "BUY").upper()
    side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL

    exec_type = data.get("executionType", "MARKET").upper()
    if side == OrderSide.BUY:
        order_type = OrderType.BUY if exec_type == "LIMIT" else OrderType.MARKET_BUY
    else:
        order_type = OrderType.SELL if exec_type == "LIMIT" else OrderType.MARKET_SELL

    price: float | None = None
    raw_price = data.get("price") or data.get("orderPrice")
    if raw_price is not None:
        try:
            price = float(raw_price)
        except (ValueError, TypeError):
            pass

    raw_size = data.get("size", data.get("orderSize", 0))
    try:
        amount = float(raw_size)
    except (ValueError, TypeError):
        amount = 0.0

    # symbol → pair (USD_JPY → usd_jpy)
    symbol = data.get("symbol", "")
    resolved_pair = pair or symbol.lower()

    order_status_str = data.get("orderStatus", "ORDERED").upper()
    status_map = {
        "WAITING": OrderStatus.PENDING,
        "ORDERED": OrderStatus.OPEN,
        "MODIFYING": OrderStatus.OPEN,
        "EXECUTED": OrderStatus.COMPLETED,
        "CANCELED": OrderStatus.CANCELLED,
        "EXPIRED": OrderStatus.CANCELLED,
    }
    status = status_map.get(order_status_str, OrderStatus.OPEN)

    root_order_id = str(data.get("rootOrderId", data.get("orderId", "")))

    return Order(
        order_id=root_order_id,
        pair=resolved_pair,
        order_type=order_type,
        side=side,
        price=price,
        amount=amount,
        status=status,
        created_at=datetime.now(timezone.utc),
        raw=data,
    )
