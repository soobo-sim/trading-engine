"""
BitFlyerAdapter 단위 테스트.

httpx mock을 사용하여 실제 API 호출 없이 어댑터 동작을 검증.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from adapters.bitflyer.client import BitFlyerAdapter
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
def adapter() -> BitFlyerAdapter:
    return BitFlyerAdapter(
        api_key="test_key",
        api_secret="test_secret",
        base_url="https://api.bitflyer.com",
    )


def make_mock_response(status_code: int, body) -> MagicMock:
    """httpx.Response 모의 객체 생성."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = json.dumps(body)
    resp.raise_for_status = MagicMock()
    return resp


# ──────────────────────────────────────────────────────────────
# Protocol + 기본 속성
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bitflyer_protocol_compliance(adapter: BitFlyerAdapter) -> None:
    """BitFlyerAdapter 는 ExchangeAdapter Protocol을 충족한다."""
    assert isinstance(adapter, ExchangeAdapter)


@pytest.mark.asyncio
async def test_bitflyer_exchange_name(adapter: BitFlyerAdapter) -> None:
    assert adapter.exchange_name == "bitflyer"


# ──────────────────────────────────────────────────────────────
# pair → product_code 변환
# ──────────────────────────────────────────────────────────────

def test_pair_to_product_code_lowercase_to_upper() -> None:
    """xrp_jpy → XRP_JPY 대문자 변환."""
    assert BitFlyerAdapter._pair_to_product_code("xrp_jpy") == "XRP_JPY"
    assert BitFlyerAdapter._pair_to_product_code("btc_jpy") == "BTC_JPY"
    assert BitFlyerAdapter._pair_to_product_code("eth_jpy") == "ETH_JPY"


# ──────────────────────────────────────────────────────────────
# place_order
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_place_order_limit_buy_returns_order(adapter: BitFlyerAdapter) -> None:
    """지정가 매수 주문 → Order DTO 반환, order_id는 JRF... 문자열."""
    bf_response = {"child_order_acceptance_id": "JRF20150707-050237-639234"}
    mock_resp = make_mock_response(200, bf_response)

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
    assert order.order_id == "JRF20150707-050237-639234"
    assert order.side == OrderSide.BUY
    assert order.pair == "xrp_jpy"
    assert order.amount == 10.0
    assert order.price == 100.0

    # POST 요청에 product_code=XRP_JPY 가 포함됐는지 확인
    call_kwargs = mock_post.call_args
    sent_body = json.loads(call_kwargs.kwargs.get("content", "{}"))
    assert sent_body["product_code"] == "XRP_JPY"
    assert sent_body["side"] == "BUY"
    assert sent_body["child_order_type"] == "LIMIT"
    assert sent_body["size"] == 10.0

    await adapter.close()


@pytest.mark.asyncio
async def test_place_order_market_buy_converts_jpy_to_coin(adapter: BitFlyerAdapter) -> None:
    """시장가 매수: JPY 금액 → ticker 조회 → 코인 수량 변환."""
    ticker_response = {
        "product_code": "XRP_JPY",
        "ltp": 100.0,
        "best_bid": 99.0,
        "best_ask": 101.0,
        "volume": 1000.0,
    }
    bf_response = {"child_order_acceptance_id": "JRF20260317-000001-000001"}
    mock_ticker_resp = make_mock_response(200, ticker_response)
    mock_order_resp = make_mock_response(200, bf_response)

    await adapter.connect()
    with patch.object(adapter._client, "get", new_callable=AsyncMock) as mock_get, \
         patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_get.return_value = mock_ticker_resp
        mock_post.return_value = mock_order_resp
        order = await adapter.place_order(
            order_type=OrderType.MARKET_BUY,
            pair="xrp_jpy",
            amount=10000.0,  # JPY 금액
        )

    # ticker 조회 → 10000 JPY / 100 ltp = 100 XRP
    call_kwargs = mock_post.call_args
    sent_body = json.loads(call_kwargs.kwargs.get("content", "{}"))
    assert sent_body["child_order_type"] == "MARKET"
    assert sent_body["side"] == "BUY"
    assert sent_body["size"] == 100.0  # JPY → coin 변환됨
    assert "price" not in sent_body

    # BUG-016: MARKET_BUY는 코인 수량(actual_amount)을 반환한다.
    assert order.order_type == OrderType.MARKET_BUY
    assert order.amount == 100.0  # 10000 JPY / 100 ltp = 100 XRP
    await adapter.close()


