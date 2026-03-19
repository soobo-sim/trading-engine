"""
CoincheckAdapter — ExchangeAdapter Protocol 구현.

Coincheck REST API + WebSocket 을 표준 ExchangeAdapter 인터페이스로 감싼다.
core/ 도메인 로직은 이 어댑터에 의존하지 않고, Protocol에만 의존한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional
from urllib.parse import urlencode

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from core.exchange.base import ExchangeAdapter  # noqa: F401 — Protocol 등록 확인용
from core.exchange.errors import (
    AuthenticationError,
    ConnectionError,
    ExchangeError,
    OrderError,
    RateLimitError,
)
from core.exchange.types import (
    Balance,
    CurrencyBalance,
    ExchangeConstraints,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Ticker,
)
from adapters.coincheck.signer import CoincheckSigner

logger = logging.getLogger(__name__)


class CoincheckAdapter:
    """
    Coincheck 거래소 어댑터.

    ExchangeAdapter Protocol을 구조적 서브타이핑으로 충족한다.
    명시적 상속 불필요.
    """

    def __init__(self, api_key: str, api_secret: str, base_url: str) -> None:
        self._signer = CoincheckSigner(api_key=api_key, api_secret=api_secret)
        self._base_url = base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        # WS 상태
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_connected: bool = False
        self._ws_url = os.environ.get("COINCHECK_WS_URL", "wss://ws-api.coincheck.com")

    # ── 거래소 식별 ─────────────────────────────────────────────

    @property
    def exchange_name(self) -> str:
        return "coincheck"

    @property
    def constraints(self) -> ExchangeConstraints:
        return ExchangeConstraints(
            min_order_sizes={"xrp": 1.0, "btc": 0.001, "eth": 0.01},
            rate_limit=(180, 60),
        )

    # ── 연결 관리 ───────────────────────────────────────────────

    async def connect(self) -> None:
        """httpx.AsyncClient 초기화."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """httpx.AsyncClient + WS 종료."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            self._ws_connected = False
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def is_ws_connected(self) -> bool:
        """WebSocket 연결 상태."""
        return self._ws_connected

    # ── 내부 헬퍼 ──────────────────────────────────────────────

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ConnectionError("connect() 를 먼저 호출해야 합니다.")
        return self._client

    def _get_auth_headers(self, url: str, body: str = "") -> dict[str, str]:
        return self._signer.sign(url=url, body=body)

    def _raise_for_exchange_error(self, response: httpx.Response) -> None:
        """HTTP 상태코드를 표준 ExchangeError로 변환."""
        if response.status_code == 401:
            raise AuthenticationError(f"Coincheck 인증 실패: {response.text}")
        if response.status_code == 429:
            raise RateLimitError(f"Coincheck 레이트 리밋: {response.text}")
        if response.status_code >= 400:
            raise ExchangeError(
                f"Coincheck API 오류: status={response.status_code} body={response.text}"
            )

    # ── 주문 ────────────────────────────────────────────────────

    async def place_order(
        self,
        order_type: OrderType,
        pair: str,
        amount: float,
        price: Optional[float] = None,
    ) -> Order:
        """
        주문 실행.

        CK API 필드:
        - rate: str(int(price)) — 소수점 없는 정수 문자열
        - amount: str(amount) — 수량 문자열
        - pair: 소문자 그대로 (xrp_jpy)
        - order_type: buy | sell | market_buy | market_sell
        """
        client = self._get_client()

        payload: dict[str, Any] = {
            "pair": pair,
            "order_type": order_type.value,
        }
        if order_type in (OrderType.BUY, OrderType.SELL):
            # 지정가 주문
            if price is None:
                raise OrderError("지정가 주문에는 price가 필요합니다.")
            payload["rate"] = str(int(price))
            payload["amount"] = str(amount)
        elif order_type == OrderType.MARKET_BUY:
            # 시장가 매수: Coincheck API는 market_buy_amount(JPY 금액)을 요구.
            # Protocol의 amount는 코인 수량이므로, 현재가 * 수량으로 JPY 변환.
            # 매니저에서 직접 JPY 금액을 넘기고 싶으면 price=None인 상태로
            # amount에 JPY 금액을 넣되 _jpy_amount 플래그를 사용한다.
            # → Phase 2에서 매니저 통합 시 확정. 현재는 amount를 JPY 금액으로 간주.
            payload["market_buy_amount"] = str(amount)
        elif order_type == OrderType.MARKET_SELL:
            # 시장가 매도: 수량만
            payload["amount"] = str(amount)

        body_str = json.dumps(payload, separators=(",", ":"))
        url = f"{self._base_url}/api/exchange/orders"
        headers = self._get_auth_headers(url, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            response = await client.post(url, headers=headers, content=body_str)
            self._raise_for_exchange_error(response)
            data = response.json()
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"Coincheck HTTP 오류: {e}") from e

        if not data.get("success"):
            raise OrderError(f"Coincheck 주문 실패: {data}")

        return self._parse_order(data)

    async def cancel_order(self, order_id: str, pair: str = "") -> bool:
        """
        주문 취소.

        CK API: DELETE /api/exchange/orders/{id}
        """
        client = self._get_client()
        url = f"{self._base_url}/api/exchange/orders/{order_id}"
        headers = self._get_auth_headers(url)

        try:
            response = await client.delete(url, headers=headers)
            if response.status_code == 404:
                logger.warning(f"Coincheck 주문 없음 (이미 취소됨): {order_id}")
                return False
            self._raise_for_exchange_error(response)
            data = response.json()
            return bool(data.get("success"))
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"Coincheck HTTP 오류: {e}") from e

    async def get_open_orders(self, pair: str) -> list[Order]:
        """
        미체결 주문 목록.

        CK API: GET /api/exchange/orders/opens?pair={pair}
        HMAC 서명은 쿼리스트링 포함 전체 URL 대상.
        """
        client = self._get_client()
        query = urlencode({"pair": pair})
        url = f"{self._base_url}/api/exchange/orders/opens?{query}"
        headers = self._get_auth_headers(url)

        try:
            response = await client.get(url, headers=headers)
            self._raise_for_exchange_error(response)
            data = response.json()
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"Coincheck HTTP 오류: {e}") from e

        orders = data.get("orders", [])
        return [self._parse_open_order(o) for o in orders]

    async def get_order(self, order_id: str, pair: str = "") -> Optional[Order]:
        """
        주문 상세 조회.

        CK에는 단건 조회 API가 없으므로, transactions_pagination으로
        최근 100건을 가져와 order_id로 필터링.
        """
        client = self._get_client()
        params: dict[str, Any] = {"limit": 100}
        if pair:
            params["pair"] = pair
        query = urlencode(params)
        url = f"{self._base_url}/api/exchange/orders/transactions_pagination?{query}"
        headers = self._get_auth_headers(url)

        try:
            response = await client.get(url, headers=headers)
            self._raise_for_exchange_error(response)
            data = response.json()
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"Coincheck HTTP 오류: {e}") from e

        transactions = data.get("data", data.get("transactions", []))
        for txn in transactions:
            if str(txn.get("id")) == str(order_id):
                return self._parse_transaction(txn)
        return None

    # ── 잔고 ───────────────────────────────────────────────────

    async def get_balance(self) -> Balance:
        """
        전체 잔고 조회.

        CK 응답: flat dict — {"jpy": "1000000", "jpy_reserved": "0", "xrp": "50.5", ...}
        reserved = f"{currency}_reserved" 필드
        available = float(total) - float(reserved)
        """
        client = self._get_client()
        url = f"{self._base_url}/api/accounts/balance"
        headers = self._get_auth_headers(url)

        try:
            response = await client.get(url, headers=headers)
            self._raise_for_exchange_error(response)
            data = response.json()
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"Coincheck HTTP 오류: {e}") from e

        currencies: dict[str, CurrencyBalance] = {}
        # reserved 필드는 {currency}_reserved로 표기됨
        reserved_keys = {k for k in data if k.endswith("_reserved")}
        currency_keys = {
            k for k in data
            if k not in reserved_keys
            and not k.startswith("success")
            and isinstance(data[k], str)
            and not k.endswith("_lend_in_use")
        }

        for currency in currency_keys:
            try:
                total = float(data[currency])
            except (ValueError, TypeError):
                continue
            reserved_key = f"{currency}_reserved"
            reserved = float(data.get(reserved_key, 0))
            available = total - reserved
            currencies[currency] = CurrencyBalance(
                currency=currency,
                amount=total,
                available=available,
            )

        return Balance(currencies=currencies)

    # ── 시세 ───────────────────────────────────────────────────

    async def get_ticker(self, pair: str) -> Ticker:
        """
        현재가 스냅샷. Public API (서명 불필요).

        CK 응답: {"last":100.0, "bid":99.9, "ask":100.1, "high":101.0, "low":99.0, "volume":"1234.5", "timestamp":1700000000}
        """
        client = self._get_client()
        query = urlencode({"pair": pair})
        url = f"{self._base_url}/api/ticker?{query}"

        try:
            response = await client.get(url)
            self._raise_for_exchange_error(response)
            data = response.json()
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"Coincheck HTTP 오류: {e}") from e

        return Ticker(
            pair=pair,
            last=float(data["last"]),
            bid=float(data["bid"]),
            ask=float(data["ask"]),
            high=float(data["high"]),
            low=float(data["low"]),
            volume=float(data["volume"]),
        )

    # ── WebSocket (Phase 2에서 완전 구현) ──────────────────────

    async def subscribe_trades(
        self,
        pair: str,
        callback: Callable[[float, float], Coroutine[Any, Any, None]],
    ) -> None:
        """
        Coincheck Public WS 체결 구독.

        wss://ws-api.coincheck.com 에 연결, {pair}-trades 채널 구독.
        체결 메시지: [[ts, id, pair, rate, amount, ...], ...]
        callback(price, amount)로 각 체결을 전달한다.
        재접속: 지수 백오프 (1s → 30s cap).
        """
        channel = f"{pair}-trades"
        delay = 1

        while True:
            try:
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._ws_connected = True
                    logger.info(f"[CK WS] 연결 완료: {self._ws_url}")

                    await ws.send(json.dumps({"type": "subscribe", "channel": channel}))
                    logger.info(f"[CK WS] 구독: {channel}")

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            # trades: [[ts, id, pair, rate, amount, order_type, ...], ...]
                            if isinstance(data, list) and data and isinstance(data[0], list):
                                for trade_arr in data:
                                    if len(trade_arr) >= 5:
                                        price = float(trade_arr[3])
                                        amount = float(trade_arr[4])
                                        await callback(price, amount)
                        except (json.JSONDecodeError, ValueError, IndexError, TypeError):
                            continue

                    # 서버가 정상 종료한 경우
                    self._ws_connected = False
                    delay = 1

            except asyncio.CancelledError:
                self._ws_connected = False
                raise
            except (ConnectionClosed, OSError, Exception) as e:
                self._ws_connected = False
                logger.warning(f"[CK WS] 연결 끊김: {e}. {delay}초 후 재접속...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def subscribe_executions(
        self,
        callback: Callable[[dict], Coroutine[Any, Any, None]],
    ) -> None:
        """내 주문 체결 이벤트 구독 (Private WS — 향후 구현)."""
        logger.warning("subscribe_executions: 아직 미구현. Private WS 필요.")

    # ── 내부 변환 헬퍼 ─────────────────────────────────────────

    def _parse_order(self, data: dict) -> Order:
        """주문 생성 응답 → Order DTO."""
        order_type_str = data.get("order_type", "buy")
        order_type = OrderType(order_type_str)
        side = OrderSide.BUY if order_type in (OrderType.BUY, OrderType.MARKET_BUY) else OrderSide.SELL

        price: Optional[float] = None
        raw_rate = data.get("rate")
        if raw_rate is not None:
            try:
                price = float(raw_rate)
            except (ValueError, TypeError):
                pass

        raw_amount = data.get("amount") or data.get("pending_amount", "0")
        try:
            amount = float(raw_amount)
        except (ValueError, TypeError):
            amount = 0.0

        created_at: Optional[datetime] = None
        raw_ts = data.get("created_at")
        if raw_ts:
            try:
                created_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        return Order(
            order_id=str(data["id"]),
            pair=data.get("pair", ""),
            order_type=order_type,
            side=side,
            price=price,
            amount=amount,
            status=OrderStatus.OPEN,
            created_at=created_at,
            raw=data,
        )

    def _parse_open_order(self, data: dict) -> Order:
        """미체결 주문 응답 항목 → Order DTO."""
        return self._parse_order(data)

    def _parse_transaction(self, data: dict) -> Order:
        """체결 내역 항목 → Order DTO (completed 상태)."""
        order = self._parse_order(data)
        # completed 트랜잭션은 COMPLETED 상태로 재설정
        return Order(
            order_id=order.order_id,
            pair=order.pair,
            order_type=order.order_type,
            side=order.side,
            price=order.price,
            amount=order.amount,
            status=OrderStatus.COMPLETED,
            created_at=order.created_at,
            raw=data,
        )
