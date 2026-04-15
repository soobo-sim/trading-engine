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


# ──────────────────────────────────────────────────────────────
# 피라미딩 (_add_to_position) 테스트
# ──────────────────────────────────────────────────────────────

def _inject_long_position(mgr, entry_price: float = 10_000_000.0, amount: float = 0.3,
                           stop_loss: float = 9_600_000.0, pyramid_count: int = 0,
                           total_size_pct: float = 0.20) -> None:
    """매니저에 롱 포지션을 인메모리 주입하는 헬퍼."""
    pos = Position(
        pair="btc_jpy",
        entry_price=entry_price,
        entry_amount=amount,
        stop_loss_price=stop_loss,
        extra={
            "side": "buy",
            "pyramid_count": pyramid_count,
            "pyramid_entries": [],
            "total_size_pct": total_size_pct,
        },
    )
    pos.db_record_id = 42
    mgr._position["btc_jpy"] = pos


def _inject_short_position(mgr, entry_price: float = 10_500_000.0, amount: float = 0.3,
                            stop_loss: float = 10_900_000.0, pyramid_count: int = 0) -> None:
    """매니저에 숏 포지션을 인메모리 주입하는 헬퍼."""
    pos = Position(
        pair="btc_jpy",
        entry_price=entry_price,
        entry_amount=amount,
        stop_loss_price=stop_loss,
        extra={
            "side": "sell",
            "pyramid_count": pyramid_count,
            "pyramid_entries": [],
            "total_size_pct": 0.20,
        },
    )
    pos.db_record_id = 43
    mgr._position["btc_jpy"] = pos


@pytest.mark.asyncio
async def test_add_to_position_buy_weighted_avg():
    """
    GT-11: 롱 포지션 피라미딩 — 가중평균가 계산 검증.

    Given: 기존 entry=10,000,000 amount=0.3, 추가 exec_price=11,000,000 amount=0.3
    expected avg = (10M*0.3 + 11M*0.3)/(0.3+0.3) = 10,500,000
    """
    exec_price = 11_000_000.0
    add_amount = 0.3
    mgr = _make_gmoc_manager(
        collateral=300_000.0,
        place_order_return=_make_order(OrderType.MARKET_BUY, amount=add_amount, price=exec_price),
    )
    mgr._update_position_in_db = AsyncMock()
    _inject_long_position(mgr, entry_price=10_000_000.0, amount=0.3)

    params = {**_BASE_PARAMS, "position_size_pct": 20.0, "atr_multiplier_stop": 2.0}
    await mgr._add_to_position("btc_jpy", "buy", exec_price, 100_000.0, params)

    pos = mgr._position.get("btc_jpy")
    assert pos is not None
    assert pos.entry_price == pytest.approx(10_500_000.0, abs=1.0)
    assert pos.entry_amount == pytest.approx(0.6, abs=0.001)
    assert pos.extra["pyramid_count"] == 1


@pytest.mark.asyncio
async def test_add_to_position_short_sl_not_worsen():
    """
    GT-12: 숏 포지션 피라미딩 — SL not-worsen (숏은 SL이 낮을수록 유리).

    Given: 기존 SL=10,900,000 (숏 SL은 위), 새 계산 SL > 기존 → not-worsen으로 유지 (min)
    """
    entry_price = 10_500_000.0
    add_exec_price = 10_200_000.0
    atr = 200_000.0
    atr_mult = 2.0
    new_avg = (10_500_000.0 * 0.3 + 10_200_000.0 * 0.3) / 0.6  # 10,350,000
    new_sl_candidate = round(new_avg + atr * atr_mult, 6)  # 10,350,000 + 400,000 = 10,750,000

    mgr = _make_gmoc_manager(
        collateral=300_000.0,
        place_order_return=_make_order(OrderType.MARKET_SELL, amount=0.3, price=add_exec_price),
    )
    mgr._update_position_in_db = AsyncMock()
    _inject_short_position(mgr, entry_price=entry_price, amount=0.3, stop_loss=10_900_000.0)

    params = {**_BASE_PARAMS, "position_size_pct": 20.0, "atr_multiplier_stop": atr_mult}
    await mgr._add_to_position("btc_jpy", "sell", add_exec_price, atr, params)

    pos = mgr._position.get("btc_jpy")
    assert pos is not None
    # SL not-worsen: min(기존10,900,000, 새후보10,750,000) = 10,750,000 (낮을수록 유리)
    assert pos.stop_loss_price == pytest.approx(new_sl_candidate, abs=1.0)
    assert pos.stop_loss_price <= 10_900_000.0


