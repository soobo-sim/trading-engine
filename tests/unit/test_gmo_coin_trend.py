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


# ──────────────────────────────────────────────────────────────
# GT-18: ERR-422 (거래소 포지션 없음) → 인메모리 클리어 + DB 기록
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_position_err422_clears_memory():
    """GT-18: ERR-422 시 인메모리 포지션 클리어하고 DB 기록 시도."""
    from core.exchange.errors import ExchangeError
    mgr = _make_gmoc_manager()
    mgr._position["BTC_JPY"] = Position(
        pair="BTC_JPY",
        entry_price=11_000_000.0,
        entry_amount=0.001,
        stop_loss_price=10_800_000.0,
        db_record_id=50,
        extra={"side": "buy"},
    )
    mgr._params["BTC_JPY"] = {"min_coin_size": 0.0001}
    mgr._latest_price["BTC_JPY"] = 11_200_000.0
    mgr._adapter.close_position_bulk = AsyncMock(
        side_effect=ExchangeError(
            "GMO 코인 비즈니스 에러: ERR-422: There are no open positions that can be settled."
        )
    )

    await mgr._close_position_impl("BTC_JPY", "stop_loss")

    assert mgr._position.get("BTC_JPY") is None  # 인메모리 클리어


@pytest.mark.asyncio
async def test_close_position_err422_no_db_record():
    """GT-19: ERR-422 + db_record_id=None → 인메모리만 클리어."""
    from core.exchange.errors import ExchangeError
    mgr = _make_gmoc_manager()
    mgr._position["BTC_JPY"] = Position(
        pair="BTC_JPY",
        entry_price=11_000_000.0,
        entry_amount=0.001,
        stop_loss_price=10_800_000.0,
        db_record_id=None,
        extra={"side": "sell"},
    )
    mgr._params["BTC_JPY"] = {"min_coin_size": 0.0001}
    mgr._adapter.close_position_bulk = AsyncMock(
        side_effect=ExchangeError(
            "GMO 코인 비즈니스 에러: ERR-422: There are no open positions that can be settled."
        )
    )

    await mgr._close_position_impl("BTC_JPY", "exit_warning")

    assert mgr._position.get("BTC_JPY") is None


@pytest.mark.asyncio
async def test_close_position_err422_calls_record_close_with_correct_reason():
    """GT-20: ERR-422 + db_record_id 있음 → _record_close reason에 '_exchange_already_closed' 포함."""
    from core.exchange.errors import ExchangeError
    mgr = _make_gmoc_manager()
    mgr._position["BTC_JPY"] = Position(
        pair="BTC_JPY",
        entry_price=11_000_000.0,
        entry_amount=0.001,
        stop_loss_price=10_800_000.0,
        db_record_id=77,
        extra={"side": "buy"},
    )
    mgr._params["BTC_JPY"] = {"min_coin_size": 0.0001}
    mgr._latest_price["BTC_JPY"] = 11_300_000.0
    mgr._adapter.close_position_bulk = AsyncMock(
        side_effect=ExchangeError(
            "GMO 코인 비즈니스 에러: ERR-422: There are no open positions that can be settled."
        )
    )

    await mgr._close_position_impl("BTC_JPY", "stop_loss")

    assert mgr._position.get("BTC_JPY") is None
    mgr._record_close.assert_called_once()
    call_kwargs = mgr._record_close.call_args[1]
    assert call_kwargs["reason"] == "stop_loss_exchange_already_closed"
    assert call_kwargs["db_record_id"] == 77


@pytest.mark.asyncio
async def test_close_position_err422_record_close_failure_still_clears_memory():
    """GT-21: ERR-422 + _record_close 실패 → 메모리 클리어는 유지 (에러 복구)."""
    from core.exchange.errors import ExchangeError
    mgr = _make_gmoc_manager()
    mgr._position["BTC_JPY"] = Position(
        pair="BTC_JPY",
        entry_price=11_000_000.0,
        entry_amount=0.001,
        stop_loss_price=10_800_000.0,
        db_record_id=88,
        extra={"side": "buy"},
    )
    mgr._params["BTC_JPY"] = {"min_coin_size": 0.0001}
    mgr._adapter.close_position_bulk = AsyncMock(
        side_effect=ExchangeError(
            "GMO 코인 비즈니스 에러: ERR-422: There are no open positions that can be settled."
        )
    )
    mgr._record_close = AsyncMock(side_effect=RuntimeError("DB 연결 실패"))

    await mgr._close_position_impl("BTC_JPY", "trailing_stop")

    # DB 기록 실패해도 인메모리 클리어는 보장
    assert mgr._position.get("BTC_JPY") is None


