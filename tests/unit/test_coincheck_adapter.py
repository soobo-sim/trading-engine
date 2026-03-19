"""
CoincheckAdapter 단위 테스트.

httpx mock을 사용하여 실제 API 호출 없이 어댑터 동작을 검증.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from adapters.coincheck.client import CoincheckAdapter
from core.exchange.base import ExchangeAdapter
from core.exchange.errors import AuthenticationError, OrderError
from core.exchange.types import (
    Balance,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Ticker,
)


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def adapter() -> CoincheckAdapter:
    return CoincheckAdapter(
        api_key="test_key",
        api_secret="test_secret",
        base_url="https://coincheck.com",
    )


def make_mock_response(status_code: int, body: dict) -> MagicMock:
    """httpx.Response 모의 객체 생성."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = json.dumps(body)
    resp.raise_for_status = MagicMock()
    return resp


# ──────────────────────────────────────────────────────────────
# connect / Protocol
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_coincheck_protocol_compliance(adapter: CoincheckAdapter) -> None:
    """CoincheckAdapter 는 ExchangeAdapter Protocol을 충족한다."""
    assert isinstance(adapter, ExchangeAdapter)


@pytest.mark.asyncio
async def test_coincheck_exchange_name(adapter: CoincheckAdapter) -> None:
    assert adapter.exchange_name == "coincheck"


@pytest.mark.asyncio
async def test_coincheck_connect_creates_client(adapter: CoincheckAdapter) -> None:
    """connect() 후 _client 가 초기화된다."""
    await adapter.connect()
    assert adapter._client is not None
    await adapter.close()


# ──────────────────────────────────────────────────────────────
# place_order
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_place_order_limit_buy_returns_order(adapter: CoincheckAdapter) -> None:
    """지정가 매수 주문 → Order DTO 반환, order_id는 str."""
    ck_response = {
        "success": True,
        "id": 12345,
        "order_type": "buy",
        "rate": "100",
        "amount": "10.0",
        "pair": "xrp_jpy",
        "created_at": "2026-03-17T00:00:00.000Z",
    }
    mock_resp = make_mock_response(200, ck_response)

    await adapter.connect()
    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        order = await adapter.place_order(
            order_type=OrderType.BUY,
            pair="xrp_jpy",
            amount=10.0,
            price=100.0,
        )

    assert isinstance(order, Order)
    assert order.order_id == "12345"   # int → str 변환 확인
    assert isinstance(order.order_id, str)
    assert order.side == OrderSide.BUY
    assert order.pair == "xrp_jpy"
    assert order.amount == 10.0
    assert order.price == 100.0
    await adapter.close()


@pytest.mark.asyncio
async def test_place_order_market_sell(adapter: CoincheckAdapter) -> None:
    """시장가 매도 주문."""
    ck_response = {
        "success": True,
        "id": 99999,
        "order_type": "market_sell",
        "amount": "5.0",
        "pair": "xrp_jpy",
        "created_at": "2026-03-17T00:00:00.000Z",
    }
    mock_resp = make_mock_response(200, ck_response)

    await adapter.connect()
    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        order = await adapter.place_order(
            order_type=OrderType.MARKET_SELL,
            pair="xrp_jpy",
            amount=5.0,
        )

    assert order.side == OrderSide.SELL
    assert order.order_type == OrderType.MARKET_SELL
    await adapter.close()


@pytest.mark.asyncio
async def test_place_order_failure_raises_order_error(adapter: CoincheckAdapter) -> None:
    """success=False 응답 → OrderError."""
    ck_response = {"success": False, "error": "insufficient balance"}
    mock_resp = make_mock_response(200, ck_response)

    await adapter.connect()
    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        with pytest.raises(OrderError):
            await adapter.place_order(OrderType.BUY, "xrp_jpy", 10.0, price=100.0)
    await adapter.close()


@pytest.mark.asyncio
async def test_place_order_401_raises_auth_error(adapter: CoincheckAdapter) -> None:
    """HTTP 401 → AuthenticationError."""
    mock_resp = make_mock_response(401, {"error": "unauthorized"})

    await adapter.connect()
    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        with pytest.raises(AuthenticationError):
            await adapter.place_order(OrderType.BUY, "xrp_jpy", 10.0, price=100.0)
    await adapter.close()