@pytest.mark.asyncio
async def test_add_to_position_skips_when_no_position():
    """
    GT-13: 포지션 없을 때 add_to_position 호출 → place_order 미호출, WARNING.
    """
    mgr = _make_gmoc_manager()
    # 포지션 주입 안 함

    params = {**_BASE_PARAMS, "position_size_pct": 20.0}
    await mgr._add_to_position("btc_jpy", "buy", 10_000_000.0, None, params)

    mgr._adapter.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_add_to_position_uses_decision_size_pct():
    """
    GT-14: result.decision.size_pct가 있으면 params.position_size_pct 대신 사용.

    decision.size_pct=0.15 (15%) → 기본 position_size_pct(20%)가 아닌 15% 계산.
    """
    exec_price = 11_000_000.0
    add_amount = 0.2
    mgr = _make_gmoc_manager(
        collateral=300_000.0,
        place_order_return=_make_order(OrderType.MARKET_BUY, amount=add_amount, price=exec_price),
    )
    mgr._update_position_in_db = AsyncMock()
    _inject_long_position(mgr, entry_price=10_000_000.0, amount=0.3)

    # Decision with size_pct=0.15
    from types import SimpleNamespace
    decision = SimpleNamespace(size_pct=0.15)
    result = SimpleNamespace(decision=decision, judgment_id=None)

    params = {**_BASE_PARAMS, "position_size_pct": 20.0}  # 기본 20%
    await mgr._add_to_position("btc_jpy", "buy", exec_price, 100_000.0, params, result=result)

    # place_order가 호출되었는지만 확인(실제 invest_jpy는 available*15%)
    mgr._adapter.place_order.assert_called_once()
    call_kwargs = mgr._adapter.place_order.call_args.kwargs
    # MARKET_BUY: amount = JPY 금액 (300,000 * 0.15 = 45,000)
    assert call_kwargs.get("order_type") == OrderType.MARKET_BUY
    invest_jpy = call_kwargs.get("amount")
    assert invest_jpy == pytest.approx(45_000.0, abs=100.0)


@pytest.mark.asyncio
async def test_add_to_position_skips_when_no_collateral():
    """
    GT-15: 여유 증거금 없음 (available=0) → 피라미딩 스킵.
    """
    mgr = _make_gmoc_manager(collateral=100_000.0, require_collateral=100_000.0)
    mgr._update_position_in_db = AsyncMock()
    _inject_long_position(mgr)

    params = {**_BASE_PARAMS, "position_size_pct": 20.0}
    await mgr._add_to_position("btc_jpy", "buy", 10_000_000.0, None, params)

    mgr._adapter.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_update_position_in_db_skips_when_no_record_id():
    """
    GT-16: db_record_id=None → DB 업데이트 스킵, 크래시 없음.
    """
    mgr = _make_gmoc_manager()
    mgr._position_model = MagicMock()
    mgr._session_factory = MagicMock()

    # db_record_id=None → WARNING 로그만, 예외 없음
    await mgr._update_position_in_db(
        product_code="btc_jpy",
        db_record_id=None,
        entry_price=10_000_000.0,
        size=0.6,
        stop_loss_price=9_500_000.0,
        pyramid_count=1,
    )
    # session_factory가 호출되지 않아야 함 (early return)
    mgr._session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_open_position_initializes_pyramid_state():
    """
    GT-17: _open_position 완료 후 Position.extra에 pyramid 초기화값 존재.
    """
    exec_price = 11_000_000.0
    mgr = _make_gmoc_manager(
        collateral=100_000.0,
        ticker_last=exec_price,
        place_order_return=_make_order(OrderType.MARKET_BUY, amount=0.005, price=exec_price),
    )
    params = {**_BASE_PARAMS, "position_size_pct": 50.0}
    await mgr._open_position("btc_jpy", "buy", exec_price, 100_000.0, params)

    pos = mgr._position.get("btc_jpy")
    assert pos is not None
    assert pos.extra.get("pyramid_count") == 0
    assert pos.extra.get("pyramid_entries") == []
    assert "total_size_pct" in pos.extra
