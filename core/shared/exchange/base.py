"""
ExchangeAdapter — 거래소 어댑터 Protocol.

모든 거래소 어댑터는 이 Protocol을 구현해야 한다.
ABC가 아닌 Protocol (structural subtyping)을 사용하여,
어댑터가 명시적 상속 없이도 계약을 충족하면 호환된다.

core/ 도메인 로직은 이 Protocol에만 의존한다.
adapters/{exchange}/ 가 구체 구현을 제공한다.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Coroutine, Optional, Protocol, runtime_checkable

from core.shared.exchange.types import (
    Balance,
    Candle,
    ExchangeConstraints,
    Order,
    OrderType,
    Ticker,
)


@runtime_checkable
class ExchangeAdapter(Protocol):
    """
    거래소 어댑터 인터페이스.

    CoincheckAdapter, BitFlyerAdapter, FakeExchangeAdapter가 이 계약을 구현.
    모든 메서드는 async. 거래소 고유 예외는 어댑터 내부에서 처리하고,
    core/에는 표준 예외(ExchangeError 등)만 전파한다.
    """

    @property
    def exchange_name(self) -> str:
        ...

    @property
    def constraints(self) -> ExchangeConstraints:
        ...

    async def place_order(
        self,
        order_type: OrderType,
        pair: str,
        amount: float,
        price: Optional[float] = None,
    ) -> Order:
        ...

    async def cancel_order(self, order_id: str, pair: str = "") -> bool:
        ...

    async def get_open_orders(self, pair: str) -> list[Order]:
        ...

    async def get_order(self, order_id: str, pair: str = "") -> Optional[Order]:
        ...

    async def get_balance(self) -> Balance:
        ...

    async def get_ticker(self, pair: str) -> Ticker:
        ...

    async def subscribe_trades(
        self,
        pair: str,
        callback: Callable[[float, float], Coroutine[Any, Any, None]],
    ) -> None:
        ...

    async def subscribe_executions(
        self,
        callback: Callable[[dict], Coroutine[Any, Any, None]],
    ) -> None:
        ...

    async def connect(self) -> None:
        ...

    async def close(self) -> None:
        ...

    def is_ws_connected(self) -> bool:
        ...