@pytest.mark.asyncio
async def test_close_position_non_err422_exchange_error_keeps_position():
    """GT-22: ERR-422 아닌 ExchangeError → 포지션 유지 (오판 방지)."""
    from core.exchange.errors import ExchangeError
    mgr = _make_gmoc_manager()
    mgr._position["BTC_JPY"] = Position(
        pair="BTC_JPY",
        entry_price=11_000_000.0,
        entry_amount=0.001,
        stop_loss_price=10_800_000.0,
        db_record_id=99,
        extra={"side": "sell"},
    )
    mgr._params["BTC_JPY"] = {"min_coin_size": 0.0001}
    mgr._adapter.close_position_bulk = AsyncMock(
        side_effect=ExchangeError("GMO 코인 비즈니스 에러: ERR-400: Invalid request.")
    )

    await mgr._close_position_impl("BTC_JPY", "stop_loss")

    # ERR-422 아니면 포지션 클리어하면 안 됨
    assert mgr._position.get("BTC_JPY") is not None


# ──────────────────────────────────────────────────────────────
# TRL-01~TRL-08: _update_trailing_stop 손익분기 바닥 + profit mult
# ──────────────────────────────────────────────────────────────

_TRL_PARAMS = {
    "trailing_stop_atr_initial": 1.5,
    "trailing_stop_atr_mature": 1.2,
    "trailing_stop_decay_per_atr": 0.2,
    "trailing_stop_atr_min": 0.3,
    "tighten_stop_atr": 1.0,
    "breakeven_trigger_atr": 1.0,
    "ema_slope_weak_threshold": 0.03,
    "rsi_overbought": 75,
    "min_coin_size": 0.0001,
}


def _make_long_pos(entry: float, sl: float) -> Position:
    return Position(
        pair="BTC_JPY",
        entry_price=entry,
        entry_amount=0.004,
        stop_loss_price=sl,
        db_record_id=42,
        extra={"side": "buy"},
    )


def _make_short_pos(entry: float, sl: float) -> Position:
    return Position(
        pair="BTC_JPY",
        entry_price=entry,
        entry_amount=0.004,
        stop_loss_price=sl,
        db_record_id=42,
        extra={"side": "sell"},
    )


@pytest.mark.asyncio
async def test_trl01_breakeven_floor_activated():
    """TRL-01: 이익 >= ATR×breakeven_trigger → 스탑이 진입가 이상으로 상향."""
    mgr = _make_gmoc_manager()
    mgr._update_trailing_stop_in_db = AsyncMock()

    entry = 11_000_000.0
    atr = 100_000.0
    # 이익 = 110_000 >= ATR×1.0 = 100_000 → 손익분기 바닥 발동
    current = entry + atr * 1.1  # 11_110_000

    pos = _make_long_pos(entry=entry, sl=entry - 200_000.0)
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # 스탑이 진입가 이상이어야 함
    assert pos.stop_loss_price >= entry


@pytest.mark.asyncio
async def test_trl02_breakeven_floor_not_triggered():
    """TRL-02: 이익 < ATR×breakeven_trigger → 스탑이 진입가 아래 유지."""
    mgr = _make_gmoc_manager()
    mgr._update_trailing_stop_in_db = AsyncMock()

    entry = 11_000_000.0
    atr = 100_000.0
    # 이익 = 50_000 < ATR×1.0 = 100_000 → 손익분기 미발동
    current = entry + 50_000.0  # 11_050_000

    pos = _make_long_pos(entry=entry, sl=entry - 300_000.0)
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # 스탑이 진입가 아래여야 함 (current - atr*1.5 = 11_050_000 - 150_000 = 10_900_000)
    assert pos.stop_loss_price < entry


