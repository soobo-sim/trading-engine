"""GMO 코인 API 응답 → Order DTO 파서."""
from __future__ import annotations

from datetime import datetime, timezone

from core.exchange.types import Order, OrderSide, OrderStatus, OrderType


def parse_order(data: dict, pair: str = "") -> Order:
    """
    GMO 코인 주문 응답 → Order DTO.

    GmoFx 파서와 유사하나 다음 차이:
    - orderId는 number (int → str 변환)
    - settleType 필드 존재 (OPEN/CLOSE) — raw에 보존
    - losscutPrice 필드 존재 — raw에 보존
    - orderStatus(WS) vs status(REST) 두 가지 필드 처리
    """
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
            p = float(raw_price)
            if p > 0:
                price = p
        except (ValueError, TypeError):
            pass

    raw_size = data.get("size") or data.get("orderSize") or data.get("executionSize", 0)
    try:
        amount = float(raw_size)
    except (ValueError, TypeError):
        amount = 0.0

    # symbol → pair (BTC_JPY → btc_jpy)
    symbol = data.get("symbol", "")
    resolved_pair = pair or symbol.lower()

    # REST: status / WS: orderStatus
    status_raw = (data.get("status") or data.get("orderStatus", "ORDERED")).upper()
    status_map = {
        "WAITING": OrderStatus.PENDING,    # 역지정가 대기
        "ORDERED": OrderStatus.OPEN,
        "MODIFYING": OrderStatus.OPEN,
        "CANCELLING": OrderStatus.OPEN,
        "EXECUTED": OrderStatus.COMPLETED,
        "CANCELED": OrderStatus.CANCELLED,
        "EXPIRED": OrderStatus.CANCELLED,
    }
    status = status_map.get(status_raw, OrderStatus.OPEN)

    order_id = str(data.get("orderId") or data.get("rootOrderId", ""))

    return Order(
        order_id=order_id,
        pair=resolved_pair,
        order_type=order_type,
        side=side,
        price=price,
        amount=amount,
        status=status,
        created_at=datetime.now(timezone.utc),
        raw=data,
    )
