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
from zoneinfo import ZoneInfo

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
from adapters.gmo_fx import parsers as _parsers

logger = logging.getLogger(__name__)

# POST 1회/초 제한 준수용
_POST_INTERVAL = 1.1  # 초

# GMO FX API 트라이얼: 2026-04-30 05:59 JST까지 수수료 0%
_JST = ZoneInfo("Asia/Tokyo")
GMOFX_TRIAL_EXPIRY = datetime(2026, 4, 30, 5, 59, 0, tzinfo=_JST)
GMOFX_POST_TRIAL_FEE_PCT = 0.04  # 트라이얼 후 수수료 (편도 %)


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

    @property
    def is_margin_trading(self) -> bool:
        """증거금 거래 여부. GMO FX는 항상 True."""
        return True

    @property
    def fee_rate_pct(self) -> float:
        """현재 적용 수수료 (편도 %). 트라이얼 기간이면 0."""
        now = datetime.now(_JST)
        if now < GMOFX_TRIAL_EXPIRY:
            return 0.0
        return GMOFX_POST_TRIAL_FEE_PCT

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

    def has_credentials(self) -> bool:
        """API 키/시크릿이 설정됐는지 확인."""
        return bool(self._signer._api_key and self._signer._api_secret)

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
        if not self.has_credentials():
            raise RuntimeError("GMO FX API 키 미설정 — 주문 실행 불가. 키 설정 후 재시작 필요.")

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

    # GMO FX pair별 가격 소수점 허용 자릿수
    _STOP_PRICE_DECIMALS: dict[str, int] = {
        "USD_JPY": 3, "EUR_JPY": 3, "GBP_JPY": 3, "AUD_JPY": 3,
        "NZD_JPY": 3, "CAD_JPY": 3, "CHF_JPY": 3,
        "EUR_USD": 5, "GBP_USD": 5,
    }

    def _round_price(self, pair: str, price: float) -> float:
        """GMO FX 통화쌍별 소수점 자릿수 round. 모든 주문 가격 필드에 공용 사용."""
        decimals = self._STOP_PRICE_DECIMALS.get(pair.upper(), 3)
        return round(price, decimals)

    async def close_order_stop(
        self,
        symbol: str,
        side: str,
        position_id: int,
        size: int,
        trigger_price: float,
    ) -> Order:
        """
        역지정(STOP) 결제 주문. 거래소 자체 SL 등록용.

        POST /private/v1/closeOrder
        executionType=STOP + stopPrice 사용.
        """
        # GMO FX 규격: stopPrice 소수점 자릿수 + size 정수 보장
        rounded_price = self._round_price(symbol, trigger_price)
        size_int = int(size)

        payload: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "executionType": "STOP",
            "stopPrice": str(rounded_price),
            "settlePosition": [
                {"positionId": position_id, "size": str(size_int)},
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
            price=trigger_price,
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
        sign_path = "/v1/activeOrders"
        request_path = f"{sign_path}?{query}"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{request_path}", headers=headers)
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
        sign_path = "/v1/orders"
        request_path = f"{sign_path}?{query}"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{request_path}", headers=headers)
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
        if not self.has_credentials():
            raise RuntimeError("GMO FX API 키 미설정 — 잔고 조회 불가.")
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

        raw = data.get("data", [{}])
        # API 문서상 list 반환이지만, dict로 오는 경우(주말 휴장 등) 방어
        if isinstance(raw, dict):
            asset = raw
        elif isinstance(raw, list):
            asset = raw[0] if raw else {}
        else:
            asset = {}
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

        item = next((i for i in items if i.get("symbol") == symbol), items[0]) if isinstance(items, list) else items
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
        sign_path = "/v1/openPositions"
        request_path = f"{sign_path}?{query}"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{request_path}", headers=headers)
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

            raw_pid = item.get("positionId")
            pid = int(raw_pid) if raw_pid is not None else None
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
                position_id=pid,
            ))
        return positions

    async def get_position_summary(self, symbol: str = "USD_JPY") -> dict:
        """
        건옥 요약 조회.

        GET /private/v1/positionSummary?symbol={SYMBOL}
        """
        client = self._get_client()
        query = urlencode({"symbol": symbol.upper()})
        sign_path = "/v1/positionSummary"
        request_path = f"{sign_path}?{query}"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{request_path}", headers=headers)
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
                    logger.debug(f"[GMO FX WS] 연결: {self._ws_public_url}")

                    await ws.send(json.dumps({
                        "command": "subscribe",
                        "channel": "ticker",
                        "symbol": symbol,
                    }))
                    logger.debug(f"[GMO FX WS] ticker 구독: {symbol}")

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            if data.get("channel") == "ticker" or data.get("symbol"):
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
                from core.monitoring.maintenance import is_maintenance_window, seconds_until_maintenance_end
                if is_maintenance_window("gmofx"):
                    wait = seconds_until_maintenance_end("gmofx") or 3600
                    logger.debug(f"[GMO FX WS] 정기 메인터넌스 중 — {wait}초 대기 후 재접속")
                    await asyncio.sleep(wait)
                    delay = 1
                else:
                    logger.warning(f"[GMO FX WS] 끊김: {e}. {delay}초 후 재접속...")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

    async def _get_ws_auth_token(self) -> str:
        """
        Private WS 인증 토큰 취득.

        POST /private/v1/ws-auth → access token
        토큰 유효시간 60분. 접속 중에는 자동 연장.
        """
        sign_path = "/v1/ws-auth"
        body_str = ""
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
            raise ConnectionError(f"GMO FX ws-auth HTTP 오류: {e}") from e

        return data.get("data", "")

    async def subscribe_executions(
        self,
        callback: Callable[[dict], Coroutine[Any, Any, None]],
    ) -> None:
        """
        Private WS 체결 이벤트 구독.

        1. POST /private/v1/ws-auth → access token
        2. wss://...ws/private/v1/{token} 접속
        3. subscribe executionEvents + orderEvents

        callback: 체결 이벤트 dict를 받는 코루틴.
        """
        delay = 1

        while True:
            try:
                token = await self._get_ws_auth_token()
                if not token:
                    raise AuthenticationError("GMO FX ws-auth 토큰 획득 실패")

                ws_url = f"{self._ws_private_url}/{token}"

                async with websockets.connect(
                    ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    logger.debug("[GMO FX Private WS] 연결 성공")

                    # executionEvents 구독 (INITIAL: 미체결 초기 데이터 1회)
                    await ws.send(json.dumps({
                        "command": "subscribe",
                        "channel": "executionEvents",
                        "option": "INITIAL",
                    }))
                    logger.debug("[GMO FX Private WS] executionEvents 구독")

                    # orderEvents 구독
                    await ws.send(json.dumps({
                        "command": "subscribe",
                        "channel": "orderEvents",
                    }))
                    logger.debug("[GMO FX Private WS] orderEvents 구독")

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            channel = data.get("channel", "")
                            if channel in ("executionEvents", "orderEvents"):
                                await callback(data)
                        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                            continue

                    delay = 1

            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError, Exception) as e:
                from core.monitoring.maintenance import is_maintenance_window, seconds_until_maintenance_end
                if is_maintenance_window("gmofx"):
                    wait = seconds_until_maintenance_end("gmofx") or 3600
                    logger.debug(f"[GMO FX Private WS] 정기 메인터넌스 중 — {wait}초 대기 후 재접속")
                    await asyncio.sleep(wait)
                    delay = 1
                else:
                    logger.warning(f"[GMO FX Private WS] 끊김: {e}. {delay}초 후 재접속...")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)

    # ── 내부 변환 헬퍼 (parsers.py 위임) ──────────────────────

    @staticmethod
    def _parse_order(data: dict, pair: str = "") -> Order:
        return _parsers.parse_order(data, pair)

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

    # ── IFD / IFDOCO 주문 ─────────────────────────────────────

    async def place_ifd_order(
        self,
        pair: str,
        side: str,
        size: int,
        first_execution_type: str = "LIMIT",
        first_price: Optional[float] = None,
        second_execution_type: str = "LIMIT",
        second_size: Optional[int] = None,
        second_price: Optional[float] = None,
    ) -> dict:
        """
        IFD 주문 — 신규 + 결제 동시 설정.

        POST /private/v1/ifdOrder
        1st 주문 체결 시 자동으로 2nd 결제 주문 발동.

        Returns:
            {"rootOrderId": ..., "clientOrderId": ..., ...}
        """
        payload: dict[str, Any] = {
            "symbol": self._pair_to_symbol(pair),
            "firstSide": side.upper(),
            "firstExecutionType": first_execution_type,
            "firstSize": str(size),
            "secondExecutionType": second_execution_type,
            "secondSize": str(second_size or size),
        }
        # API 규격: firstPrice (LIMIT/STOP 공통 필드)
        if first_price is not None:
            payload["firstPrice"] = str(self._round_price(pair, first_price))
        if second_execution_type == "LIMIT" and second_price is not None:
            payload["secondPrice"] = str(self._round_price(pair, second_price))
        if second_execution_type == "STOP" and second_price is not None:
            payload["secondStopPrice"] = str(self._round_price(pair, second_price))

        sign_path = "/v1/ifdOrder"
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

        result = data.get("data", [{}])
        return result[0] if result else data

    async def place_ifdoco_order(
        self,
        pair: str,
        side: str,
        size: int,
        first_execution_type: str = "LIMIT",
        first_price: Optional[float] = None,
        second_size: Optional[int] = None,
        take_profit_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
    ) -> dict:
        """
        IFDOCO 주문 — 신규 + (TP or SL) OCO.

        POST /private/v1/ifoOrder
        1st 체결 → take_profit(LIMIT) + stop_loss(STOP) 둘 다 발동.
        한쪽 체결되면 다른 쪽 자동 취소.

        Returns:
            {"rootOrderId": ..., ...}
        """
        payload: dict[str, Any] = {
            "symbol": self._pair_to_symbol(pair),
            "firstSide": side.upper(),
            "firstExecutionType": first_execution_type,
            "firstSize": str(size),
            "secondSize": str(second_size or size),
        }
        # API 규격: firstPrice (LIMIT/STOP 공통 필드). firstStopPrice는 존재하지 않음.
        if first_price is not None:
            payload["firstPrice"] = str(self._round_price(pair, first_price))
        if take_profit_price is not None:
            payload["secondLimitPrice"] = str(self._round_price(pair, take_profit_price))
        if stop_loss_price is not None:
            payload["secondStopPrice"] = str(self._round_price(pair, stop_loss_price))

        sign_path = "/v1/ifoOrder"
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

        result = data.get("data", [{}])
        return result[0] if result else data

    async def get_orders_by_root(self, root_order_id: str) -> list[dict]:
        """
        rootOrderId로 전체 서브 주문 raw dict 조회. IFD-OCO 상태 폴링용.

        GET /private/v1/orders?rootOrderId={id}

        IFDOCO의 경우 list 3개 반환:
          - settleType=OPEN: 1차 주문 (진입)
          - settleType=CLOSE + executionType=LIMIT: 2차 TP
          - settleType=CLOSE + executionType=STOP: 2차 SL

        Returns:
            list[dict] — raw dict 목록. 오류/미존재 시 빈 리스트.
        """
        client = self._get_client()
        query = urlencode({"rootOrderId": root_order_id})
        sign_path = "/v1/orders"
        request_path = f"{sign_path}?{query}"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{request_path}", headers=headers)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        return data.get("data", {}).get("list", [])

    # ── 주문 변경 ──────────────────────────────────────────────

    async def change_order(
        self, root_order_id: int, price: float
    ) -> bool:
        """
        주문 가격 변경.

        POST /private/v1/changeOrder
        """
        payload = {
            "rootOrderId": root_order_id,
            "price": str(price),
        }

        sign_path = "/v1/changeOrder"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", sign_path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            response = await self._post_with_rate_limit(
                f"{self._private_url}{sign_path}", headers, body_str
            )
            data = response.json()
            if data.get("status") != 0:
                logger.warning(f"GMO FX 주문 변경 실패: {data}")
                return False
            return True
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

    async def cancel_bulk_orders(self, symbol: str) -> bool:
        """
        특정 심볼 전체 주문 일괄 취소.

        POST /private/v1/cancelBulkOrder
        """
        payload = {"symbol": symbol.upper(), "desc": True}

        sign_path = "/v1/cancelBulkOrder"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", sign_path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            response = await self._post_with_rate_limit(
                f"{self._private_url}{sign_path}", headers, body_str
            )
            data = response.json()
            if data.get("status") != 0:
                logger.warning(f"GMO FX 일괄 취소 실패: {data}")
                return False
            return True
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

    # ── 약정 조회 ──────────────────────────────────────────────

    async def get_executions(
        self, order_id: Optional[str] = None, symbol: Optional[str] = None
    ) -> list[dict]:
        """
        약정(체결) 내역 조회.

        GET /private/v1/executions?orderId={orderId}
        """
        client = self._get_client()
        params: dict[str, str] = {}
        if order_id:
            params["orderId"] = order_id
        if symbol:
            params["symbol"] = symbol.upper()

        query = urlencode(params) if params else ""
        sign_path = "/v1/executions"
        request_path = f"{sign_path}?{query}" if query else sign_path
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{request_path}", headers=headers)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        return data.get("data", {}).get("list", [])

    async def get_latest_executions(self, symbol: str, count: int = 100) -> list[dict]:
        """
        최신 약정 내역.

        GET /private/v1/latestExecutions?symbol={SYMBOL}&count={COUNT}
        """
        client = self._get_client()
        params = {"symbol": symbol.upper(), "count": str(count)}
        query = urlencode(params)
        sign_path = "/v1/latestExecutions"
        request_path = f"{sign_path}?{query}"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{request_path}", headers=headers)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO FX HTTP 오류: {e}") from e

        return data.get("data", {}).get("list", [])

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