@pytest.mark.asyncio
async def test_place_order_missing_order_id_raises_error(adapter: BitFlyerAdapter) -> None:
    """order_id 없는 응답 → OrderError."""
    bf_response = {}
    mock_resp = make_mock_response(200, bf_response)

    await adapter.connect()
    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        with pytest.raises(OrderError):
            await adapter.place_order(OrderType.BUY, "xrp_jpy", 10.0, price=100.0)
    await adapter.close()


# ──────────────────────────────────────────────────────────────
# cancel_order
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_order_uses_post_not_delete(adapter: BitFlyerAdapter) -> None:
    """BF 주문 취소는 POST 방식 (CK DELETE 아님)."""
    mock_resp = make_mock_response(200, {})

    await adapter.connect()
    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        with patch.object(adapter._client, "delete", new_callable=AsyncMock) as mock_del:
            mock_post.return_value = mock_resp
            result = await adapter.cancel_order("JRF20150707-050237-639234", pair="xrp_jpy")

    assert result is True
    mock_post.assert_called_once()
    mock_del.assert_not_called()
    await adapter.close()


@pytest.mark.asyncio
async def test_cancel_order_not_found_returns_false(adapter: BitFlyerAdapter) -> None:
    """HTTP 404 → False 반환."""
    mock_resp = make_mock_response(404, {"status": -1, "error_message": "Order not found"})

    await adapter.connect()
    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        result = await adapter.cancel_order("JRF-NOTFOUND", pair="xrp_jpy")

    assert result is False
    await adapter.close()


# ──────────────────────────────────────────────────────────────
# get_balance
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_balance_array_to_balance_dto(adapter: BitFlyerAdapter) -> None:
    """BF 잔고: 배열 → Balance DTO 변환, 통화코드 소문자."""
    bf_response = [
        {"currency_code": "JPY", "amount": 1_000_000.0, "available": 950_000.0},
        {"currency_code": "XRP", "amount": 50.5, "available": 50.0},
        {"currency_code": "BTC", "amount": 0.001, "available": 0.001},
    ]
    mock_resp = make_mock_response(200, bf_response)

    await adapter.connect()
    with patch.object(adapter._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        balance = await adapter.get_balance()

    assert isinstance(balance, Balance)

    # JPY → jpy (소문자)
    jpy = balance.get("jpy")
    assert jpy.amount == 1_000_000.0
    assert jpy.available == 950_000.0

    # XRP → xrp (소문자)
    xrp = balance.get("xrp")
    assert xrp.amount == 50.5
    assert xrp.available == 50.0

    await adapter.close()


@pytest.mark.asyncio
async def test_get_balance_currency_code_is_lowercase(adapter: BitFlyerAdapter) -> None:
    """BF "JPY" 대문자 → balance.get("jpy") 소문자로 접근 가능."""
    bf_response = [
        {"currency_code": "JPY", "amount": 500_000.0, "available": 500_000.0},
    ]
    mock_resp = make_mock_response(200, bf_response)

    await adapter.connect()
    with patch.object(adapter._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        balance = await adapter.get_balance()

    # Balance.get() 은 내부적으로 .lower() 정규화하므로 jpy/JPY 둘 다 동일값
    assert balance.get_available("jpy") == 500_000.0
    # 어댑터가 저장 시 소문자로 변환했으므로 내부 키는 소문자
    assert "jpy" in balance.currencies
    assert "JPY" not in balance.currencies
    await adapter.close()


# ──────────────────────────────────────────────────────────────
# get_ticker
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_ticker_returns_ticker_dto(adapter: BitFlyerAdapter) -> None:
    """Ticker DTO 반환. pair 소문자로 유지."""
    bf_response = {
        "product_code": "XRP_JPY",
        "state": "RUNNING",
        "timestamp": "2026-03-17T00:00:00.123",
        "tick_id": 12345,
        "best_bid": 100.0,
        "best_ask": 101.0,
        "best_bid_size": 1000.0,
        "best_ask_size": 500.0,
        "total_bid_depth": 10000.0,
        "total_ask_depth": 8000.0,
        "market_bid_size": 0.0,
        "market_ask_size": 0.0,
        "ltp": 100.5,
        "volume": 234567.89,
        "volume_by_product": 234567.89,
    }
    mock_resp = make_mock_response(200, bf_response)

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
