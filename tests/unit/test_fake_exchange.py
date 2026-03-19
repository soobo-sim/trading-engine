"""FakeExchangeAdapter — Protocol 준수 + 기본 동작 테스트."""

import pytest
import pytest_asyncio

from core.exchange.base import ExchangeAdapter
from core.exchange.types import OrderStatus, OrderType
from tests.fake_exchange import FakeExchangeAdapter


class TestFakeExchangeProtocol:
    def test_protocol_conformance(self):
        """FakeExchangeAdapter가 ExchangeAdapter Protocol을 만족하는지 확인."""
        adapter = FakeExchangeAdapter()
        assert isinstance(adapter, ExchangeAdapter)


class TestFakeExchangeOrders:
    @pytest.mark.asyncio
    async def test_place_market_buy(self, fake_exchange: FakeExchangeAdapter):
        # MARKET_BUY: amount = JPY 투자금. 10000 JPY / 100 price = 100 coins
        order = await fake_exchange.place_order(
            OrderType.MARKET_BUY, "xrp_jpy", amount=10_000.0,
        )
        assert order.status == OrderStatus.COMPLETED
        assert order.order_id.startswith("FAKE-")
        assert order.amount == 100.0  # 10000 JPY / 100 price

        balance = await fake_exchange.get_balance()
        assert balance.get_amount("xrp") == 100.0
        assert balance.get_amount("jpy") == 990_000.0

    @pytest.mark.asyncio
    async def test_place_market_sell(self, fake_exchange: FakeExchangeAdapter):
        # 먼저 매수 (1000 JPY → 10 coins)
        await fake_exchange.place_order(OrderType.MARKET_BUY, "xrp_jpy", amount=1_000.0)
        # 매도 5 coins
        order = await fake_exchange.place_order(OrderType.MARKET_SELL, "xrp_jpy", amount=5.0)
        assert order.status == OrderStatus.COMPLETED

        balance = await fake_exchange.get_balance()
        assert balance.get_amount("xrp") == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_cancel_order(self, fake_exchange: FakeExchangeAdapter):
        order = await fake_exchange.place_order(OrderType.MARKET_BUY, "xrp_jpy", amount=1.0)
        result = await fake_exchange.cancel_order(order.order_id)
        assert result is True

        cancelled = await fake_exchange.get_order(order.order_id)
        assert cancelled is not None
        assert cancelled.status == OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self, fake_exchange: FakeExchangeAdapter):
        result = await fake_exchange.cancel_order("DOES-NOT-EXIST")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_order(self, fake_exchange: FakeExchangeAdapter):
        order = await fake_exchange.place_order(OrderType.MARKET_BUY, "xrp_jpy", amount=1.0)
        fetched = await fake_exchange.get_order(order.order_id)
        assert fetched is not None
        assert fetched.order_id == order.order_id

    @pytest.mark.asyncio
    async def test_get_order_nonexistent(self, fake_exchange: FakeExchangeAdapter):
        result = await fake_exchange.get_order("NOPE")
        assert result is None


class TestFakeExchangeBalance:
    @pytest.mark.asyncio
    async def test_initial_balance(self, fake_exchange: FakeExchangeAdapter):
        balance = await fake_exchange.get_balance()
        assert balance.get_amount("jpy") == 1_000_000.0
        assert balance.get_amount("xrp") == 0.0

    @pytest.mark.asyncio
    async def test_nonexistent_currency(self, fake_exchange: FakeExchangeAdapter):
        balance = await fake_exchange.get_balance()
        cb = balance.get("eth")
        assert cb.amount == 0.0
        assert cb.available == 0.0


class TestFakeExchangeTicker:
    @pytest.mark.asyncio
    async def test_ticker(self, fake_exchange: FakeExchangeAdapter):
        ticker = await fake_exchange.get_ticker("xrp_jpy")
        assert ticker.last == 100.0
        assert ticker.pair == "xrp_jpy"

    @pytest.mark.asyncio
    async def test_set_ticker_price(self, fake_exchange: FakeExchangeAdapter):
        fake_exchange.set_ticker_price(200.0)
        ticker = await fake_exchange.get_ticker("xrp_jpy")
        assert ticker.last == 200.0


class TestFakeExchangeConnection:
    @pytest.mark.asyncio
    async def test_connect_close(self):
        adapter = FakeExchangeAdapter()
        assert not adapter.is_ws_connected()
        await adapter.connect()
        assert adapter.is_ws_connected()
        await adapter.close()
        assert not adapter.is_ws_connected()
