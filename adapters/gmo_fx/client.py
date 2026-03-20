"""
GmoFxAdapter — ExchangeAdapter Protocol 구현 (GMO FX 외국환).

GMO FX REST API + WebSocket을 표준 ExchangeAdapter 인터페이스로 감싼다.

주요 특징:
- 인증: timestamp(ms) + METHOD + sign_path + body → HMAC-SHA256
- 서명 경로: /v1/... (private/ 제외)
- Base URL 분리: public → /public/, private → /private/
- 주문: speedOrder(시장가), order(지정가), closeOrder(건옥 결제)
- 잔고: account/assets → equity, availableAmount, marginRatio
- 포지션: openPositions → positionId, symbol, side, size, price
- POST 레이트 리밋: 1회/초 (핵심 제약)
- GET 레이트 리밋: 6회/초
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

from core.exchange.base import ExchangeAdapter  # noqa: F401
from core.exchange.errors import (
    AuthenticationError,
    ConnectionError,
    ExchangeError,
    OrderError,
    RateLimitError,
)
from core.exchange.types import (
    Balance,
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
from adapters.gmo_fx.signer import GmoFxSigner

logger = logging.getLogger(__name__)

# POST 1회/초 제한 준수용
_POST_INTERVAL = 1.1  # 초


class GmoFxAdapter:
    """
    GMO FX 외국환 어댑터.

    ExchangeAdapter Protocol을 구조적 서브타이핑으로 충족한다.
    추가로 get_collateral(), get_positions()를 제공하여
    CfdTrendFollowingManager와 호환된다.
    """

    def __init__(self, api_key: str, api_secret: str, base_url: str) -> None:
        self._signer = GmoFxSigner(api_key=api_key, api_secret=api_secret)
        # base_url: https://forex-api.coin.z.com
        self._base_url = base_url.rstrip("/")
        self._public_url = f"{self._base_url}/public"
        self._private_url = f"{self._base_url}/private"
        self._client: Optional[httpx.AsyncClient] = None
        # WS
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_connected: bool = False
        self._ws_public_url = os.environ.get(
            "GMOFX_WS_PUBLIC_URL", "wss://forex-api.coin.z.com/ws/public/v1"
        )
        self._ws_private_url = os.environ.get(
            "GMOFX_WS_PRIVATE_URL", "wss://forex-api.coin.z.com/ws/private/v1"
        )
        # POST 레이트 리밋 관리
        self._last_post_time: float = 0
        self._post_lock = asyncio.Lock()

    # ── 거래소 식별 ─────────────────────────────────────────────

    @property
    def exchange_name(self) -> str:
        return "gmofx"

    @property
    def constraints(self) -> ExchangeConstraints:
        return ExchangeConstraints(
            min_order_sizes={"usd": 1, "eur": 1, "gbp": 1},
            rate_limit=(6, 1),  # GET 6회/초
            extra={
                "post_rate_limit": (1, 1),  # POST 1회/초
                "leverage_max": 25,
            },
        )

    # ── 연결 관리 ───────────────────────────────────────────────

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
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
        return self._ws_connected

    # ── 내부 헬퍼 ──────────────────────────────────────────────

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ConnectionError("connect() 를 먼저 호출해야 합니다.")
        return self._client

    @staticmethod
    def _pair_to_symbol(pair: str) -> str:
        """usd_jpy → USD_JPY."""
        return pair.upper()

    def _get_auth_headers(self, method: str, sign_path: str, body: str = "") -> dict[str, str]:
        """서명 헤더 생성. sign_path는 /v1/... 형식."""
        return self._signer.sign(method=method, path=sign_path, body=body)

    def _raise_for_exchange_error(self, response: httpx.Response, data: dict | None = None) -> None:
        """HTTP 상태코드 + GMO status 코드를 표준 ExchangeError로 변환."""
        if response.status_code == 401:
            raise AuthenticationError(f"GMO FX 인증 실패: {response.text}")
        if response.status_code == 429:
            raise RateLimitError(f"GMO FX 레이트 리밋: {response.text}")
        if response.status_code >= 400:
            raise ExchangeError(
                f"GMO FX API 오류: status={response.status_code} body={response.text}"
            )
        # GMO API는 HTTP 200이어도 status != 0 이면 에러
        if data and data.get("status") != 0:
            messages = data.get("messages", [])
            msg_text = "; ".join(
                f"{m.get('message_code', '?')}: {m.get('message_string', '?')}"
                for m in messages
            ) if messages else str(data)
            # ERR-200: 잔고 부족, ERR-201: 증거금 부족
            raise ExchangeError(f"GMO FX 비즈니스 에러: {msg_text}", raw=data)

    async def _post_with_rate_limit(self, url: str, headers: dict, content: str) -> httpx.Response:
        """POST 1회/초 제한 준수. 동시 호출 시 순차 대기."""
        client = self._get_client()
        async with self._post_lock:
            import time
            now = time.monotonic()
            elapsed = now - self._last_post_time
            if elapsed < _POST_INTERVAL:
                await asyncio.sleep(_POST_INTERVAL - elapsed)
            response = await client.post(url, headers=headers, content=content)
            self._last_post_time = time.monotonic()
            return response

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

        MARKET_BUY / MARKET_SELL: speedOrder (시장가 즉시 체결)
        BUY / SELL: order (지정가)

        GMO FX에서 amount는 항상 통화 수량 (JPY 아님).
        예: USD/JPY 1000통화 → amount=1000
        """
        symbol = self._pair_to_symbol(pair)

        if order_type in (OrderType.MARKET_BUY, OrderType.MARKET_SELL):
            return await self._speed_order(order_type, pair, symbol, amount)
        else:
            return await self._limit_order(order_type, pair, symbol, amount, price)

    async def _speed_order(
        self, order_type: OrderType, pair: str, symbol: str, amount: float
    ) -> Order:
        """스피드 주문 (시장가 즉시 체결)."""
        side = "BUY" if order_type in (OrderType.BUY, OrderType.MARKET_BUY) else "SELL"
        payload = {
            "symbol": symbol,
            "side": side,
            "size": str(int(amount)),
        }

        sign_path = "/v1/speedOrder"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", sign_path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            response = await self._post_with_rate_limit(
                f"{self._private_url}{sign_path}", headers, body_str
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        result = data.get("data", [])
        root_order_id = str(result[0].get("rootOrderId", "")) if result else ""
        if not root_order_id:
            raise OrderError(f"GMO FX speedOrder 실패: {data}")

        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        return Order(
            order_id=root_order_id,
            pair=pair,
            order_type=order_type,
            side=order_side,
            price=None,
            amount=amount,
            status=OrderStatus.OPEN,
            created_at=datetime.now(timezone.utc),
            raw=data,
        )

    async def _limit_order(
        self, order_type: OrderType, pair: str, symbol: str, amount: float, price: Optional[float]
    ) -> Order:
        """지정가 주문."""
        if price is None:
            raise OrderError("지정가 주문에는 price가 필요합니다.")

        side = "BUY" if order_type == OrderType.BUY else "SELL"
        payload = {
            "symbol": symbol,
            "side": side,
            "size": str(int(amount)),
            "executionType": "LIMIT",
            "limitPrice": str(price),
        }

        sign_path = "/v1/order"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", sign_path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            response = await self._post_with_rate_limit(
                f"{self._private_url}{sign_path}", headers, body_str
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        result = data.get("data", [])
        root_order_id = str(result[0].get("rootOrderId", "")) if result else ""
        if not root_order_id:
            raise OrderError(f"GMO FX order 실패: {data}")

        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        return Order(
            order_id=root_order_id,
            pair=pair,
            order_type=order_type,
            side=order_side,
            price=price,
            amount=amount,
            status=OrderStatus.OPEN,
            created_at=datetime.now(timezone.utc),
            raw=data,
        )

    async def close_position(
        self, symbol: str, side: str, position_id: int, size: int,
        execution_type: str = "MARKET",
    ) -> Order:
        """
        건옥 결제 주문 (positionId 지정).

        POST /private/v1/closeOrder
        """
        payload: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "executionType": execution_type,
            "settlePosition": [
                {"positionId": position_id, "size": str(size)},
            ],
        }

        sign_path = "/v1/closeOrder"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", sign_path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            response = await self._post_with_rate_limit(
                f"{self._private_url}{sign_path}", headers, body_str
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        result = data.get("data", [])
        root_order_id = str(result[0].get("rootOrderId", "")) if result else ""

        return Order(
            order_id=root_order_id,
            pair=symbol.lower(),
            order_type=OrderType.SELL if side.upper() == "SELL" else OrderType.BUY,
            side=OrderSide.SELL if side.upper() == "SELL" else OrderSide.BUY,
            price=None,
            amount=float(size),
            status=OrderStatus.OPEN,
            created_at=datetime.now(timezone.utc),
            raw=data,
        )

    async def cancel_order(self, order_id: str, pair: str = "") -> bool:
        """
        주문 취소.

        POST /private/v1/cancelOrders
        """
        payload = {"rootOrderIds": [int(order_id)]}

        sign_path = "/v1/cancelOrders"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", sign_path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            response = await self._post_with_rate_limit(
                f"{self._private_url}{sign_path}", headers, body_str
            )
            data = response.json()
            if data.get("status") != 0:
                logger.warning(f"GMO FX 주문 취소 실패: {data}")
                return False
            return True
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

    async def get_open_orders(self, pair: str) -> list[Order]:
        """
        미체결 주문 목록.

        GET /private/v1/activeOrders?symbol={SYMBOL}
        """
        client = self._get_client()
        symbol = self._pair_to_symbol(pair)
        query = urlencode({"symbol": symbol})
        sign_path = f"/v1/activeOrders?{query}"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{sign_path}", headers=headers)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        orders_data = data.get("data", {}).get("list", [])
        return [self._parse_order(o, pair) for o in orders_data]

    async def get_order(self, order_id: str, pair: str = "") -> Optional[Order]:
        """
        주문 상세 조회.

        GET /private/v1/orders?rootOrderId={id}
        """
        client = self._get_client()
        query = urlencode({"rootOrderId": order_id})
        sign_path = f"/v1/orders?{query}"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{sign_path}", headers=headers)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        orders_data = data.get("data", {}).get("list", [])
        if not orders_data:
            return None
        return self._parse_order(orders_data[0], pair)

    # ── 잔고 ───────────────────────────────────────────────────

    async def get_balance(self) -> Balance:
        """
        계좌 자산 조회.

        GET /private/v1/account/assets
        GMO FX는 단일 JPY 계좌. availableAmount → 주문 가능 금액.
        """
        client = self._get_client()
        sign_path = "/v1/account/assets"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{sign_path}", headers=headers)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        assets = data.get("data", [{}])
        asset = assets[0] if assets else {}
        equity = float(asset.get("equity", 0))
        available = float(asset.get("availableAmount", 0))

        return Balance(currencies={
            "jpy": CurrencyBalance(currency="jpy", amount=equity, available=available),
        })

    # ── 시세 ───────────────────────────────────────────────────

    async def get_ticker(self, pair: str) -> Ticker:
        """
        현재가 스냅샷 (Public API).

        GET /public/v1/ticker?symbol={SYMBOL}
        """
        client = self._get_client()
        symbol = self._pair_to_symbol(pair)
        query = urlencode({"symbol": symbol})
        url = f"{self._public_url}/v1/ticker?{query}"

        try:
            response = await client.get(url)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        items = data.get("data", [])
        if not items:
            raise ExchangeError(f"GMO FX ticker 데이터 없음: {symbol}")

        item = items[0] if isinstance(items, list) else items
        ask = float(item.get("ask", 0))
        bid = float(item.get("bid", 0))
        mid = (ask + bid) / 2 if ask > 0 and bid > 0 else 0

        return Ticker(
            pair=pair,
            last=mid,
            bid=bid,
            ask=ask,
            high=float(item.get("high", mid)),
            low=float(item.get("low", mid)),
            volume=0.0,  # GMO FX ticker에 volume 없음
        )

    # ── 증거금 (CFD/FX 전용) ─────────────────────────────────

    async def get_collateral(self) -> Collateral:
        """
        증거금 상태 조회.

        GET /private/v1/account/assets
        marginRatio → keep_rate 매핑.
        """
        client = self._get_client()
        sign_path = "/v1/account/assets"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{sign_path}", headers=headers)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        assets = data.get("data", [{}])
        asset = assets[0] if assets else {}

        equity = float(asset.get("equity", 0))
        margin = float(asset.get("margin", 0))
        margin_ratio = float(asset.get("marginRatio", 0))

        # keep_rate: marginRatio를 비율로 변환 (GMO: % 단위 → /100)
        keep_rate = margin_ratio / 100 if margin_ratio > 0 else 999.0

        return Collateral(
            collateral=equity,
            open_position_pnl=float(asset.get("positionLossGain", 0)),
            require_collateral=margin,
            keep_rate=keep_rate,
        )

    async def get_positions(self, product_code: str = "USD_JPY") -> list[FxPosition]:
        """
        건옥 목록 조회.

        GET /private/v1/openPositions?symbol={SYMBOL}
        """
        client = self._get_client()
        symbol = product_code.upper()
        query = urlencode({"symbol": symbol})
        sign_path = f"/v1/openPositions?{query}"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{sign_path}", headers=headers)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        items_data = data.get("data", {}).get("list", [])
        positions = []
        for item in items_data:
            open_date = None
            raw_ts = item.get("timestamp")
            if raw_ts:
                try:
                    open_date = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass

            positions.append(FxPosition(
                product_code=item.get("symbol", symbol),
                side=item.get("side", "BUY"),
                price=float(item.get("price", 0)),
                size=float(item.get("size", 0)),
                pnl=float(item.get("lossGain", 0)),
                leverage=0,  # GMO FX는 계좌 단위 레버리지
                require_collateral=0,
                swap_point_accumulate=float(item.get("totalSwap", 0)),
                sfd=0,
                open_date=open_date,
            ))
        return positions

    async def get_position_summary(self, symbol: str = "USD_JPY") -> dict:
        """
        건옥 요약 조회.

        GET /private/v1/positionSummary?symbol={SYMBOL}
        """
        client = self._get_client()
        query = urlencode({"symbol": symbol.upper()})
        sign_path = f"/v1/positionSummary?{query}"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{sign_path}", headers=headers)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        return data.get("data", {})

    # ── WebSocket ──────────────────────────────────────────────

    async def subscribe_trades(
        self,
        pair: str,
        callback: Callable[[float, float], Coroutine[Any, Any, None]],
    ) -> None:
        """
        GMO FX Public WS ticker 구독.

        wss://forex-api.coin.z.com/ws/public/v1
        subscribe → ticker チャンネル
        callback(mid_price, 0) — FX는 volume 없으므로 0.
        """
        symbol = self._pair_to_symbol(pair)
        delay = 1

        while True:
            try:
                async with websockets.connect(
                    self._ws_public_url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._ws_connected = True
                    logger.info(f"[GMO FX WS] 연결: {self._ws_public_url}")

                    await ws.send(json.dumps({
                        "command": "subscribe",
                        "channel": "ticker",
                        "symbol": symbol,
                    }))
                    logger.info(f"[GMO FX WS] ticker 구독: {symbol}")

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            if data.get("channel") == "ticker":
                                ask = float(data.get("ask", 0))
                                bid = float(data.get("bid", 0))
                                mid = (ask + bid) / 2 if ask > 0 and bid > 0 else 0
                                if mid > 0:
                                    await callback(mid, 0)
                        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                            continue

                    self._ws_connected = False
                    delay = 1

            except asyncio.CancelledError:
                self._ws_connected = False
                raise
            except (ConnectionClosed, OSError, Exception) as e:
                self._ws_connected = False
                logger.warning(f"[GMO FX WS] 끊김: {e}. {delay}초 후 재접속...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def subscribe_executions(
        self,
        callback: Callable[[dict], Coroutine[Any, Any, None]],
    ) -> None:
        """내 주문 체결 이벤트 구독 (Private WS — Phase 2 구현)."""
        logger.warning("subscribe_executions: GMO FX Private WS — Phase 2 예정")

    # ── 내부 변환 헬퍼 ─────────────────────────────────────────

    def _parse_order(self, data: dict, pair: str = "") -> Order:
        """GMO FX 주문 응답 → Order DTO."""
        side_str = data.get("side", "BUY").upper()
        side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL

        exec_type = data.get("executionType", "MARKET").upper()
        if side == OrderSide.BUY:
            order_type = OrderType.BUY if exec_type == "LIMIT" else OrderType.MARKET_BUY
        else:
            order_type = OrderType.SELL if exec_type == "LIMIT" else OrderType.MARKET_SELL

        price: Optional[float] = None
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

    # ── KLine (Public API) ─────────────────────────────────────

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        date: str,
        price_type: str = "BID",
    ) -> list[dict]:
        """
        OHLCV 캔들 데이터 조회 (Public API).

        GET /public/v1/klines?symbol={SYMBOL}&priceType={TYPE}&interval={INTERVAL}&date={DATE}

        Args:
            symbol:     USD_JPY 등 (대문자)
            interval:   1min, 5min, 15min, 30min, 1hour, 4hour, 8hour, 12hour, 1day, 1week, 1month
            date:       YYYYMMDD (1hour 이하) or YYYY (4hour 이상)
            price_type: ASK or BID

        Returns:
            [{"openTime": "...", "open": "...", "high": "...", "low": "...", "close": "..."}, ...]
        """
        client = self._get_client()
        params = {
            "symbol": symbol,
            "priceType": price_type,
            "interval": interval,
            "date": date,
        }
        url = f"{self._public_url}/v1/klines?{urlencode(params)}"

        try:
            response = await client.get(url)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        return data.get("data", [])

    async def get_symbols(self) -> list[dict]:
        """
        거래 규칙 조회 (Public API).

        GET /public/v1/symbols
        """
        client = self._get_client()
        url = f"{self._public_url}/v1/symbols"

        try:
            response = await client.get(url)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        return data.get("data", [])

    async def get_status(self) -> dict:
        """
        서비스 상태 조회 (Public API).

        GET /public/v1/status
        """
        client = self._get_client()
        url = f"{self._public_url}/v1/status"

        try:
            response = await client.get(url)
            data = response.json()
            return data.get("data", {})
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e
