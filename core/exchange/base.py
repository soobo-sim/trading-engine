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

from core.exchange.types import (
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

    # ── 거래소 식별 ─────────────────────────

    @property
    def exchange_name(self) -> str:
        """거래소 식별자 (예: "coincheck", "bitflyer")."""
        ...

    @property
    def constraints(self) -> ExchangeConstraints:
        """거래소 고유 제약 (최소 주문, 레이트 리밋 등)."""
        ...

    # ── 주문 ────────────────────────────────

    async def place_order(
        self,
        order_type: OrderType,
        pair: str,
        amount: float,
        price: Optional[float] = None,
    ) -> Order:
        """
        주문 실행.

        Args:
            order_type: buy, sell, market_buy, market_sell
            pair:       거래 페어 (소문자: "xrp_jpy"). 어댑터가 내부적으로
                        거래소 포맷으로 변환 (BF: "XRP_JPY")
            amount:     MARKET_BUY → **JPY 금액** (어댑터가 내부적으로 변환).
                        그 외 → 코인 수량.
                        CK: market_buy_amount 필드로 직접 전달.
                        BF: size = amount / ticker.ltp 로 변환 후 전달.
            price:      지정가. 시장가이면 None.

        Returns:
            Order DTO (order_id는 항상 str)
        """
        ...

    async def cancel_order(self, order_id: str, pair: str = "") -> bool:
        """
        주문 취소.

        Returns:
            성공 여부
        """
        ...

    async def get_open_orders(self, pair: str) -> list[Order]:
        """미체결 주문 목록."""
        ...

    async def get_order(self, order_id: str, pair: str = "") -> Optional[Order]:
        """주문 상세 조회. 없으면 None."""
        ...

    # ── 잔고 ────────────────────────────────

    async def get_balance(self) -> Balance:
        """전체 잔고 조회. 통화코드는 소문자 통일."""
        ...

    # ── 시세 ────────────────────────────────

    async def get_ticker(self, pair: str) -> Ticker:
        """현재가 스냅샷."""
        ...

    # ── WebSocket / 실시간 ──────────────────

    async def subscribe_trades(
        self,
        pair: str,
        callback: Callable[[float, float], Coroutine[Any, Any, None]],
    ) -> None:
        """
        실시간 체결 구독.

        callback(price, amount) — 체결 발생 시 호출.
        StopLossMonitor가 이 콜백으로 실시간 가격을 받는다.
        """
        ...

    async def subscribe_executions(
        self,
        callback: Callable[[dict], Coroutine[Any, Any, None]],
    ) -> None:
        """
        내 주문 체결 이벤트 구독 (Private WS).

        callback(execution_data) — 내 주문이 체결되면 호출.
        """
        ...

    # ── 연결 관리 ──────────────────────────

    async def connect(self) -> None:
        """HTTP 클라이언트 + WS 연결 초기화."""
        ...

    async def close(self) -> None:
        """모든 연결 정리."""
        ...

    def is_ws_connected(self) -> bool:
        """WS 연결 상태."""
        ...
