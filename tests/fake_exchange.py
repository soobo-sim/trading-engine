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
    Collateral,
    CurrencyBalance,
    ExchangeConstraints,
    FxPosition,
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
        self._is_margin_trading = False

        # 잔고 초기화
        balances = initial_balances or {"jpy": 1_000_000.0, "xrp": 0.0, "btc": 0.0}
        self._balances: dict[str, float] = {k.lower(): v for k, v in balances.items()}

        # 주문 저장소
        self._orders: dict[str, Order] = {}
        self._order_counter = 0

        # 콜백 기록 (테스트 검증용)
        self.trade_callbacks: list[Callable] = []
        self.execution_callbacks: list[Callable] = []

        # CFD テスト用
        self._collateral = Collateral(
            collateral=1_000_000.0,
            open_position_pnl=0.0,
            require_collateral=0.0,
            keep_rate=999.0,
        )
        self._fx_positions: list[FxPosition] = []
        self._stop_orders: dict[str, dict] = {}  # order_id → STOP 주문 정보

    # ── 거래소 식별 ─────────────────────────

    @property
    def exchange_name(self) -> str:
        return self._exchange_name

    @property
    def is_margin_trading(self) -> bool:
        """테스트용: 기본 현물. set_margin_trading()으로 변경 가능."""
        return self._is_margin_trading

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
        parts = pair.split("_")  # xrp_jpy → [xrp, jpy], FX_BTC_JPY → [FX, BTC, JPY]
        if len(parts) == 3:
            base, quote = parts[1].lower(), parts[2].lower()  # CFD: FX_BTC_JPY → btc, jpy
        else:
            base, quote = parts[0].lower(), parts[1].lower()  # 현물: xrp_jpy → xrp, jpy

        # MARKET_BUY: amount = JPY 금액 → 코인 수량 변환 (현물만. FX/CFD는 코인 수량 직접)
        if order_type == OrderType.MARKET_BUY and not self._is_margin_trading:
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
        if order_id in self._stop_orders:
            del self._stop_orders[order_id]
            return True
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

    # ── CFD (FX) ──────────────────────────

    async def get_collateral(self) -> Collateral:
        return self._collateral

    async def get_positions(self, product_code: str = "FX_BTC_JPY") -> list[FxPosition]:
        return [p for p in self._fx_positions if p.product_code == product_code]

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

    def set_collateral(self, collateral: Collateral) -> None:
        """테스트에서 증거금 직접 설정."""
        self._collateral = collateral

    def set_fx_positions(self, positions: list[FxPosition]) -> None:
        """테스트에서 FX 포지션 직접 설정."""
        self._fx_positions = positions

    def set_margin_trading(self, enabled: bool) -> None:
        """테스트에서 증거금 거래 모드 전환."""
        self._is_margin_trading = enabled

    async def close_order_stop(
        self,
        symbol: str,
        side: str,
        position_id: int,
        size: int,
        trigger_price: float,
    ) -> Order:
        """FX 역지정(STOP) SL 주문 시뮬레이션."""
        self._order_counter += 1
        order_id = f"FAKE-STOP-{self._order_counter:06d}"
        self._stop_orders[order_id] = {
            "symbol": symbol,
            "side": side,
            "position_id": position_id,
            "size": size,
            "trigger_price": trigger_price,
        }
        return Order(
            order_id=order_id,
            pair=symbol.lower(),
            order_type=OrderType.SELL if side.upper() == "SELL" else OrderType.BUY,
            side=OrderSide.SELL if side.upper() == "SELL" else OrderSide.BUY,
            price=None,
            amount=float(size),
            status=OrderStatus.OPEN,
            created_at=datetime.now(timezone.utc),
        )

    async def close_position(
        self, symbol: str, side: str, position_id: int, size: int,
        execution_type: str = "MARKET",
    ) -> Order:
        """FX 건옥 결제 시뮬레이션."""
        self._order_counter += 1
        order_id = f"FAKE-CLOSE-{self._order_counter:06d}"

        # 시뮬레이션: 해당 포지션 제거
        self._fx_positions = [
            p for p in self._fx_positions
            if not (getattr(p, "position_id", None) == position_id)
        ]

        return Order(
            order_id=order_id,
            pair=symbol.lower(),
            order_type=OrderType.SELL if side.upper() == "SELL" else OrderType.BUY,
            side=OrderSide.SELL if side.upper() == "SELL" else OrderSide.BUY,
            price=self._ticker_price,
            amount=float(size),
            status=OrderStatus.COMPLETED,
            created_at=datetime.now(timezone.utc),
        )
