"""
GmoCoinAdapter — ExchangeAdapter Protocol 구현 (GMO 코인 취引所レバレッジ).

GMO 코인 REST API + WebSocket을 표준 ExchangeAdapter 인터페이스로 감싼다.

주요 특징:
- 인증: timestamp(ms) + METHOD + /v1/path + body → HMAC-SHA256 (GmoFxSigner 재사용)
- Base URL: public → /public/, private → /private/
- 시장가 주문: POST /v1/order (executionType=MARKET) — speedOrder 없음
- 건옥 결제: POST /v1/closeOrder + settlePosition[{positionId, size}]
- 일괄 결제: POST /v1/closeBulkOrder (positionId 불필요)
- 잔고: account/assets → 현물 통화별 잔고
- 증거금: account/margin → Collateral DTO
- 건옥: openPositions → positionId, symbol, side, size, price
- POST 레이트 리밋: Tier1 20회/초 (GmoFx 1/초 대비 훨씬 여유)
- 취引所レバレッジ 12종: BTC_JPY, ETH_JPY, ..., SUI_JPY
- 24/7 운영 (메인터넌스 시 제외) — 주말 청산 불필요
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
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
from adapters.gmo_coin import parsers as _parsers

logger = logging.getLogger(__name__)

# GMO 코인 취引所 레버리지 심볼 목록 (2026-04 기준)
_LEVERAGE_SYMBOLS = frozenset({
    "BTC_JPY", "ETH_JPY", "BCH_JPY", "LTC_JPY", "XRP_JPY",
    "DOT_JPY", "ATOM_JPY", "ADA_JPY", "LINK_JPY", "DOGE_JPY",
    "SOL_JPY", "SUI_JPY",
})

# pair(소문자) → ticker API 조회용 심볼 (레버리지 페어는 대문자 그대로)
_PAIR_TO_TICKER_SYMBOL: dict[str, str] = {
    sym.lower(): sym for sym in _LEVERAGE_SYMBOLS
}
# 현물 심볼 ticker 응답 역매핑 (혹시 "BTC"로 응답 시 보정)
_SPOT_SYMBOL_FALLBACK: dict[str, str] = {
    "BTC": "BTC_JPY", "ETH": "ETH_JPY", "XRP": "XRP_JPY",
}

# GMO 코인 레버리지 취引所 sizeStep (2026-04 기준, /public/v1/symbols 응답 기반)
# https://api.coin.z.com/public/v1/symbols
_SIZE_STEP: dict[str, float] = {
    "BTC_JPY": 0.001,
    "ETH_JPY": 0.01,
    "BCH_JPY": 0.01,
    "LTC_JPY": 0.1,
    "XRP_JPY": 1.0,
    "DOT_JPY": 0.1,
    "ATOM_JPY": 0.1,
    "ADA_JPY": 1.0,
    "LINK_JPY": 0.1,
    "DOGE_JPY": 1.0,
    "SOL_JPY": 0.01,
    "SUI_JPY": 0.1,
}
_DEFAULT_SIZE_STEP = 0.001


def _floor_to_step(value: float, step: float) -> float:
    """value를 step 단위로 내림 처리. 소수점 precision 오류 방지."""
    if step <= 0:
        return value
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    floored = math.floor(value / step) * step
    return round(floored, decimals)


class GmoCoinAdapter:
    """
    GMO 코인 취引所レバレッジ 어댑터.

    ExchangeAdapter Protocol을 구조적 서브타이핑으로 충족한다.
    추가로 get_collateral(), get_positions()를 제공하여
    CfdTrendFollowingManager와 호환된다.

    ⚠️ 주말 청산 불필요: is_always_open = True (24/7 시장).
    ⚠️ IFD 미지원: SL 주문 자동 미설정 — 전략 SL이 먼저 발동하도록 losscutPrice를 넓게 설정.
    """

    def __init__(self, api_key: str, api_secret: str, base_url: str) -> None:
        # 서명 로직은 GMO FX와 100% 동일 → GmoFxSigner 재사용
        self._signer = GmoFxSigner(api_key=api_key, api_secret=api_secret)
        self._base_url = base_url.rstrip("/")
        self._public_url = f"{self._base_url}/public"
        self._private_url = f"{self._base_url}/private"
        self._client: Optional[httpx.AsyncClient] = None
        # WS
        self._ws: Optional[Any] = None
        self._ws_connected: bool = False
        self._ws_public_url = os.environ.get(
            "GMO_COIN_WS_PUBLIC_URL", "wss://api.coin.z.com/ws/public/v1"
        )
        self._ws_private_url = os.environ.get(
            "GMO_COIN_WS_PRIVATE_URL", "wss://api.coin.z.com/ws/private/v1"
        )

    # ── 거래소 식별 ─────────────────────────────────────────────

    @property
    def exchange_name(self) -> str:
        return "gmo_coin"

    @property
    def constraints(self) -> ExchangeConstraints:
        return ExchangeConstraints(
            min_order_sizes={
                "btc": 0.01,   # BTC_JPY sizeStep
                "eth": 0.1,
                "xrp": 10.0,
                "doge": 100.0,
            },
            rate_limit=(20, 1),  # Tier1: 20req/s
            extra={
                "leverage_max": 2,
                "leverage_fee_pct_daily": 0.04,  # 0.04%/일
                "losscut_ratio": 0.30,            # 유지율 30% 이하 강제청산
                "margin_call_ratio": 0.50,         # 50% 이하 추증 알림
            },
        )

    @property
    def is_margin_trading(self) -> bool:
        """레버리지 거래 전용 어댑터 — 항상 True."""
        return True

    @property
    def is_always_open(self) -> bool:
        """24/7 시장 — 주말 자동 청산 불필요."""
        return True

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
        return bool(self._signer._api_key and self._signer._api_secret)

    # ── 내부 헬퍼 ──────────────────────────────────────────────

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ConnectionError("connect()를 먼저 호출해야 합니다.")
        return self._client

    def _get_auth_headers(self, method: str, sign_path: str, body: str = "") -> dict[str, str]:
        """서명 헤더 생성. sign_path는 /v1/... 형식."""
        return self._signer.sign(method=method, path=sign_path, body=body)

    @staticmethod
    def _pair_to_symbol(pair: str) -> str:
        """btc_jpy → BTC_JPY."""
        return pair.upper()

    def _raise_for_exchange_error(
        self, response: httpx.Response, data: dict | None = None
    ) -> None:
        """HTTP 상태코드 + GMO 코인 status 코드를 표준 ExchangeError로 변환."""
        if response.status_code == 401:
            raise AuthenticationError(f"GMO 코인 인증 실패: {response.text}")
        if response.status_code == 429:
            raise RateLimitError(f"GMO 코인 레이트 리밋: {response.text}")
        if response.status_code >= 400:
            raise ExchangeError(
                f"GMO 코인 API HTTP 오류: status={response.status_code} body={response.text}"
            )
        if data and data.get("status") != 0:
            messages = data.get("messages", [])
            msg_codes = [m.get("message_code", "?") for m in messages]
            msg_text = "; ".join(
                f"{m.get('message_code', '?')}: {m.get('message_string', '?')}"
                for m in messages
            ) if messages else str(data)

            # 에러코드별 예외 분류
            if any(c in ("ERR-5003",) for c in msg_codes):
                raise RateLimitError(f"GMO 코인 레이트 리밋: {msg_text}")
            if any(c in ("ERR-5010", "ERR-5011", "ERR-5012") for c in msg_codes):
                raise AuthenticationError(f"GMO 코인 인증 에러: {msg_text}")
            if any(c in ("ERR-201", "ERR-208") for c in msg_codes):
                raise OrderError(f"GMO 코인 잔고/수량 부족: {msg_text}")
            if any(c in ("ERR-5201", "ERR-5202") for c in msg_codes):
                raise ConnectionError(f"GMO 코인 메인터넌스 중: {msg_text}")

            raise ExchangeError(f"GMO 코인 비즈니스 에러: {msg_text}", raw=data)

    # ── 시세 ───────────────────────────────────────────────────

    async def get_ticker(self, pair: str) -> Ticker:
        """
        현재가 스냅샷 (Public API).

        GET /public/v1/ticker?symbol={SYMBOL}
        레버리지 페어: pair="btc_jpy" → symbol="BTC_JPY"
        """
        client = self._get_client()
        symbol = self._pair_to_symbol(pair)
        url = f"{self._public_url}/v1/ticker?{urlencode({'symbol': symbol})}"

        try:
            response = await client.get(url)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        items = data.get("data", [])
        if not items:
            raise ExchangeError(f"GMO 코인 ticker 데이터 없음: {symbol}")

        # 리스트 반환 → 해당 심볼 찾기
        if isinstance(items, list):
            item = next(
                (i for i in items if i.get("symbol", "").upper() in (symbol, symbol.split("_")[0])),
                items[0],
            )
        else:
            item = items

        return Ticker(
            pair=pair,
            last=float(item.get("last", 0)),
            bid=float(item.get("bid", 0)),
            ask=float(item.get("ask", 0)),
            high=float(item.get("high", 0)),
            low=float(item.get("low", 0)),
            volume=float(item.get("volume", 0)),
            timestamp=None,
        )

    # ── 잔고 ───────────────────────────────────────────────────

    async def get_balance(self) -> Balance:
        """
        자산 잔고 조회.

        GET /private/v1/account/assets
        응답: [{symbol: "JPY", amount: "...", available: "..."}, {symbol: "BTC", ...}, ...]
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
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        currencies: dict[str, CurrencyBalance] = {}
        for item in data.get("data", []):
            sym = item.get("symbol", "").lower()
            if not sym:
                continue
            currencies[sym] = CurrencyBalance(
                currency=sym,
                amount=float(item.get("amount", 0)),
                available=float(item.get("available", 0)),
            )
        return Balance(currencies=currencies)

    # ── 증거금 (레버리지 전용) ──────────────────────────────────

    async def get_collateral(self) -> Collateral:
        """
        증거금 상태 조회.

        GET /private/v1/account/margin
        """
        client = self._get_client()
        sign_path = "/v1/account/margin"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(f"{self._private_url}{sign_path}", headers=headers)
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        d = data.get("data", {})
        # marginRatio는 % 단위 (예: 6683.6 → 66.836배)
        # CfdTrendFollowingManager는 keep_rate를 비율로 사용 (100=100%)
        margin_ratio_raw = float(d.get("marginRatio", 0))

        return Collateral(
            collateral=float(d.get("actualProfitLoss", 0)),
            open_position_pnl=float(d.get("profitLoss", 0)),
            require_collateral=float(d.get("margin", 0)),
            keep_rate=margin_ratio_raw,   # % 그대로 (CfdManager 기존 convention 유지)
        )

    # ── 건옥 (레버리지 전용) ─────────────────────────────────────

    async def get_positions(self, product_code: str = "BTC_JPY") -> list[FxPosition]:
        """
        건옥 목록 조회 (전체 pagination 자동).

        GET /private/v1/openPositions?symbol=BTC_JPY
        """
        client = self._get_client()
        symbol = product_code.upper()
        sign_path = "/v1/openPositions"
        query = urlencode({"symbol": symbol, "count": 100})
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(
                f"{self._private_url}{sign_path}?{query}", headers=headers
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        positions = []
        for item in data.get("data", {}).get("list", []):
            open_date = None
            raw_ts = item.get("timestamp")
            if raw_ts:
                try:
                    open_date = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass

            pid = item.get("positionId")
            positions.append(FxPosition(
                product_code=item.get("symbol", symbol),
                side=item.get("side", "BUY"),
                price=float(item.get("price", 0)),
                size=float(item.get("size", 0)),
                pnl=float(item.get("lossGain", 0)),
                leverage=float(item.get("leverage", 2)),
                require_collateral=0.0,    # API 미제공
                swap_point_accumulate=0.0,  # 암호화폐 레버리지에 스왑 없음
                sfd=0.0,
                open_date=open_date,
                position_id=int(pid) if pid is not None else None,
            ))
        return positions

    async def get_position_summary(self, symbol: str = "BTC_JPY") -> dict:
        """
        건옥 요약 조회.

        GET /private/v1/positionSummary?symbol=BTC_JPY
        """
        client = self._get_client()
        sign_path = "/v1/positionSummary"
        query = urlencode({"symbol": symbol.upper()})
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(
                f"{self._private_url}{sign_path}?{query}", headers=headers
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        result = data.get("data", {})
        # positionSummary는 list로 반환될 수 있음
        if isinstance(result, dict) and "list" in result:
            return result
        return result

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

        MARKET_BUY:  amount=JPY 금액 → ticker.ask로 BTC 수량 계산
        MARKET_SELL: amount=BTC 수량
        BUY/SELL:    amount=BTC 수량, price 필수

        POST /private/v1/order
        """
        if not self.has_credentials():
            raise RuntimeError("GMO 코인 API 키 미설정 — 주문 실행 불가.")

        symbol = self._pair_to_symbol(pair)
        side = "BUY" if order_type in (OrderType.BUY, OrderType.MARKET_BUY) else "SELL"
        is_market = order_type in (OrderType.MARKET_BUY, OrderType.MARKET_SELL)

        # MARKET_BUY: JPY → 코인 수량 변환
        if order_type == OrderType.MARKET_BUY:
            ticker = await self.get_ticker(pair)
            if ticker.ask <= 0:
                raise OrderError(f"GMO 코인 ticker ask 조회 실패: {pair}")
            raw_size = amount / ticker.ask
        else:
            raw_size = amount

        # sizeStep 단위로 내림 (ERR-5114 방지)
        step = _SIZE_STEP.get(symbol, _DEFAULT_SIZE_STEP)
        size = _floor_to_step(raw_size, step)
        if size <= 0:
            raise OrderError(
                f"GMO 코인 주문 불가 — 수량이 sizeStep({step}) 미만: "
                f"raw={raw_size:.8f} pair={pair}"
            )

        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "executionType": "MARKET" if is_market else "LIMIT",
            "size": str(size),
        }
        if not is_market:
            if price is None:
                raise OrderError("지정가 주문에는 price가 필요합니다.")
            payload["price"] = str(int(price))

        sign_path = "/v1/order"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", sign_path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            client = self._get_client()
            response = await client.post(
                f"{self._private_url}{sign_path}",
                headers=headers,
                content=body_str,
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError, OrderError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        # GMO 코인 order 응답: data 필드가 직접 orderId (string)
        order_id = str(data.get("data", ""))
        if not order_id:
            raise OrderError(f"GMO 코인 order 실패 — orderId 없음: {data}")

        logger.info(f"[GMO Coin] 주문 완료: {side} {size} {symbol} orderId={order_id}")

        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        return Order(
            order_id=order_id,
            pair=pair,
            order_type=order_type,
            side=order_side,
            price=price,
            amount=size,
            status=OrderStatus.OPEN,
            created_at=datetime.now(timezone.utc),
            raw=data,
        )

    async def close_position(
        self, position_id: int, side: str, size: float, pair: str,
    ) -> Order:
        """
        개별 건옥 결제 (positionId 지정).

        POST /private/v1/closeOrder
        side: 전략에서 넘긴 원래 포지션의 side → 반대편으로 청산
        """
        symbol = self._pair_to_symbol(pair)
        # 청산 side는 원 포지션의 반대
        close_side = "SELL" if side.upper() == "BUY" else "BUY"

        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": close_side,
            "executionType": "MARKET",
            "settlePosition": [
                {"positionId": position_id, "size": str(size)},
            ],
        }

        sign_path = "/v1/closeOrder"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", sign_path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            client = self._get_client()
            response = await client.post(
                f"{self._private_url}{sign_path}",
                headers=headers,
                content=body_str,
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        order_id = str(data.get("data", ""))
        logger.info(
            f"[GMO Coin] 건옥 결제: positionId={position_id} {close_side} {size} {symbol}"
            f" orderId={order_id}"
        )

        close_side_enum = OrderSide.BUY if close_side == "BUY" else OrderSide.SELL
        close_type = OrderType.MARKET_BUY if close_side == "BUY" else OrderType.MARKET_SELL
        return Order(
            order_id=order_id,
            pair=pair,
            order_type=close_type,
            side=close_side_enum,
            price=None,
            amount=size,
            status=OrderStatus.OPEN,
            created_at=datetime.now(timezone.utc),
            raw=data,
        )

    async def close_position_bulk(
        self, symbol: str, side: str, size: float,
    ) -> Order:
        """
        일괄 결제 (positionId 불필요).

        POST /private/v1/closeBulkOrder
        side: 원 포지션의 side → 반대편으로 청산
        """
        close_side = "SELL" if side.upper() == "BUY" else "BUY"
        sym = symbol.upper()

        payload: dict[str, Any] = {
            "symbol": sym,
            "side": close_side,
            "executionType": "MARKET",
            "size": str(size),
        }

        sign_path = "/v1/closeBulkOrder"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", sign_path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            client = self._get_client()
            response = await client.post(
                f"{self._private_url}{sign_path}",
                headers=headers,
                content=body_str,
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        order_id = str(data.get("data", ""))
        logger.info(f"[GMO Coin] 일괄 결제: {close_side} {size} {sym} orderId={order_id}")

        close_side_enum = OrderSide.BUY if close_side == "BUY" else OrderSide.SELL
        close_type = OrderType.MARKET_BUY if close_side == "BUY" else OrderType.MARKET_SELL
        return Order(
            order_id=order_id,
            pair=sym.lower(),
            order_type=close_type,
            side=close_side_enum,
            price=None,
            amount=size,
            status=OrderStatus.OPEN,
            created_at=datetime.now(timezone.utc),
            raw=data,
        )

    async def change_losscut_price(self, position_id: int, price: float) -> bool:
        """
        건옥 ロスカットレート 변경.

        POST /private/v1/changeLosscutPrice
        """
        payload = {"positionId": position_id, "losscutPrice": str(int(price))}
        sign_path = "/v1/changeLosscutPrice"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", sign_path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            client = self._get_client()
            response = await client.post(
                f"{self._private_url}{sign_path}",
                headers=headers,
                content=body_str,
            )
            data = response.json()
            if data.get("status") != 0:
                logger.warning(f"[GMO Coin] ロスカットレート 변경 실패: {data}")
                return False
            return True
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

    async def cancel_order(self, order_id: str, pair: str = "") -> bool:
        """
        주문 취소.

        POST /private/v1/cancelOrder
        """
        payload = {"orderId": int(order_id)}
        sign_path = "/v1/cancelOrder"
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._get_auth_headers("POST", sign_path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            client = self._get_client()
            response = await client.post(
                f"{self._private_url}{sign_path}",
                headers=headers,
                content=body_str,
            )
            data = response.json()
            if data.get("status") != 0:
                messages = data.get("messages", [])
                codes = [m.get("message_code", "") for m in messages]
                # ERR-5122: 이미 취소됨 → 성공으로 처리
                if "ERR-5122" in codes:
                    logger.warning(f"[GMO Coin] 주문 이미 취소됨 orderId={order_id}")
                    return True
                logger.warning(f"[GMO Coin] 주문 취소 실패: {data}")
                return False
            return True
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

    async def get_open_orders(self, pair: str) -> list[Order]:
        """
        미체결 주문 목록.

        GET /private/v1/activeOrders?symbol={SYMBOL}&count=100
        """
        client = self._get_client()
        symbol = self._pair_to_symbol(pair)
        sign_path = "/v1/activeOrders"
        query = urlencode({"symbol": symbol, "count": 100})
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(
                f"{self._private_url}{sign_path}?{query}", headers=headers
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        orders_data = data.get("data", {}).get("list", [])
        return [_parsers.parse_order(o, pair) for o in orders_data]

    async def get_order(self, order_id: str, pair: str = "") -> Optional[Order]:
        """
        주문 상세 조회.

        GET /private/v1/orders?orderId={id}
        """
        client = self._get_client()
        sign_path = "/v1/orders"
        query = urlencode({"orderId": order_id})
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(
                f"{self._private_url}{sign_path}?{query}", headers=headers
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        items = data.get("data", {}).get("list", [])
        if not items:
            return None
        return _parsers.parse_order(items[0], pair)

    async def get_executions(self, order_id: str) -> list[dict]:
        """
        약정 정보 조회 (positionId 포함).

        GET /private/v1/executions?orderId={id}
        """
        client = self._get_client()
        sign_path = "/v1/executions"
        query = urlencode({"orderId": order_id})
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(
                f"{self._private_url}{sign_path}?{query}", headers=headers
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        return data.get("data", {}).get("list", [])

    async def get_latest_executions(self, symbol: str, count: int = 100) -> list[dict]:
        """
        최신 약정 내역 조회 (orderId 불필요).

        GET /private/v1/latestExecutions?symbol={SYMBOL}&count={COUNT}
        """
        client = self._get_client()
        params = {"symbol": symbol.upper(), "count": str(count)}
        query = urlencode(params)
        sign_path = "/v1/latestExecutions"
        request_path = f"{sign_path}?{query}"
        headers = self._get_auth_headers("GET", sign_path)

        try:
            response = await client.get(
                f"{self._private_url}{request_path}", headers=headers
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        return data.get("data", {}).get("list", [])

    # ── WebSocket ──────────────────────────────────────────────

    async def subscribe_trades(
        self,
        pair: str,
        callback: Callable[[float, float], Coroutine[Any, Any, None]],
    ) -> None:
        """
        Public WS trades 채널 구독.

        wss://api.coin.z.com/ws/public/v1
        subscribe → trades 채널
        callback(price, size)
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
                    logger.debug(f"[GMO Coin WS] 연결: {self._ws_public_url}")

                    await ws.send(json.dumps({
                        "command": "subscribe",
                        "channel": "trades",
                        "symbol": symbol,
                    }))
                    logger.debug(f"[GMO Coin WS] trades 구독: {symbol}")

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            if msg.get("channel") == "trades":
                                price = float(msg.get("price", 0))
                                size = float(msg.get("size", 0))
                                if price > 0:
                                    await callback(price, size)
                        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                            continue

                    self._ws_connected = False
                    delay = 1

            except asyncio.CancelledError:
                self._ws_connected = False
                raise
            except (ConnectionClosed, OSError, Exception) as e:
                self._ws_connected = False
                logger.warning(f"[GMO Coin WS] 끊김: {e}. {delay}초 후 재접속...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _get_ws_auth_token(self) -> str:
        """
        Private WS 인증 토큰 취득.

        POST /private/v1/ws-auth body={} → access token (유효 60분)
        """
        sign_path = "/v1/ws-auth"
        body_str = "{}"
        headers = self._get_auth_headers("POST", sign_path, body=body_str)
        headers["Content-Type"] = "application/json"

        try:
            client = self._get_client()
            response = await client.post(
                f"{self._private_url}{sign_path}",
                headers=headers,
                content=body_str,
            )
            data = response.json()
            self._raise_for_exchange_error(response, data)
        except (AuthenticationError, RateLimitError, ExchangeError):
            raise
        except httpx.HTTPError as e:
            raise ConnectionError(f"GMO 코인 ws-auth HTTP 오류: {e}") from e

        return str(data.get("data", ""))

    async def subscribe_executions(
        self,
        callback: Callable[[dict], Coroutine[Any, Any, None]],
    ) -> None:
        """
        Private WS 체결/주문/포지션 이벤트 구독.

        1. POST /private/v1/ws-auth → access token
        2. wss://api.coin.z.com/ws/private/v1/{token} 접속
        3. subscribe executionEvents, orderEvents, positionEvents

        callback: 이벤트 dict를 받는 코루틴.
        이벤트 dict 주요 필드: channel, orderId, executionId,
          positionId(레버리지), settleType, side, executionPrice.
        """
        delay = 1

        while True:
            try:
                token = await self._get_ws_auth_token()
                if not token:
                    raise AuthenticationError("GMO 코인 ws-auth 토큰 획득 실패")

                ws_url = f"{self._ws_private_url}/{token}"

                async with websockets.connect(
                    ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    logger.debug("[GMO Coin Private WS] 연결 성공")

                    for channel in ("executionEvents", "orderEvents", "positionEvents"):
                        await ws.send(json.dumps({
                            "command": "subscribe",
                            "channel": channel,
                        }))
                    logger.debug("[GMO Coin Private WS] 채널 구독 완료")

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            channel = msg.get("channel", "")
                            if channel in (
                                "executionEvents", "orderEvents", "positionEvents",
                            ):
                                await callback(msg)
                        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                            continue

                    delay = 1

            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError, Exception) as e:
                logger.warning(f"[GMO Coin Private WS] 끊김: {e}. {delay}초 후 재접속...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    # ── KLine (Public API) ─────────────────────────────────────

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        date: str,
    ) -> list[dict]:
        """
        OHLCV 캔들 데이터 조회 (Public API).

        GET /public/v1/klines?symbol={SYMBOL}&interval={INTERVAL}&date={DATE}

        Args:
            symbol:   BTC_JPY 등 (대문자)
            interval: 1min, 5min, 10min, 15min, 30min, 1hour,
                      4hour, 8hour, 12hour, 1day, 1week, 1month
            date:     YYYYMMDD (1hour 이하) or YYYY (4hour 이상)

        Returns:
            [{"openTime": "...", "open": "...", "high": "...",
              "low": "...", "close": "...", "volume": "..."}, ...]
        """
        client = self._get_client()
        params = {
            "symbol": symbol.upper(),
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
            raise ConnectionError(f"GMO 코인 HTTP 오류: {e}") from e

        return data.get("data", [])
