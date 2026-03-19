"""
pytest 공통 fixtures.
"""

import asyncio
from typing import Generator

import pytest
import pytest_asyncio

from tests.fake_exchange import FakeExchangeAdapter


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """세션 단위 이벤트 루프 (pytest-asyncio 호환)."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def fake_exchange() -> FakeExchangeAdapter:
    """기본 FakeExchangeAdapter 인스턴스."""
    adapter = FakeExchangeAdapter(
        initial_balances={"jpy": 1_000_000.0, "xrp": 0.0, "btc": 0.0},
        ticker_price=100.0,
    )
    await adapter.connect()
    yield adapter
    await adapter.close()
