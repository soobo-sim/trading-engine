"""
GmoCoinTrendManager 단위 테스트.

테스트:
  GT-01: _supports_short == True (CdfMgr 상속)
  GT-02: MARKET_BUY → JPY 금액 전달 (롱 진입)
  GT-03: MARKET_SELL → coin_size 전달 (숏 진입)
  GT-04: 롱 청산 → close_position_bulk(side="buy")
  GT-05: 숏 청산 → close_position_bulk(side="sell")
  GT-06: dust 포지션 → close_position_bulk 미호출, 인메모리 해제
  GT-07: invest_jpy < min_jpy → place_order 미호출
  GT-08: 여유 증거금 없음 → place_order 미호출
  GT-09: 롱 진입 SL = exec_price - atr * mult
  GT-10: 숏 진입 SL = exec_price + atr * mult (반전)
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.exchange.types import (
    Collateral,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Ticker,
)


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _make_order(order_type: OrderType = OrderType.MARKET_BUY, amount: float = 0.001, price: float = 11_450_000.0) -> Order:
    side = OrderSide.BUY if order_type in (OrderType.BUY, OrderType.MARKET_BUY) else OrderSide.SELL
    return Order(
        order_id="test_order_001",
        pair="btc_jpy",
        order_type=order_type,
        side=side,
        price=price,
        amount=amount,
        status=OrderStatus.COMPLETED,
        created_at=datetime.now(timezone.utc),
    )


def _make_gmoc_manager(
    *,
    collateral: float = 100_000.0,
    require_collateral: float = 0.0,
    ticker_ask: float = 11_500_000.0,
    ticker_last: float = 11_450_000.0,
    place_order_return: Order | None = None,
    close_bulk_return: Order | None = None,
):
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager

    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.is_margin_trading = True

    adapter.get_collateral = AsyncMock(return_value=Collateral(
        collateral=collateral,
        open_position_pnl=0.0,
        require_collateral=require_collateral,
        keep_rate=999.0 if require_collateral == 0 else collateral / require_collateral,
    ))
    adapter.get_ticker = AsyncMock(return_value=Ticker(
        pair="btc_jpy",
        last=ticker_last,
        bid=11_400_000.0,
        ask=ticker_ask,
        high=12_000_000.0,
        low=11_000_000.0,
        volume=100.0,
    ))
    adapter.place_order = AsyncMock(
        return_value=place_order_return or _make_order(OrderType.MARKET_BUY, amount=0.008695, price=ticker_last)
    )
    adapter.close_position_bulk = AsyncMock(
        return_value=close_bulk_return or _make_order(OrderType.MARKET_SELL, amount=0.001, price=ticker_last)
    )
    adapter.get_positions = AsyncMock(return_value=[])

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
    # DB 기록 비활성화
    mgr._record_open = AsyncMock(return_value=42)
    mgr._record_close = AsyncMock()
    return mgr


_BASE_PARAMS = {
    "position_size_pct": 100,
    "min_order_jpy": 500,
    "atr_multiplier_stop": 2.0,
    "max_slippage_pct": 5.0,
    "min_coin_size": 0.0001,
    "max_leverage": 10.0,
}

# ──────────────────────────────────────────────────────────────
# GT-01: _supports_short == True
# ──────────────────────────────────────────────────────────────

def test_supports_short_is_true():
    """GT-01: GmoCoinTrendManager._supports_short == True (CdfMgr 상속)."""
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager
    assert GmoCoinTrendManager._supports_short is True


# ──────────────────────────────────────────────────────────────
# GT-02: 롱 진입 → MARKET_BUY + JPY 금액 전달
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_position_buy_passes_jpy():
    """GT-02: side='buy' → place_order(MARKET_BUY, amount≈JPY 정수)."""
    mgr = _make_gmoc_manager(collateral=100_000)
    params = dict(_BASE_PARAMS)

    await mgr._open_position("BTC_JPY", "buy", 11_500_000.0, 100_000.0, params)

    mgr._adapter.place_order.assert_called_once()
    call_kwargs = mgr._adapter.place_order.call_args
    assert call_kwargs.kwargs["order_type"] == OrderType.MARKET_BUY
    # amount는 JPY 금액 (100000 * 100% = 100000)
    assert call_kwargs.kwargs["amount"] == pytest.approx(100_000.0, abs=1.0)
    # BTC 수량을 전달하지 않음 (소수 8자리 아님)
    assert call_kwargs.kwargs["amount"] >= 1000  # JPY 금액은 크다


# ──────────────────────────────────────────────────────────────
# GT-03: 숏 진입 → MARKET_SELL + coin_size 전달
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_position_sell_passes_coin_size():
    """GT-03: side='sell' → place_order(MARKET_SELL, amount=coin_size≈invest_jpy/price)."""
    collateral = 100_000.0
    price = 11_500_000.0
    mgr = _make_gmoc_manager(collateral=collateral)
    params = dict(_BASE_PARAMS)

    await mgr._open_position("BTC_JPY", "sell", price, 100_000.0, params)

    call_kwargs = mgr._adapter.place_order.call_args
    assert call_kwargs.kwargs["order_type"] == OrderType.MARKET_SELL
    # BUG-031: 재ticker 후 bid=11_400_000 으로 price 갱신됨
    refreshed_price = 11_400_000.0
    invest_jpy = collateral * float(params.get("position_size_pct", 100.0)) / 100.0
    expected_coin = round(invest_jpy / refreshed_price, 8)
    assert call_kwargs.kwargs["amount"] == pytest.approx(expected_coin, rel=1e-4)
    # coin_size는 작은 소수
    assert call_kwargs.kwargs["amount"] < 1.0


# ──────────────────────────────────────────────────────────────
# GT-04: 롱 청산 → close_position_bulk(side="buy")
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_position_buy_calls_bulk_with_buy():
    """GT-04: 롱 포지션 청산 → close_position_bulk(side='buy', size=entry_amount)."""
    mgr = _make_gmoc_manager()
    mgr._position["BTC_JPY"] = Position(
        pair="BTC_JPY",
        entry_price=11_000_000.0,
        entry_amount=0.001,
        stop_loss_price=10_800_000.0,
        db_record_id=42,
        extra={"side": "buy"},
    )
    mgr._params["BTC_JPY"] = {"min_coin_size": 0.0001}

    await mgr._close_position_impl("BTC_JPY", "stop_loss")

    mgr._adapter.close_position_bulk.assert_called_once_with(
        symbol="BTC_JPY", side="buy", size=0.001
    )
    assert mgr._position.get("BTC_JPY") is None


# ──────────────────────────────────────────────────────────────
# GT-05: 숏 청산 → close_position_bulk(side="sell")
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_position_sell_calls_bulk_with_sell():
    """GT-05: 숏 포지션 청산 → close_position_bulk(side='sell', size=entry_amount)."""
    mgr = _make_gmoc_manager()
    mgr._position["BTC_JPY"] = Position(
        pair="BTC_JPY",
        entry_price=12_000_000.0,
        entry_amount=0.001,
        stop_loss_price=12_200_000.0,
        db_record_id=43,
        extra={"side": "sell"},
    )
    mgr._params["BTC_JPY"] = {"min_coin_size": 0.0001}

    await mgr._close_position_impl("BTC_JPY", "stop_loss")

    mgr._adapter.close_position_bulk.assert_called_once_with(
        symbol="BTC_JPY", side="sell", size=0.001
    )
    assert mgr._position.get("BTC_JPY") is None


# ──────────────────────────────────────────────────────────────
# GT-06: dust 포지션 → close_position_bulk 미호출, 인메모리 해제
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_position_dust_skips_bulk():
    """GT-06: dust 포지션(size < min_coin_size) → close_position_bulk 미호출."""
    mgr = _make_gmoc_manager()
    mgr._position["BTC_JPY"] = Position(
        pair="BTC_JPY",
        entry_price=11_000_000.0,
        entry_amount=0.00001,  # dust: < 0.001
        stop_loss_price=None,
        db_record_id=44,
        extra={"side": "buy"},
    )
    mgr._params["BTC_JPY"] = {"min_coin_size": 0.001}

    await mgr._close_position_impl("BTC_JPY", "stop_loss")

    mgr._adapter.close_position_bulk.assert_not_called()
    assert mgr._position.get("BTC_JPY") is None  # 인메모리 해제됨


# ──────────────────────────────────────────────────────────────
# GT-07: invest_jpy < min_jpy → place_order 미호출
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_position_skips_when_invest_below_min():
    """GT-07: invest_jpy < min_jpy → 주문 발주 없음."""
    mgr = _make_gmoc_manager(collateral=100.0)  # collateral 작음 → invest_jpy < min_jpy
    params = {**_BASE_PARAMS, "position_size_pct": 1, "min_order_jpy": 500}  # invest=1 < 500

    await mgr._open_position("BTC_JPY", "buy", 11_500_000.0, None, params)

    mgr._adapter.place_order.assert_not_called()


# ──────────────────────────────────────────────────────────────
# GT-08: 여유 증거금 없음 → place_order 미호출
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_position_skips_when_no_available_collateral():
    """GT-08: available_collateral <= 0 → 주문 발주 없음."""
    mgr = _make_gmoc_manager(collateral=50_000.0, require_collateral=50_000.0)

    await mgr._open_position("BTC_JPY", "buy", 11_500_000.0, None, _BASE_PARAMS)

    mgr._adapter.place_order.assert_not_called()


# ──────────────────────────────────────────────────────────────
# GT-09: 롱 SL = exec_price - atr * mult
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_position_buy_stop_loss_below_entry():
    """GT-09: 롱 진입 SL = exec_price - atr * atr_multiplier_stop."""
    exec_price = 11_450_000.0
    atr = 100_000.0
    mult = 2.0
    expected_sl = round(exec_price - atr * mult, 6)

    mgr = _make_gmoc_manager(
        collateral=100_000,
        ticker_last=exec_price,
        place_order_return=_make_order(OrderType.MARKET_BUY, amount=0.001, price=exec_price),
    )
    params = {**_BASE_PARAMS, "atr_multiplier_stop": mult}

    await mgr._open_position("BTC_JPY", "buy", exec_price, atr, params)

    pos = mgr._position.get("BTC_JPY")
    assert pos is not None
    assert pos.stop_loss_price == expected_sl
    assert pos.stop_loss_price < exec_price  # SL은 진입가 아래


# ──────────────────────────────────────────────────────────────
# GT-10: 숏 SL = exec_price + atr * mult (반전)
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_position_sell_stop_loss_above_entry():
    """GT-10: 숏 진입 SL = exec_price + atr * atr_multiplier_stop (롱과 반전)."""
    exec_price = 11_450_000.0
    atr = 100_000.0
    mult = 2.0
    expected_sl = round(exec_price + atr * mult, 6)

    mgr = _make_gmoc_manager(
        collateral=100_000,
        ticker_last=exec_price,
        place_order_return=_make_order(OrderType.MARKET_SELL, amount=0.001, price=exec_price),
    )
    params = {**_BASE_PARAMS, "atr_multiplier_stop": mult}

    await mgr._open_position("BTC_JPY", "sell", exec_price, atr, params)

    pos = mgr._position.get("BTC_JPY")
    assert pos is not None
    assert pos.stop_loss_price == expected_sl
    assert pos.stop_loss_price > exec_price  # SL은 진입가 위
