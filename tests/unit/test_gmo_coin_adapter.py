"""
GmoCoinAdapter 단위 테스트.

실제 API 호출 없음 — 어댑터 변환 로직·에러 처리 검증.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from adapters.gmo_coin.client import GmoCoinAdapter
from adapters.gmo_coin import parsers
from core.exchange.errors import (
    AuthenticationError,
    ConnectionError,
    ExchangeError,
    OrderError,
    RateLimitError,
)
from core.exchange.types import (
    OrderSide,
    OrderStatus,
    OrderType,
)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _mock_response(data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def adapter() -> GmoCoinAdapter:
    a = GmoCoinAdapter(
        api_key="test_key",
        api_secret="test_secret",
        base_url="https://api.coin.z.com",
    )
    await a.connect()
    yield a
    await a.close()


# ─────────────────────────────────────────────────────────────
# 기본 속성
# ─────────────────────────────────────────────────────────────

def test_exchange_name(adapter: GmoCoinAdapter) -> None:
    assert adapter.exchange_name == "gmo_coin"


def test_constraints_rate_limit(adapter: GmoCoinAdapter) -> None:
    assert adapter.constraints.rate_limit == (20, 1)


def test_is_margin_trading(adapter: GmoCoinAdapter) -> None:
    assert adapter.is_margin_trading is True


def test_is_always_open(adapter: GmoCoinAdapter) -> None:
    """24/7 시장 — 주말 청산 불필요."""
    assert adapter.is_always_open is True


def test_has_credentials_true(adapter: GmoCoinAdapter) -> None:
    assert adapter.has_credentials() is True


def test_has_credentials_false() -> None:
    a = GmoCoinAdapter(api_key="", api_secret="", base_url="https://api.coin.z.com")
    assert a.has_credentials() is False


def test_pair_to_symbol() -> None:
    assert GmoCoinAdapter._pair_to_symbol("btc_jpy") == "BTC_JPY"
    assert GmoCoinAdapter._pair_to_symbol("eth_jpy") == "ETH_JPY"


def test_url_structure(adapter: GmoCoinAdapter) -> None:
    assert adapter._public_url == "https://api.coin.z.com/public"
    assert adapter._private_url == "https://api.coin.z.com/private"


def test_signer_reused(adapter: GmoCoinAdapter) -> None:
    """GmoFxSigner의 서명 헤더 키 3개 확인 (재사용 검증)."""
    headers = adapter._get_auth_headers("GET", "/v1/orders")
    assert set(headers.keys()) == {"API-KEY", "API-TIMESTAMP", "API-SIGN"}
    assert headers["API-KEY"] == "test_key"


# ─────────────────────────────────────────────────────────────
# 에러 처리
# ─────────────────────────────────────────────────────────────

def test_raise_for_exchange_error_ok(adapter: GmoCoinAdapter) -> None:
    """status=0 → 예외 없음."""
    resp = _mock_response({"status": 0, "data": "123"})
    adapter._raise_for_exchange_error(resp, {"status": 0})  # no raise


def test_raise_for_exchange_error_rate_limit(adapter: GmoCoinAdapter) -> None:
    """ERR-5003 → RateLimitError."""
    resp = _mock_response({"status": 1, "messages": [{"message_code": "ERR-5003", "message_string": "Rate limit"}]})
    with pytest.raises(RateLimitError):
        adapter._raise_for_exchange_error(resp, resp.json())


def test_raise_for_exchange_error_auth(adapter: GmoCoinAdapter) -> None:
    """ERR-5012 → AuthenticationError."""
    resp = _mock_response({"status": 1, "messages": [{"message_code": "ERR-5012", "message_string": "Unauthorized"}]})
    with pytest.raises(AuthenticationError):
        adapter._raise_for_exchange_error(resp, resp.json())


def test_raise_for_exchange_error_maintenance(adapter: GmoCoinAdapter) -> None:
    """ERR-5201 → ConnectionError (메인터넌스)."""
    resp = _mock_response({"status": 1, "messages": [{"message_code": "ERR-5201", "message_string": "Maintenance"}]})
    with pytest.raises(ConnectionError, match="메인터넌스"):
        adapter._raise_for_exchange_error(resp, resp.json())


def test_raise_for_exchange_error_order(adapter: GmoCoinAdapter) -> None:
    """ERR-201 → OrderError (잔고/수량 부족)."""
    resp = _mock_response({"status": 1, "messages": [{"message_code": "ERR-201", "message_string": "Insufficient funds"}]})
    with pytest.raises(OrderError):
        adapter._raise_for_exchange_error(resp, resp.json())


def test_raise_for_exchange_error_http_401(adapter: GmoCoinAdapter) -> None:
    """HTTP 401 → AuthenticationError."""
    resp = _mock_response({}, status_code=401)
    with pytest.raises(AuthenticationError):
        adapter._raise_for_exchange_error(resp)


def test_raise_for_exchange_error_http_429(adapter: GmoCoinAdapter) -> None:
    """HTTP 429 → RateLimitError."""
    resp = _mock_response({}, status_code=429)
    with pytest.raises(RateLimitError):
        adapter._raise_for_exchange_error(resp)


# ─────────────────────────────────────────────────────────────
# get_ticker
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_ticker(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({
        "status": 0,
        "data": [
            {
                "symbol": "BTC_JPY",
                "last": "9000000",
                "bid": "8999000",
                "ask": "9001000",
                "high": "9200000",
                "low": "8800000",
                "volume": "123.456",
            }
        ],
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    ticker = await adapter.get_ticker("btc_jpy")
    assert ticker.pair == "btc_jpy"
    assert ticker.last == 9000000.0
    assert ticker.bid == 8999000.0
    assert ticker.ask == 9001000.0


@pytest.mark.asyncio
async def test_get_ticker_no_data(adapter: GmoCoinAdapter) -> None:
    """빈 data → ExchangeError."""
    resp = _mock_response({"status": 0, "data": []})
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    with pytest.raises(ExchangeError, match="ticker 데이터 없음"):
        await adapter.get_ticker("btc_jpy")


# ─────────────────────────────────────────────────────────────
# get_balance
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_balance(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({
        "status": 0,
        "data": [
            {"symbol": "JPY", "amount": "1000000", "available": "500000"},
            {"symbol": "BTC", "amount": "0.5", "available": "0.5"},
        ],
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    balance = await adapter.get_balance()
    assert "jpy" in balance.currencies
    assert balance.currencies["jpy"].amount == 1000000.0
    assert balance.currencies["jpy"].available == 500000.0
    assert "btc" in balance.currencies
    assert balance.currencies["btc"].amount == 0.5


# ─────────────────────────────────────────────────────────────
# get_collateral
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_collateral(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({
        "status": 0,
        "data": {
            "actualProfitLoss": "1000000",
            "profitLoss": "50000",
            "margin": "15000",
            "marginRatio": "6683.6",
        },
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    col = await adapter.get_collateral()
    assert col.collateral == 1000000.0
    assert col.open_position_pnl == 50000.0
    assert col.require_collateral == 15000.0
    assert col.keep_rate == 6683.6


# ─────────────────────────────────────────────────────────────
# get_positions
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_positions_empty(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({"status": 0, "data": {"list": []}})
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    positions = await adapter.get_positions("BTC_JPY")
    assert positions == []


@pytest.mark.asyncio
async def test_get_positions_with_short(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({
        "status": 0,
        "data": {
            "list": [
                {
                    "positionId": 123456,
                    "symbol": "BTC_JPY",
                    "side": "SELL",
                    "size": "0.01",
                    "price": "9100000",
                    "lossGain": "-1500",
                    "leverage": "2",
                    "timestamp": "2026-04-01T12:00:00.000Z",
                }
            ]
        },
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    positions = await adapter.get_positions("BTC_JPY")
    assert len(positions) == 1
    pos = positions[0]
    assert pos.position_id == 123456
    assert pos.side == "SELL"
    assert pos.size == 0.01
    assert pos.price == 9100000.0
    assert pos.pnl == -1500.0


@pytest.mark.asyncio
async def test_get_positions_with_long(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({
        "status": 0,
        "data": {
            "list": [
                {
                    "positionId": 789,
                    "symbol": "BTC_JPY",
                    "side": "BUY",
                    "size": "0.02",
                    "price": "8800000",
                    "lossGain": "3000",
                    "leverage": "2",
                    "timestamp": "2026-04-02T09:00:00.000Z",
                }
            ]
        },
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    positions = await adapter.get_positions("BTC_JPY")
    pos = positions[0]
    assert pos.position_id == 789
    assert pos.side == "BUY"
    assert pos.pnl == 3000.0


# ─────────────────────────────────────────────────────────────
# place_order
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_place_order_market_buy(adapter: GmoCoinAdapter) -> None:
    """MARKET_BUY: executionType=MARKET, side=BUY."""
    # get_ticker mock
    ticker_resp = _mock_response({
        "status": 0,
        "data": [{"symbol": "BTC_JPY", "last": "9000000", "bid": "8999000", "ask": "9001000", "high": "9200000", "low": "8800000", "volume": "100"}],
    })
    order_resp = _mock_response({"status": 0, "data": "637000"})

    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    # 1st GET → ticker, then POST → order
    adapter._client.get = AsyncMock(return_value=ticker_resp)
    adapter._client.post = AsyncMock(return_value=order_resp)

    order = await adapter.place_order(OrderType.MARKET_BUY, "btc_jpy", amount=90010.0)

    assert order.order_id == "637000"
    assert order.side == OrderSide.BUY
    assert order.order_type == OrderType.MARKET_BUY

    # 실제 POST body 검증
    call_args = adapter._client.post.call_args
    body = json.loads(call_args.kwargs.get("content", call_args.args[1] if len(call_args.args) > 1 else "{}"))
    assert body["executionType"] == "MARKET"
    assert body["side"] == "BUY"
    assert body["symbol"] == "BTC_JPY"


@pytest.mark.asyncio
async def test_place_order_limit_sell(adapter: GmoCoinAdapter) -> None:
    """SELL 지정가: executionType=LIMIT, side=SELL, price 포함."""
    order_resp = _mock_response({"status": 0, "data": "638000"})

    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.post = AsyncMock(return_value=order_resp)

    order = await adapter.place_order(OrderType.SELL, "btc_jpy", amount=0.01, price=9200000.0)

    assert order.order_id == "638000"
    assert order.side == OrderSide.SELL

    call_args = adapter._client.post.call_args
    body = json.loads(call_args.kwargs.get("content", "{}"))
    assert body["executionType"] == "LIMIT"
    assert body["side"] == "SELL"
    assert body["price"] == "9200000"


@pytest.mark.asyncio
async def test_place_order_market_buy_jpy_to_size(adapter: GmoCoinAdapter) -> None:
    """MARKET_BUY: JPY amount → BTC size, sizeStep(0.001) 단위로 floor 처리."""
    ticker_resp = _mock_response({
        "status": 0,
        "data": [{"symbol": "BTC_JPY", "last": "10000000", "bid": "9999000", "ask": "10001000", "high": "11000000", "low": "9000000", "volume": "50"}],
    })
    order_resp = _mock_response({"status": 0, "data": "999001"})

    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=ticker_resp)
    adapter._client.post = AsyncMock(return_value=order_resp)

    # 90010 JPY / ask=10001000 = 0.0090000999... → sizeStep=0.001 floor → 0.009
    order = await adapter.place_order(OrderType.MARKET_BUY, "btc_jpy", amount=90010.0)

    call_args = adapter._client.post.call_args
    body = json.loads(call_args.kwargs.get("content", "{}"))
    size = float(body["size"])
    assert size == 0.009  # floor to sizeStep=0.001
    # 소수점 3자리 이하 확인
    assert len(body["size"].split(".")[1]) <= 3 if "." in body["size"] else True


@pytest.mark.asyncio
async def test_place_order_market_buy_size_too_small(adapter: GmoCoinAdapter) -> None:
    """MARKET_BUY: size가 sizeStep 미만이면 OrderError."""
    ticker_resp = _mock_response({
        "status": 0,
        "data": [{"symbol": "BTC_JPY", "last": "15000000", "bid": "14999000", "ask": "15001000", "high": "16000000", "low": "14000000", "volume": "10"}],
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=ticker_resp)

    # 100 JPY / 15001000 = 0.0000066... → floor to 0.001 → 0.000 → OrderError
    with pytest.raises(OrderError, match="sizeStep"):
        await adapter.place_order(OrderType.MARKET_BUY, "btc_jpy", amount=100.0)


def test_floor_to_step_btc() -> None:
    """_floor_to_step: BTC_JPY sizeStep=0.001 floor 정확성 검증."""
    from adapters.gmo_coin.client import _floor_to_step

    # 소수점 많은 float → 내림
    assert _floor_to_step(0.007142857, 0.001) == 0.007
    assert _floor_to_step(0.009999000, 0.001) == 0.009
    assert _floor_to_step(0.001, 0.001) == 0.001
    assert _floor_to_step(0.0009, 0.001) == 0.0  # 미만
    # sizeStep=1.0 (XRP)
    assert _floor_to_step(9.9, 1.0) == 9.0
    assert _floor_to_step(10.0, 1.0) == 10.0


@pytest.mark.asyncio
async def test_place_order_limit_without_price(adapter: GmoCoinAdapter) -> None:
    """지정가 주문에 price 없으면 OrderError."""
    adapter._client = AsyncMock(spec=httpx.AsyncClient)

    with pytest.raises(OrderError, match="price"):
        await adapter.place_order(OrderType.BUY, "btc_jpy", amount=0.01, price=None)


@pytest.mark.asyncio
async def test_place_order_market_buy_empty_order_id(adapter: GmoCoinAdapter) -> None:
    """orderId 없는 응답 → OrderError."""
    ticker_resp = _mock_response({
        "status": 0,
        "data": [{"symbol": "BTC_JPY", "last": "9000000", "bid": "8999000", "ask": "9001000", "high": "9100000", "low": "8900000", "volume": "10"}],
    })
    order_resp = _mock_response({"status": 0, "data": ""})

    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=ticker_resp)
    adapter._client.post = AsyncMock(return_value=order_resp)

    with pytest.raises(OrderError, match="orderId 없음"):
        await adapter.place_order(OrderType.MARKET_BUY, "btc_jpy", amount=90010.0)


# ─────────────────────────────────────────────────────────────
# close_position
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_position_buy_to_sell(adapter: GmoCoinAdapter) -> None:
    """BUY 건옥 청산 → closeOrder side=SELL."""
    order_resp = _mock_response({"status": 0, "data": "640000"})
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.post = AsyncMock(return_value=order_resp)

    order = await adapter.close_position(position_id=123456, side="BUY", size=0.01, pair="btc_jpy")

    assert order.order_id == "640000"
    assert order.side == OrderSide.SELL

    call_args = adapter._client.post.call_args
    body = json.loads(call_args.kwargs.get("content", "{}"))
    assert body["side"] == "SELL"
    assert body["settlePosition"] == [{"positionId": 123456, "size": "0.01"}]


@pytest.mark.asyncio
async def test_close_position_sell_to_buy(adapter: GmoCoinAdapter) -> None:
    """SELL 건옥 청산 → closeOrder side=BUY."""
    order_resp = _mock_response({"status": 0, "data": "641000"})
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.post = AsyncMock(return_value=order_resp)

    order = await adapter.close_position(position_id=789012, side="SELL", size=0.02, pair="btc_jpy")

    assert order.side == OrderSide.BUY

    call_args = adapter._client.post.call_args
    body = json.loads(call_args.kwargs.get("content", "{}"))
    assert body["side"] == "BUY"
    assert body["settlePosition"][0]["positionId"] == 789012


# ─────────────────────────────────────────────────────────────
# cancel_order
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_order_success(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({"status": 0})
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.post = AsyncMock(return_value=resp)

    result = await adapter.cancel_order("637000")
    assert result is True


@pytest.mark.asyncio
async def test_cancel_order_already_canceled(adapter: GmoCoinAdapter) -> None:
    """ERR-5122 (이미 취소) → True (예외 없음)."""
    resp = _mock_response({
        "status": 1,
        "messages": [{"message_code": "ERR-5122", "message_string": "Already cancelled"}],
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.post = AsyncMock(return_value=resp)

    result = await adapter.cancel_order("637000")
    assert result is True  # 예외 없이 True


@pytest.mark.asyncio
async def test_cancel_order_failure(adapter: GmoCoinAdapter) -> None:
    """기타 에러 응답 → False."""
    resp = _mock_response({
        "status": 1,
        "messages": [{"message_code": "ERR-9999", "message_string": "Unknown"}],
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.post = AsyncMock(return_value=resp)

    result = await adapter.cancel_order("999")
    assert result is False


# ─────────────────────────────────────────────────────────────
# get_open_orders
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_open_orders(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({
        "status": 0,
        "data": {
            "pagination": {"count": 1, "currentPage": 1, "pageCount": 1},
            "list": [
                {
                    "orderId": 637001,
                    "symbol": "BTC_JPY",
                    "side": "BUY",
                    "executionType": "LIMIT",
                    "price": "8500000",
                    "size": "0.01",
                    "status": "ORDERED",
                }
            ],
        },
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    orders = await adapter.get_open_orders("btc_jpy")
    assert len(orders) == 1
    assert orders[0].order_id == "637001"
    assert orders[0].side == OrderSide.BUY
    assert orders[0].status == OrderStatus.OPEN


@pytest.mark.asyncio
async def test_get_open_orders_empty(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({"status": 0, "data": {"list": []}})
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    orders = await adapter.get_open_orders("btc_jpy")
    assert orders == []


# ─────────────────────────────────────────────────────────────
# get_order
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_order_found(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({
        "status": 0,
        "data": {
            "list": [
                {
                    "orderId": 637002,
                    "symbol": "BTC_JPY",
                    "side": "SELL",
                    "executionType": "MARKET",
                    "size": "0.01",
                    "status": "EXECUTED",
                }
            ]
        },
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    order = await adapter.get_order("637002", "btc_jpy")
    assert order is not None
    assert order.order_id == "637002"
    assert order.status == OrderStatus.COMPLETED


@pytest.mark.asyncio
async def test_get_order_not_found(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({"status": 0, "data": {"list": []}})
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    order = await adapter.get_order("999999", "btc_jpy")
    assert order is None


# ─────────────────────────────────────────────────────────────
# parsers.parse_order — 레버리지 전용 필드
# ─────────────────────────────────────────────────────────────

def test_parse_order_leverage_fields_in_raw() -> None:
    """settleType, losscutPrice 필드가 raw에 보존됨."""
    raw = {
        "orderId": 123456,
        "symbol": "BTC_JPY",
        "side": "SELL",
        "executionType": "MARKET",
        "size": "0.01",
        "status": "ORDERED",
        "settleType": "OPEN",
        "losscutPrice": "8000000",
    }
    order = parsers.parse_order(raw, "btc_jpy")
    assert order.raw is not None
    assert order.raw.get("settleType") == "OPEN"
    assert order.raw.get("losscutPrice") == "8000000"


def test_parse_order_market_buy_status_mapping() -> None:
    """WAITING → PENDING, EXECUTED → COMPLETED."""
    raw_waiting = {"orderId": 1, "symbol": "BTC_JPY", "side": "BUY", "executionType": "MARKET", "size": "0.01", "status": "WAITING"}
    raw_executed = {"orderId": 2, "symbol": "BTC_JPY", "side": "BUY", "executionType": "MARKET", "size": "0.01", "status": "EXECUTED"}

    assert parsers.parse_order(raw_waiting).status == OrderStatus.PENDING
    assert parsers.parse_order(raw_executed).status == OrderStatus.COMPLETED


def test_parse_order_limit_sell() -> None:
    """SELL 지정가: type=SELL, price 파싱."""
    raw = {
        "orderId": 637002,
        "symbol": "BTC_JPY",
        "side": "SELL",
        "executionType": "LIMIT",
        "price": "9200000",
        "size": "0.05",
        "status": "ORDERED",
    }
    order = parsers.parse_order(raw, "btc_jpy")
    assert order.order_type == OrderType.SELL
    assert order.price == 9200000.0
    assert order.amount == 0.05


def test_parse_order_ws_order_status_field() -> None:
    """WS 이벤트: orderStatus 필드 (REST: status 필드 대신)."""
    raw = {
        "orderId": 999,
        "symbol": "BTC_JPY",
        "side": "BUY",
        "executionType": "MARKET",
        "size": "0.01",
        "orderStatus": "EXECUTED",   # WS 필드
    }
    order = parsers.parse_order(raw, "btc_jpy")
    assert order.status == OrderStatus.COMPLETED


def test_parse_order_order_id_as_int() -> None:
    """orderId가 int → str 변환."""
    raw = {
        "orderId": 637000,   # int
        "symbol": "BTC_JPY",
        "side": "BUY",
        "executionType": "MARKET",
        "size": "0.01",
        "status": "ORDERED",
    }
    order = parsers.parse_order(raw, "btc_jpy")
    assert order.order_id == "637000"
    assert isinstance(order.order_id, str)


def test_parse_order_expired_is_cancelled() -> None:
    """EXPIRED → CANCELLED."""
    raw = {
        "orderId": 12345,
        "symbol": "BTC_JPY",
        "side": "SELL",
        "executionType": "LIMIT",
        "size": "0.01",
        "status": "EXPIRED",
    }
    order = parsers.parse_order(raw)
    assert order.status == OrderStatus.CANCELLED


def test_parse_order_symbol_to_pair() -> None:
    """symbol 필드로 pair 자동 추론 (pair 인자 미전달)."""
    raw = {
        "orderId": 99,
        "symbol": "ETH_JPY",
        "side": "BUY",
        "executionType": "LIMIT",
        "size": "0.1",
        "status": "ORDERED",
    }
    order = parsers.parse_order(raw)
    assert order.pair == "eth_jpy"


# ─────────────────────────────────────────────────────────────
# 엣지 케이스 보강
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_ticker_symbol_returns_btc(adapter: GmoCoinAdapter) -> None:
    """
    API가 "BTC" (현물 심볼)로 응답 시 pair="btc_jpy" 그대로 유지되는지.
    symbol 검색 조건에서 split("_")[0] == "BTC"로 매칭해야 한다.
    """
    resp = _mock_response({
        "status": 0,
        "data": [
            {
                "symbol": "BTC",   # ← 현물 심볼
                "last": "9000000",
                "bid": "8999000",
                "ask": "9001000",
                "high": "9200000",
                "low": "8800000",
                "volume": "50",
            }
        ],
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    ticker = await adapter.get_ticker("btc_jpy")
    # pair는 요청한 pair 그대로
    assert ticker.pair == "btc_jpy"
    assert ticker.last == 9000000.0
    assert ticker.ask == 9001000.0


@pytest.mark.asyncio
async def test_get_ticker_data_as_dict(adapter: GmoCoinAdapter) -> None:
    """data가 list가 아닌 dict 단건으로 반환되는 경우."""
    resp = _mock_response({
        "status": 0,
        "data": {
            "symbol": "BTC_JPY",
            "last": "8500000",
            "bid": "8499000",
            "ask": "8501000",
            "high": "8600000",
            "low": "8400000",
            "volume": "20",
        },
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    ticker = await adapter.get_ticker("btc_jpy")
    assert ticker.last == 8500000.0


@pytest.mark.asyncio
async def test_connect_idempotent(adapter: GmoCoinAdapter) -> None:
    """connect() 중복 호출 시 기존 클라이언트 유지 (멱등성)."""
    first_client = adapter._client
    await adapter.connect()  # 두 번째 호출
    # 동일 클라이언트 인스턴스여야 함
    assert adapter._client is first_client


@pytest.mark.asyncio
async def test_get_positions_invalid_timestamp(adapter: GmoCoinAdapter) -> None:
    """잘못된 timestamp 형식 → open_date=None (예외 없음)."""
    resp = _mock_response({
        "status": 0,
        "data": {
            "list": [
                {
                    "positionId": 555,
                    "symbol": "BTC_JPY",
                    "side": "BUY",
                    "size": "0.01",
                    "price": "9000000",
                    "lossGain": "0",
                    "leverage": "2",
                    "timestamp": "INVALID_TS",   # ← 파싱 불가
                }
            ]
        },
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    positions = await adapter.get_positions("BTC_JPY")
    assert len(positions) == 1
    assert positions[0].open_date is None  # 예외 아닌 None


@pytest.mark.asyncio
async def test_place_order_market_sell_uses_amount_directly(adapter: GmoCoinAdapter) -> None:
    """MARKET_SELL은 ticker 조회 없이 amount(BTC 수량) 그대로 사용."""
    order_resp = _mock_response({"status": 0, "data": "650000"})

    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.post = AsyncMock(return_value=order_resp)
    # GET은 호출되지 않아야 함

    order = await adapter.place_order(OrderType.MARKET_SELL, "btc_jpy", amount=0.01)

    assert order.order_id == "650000"
    assert order.side == OrderSide.SELL
    adapter._client.get.assert_not_called()  # ticker GET 호출 없음

    call_args = adapter._client.post.call_args
    body = json.loads(call_args.kwargs.get("content", "{}"))
    assert body["side"] == "SELL"
    assert body["executionType"] == "MARKET"
    assert float(body["size"]) == 0.01


@pytest.mark.asyncio
async def test_change_losscut_price_success(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({"status": 0})
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.post = AsyncMock(return_value=resp)

    result = await adapter.change_losscut_price(position_id=123456, price=8000000.0)
    assert result is True

    call_args = adapter._client.post.call_args
    body = json.loads(call_args.kwargs.get("content", "{}"))
    assert body["positionId"] == 123456
    assert body["losscutPrice"] == "8000000"


@pytest.mark.asyncio
async def test_change_losscut_price_failure(adapter: GmoCoinAdapter) -> None:
    """status != 0 (ERR-9000 등 일반 오류) → False (예외 없음, 로그 경고)."""
    resp = _mock_response({
        "status": 1,
        "messages": [{"message_code": "ERR-9000", "message_string": "Some error"}],
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.post = AsyncMock(return_value=resp)

    result = await adapter.change_losscut_price(position_id=123456, price=8000000.0)
    assert result is False


@pytest.mark.asyncio
async def test_change_losscut_price_err578(adapter: GmoCoinAdapter) -> None:
    """ERR-578 (trailing race condition) → False, 예외 없음."""
    resp = _mock_response({
        "status": 1,
        "messages": [{"message_code": "ERR-578", "message_string": "Specify losscutprice greater than 14929028."}],
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.post = AsyncMock(return_value=resp)

    result = await adapter.change_losscut_price(position_id=283501561, price=12098106.0)
    assert result is False  # False 반환, 예외 없음


@pytest.mark.asyncio
async def test_get_balance_empty(adapter: GmoCoinAdapter) -> None:
    """빈 배열 → Balance(currencies={})."""
    resp = _mock_response({"status": 0, "data": []})
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    balance = await adapter.get_balance()
    assert balance.currencies == {}


@pytest.mark.asyncio
async def test_get_executions(adapter: GmoCoinAdapter) -> None:
    """약정 정보 조회 → list[dict]."""
    resp = _mock_response({
        "status": 0,
        "data": {
            "list": [
                {
                    "executionId": 1111,
                    "orderId": 637000,
                    "positionId": 123456,
                    "symbol": "BTC_JPY",
                    "side": "BUY",
                    "executionPrice": "9000000",
                    "executionSize": "0.01",
                }
            ]
        },
    })
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    execs = await adapter.get_executions("637000")
    assert len(execs) == 1
    assert execs[0]["positionId"] == 123456
    assert execs[0]["executionPrice"] == "9000000"


@pytest.mark.asyncio
async def test_get_executions_empty(adapter: GmoCoinAdapter) -> None:
    resp = _mock_response({"status": 0, "data": {"list": []}})
    adapter._client = AsyncMock(spec=httpx.AsyncClient)
    adapter._client.get = AsyncMock(return_value=resp)

    execs = await adapter.get_executions("999999")
    assert execs == []