# ──────────────────────────────────────────────────────────────
# cancel_order
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_order_success(adapter: CoincheckAdapter) -> None:
    """주문 취소 성공 → True 반환."""
    ck_response = {"success": True, "id": 12345}
    mock_resp = make_mock_response(200, ck_response)

    await adapter.connect()
    with patch.object(adapter._client, "delete", new_callable=AsyncMock) as mock_del:
        mock_del.return_value = mock_resp
        result = await adapter.cancel_order("12345")

    assert result is True
    await adapter.close()


@pytest.mark.asyncio
async def test_cancel_order_not_found_returns_false(adapter: CoincheckAdapter) -> None:
    """HTTP 404 → False 반환 (예외 아님)."""
    mock_resp = make_mock_response(404, {"error": "not found"})

    await adapter.connect()
    with patch.object(adapter._client, "delete", new_callable=AsyncMock) as mock_del:
        mock_del.return_value = mock_resp
        result = await adapter.cancel_order("99999")

    assert result is False
    await adapter.close()


# ──────────────────────────────────────────────────────────────
# get_balance
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_balance_flat_dict_to_balance_dto(adapter: CoincheckAdapter) -> None:
    """CK 잔고: flat dict → Balance DTO 변환."""
    ck_response = {
        "success": True,
        "jpy": "1000000",
        "jpy_reserved": "50000",
        "xrp": "50.5",
        "xrp_reserved": "0.5",
        "btc": "0.001",
        "btc_reserved": "0",
    }
    mock_resp = make_mock_response(200, ck_response)

    await adapter.connect()
    with patch.object(adapter._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        balance = await adapter.get_balance()

    assert isinstance(balance, Balance)
    # JPY: 1,000,000 - 50,000 = 950,000 available
    jpy = balance.get("jpy")
    assert jpy.amount == 1_000_000.0
    assert jpy.available == 950_000.0

    # XRP: 50.5 - 0.5 = 50.0 available
    xrp = balance.get("xrp")
    assert xrp.amount == 50.5
    assert xrp.available == 50.0

    await adapter.close()


@pytest.mark.asyncio
async def test_get_balance_lowercase_currency_keys(adapter: CoincheckAdapter) -> None:
    """CK 잔고 통화코드는 이미 소문자 — 그대로 유지."""
    ck_response = {
        "success": True,
        "jpy": "500000",
        "jpy_reserved": "0",
    }
    mock_resp = make_mock_response(200, ck_response)

    await adapter.connect()
    with patch.object(adapter._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        balance = await adapter.get_balance()

    # balance.get("jpy") 로 접근 가능해야 함
    assert balance.get_available("jpy") == 500_000.0
    await adapter.close()


# ──────────────────────────────────────────────────────────────
# get_ticker
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_ticker_returns_ticker_dto(adapter: CoincheckAdapter) -> None:
    """Ticker DTO 반환. pair는 파라미터 그대로."""
    ck_response = {
        "last": 100.5,
        "bid": 100.0,
        "ask": 101.0,
        "high": 102.0,
        "low": 99.0,
        "volume": "123456.78",
        "timestamp": 1700000000,
    }
    mock_resp = make_mock_response(200, ck_response)

    await adapter.connect()
    with patch.object(adapter._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        ticker = await adapter.get_ticker("xrp_jpy")

    assert isinstance(ticker, Ticker)
    assert ticker.pair == "xrp_jpy"
    assert ticker.last == 100.5
    assert ticker.bid == 100.0
    assert ticker.ask == 101.0
    await adapter.close()


# ──────────────────────────────────────────────────────────────
# get_open_orders
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_open_orders_returns_list(adapter: CoincheckAdapter) -> None:
    """미체결 주문 목록 → list[Order]."""
    ck_response = {
        "success": True,
        "orders": [
            {
                "id": 1111,
                "order_type": "buy",
                "rate": "100",
                "amount": "10.0",
                "pair": "xrp_jpy",
                "created_at": "2026-03-17T00:00:00.000Z",
            },
            {
                "id": 2222,
                "order_type": "sell",
                "rate": "110",
                "amount": "5.0",
                "pair": "xrp_jpy",
                "created_at": "2026-03-17T01:00:00.000Z",
            },
        ],
    }
    mock_resp = make_mock_response(200, ck_response)

    await adapter.connect()
    with patch.object(adapter._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        orders = await adapter.get_open_orders("xrp_jpy")

    assert len(orders) == 2
    assert all(isinstance(o, Order) for o in orders)
    assert orders[0].order_id == "1111"
    assert orders[1].order_id == "2222"
    await adapter.close()