@pytest.mark.asyncio
async def test_trl03_loss_position_no_breakeven():
    """TRL-03: 손실 중 → breakeven 미발동, 일반 trailing."""
    mgr = _make_gmoc_manager()
    mgr._update_trailing_stop_in_db = AsyncMock()

    entry = 11_000_000.0
    atr = 100_000.0
    # 현재가 < 진입가 → 손실
    current = entry - 50_000.0  # 10_950_000

    pos = _make_long_pos(entry=entry, sl=entry - 500_000.0)
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # 스탑이 진입가보다 낮아야 함
    assert pos.stop_loss_price < entry


@pytest.mark.asyncio
async def test_trl04_stop_tightened_uses_min_of_tighten_and_profit_mult():
    """TRL-04: stop_tightened=True → min(tighten_stop_atr, profit_mult) — 이익 크면 더 좁아짐."""
    mgr = _make_gmoc_manager()
    mgr._update_trailing_stop_in_db = AsyncMock()

    entry = 11_000_000.0
    atr = 100_000.0
    # 이익 ATR×3.0 → profit_mult = max(0.3, 1.5-0.2×3.0) = 0.9 < tighten_stop_atr=1.0
    current = entry + atr * 3.0  # 11_300_000

    pos = _make_long_pos(entry=entry, sl=entry - 100_000.0)
    pos.stop_tightened = True
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # mult = min(1.0, 0.9) = 0.9 → 11_300_000 - 90_000 = 11_210_000
    expected = round(current - atr * 0.9, 6)
    assert pos.stop_loss_price == expected


@pytest.mark.asyncio
async def test_trl05_entry_price_none_no_error():
    """TRL-05: entry_price=None → AttributeError 없이 정상 처리."""
    mgr = _make_gmoc_manager()
    mgr._update_trailing_stop_in_db = AsyncMock()

    pos = Position(
        pair="BTC_JPY",
        entry_price=None,
        entry_amount=0.004,
        stop_loss_price=10_500_000.0,
        db_record_id=42,
        extra={"side": "buy"},
    )
    current = 11_000_000.0
    atr = 100_000.0
    # 예외 없이 실행되어야 함
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)


@pytest.mark.asyncio
async def test_trl06_short_breakeven_ceiling():
    """TRL-06: 숏 이익 >= ATR×trigger → 스탑이 진입가 이하(ceiling)로 제한."""
    mgr = _make_gmoc_manager()
    mgr._update_trailing_stop_in_db = AsyncMock()

    entry = 11_000_000.0
    atr = 100_000.0
    # 숏 이익 = 110_000 >= ATR×1.0 → 손익분기 바닥 발동 (ceiling=진입가)
    current = entry - atr * 1.1  # 10_890_000

    pos = _make_short_pos(entry=entry, sl=entry + 200_000.0)
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # 숏에서 스탑이 진입가 이하여야 함 (ceiling=진입가)
    assert pos.stop_loss_price <= entry


@pytest.mark.asyncio
async def test_trl07_profit_mult_beats_adaptive():
    """TRL-07: profit_mult < adaptive_mult → min()에 의해 profit_mult 사용."""
    mgr = _make_gmoc_manager()
    mgr._update_trailing_stop_in_db = AsyncMock()

    entry = 11_000_000.0
    atr = 100_000.0
    # 이익 ATR×1.7 → profit_mult = max(0.3, 1.5-0.2×1.7) = 1.16
    # adaptive_mult = 1.5 (slope=0.5% >= 0.03, RSI=55 < 75 → initial)
    # min(1.5, 1.16) = 1.16 사용
    current = entry + atr * 1.7  # 11_170_000

    pos = _make_long_pos(entry=entry, sl=entry - 500_000.0)
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # mult=1.16 → new_sl = 11_170_000 - 116_000 = 11_054_000
    # breakeven: 170_000 >= ATR×1.0 → floor=entry → max(11_054_000, 11_000_000) = 11_054_000
    expected = round(current - atr * 1.16, 6)
    assert pos.stop_loss_price == expected


