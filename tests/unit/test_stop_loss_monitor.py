"""
스탑로스 모니터 — subscribe_trades 블로킹 버그 회귀 방지 테스트.

BUG-036: subscribe_trades()가 내부 while-True 루프를 가진 블로킹 코루틴이어서
         await 시 제어가 반환되지 않음 → SL 체크 코드 도달 불가.
         수정: asyncio.create_task()로 백그라운드 실행.

테스트:
  SL-01: subscribe_trades 이후 큐 소비 루프가 실행된다
  SL-02: 가격이 SL 이하 → 청산 호출
  SL-03: 가격이 SL 위 → 청산 미호출
  SL-04: CancelledError 시 ws_task도 cancel
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.exchange.types import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)


def _make_base_manager():
    """BaseTrendManager 구현체(GmoCoinTrendManager) 인스턴스 생성."""
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager

    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.is_margin_trading = True

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = GmoCoinTrendManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=MagicMock(),
        candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )
    mgr._record_open = AsyncMock(return_value=42)
    mgr._record_close = AsyncMock()
    return mgr


# ──────────────────────────────────────────────────────────────
# SL-01: subscribe_trades 이후 큐 소비 루프가 실행된다
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sl01_queue_consumer_runs_after_subscribe():
    """subscribe_trades가 블로킹이어도 큐 소비 루프가 실행되어
    _latest_price가 갱신된다."""
    mgr = _make_base_manager()
    pair = "btc_jpy"

    # 포지션 설정 (SL을 가격보다 낮게 → 청산은 안 되지만 큐 소비는 됨)
    mgr._position[pair] = Position(
        pair=pair, entry_price=100_000, entry_amount=0.01,
        stop_loss_price=10_000, extra={"side": "buy"},
    )
    mgr._params[pair] = {}

    prices_fed = []

    async def fake_subscribe_trades(p, callback):
        """콜백에 가격 3개를 넣은 뒤 블로킹."""
        for price in [50_000, 60_000, 70_000]:
            await callback(price, 0.1)
            prices_fed.append(price)
        # 블로킹 시뮬레이션 (이전 버그에서는 여기서 영원히 대기)
        await asyncio.sleep(999)

    mgr._adapter.subscribe_trades = fake_subscribe_trades

    task = asyncio.create_task(mgr._stop_loss_monitor(pair))
    # 콜백 + 큐 소비에 충분한 시간 부여
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # 큐 소비 루프가 실행되어 _latest_price가 갱신되었어야 한다
    assert mgr._latest_price.get(pair) == 70_000


# ──────────────────────────────────────────────────────────────
# SL-02: 가격이 SL 이하 → 청산 호출
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sl02_stop_triggered_calls_close():
    """롱 포지션에서 가격이 SL 이하 → _close_position 호출."""
    mgr = _make_base_manager()
    pair = "btc_jpy"

    mgr._position[pair] = Position(
        pair=pair, entry_price=100_000, entry_amount=0.01,
        stop_loss_price=95_000, extra={"side": "buy"},
    )
    mgr._params[pair] = {}
    mgr._close_position = AsyncMock()

    async def fake_subscribe_trades(p, callback):
        # SL 이하 가격 전달
        await callback(94_000, 0.1)
        await asyncio.sleep(999)

    mgr._adapter.subscribe_trades = fake_subscribe_trades

    task = asyncio.create_task(mgr._stop_loss_monitor(pair))
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    mgr._close_position.assert_awaited_once_with(pair, "stop_loss")


# ──────────────────────────────────────────────────────────────
# SL-03: 가격이 SL 위 → 청산 미호출
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sl03_price_above_sl_no_close():
    """롱 포지션에서 가격이 SL 위 → _close_position 미호출."""
    mgr = _make_base_manager()
    pair = "btc_jpy"

    mgr._position[pair] = Position(
        pair=pair, entry_price=100_000, entry_amount=0.01,
        stop_loss_price=95_000, extra={"side": "buy"},
    )
    mgr._params[pair] = {}
    mgr._close_position = AsyncMock()

    async def fake_subscribe_trades(p, callback):
        # SL 위 가격 전달
        await callback(96_000, 0.1)
        await asyncio.sleep(999)

    mgr._adapter.subscribe_trades = fake_subscribe_trades

    task = asyncio.create_task(mgr._stop_loss_monitor(pair))
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    mgr._close_position.assert_not_awaited()


# ──────────────────────────────────────────────────────────────
# SL-04: CancelledError 시 ws_task도 cancel
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sl04_cancel_propagates_to_ws_task():
    """스탑로스 모니터 취소 시 WS 태스크도 취소된다."""
    mgr = _make_base_manager()
    pair = "btc_jpy"

    mgr._position[pair] = Position(
        pair=pair, entry_price=100_000, entry_amount=0.01,
        stop_loss_price=None, extra={"side": "buy"},
    )
    mgr._params[pair] = {}

    ws_cancelled = False

    async def fake_subscribe_trades(p, callback):
        nonlocal ws_cancelled
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            ws_cancelled = True
            raise

    mgr._adapter.subscribe_trades = fake_subscribe_trades

    task = asyncio.create_task(mgr._stop_loss_monitor(pair))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ws_cancelled is True
