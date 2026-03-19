"""
FakeExchangeAdapter — 테스트용 인메모리 거래소 어댑터.

ExchangeAdapter Protocol을 구현하되,
실제 네트워크 호출 없이 인메모리 상태로 동작한다.
unit / integration 테스트에서 사용.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

from core.exchange.types import (
    Balance,
    Candle,
    CurrencyBalance,
    ExchangeConstraints,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Ticker,
)


class FakeExchangeAdapter:
    """인메모리 거래소. 모든 주문은 즉시 체결."""

    def __init__(
        self,
        *,
        exchange_name: str = "fake",
        initial_balances: dict[str, float] | None = None,
        ticker_price: float = 100.0,
    ) -> None:
        self._exchange_name = exchange_name
        self._ticker_price = ticker_price
        self._ws_connected = False

        # 잔고 초기화
        balances = initial_balances or {"jpy": 1_000_000.0, "xrp": 0.0, "btc": 0.0}
        self._balances: dict[str, float] = {k.lower(): v for k, v in balances.items()}

        # 주문 저장소
        self._orders: dict[str, Order] = {}
        self._order_counter = 0

        # 콜백 기록 (테스트 검증용)
        self.trade_callbacks: list[Callable] = []
        self.execution_callbacks: list[Callable] = []

    # ── 거래소 식별 ─────────────────────────

    @property
    def exchange_name(self) -> str:
        return self._exchange_name

    @property
    def constraints(self) -> ExchangeConstraints:
        return ExchangeConstraints(
            min_order_sizes={"xrp": 1.0, "btc": 0.001},
            rate_limit=(500, 300),
        )

    # ── 주문 ────────────────────────────────

    async def place_order(
        self,
        order_type: OrderType,
        pair: str,
        amount: float,
        price: Optional[float] = None,
    ) -> Order:
        self._order_counter += 1
        order_id = f"FAKE-{self._order_counter:06d}"

        side = OrderSide.BUY if order_type in (OrderType.BUY, OrderType.MARKET_BUY) else OrderSide.SELL
        exec_price = price or self._ticker_price

        # 즉시 체결 시뮬레이션
        base, quote = pair.split("_")  # xrp_jpy → xrp, jpy

        # MARKET_BUY: amount = JPY 금액 → 코인 수량 변환
        if order_type == OrderType.MARKET_BUY:
            jpy_amount = amount
            coin_amount = jpy_amount / exec_price
            self._balances[quote] = self._balances.get(quote, 0.0) - jpy_amount
            self._balances[base] = self._balances.get(base, 0.0) + coin_amount
            exec_amount = coin_amount
        elif side == OrderSide.BUY:
            cost = exec_price * amount
            self._balances[quote] = self._balances.get(quote, 0.0) - cost
            self._balances[base] = self._balances.get(base, 0.0) + amount
            exec_amount = amount
        else:
            self._balances[base] = self._balances.get(base, 0.0) - amount
            self._balances[quote] = self._balances.get(quote, 0.0) + exec_price * amount
            exec_amount = amount

        order = Order(
            order_id=order_id,
            pair=pair,
            order_type=order_type,
            side=side,
            price=exec_price,
            amount=exec_amount,
            status=OrderStatus.COMPLETED,
            created_at=datetime.now(timezone.utc),
        )
        self._orders[order_id] = order
        return order

    async def cancel_order(self, order_id: str, pair: str = "") -> bool:
        if order_id in self._orders:
            old = self._orders[order_id]
            self._orders[order_id] = Order(
                order_id=old.order_id,
                pair=old.pair,
                order_type=old.order_type,
                side=old.side,
                price=old.price,
                amount=old.amount,
                status=OrderStatus.CANCELLED,
                created_at=old.created_at,
            )
            return True
        return False

    async def get_open_orders(self, pair: str) -> list[Order]:
        return [o for o in self._orders.values() if o.pair == pair and o.status == OrderStatus.OPEN]

    async def get_order(self, order_id: str, pair: str = "") -> Optional[Order]:
        return self._orders.get(order_id)

    # ── 잔고 ────────────────────────────────

    async def get_balance(self) -> Balance:
        currencies = {}
        for currency, amount in self._balances.items():
            currencies[currency] = CurrencyBalance(
                currency=currency,
                amount=amount,
                available=amount,
            )
        return Balance(currencies=currencies)

    # ── 시세 ────────────────────────────────

    async def get_ticker(self, pair: str) -> Ticker:
        return Ticker(
            pair=pair,
            last=self._ticker_price,
            bid=self._ticker_price * 0.999,
            ask=self._ticker_price * 1.001,
            high=self._ticker_price * 1.05,
            low=self._ticker_price * 0.95,
            volume=10000.0,
        )

    # ── WebSocket ──────────────────────────

    async def subscribe_trades(
        self,
        pair: str,
        callback: Callable[[float, float], Coroutine[Any, Any, None]],
    ) -> None:
        self.trade_callbacks.append(callback)

    async def subscribe_executions(
        self,
        callback: Callable[[dict], Coroutine[Any, Any, None]],
    ) -> None:
        self.execution_callbacks.append(callback)

    # ── 연결 관리 ──────────────────────────

    async def connect(self) -> None:
        self._ws_connected = True

    async def close(self) -> None:
        self._ws_connected = False

    def is_ws_connected(self) -> bool:
        return self._ws_connected

    # ── 테스트 헬퍼 ─────────────────────────

    def set_ticker_price(self, price: float) -> None:
        """테스트에서 현재가 변경."""
        self._ticker_price = price

    def set_balance(self, currency: str, amount: float) -> None:
        """테스트에서 잔고 직접 설정."""
        self._balances[currency.lower()] = amount

    @property
    def order_history(self) -> list[Order]:
        """전체 주문 이력 (시간순)."""
        return sorted(self._orders.values(), key=lambda o: o.created_at or datetime.min)