@pytest.mark.asyncio
async def test_trl08_ratchet_no_downward_update():
    """TRL-08: 새 스탑 < 현재 스탑 → ratchet 유지, DB 미업데이트."""
    mgr = _make_gmoc_manager()
    mgr._update_trailing_stop_in_db = AsyncMock()

    entry = 11_000_000.0
    atr = 100_000.0
    # 현재 스탑이 이미 높게 설정된 상황에서 가격이 내려옴
    current = entry + 50_000.0  # 11_050_000
    high_sl = 11_030_000.0  # 이미 높은 스탑

    pos = _make_long_pos(entry=entry, sl=high_sl)
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # profit_mult = max(0.3, 1.5-0.2×0.5) = 1.4, mult = min(1.5, 1.4) = 1.4
    # new_sl = 11_050_000 - 140_000 = 10_910_000 < high_sl → 업데이트 없음
    assert pos.stop_loss_price == high_sl
    mgr._update_trailing_stop_in_db.assert_not_called()


@pytest.mark.asyncio
async def test_trl09_stop_tightened_ceiling_wins_over_large_profit_mult():
    """TRL-09: stop_tightened=True, 이익 작음 → ceiling(1.0) < profit_mult(1.4) → ceiling 사용."""
    mgr = _make_gmoc_manager()
    mgr._update_trailing_stop_in_db = AsyncMock()

    entry = 11_000_000.0
    atr = 100_000.0
    # 이익 ATR×0.5 → profit_mult = max(0.3, 1.5-0.2×0.5) = 1.4 > tighten_ceiling=1.0
    current = entry + atr * 0.5  # 11_050_000

    pos = _make_long_pos(entry=entry, sl=entry - 100_000.0)
    pos.stop_tightened = True
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # mult = min(1.0, 1.4) = 1.0 → new_sl = 11_050_000 - 100_000 = 10_950_000
    # breakeven: 50_000 < ATR×1.0 → 미발동 → 이전 스탑(10_900_000)보다 크므로 갱신
    expected = round(current - atr * 1.0, 6)
    assert pos.stop_loss_price == expected


@pytest.mark.asyncio
async def test_trl10_stop_tightened_profit_floor_beats_ceiling():
    """TRL-10: stop_tightened=True, 이익 매우 큼 → profit_mult floor(0.3) < ceiling(1.0) → 더 타이트."""
    mgr = _make_gmoc_manager()
    mgr._update_trailing_stop_in_db = AsyncMock()

    entry = 11_000_000.0
    atr = 100_000.0
    # 이익 ATR×6.0 → profit_mult = max(0.3, 1.5-1.2) = 0.3 < tighten_ceiling=1.0
    current = entry + atr * 6.0  # 11_600_000

    pos = _make_long_pos(entry=entry, sl=entry - 100_000.0)
    pos.stop_tightened = True
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # mult = min(1.0, 0.3) = 0.3 → new_sl = 11_600_000 - 30_000 = 11_570_000
    # breakeven: 600_000 >= ATR×1.0 → floor=entry → max(11_570_000, 11_000_000) = 11_570_000
    expected = round(current - atr * 0.3, 6)
    assert pos.stop_loss_price == expected
    mgr._update_trailing_stop_in_db.assert_called_once()


@pytest.mark.asyncio
async def test_trl11_short_stop_tightened_profit_mult():
    """TRL-11: 숏 + stop_tightened=True + 큰 이익 → min(ceiling, profit_mult) 숏 방향."""
    mgr = _make_gmoc_manager()
    mgr._update_trailing_stop_in_db = AsyncMock()

    entry = 11_000_000.0
    atr = 100_000.0
    # 숏 이익 ATR×3.0 → profit_mult = max(0.3, 1.5-0.6) = 0.9 < ceiling=1.0
    current = entry - atr * 3.0  # 10_700_000

    pos = _make_short_pos(entry=entry, sl=entry + 200_000.0)
    pos.stop_tightened = True
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # mult = min(1.0, 0.9) = 0.9 → 숏 new_sl = 10_700_000 + 90_000 = 10_790_000
    # breakeven: 300_000 >= ATR×1.0 → ceiling=entry(11_000_000), min(10_790_000, 11_000_000)=10_790_000
    expected = round(current + atr * 0.9, 6)
    assert pos.stop_loss_price == expected


