"""
BitFlyerAdapter — ExchangeAdapter Protocol 구현.

BitFlyer REST API + WebSocket 을 표준 ExchangeAdapter 인터페이스로 감싼다.

주요 CK 차이점:
- 인증: timestamp(초) + METHOD + path
- 헤더: ACCESS-SIGN (SIGNATURE 아님)
- 주문 취소: DELETE 아닌 POST
- 잔고 응답: 배열 → dict 변환 필요
- order_id: "JRF..." 문자열 (26자+)
- pair → product_code 대문자 변환
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
from adapters.bitflyer.signer import BitFlyerSigner

logger = logging.getLogger(__name__)


class BitFlyerAdapter:
    """
    BitFlyer 거래소 어댑터.

    ExchangeAdapter Protocol을 구조적 서브타이핑으로 충족한다.
    """

    def __init__(self, api_key: str, api_secret: str, base_url: str) -> None:
        self._signer = BitFlyerSigner(api_key=api_key, api_secret=api_secret)
        self._base_url = base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        # WS 상태
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_connected: bool = False
        self._ws_url = os.environ.get(
            "BITFLYER_WS_URL", "wss://ws.lightstream.bitflyer.com/json-rpc"
        )

    # ── 거래소 식별 ─────────────────────────────────────────────

    @property
    def exchange_name(self) -> str:
        return "bitflyer"

    @property
    def constraints(self) -> ExchangeConstraints:
        return ExchangeConstraints(
            min_order_sizes={"xrp": 0.1, "btc": 0.001},
            rate_limit=(500, 300),
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

    @staticmethod
    def _pair_to_product_code(pair: str) -> str:
        """xrp_jpy → XRP_JPY."""
        return pair.upper()

    def _get_auth_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        return self._signer.sign(method=method, path=path, body=body)

    def _raise_for_exchange_error(self, response: httpx.Response) -> None:
        """HTTP 상태코드를 표준 ExchangeError로 변환."""
        if response.status_code == 401:
            raise AuthenticationError(f"BitFlyer 인증 실패: {response.text}")
        if response.status_code == 429:
            raise RateLimitError(f"BitFlyer 레이트 리밋: {response.text}")
        if response.status_code >= 400:
            raise ExchangeError(
                f"BitFlyer API 오류: status={response.status_code} body={response.text}"
            )

    def _build_order_payload(
        self,
        order_type: OrderType,
        product_code: str,
        amount: float,
        price: Optional[float],
    ) -> dict[str, Any]:
        """OrderType → BitFlyer child_order_type + side 분리."""
        if order_type in (OrderType.BUY, OrderType.SELL):
            side = "BUY" if order_type == OrderType.BUY else "SELL"
            child_order_type = "LIMIT"
        elif order_type == OrderType.MARKET_BUY:
            side = "BUY"
            child_order_type = "MARKET"
        elif order_type == OrderType.MARKET_SELL:
            side = "SELL"
            child_order_type = "MARKET"
        else:
            raise OrderError(f"알 수 없는 order_type: {order_type}")

        payload: dict[str, Any] = {
            "product_code": product_code,
            "child_order_type": child_order_type,
            "side": side,
            "size": amount,
        }
        if child_order_type == "LIMIT":
            if price is None:
                raise OrderError("지정가 주문에는 price가 필요합니다.")
            payload["price"] = price

        return payload

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

        POST /v1/me/sendchildorder
        응답: {"child_order_acceptance_id": "JRF..."}

        MARKET_BUY: amount는 JPY 금액. ticker 조회 후 코인 수량으로 변환.
        """
        client = self._get_client()
        product_code = self._pair_to_product_code(pair)

        # MARKET_BUY: JPY 金額 → コイン数量 変換 (현물만. CFD는 코인 수량 직접 전달)
        actual_amount = amount
        if order_type == OrderType.MARKET_BUY and not product_code.startswith("FX_"):
            ticker = await self.get_ticker(pair)
            if ticker.last <= 0:
                raise OrderError(f"BitFlyer 현재가 조회 실패: {ticker.last}")
            actual_amount = round(amount / ticker.last, 8)

        payload = self._build_order_payload(order_type, product_code, actual_amount, price)

        path = "/v1/me/sendchildorder"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            response = await client.post(
                f"{self._base_url}{path}", headers=headers, content=body_str
            )
            self._raise_for_exchange_error(response)
            data = response.json()
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"BitFlyer HTTP 오류: {e}") from e

        order_id = data.get("child_order_acceptance_id")
        if not order_id:
            raise OrderError(f"BitFlyer 주문 실패: {data}")

        side = OrderSide.BUY if order_type in (OrderType.BUY, OrderType.MARKET_BUY) else OrderSide.SELL
        return Order(
            order_id=str(order_id),
            pair=pair,
            order_type=order_type,
            side=side,
            price=price,
            amount=amount,
            status=OrderStatus.OPEN,
            created_at=datetime.now(timezone.utc),
            raw=data,
        )

    async def cancel_order(self, order_id: str, pair: str = "") -> bool:
        """
        주문 취소.

        POST /v1/me/cancelchildorder (CK DELETE 와 다름)
        성공 시 HTTP 200 빈 응답.
        """
        client = self._get_client()
        product_code = self._pair_to_product_code(pair) if pair else ""
        payload: dict[str, Any] = {"child_order_acceptance_id": order_id}
        if product_code:
            payload["product_code"] = product_code

        path = "/v1/me/cancelchildorder"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            response = await client.post(
                f"{self._base_url}{path}", headers=headers, content=body_str
            )
            if response.status_code == 404:
                logger.warning(f"BitFlyer 주문 없음 (이미 취소됨): {order_id}")
                return False
            self._raise_for_exchange_error(response)
            return True
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"BitFlyer HTTP 오류: {e}") from e

    async def get_open_orders(self, pair: str) -> list[Order]:
        """
        미체결 주문 목록.

        GET /v1/me/getchildorders?product_code={}&child_order_state=ACTIVE
        """
        client = self._get_client()
        product_code = self._pair_to_product_code(pair)
        params = {
            "product_code": product_code,
            "child_order_state": "ACTIVE",
        }
        query = urlencode(params)
        path = f"/v1/me/getchildorders?{query}"
        headers = self._get_auth_headers("GET", path)

        try:
            response = await client.get(f"{self._base_url}{path}", headers=headers)
            self._raise_for_exchange_error(response)
            orders = response.json()
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"BitFlyer HTTP 오류: {e}") from e

        return [self._parse_order(o, pair) for o in (orders or [])]

    async def get_order(self, order_id: str, pair: str = "") -> Optional[Order]:
        """
        주문 상세 조회.

        GET /v1/me/getchildorders?child_order_acceptance_id={id}
        배열 반환 → 첫 번째 요소.
        """
        client = self._get_client()
        params: dict[str, Any] = {"child_order_acceptance_id": order_id}
        if pair:
            params["product_code"] = self._pair_to_product_code(pair)
        query = urlencode(params)
        path = f"/v1/me/getchildorders?{query}"
        headers = self._get_auth_headers("GET", path)

        try:
            response = await client.get(f"{self._base_url}{path}", headers=headers)
            self._raise_for_exchange_error(response)
            orders = response.json()
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"BitFlyer HTTP 오류: {e}") from e

        if not orders:
            return None
        return self._parse_order(orders[0], pair)

    # ── 잔고 ───────────────────────────────────────────────────

    async def get_balance(self) -> Balance:
        """
        전체 잔고 조회.

        BF 응답: 배열 — [{"currency_code": "JPY", "amount": 1000000.0, "available": 900000.0}, ...]
        통화코드를 소문자로 변환하여 Balance DTO에 담는다.
        """
        client = self._get_client()
        path = "/v1/me/getbalance"
        headers = self._get_auth_headers("GET", path)

        try:
            response = await client.get(f"{self._base_url}{path}", headers=headers)
            self._raise_for_exchange_error(response)
            items = response.json()
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"BitFlyer HTTP 오류: {e}") from e

        currencies: dict[str, CurrencyBalance] = {}
        for item in (items or []):
            code = item.get("currency_code", "").lower()
            if not code:
                continue
            total = float(item.get("amount", 0))
            available = float(item.get("available", 0))
            currencies[code] = CurrencyBalance(
                currency=code,
                amount=total,
                available=available,
            )

        return Balance(currencies=currencies)

    # ── 시세 ───────────────────────────────────────────────────

    async def get_ticker(self, pair: str) -> Ticker:
        """
        현재가 스냅샷. Public API (서명 불필요).

        GET /v1/ticker?product_code={PAIR}
        """
        client = self._get_client()
        product_code = self._pair_to_product_code(pair)
        query = urlencode({"product_code": product_code})
        url = f"{self._base_url}/v1/ticker?{query}"

        try:
            response = await client.get(url)
            self._raise_for_exchange_error(response)
            data = response.json()
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"BitFlyer HTTP 오류: {e}") from e

        return Ticker(
            pair=pair,
            last=float(data["ltp"]),
            bid=float(data["best_bid"]),
            ask=float(data["best_ask"]),
            high=float(data.get("high", data["ltp"])),
            low=float(data.get("low", data["ltp"])),
            volume=float(data.get("volume", 0)),
        )

    # ── WebSocket (Phase 2에서 완전 구현) ──────────────────────

    async def subscribe_trades(
        self,
        pair: str,
        callback: Callable[[float, float], Coroutine[Any, Any, None]],
    ) -> None:
        """
        BitFlyer Public WS 체결 구독.

        JSON-RPC 2.0 프로토콜로 lightning_executions_{PRODUCT} 구독.
        체결 메시지: channelMessage.params.message = [{price, size, ...}, ...]
        callback(price, amount)로 각 체결을 전달한다.
        재접속: 지수 백오프 (1s → 30s cap).
        """
        product_code = self._pair_to_product_code(pair)
        channel = f"lightning_executions_{product_code}"
        delay = 1

        while True:
            try:
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._ws_connected = True
                    logger.info(f"[BF WS] 연결 완료: {self._ws_url}")

                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "subscribe",
                        "params": {"channel": channel},
                        "id": 1,
                    }))
                    logger.info(f"[BF WS] 구독: {channel}")

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            if data.get("method") == "channelMessage":
                                params = data.get("params", {})
                                if params.get("channel") == channel:
                                    for exec_item in params.get("message", []):
                                        price = float(exec_item["price"])
                                        amount = float(exec_item["size"])
                                        await callback(price, amount)
                        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                            continue

                    self._ws_connected = False
                    delay = 1

            except asyncio.CancelledError:
                self._ws_connected = False
                raise
            except (ConnectionClosed, OSError, Exception) as e:
                self._ws_connected = False
                logger.warning(f"[BF WS] 연결 끊김: {e}. {delay}초 후 재접속...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def subscribe_executions(
        self,
        callback: Callable[[dict], Coroutine[Any, Any, None]],
    ) -> None:
        """내 주문 체결 이벤트 구독 (Private WS — 향후 구현)."""
        logger.warning("subscribe_executions: 아직 미구현. Private WS 필요.")

    # ── 내부 변환 헬퍼 ─────────────────────────────────────────

    # ── CFD 전용 (FX_BTC_JPY) ──────────────────────────────────────

    async def get_collateral(self):
        """증거금 상태 조회. GET /v1/me/getcollateral"""
        from core.exchange.types import Collateral

        client = self._get_client()
        path = "/v1/me/getcollateral"
        headers = self._get_auth_headers("GET", path)
        try:
            response = await client.get(f"{self._base_url}{path}", headers=headers)
            self._raise_for_exchange_error(response)
            data = response.json()
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"BitFlyer HTTP 오류: {e}") from e

        collateral_val = float(data.get("collateral", 0))
        require = float(data.get("require_collateral", 0))
        keep_rate = (collateral_val / require) if require > 0 else 999.0

        return Collateral(
            collateral=collateral_val,
            open_position_pnl=float(data.get("open_position_pnl", 0)),
            require_collateral=require,
            keep_rate=keep_rate,
        )

    async def get_positions(self, product_code: str = "FX_BTC_JPY") -> list:
        """FX 건옥 목록 조회. GET /v1/me/getpositions"""
        from core.exchange.types import FxPosition

        client = self._get_client()
        query = urlencode({"product_code": product_code})
        path = f"/v1/me/getpositions?{query}"
        headers = self._get_auth_headers("GET", path)
        try:
            response = await client.get(f"{self._base_url}{path}", headers=headers)
            self._raise_for_exchange_error(response)
            items = response.json()
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"BitFlyer HTTP 오류: {e}") from e

        positions = []
        for item in (items or []):
            open_date = None
            raw_date = item.get("open_date")
            if raw_date:
                try:
                    open_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass
            positions.append(FxPosition(
                product_code=item.get("product_code", product_code),
                side=item.get("side", "BUY"),
                price=float(item.get("price", 0)),
                size=float(item.get("size", 0)),
                pnl=float(item.get("pnl", 0)),
                leverage=float(item.get("leverage", 0)),
                require_collateral=float(item.get("require_collateral", 0)),
                swap_point_accumulate=float(item.get("swap_point_accumulate", 0)),
                sfd=float(item.get("sfd", 0)),
                open_date=open_date,
            ))
        return positions

    # ── 내부 변환 헬퍼 ─────────────────────────────────────────

    def _parse_order(self, data: dict, pair: str = "") -> Order:
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