@pytest.mark.asyncio
async def test_trl12_side_missing_from_extra_defaults_to_buy():
    """TRL-12: pos.extra에 side 미설정 → "buy" 기본값 fallback."""
    mgr = _make_gmoc_manager()
    mgr._update_trailing_stop_in_db = AsyncMock()

    pos = Position(
        pair="BTC_JPY",
        entry_price=11_000_000.0,
        entry_amount=0.004,
        stop_loss_price=10_500_000.0,
        db_record_id=42,
        extra={},  # side 없음
    )
    current = 11_200_000.0
    atr = 100_000.0

    # 예외 없이 실행, "buy" 기본값으로 처리
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # 롱 방향 계산 확인: new_sl = current - atr × mult (스탑 갱신됨)
    assert pos.stop_loss_price > 10_500_000.0  # 스탑이 올라갔어야 함
    mgr._update_trailing_stop_in_db.assert_called_once()


# ──────────────────────────────────────────────────────────────
# LC-01~LC-05: _update_trailing_stop → changeLosscutPrice 거래소 동기화
# ──────────────────────────────────────────────────────────────

def _make_gmoc_manager_with_losscut(
    *,
    positions=None,
    change_losscut_return: bool = True,
):
    """changeLosscutPrice 동작 검증용 매니저 생성."""
    from core.exchange.types import FxPosition
    mgr = _make_gmoc_manager()
    fx_positions = positions if positions is not None else [
        FxPosition(
            product_code="BTC_JPY",
            side="BUY",
            price=11_728_011.0,
            size=0.004,
            pnl=1_000.0,
            leverage=2.0,
            require_collateral=0.0,
            swap_point_accumulate=0.0,
            sfd=0.0,
            position_id=305885,
        )
    ]
    mgr._adapter.get_positions = AsyncMock(return_value=fx_positions)
    mgr._adapter.change_losscut_price = AsyncMock(return_value=change_losscut_return)
    mgr._update_trailing_stop_in_db = AsyncMock()
    return mgr


@pytest.mark.asyncio
async def test_lc01_stop_updated_calls_change_losscut():
    """LC-01: 스탑이 갱신되면 changeLosscutPrice가 호출된다."""
    mgr = _make_gmoc_manager_with_losscut()

    entry = 11_000_000.0
    atr = 100_000.0
    # 충분한 이익으로 스탑이 갱신되는 상황
    current = entry + atr * 3.0  # 11_300_000
    pos = _make_long_pos(entry=entry, sl=entry - 500_000.0)

    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    mgr._adapter.change_losscut_price.assert_called_once_with(305885, pos.stop_loss_price)


@pytest.mark.asyncio
async def test_lc02_stop_not_changed_no_losscut_call():
    """LC-02: ratchet으로 스탑이 갱신되지 않으면 changeLosscutPrice 미호출."""
    mgr = _make_gmoc_manager_with_losscut()
    mgr._update_trailing_stop_in_db = AsyncMock()

    entry = 11_000_000.0
    atr = 100_000.0
    # 현재 스탑이 이미 높게 설정 → 갱신 없음
    current = entry + 50_000.0  # 11_050_000
    high_sl = 11_030_000.0

    pos = _make_long_pos(entry=entry, sl=high_sl)

    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # 스탑 미갱신 → 거래소 동기화 없음
    mgr._adapter.change_losscut_price.assert_not_called()


@pytest.mark.asyncio
async def test_lc03_multiple_positions_all_updated():
    """LC-03: 피라미딩 등 복수 건옥 → 모든 positionId에 changeLosscutPrice 호출."""
    from core.exchange.types import FxPosition
    fx_positions = [
        FxPosition(
            product_code="BTC_JPY", side="BUY", price=11_000_000.0, size=0.002,
            pnl=0.0, leverage=2.0, require_collateral=0.0, swap_point_accumulate=0.0,
            sfd=0.0, position_id=111111,
        ),
        FxPosition(
            product_code="BTC_JPY", side="BUY", price=11_200_000.0, size=0.002,
            pnl=0.0, leverage=2.0, require_collateral=0.0, swap_point_accumulate=0.0,
            sfd=0.0, position_id=222222,
        ),
    ]
    mgr = _make_gmoc_manager_with_losscut(positions=fx_positions)

    entry = 11_000_000.0
    atr = 100_000.0
    current = entry + atr * 3.0
    pos = _make_long_pos(entry=entry, sl=entry - 500_000.0)

    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    calls = [c.args[0] for c in mgr._adapter.change_losscut_price.call_args_list]
    assert set(calls) == {111111, 222222}


@pytest.mark.asyncio
async def test_lc04_get_positions_empty_no_error():
    """LC-04: get_positions()가 빈 리스트 반환 → 예외 없이 통과."""
    mgr = _make_gmoc_manager_with_losscut(positions=[])

    entry = 11_000_000.0
    atr = 100_000.0
    current = entry + atr * 3.0
    pos = _make_long_pos(entry=entry, sl=entry - 500_000.0)

    # 예외 없이 완료
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)
    mgr._adapter.change_losscut_price.assert_not_called()


@pytest.mark.asyncio
async def test_lc05_change_losscut_failure_doesnt_break_stop():
    """LC-05: changeLosscutPrice 실패 → 인메모리 스탑 갱신은 그대로 유지."""
    mgr = _make_gmoc_manager_with_losscut(change_losscut_return=False)

    entry = 11_000_000.0
    atr = 100_000.0
    current = entry + atr * 3.0
    pos = _make_long_pos(entry=entry, sl=entry - 500_000.0)
    old_sl = pos.stop_loss_price

    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # 거래소 동기화 실패해도 인메모리 스탑은 갱신됨
    assert pos.stop_loss_price != old_sl
    assert pos.stop_loss_price > old_sl


@pytest.mark.asyncio
async def test_lc06_get_positions_raises_warning_only():
    """LC-06: get_positions() 예외 → WARNING만, 인메모리 스탑 갱신은 유지."""
    mgr = _make_gmoc_manager_with_losscut()
    mgr._adapter.get_positions = AsyncMock(side_effect=RuntimeError("network error"))

    entry = 11_000_000.0
    atr = 100_000.0
    current = entry + atr * 3.0
    pos = _make_long_pos(entry=entry, sl=entry - 500_000.0)
    old_sl = pos.stop_loss_price

    # 예외 없이 완료
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # 인메모리 스탑은 갱신됨 (거래소 동기화 실패와 무관)
    assert pos.stop_loss_price > old_sl
    # changeLosscutPrice는 호출되지 않음
    mgr._adapter.change_losscut_price.assert_not_called()


@pytest.mark.asyncio
async def test_lc07_adapter_missing_methods_no_error():
    """LC-07: 어댑터에 change_losscut_price 없음 → hasattr 체크로 조용히 통과."""
    mgr = _make_gmoc_manager_with_losscut()
    # change_losscut_price 제거
    del mgr._adapter.change_losscut_price

    entry = 11_000_000.0
    atr = 100_000.0
    current = entry + atr * 3.0
    pos = _make_long_pos(entry=entry, sl=entry - 500_000.0)
    old_sl = pos.stop_loss_price

    # 예외 없이 완료, 인메모리 스탑 갱신 유지
    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)
    assert pos.stop_loss_price > old_sl


@pytest.mark.asyncio
async def test_lc08_position_id_none_skipped():
    """LC-08: position_id=None 건옥은 건너뛰고, 유효한 건옥만 처리."""
    from core.exchange.types import FxPosition
    fx_positions = [
        FxPosition(
            product_code="BTC_JPY", side="BUY", price=11_000_000.0, size=0.002,
            pnl=0.0, leverage=2.0, require_collateral=0.0, swap_point_accumulate=0.0,
            sfd=0.0, position_id=None,  # None → 건너뜀
        ),
        FxPosition(
            product_code="BTC_JPY", side="BUY", price=11_200_000.0, size=0.002,
            pnl=0.0, leverage=2.0, require_collateral=0.0, swap_point_accumulate=0.0,
            sfd=0.0, position_id=999999,  # 유효 → 처리
        ),
    ]
    mgr = _make_gmoc_manager_with_losscut(positions=fx_positions)

    entry = 11_000_000.0
    atr = 100_000.0
    current = entry + atr * 3.0
    pos = _make_long_pos(entry=entry, sl=entry - 500_000.0)

    await mgr._update_trailing_stop("BTC_JPY", pos, current, atr, 0.5, 55.0, _TRL_PARAMS)

    # 유효한 position_id=999999만 호출
    mgr._adapter.change_losscut_price.assert_called_once_with(999999, pos.stop_loss_price)
