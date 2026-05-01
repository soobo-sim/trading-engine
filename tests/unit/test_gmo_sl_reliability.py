"""
GmoCoinTrendManager — SL 신뢰성 강화 단위 테스트 (Phase 1~4).

SL-01: 진입 시 change_losscut_price 즉시 호출 확인
SL-02: 피라미딩 후 change_losscut_price 호출 확인
SL-03: _detect_existing_position — DB에서 stop_loss_price 복원 확인
SL-04: _detect_existing_position — DB 복원 후 거래소 재동기화 확인
SL-05: _sync_losscut_price — 1회 실패 후 재시도 성공
SL-06: _sync_losscut_price — 3회 모두 실패 시 _handle_losscut_sync_failure 호출
SL-07: _handle_losscut_sync_failure — SL 뚫림 감지 시 긴급 청산 태스크 생성
SL-08: _handle_losscut_sync_failure — SL 미뚫림 시 긴급 청산 미발동
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

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

def _make_fx_pos(position_id: int = 101, size: float = 0.001, price: float = 11_000_000.0, side: str = "buy") -> MagicMock:
    fp = MagicMock()
    fp.position_id = position_id
    fp.size = size
    fp.price = price
    fp.side = side
    return fp


def _make_order(
    order_type: OrderType = OrderType.MARKET_BUY,
    amount: float = 0.001,
    price: float = 11_450_000.0,
) -> Order:
    side = OrderSide.BUY if order_type in (OrderType.BUY, OrderType.MARKET_BUY) else OrderSide.SELL
    return Order(
        order_id="ord_001",
        pair="btc_jpy",
        order_type=order_type,
        side=side,
        price=price,
        amount=amount,
        status=OrderStatus.COMPLETED,
        created_at=datetime.now(timezone.utc),
    )


def _make_db_rec(
    rec_id: int = 10,
    stop_loss_price: float | None = 10_800_000.0,
    side: str = "buy",
) -> MagicMock:
    rec = MagicMock()
    rec.id = rec_id
    rec.stop_loss_price = stop_loss_price
    rec.side = side
    rec.status = "open"
    return rec


def _make_session_factory(rec: MagicMock | None = None):
    """DB 세션을 모킹하는 async context manager 팩토리 반환."""

    @asynccontextmanager
    async def _session():
        db = AsyncMock()
        result = MagicMock()
        scalars = MagicMock()
        scalars.first.return_value = rec
        result.scalars.return_value = scalars
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        yield db

    return _session


def _make_gmoc_manager(
    *,
    collateral: float = 100_000.0,
    require_collateral: float = 0.0,
    ticker_last: float = 11_450_000.0,
    ticker_ask: float = 11_500_000.0,
    ticker_bid: float = 11_400_000.0,
    fx_positions: list | None = None,
    change_losscut_ok: bool = True,
    db_rec: MagicMock | None = None,
):
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager

    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.is_margin_trading = True
    adapter.get_collateral = AsyncMock(
        return_value=Collateral(
            collateral=collateral,
            open_position_pnl=0.0,
            require_collateral=require_collateral,
            keep_rate=999.0,
        )
    )
    adapter.get_ticker = AsyncMock(
        return_value=Ticker(
            pair="btc_jpy",
            last=ticker_last,
            bid=ticker_bid,
            ask=ticker_ask,
            high=12_000_000.0,
            low=11_000_000.0,
            volume=100.0,
        )
    )
    adapter.place_order = AsyncMock(
        return_value=_make_order(OrderType.MARKET_BUY, amount=0.009, price=ticker_last)
    )
    adapter.close_position_bulk = AsyncMock(
        return_value=_make_order(OrderType.MARKET_SELL, amount=0.001, price=ticker_last)
    )
    adapter.get_positions = AsyncMock(
        return_value=fx_positions if fx_positions is not None else [_make_fx_pos()]
    )
    adapter.change_losscut_price = AsyncMock(return_value=change_losscut_ok)

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    session_factory = _make_session_factory(db_rec)

    # Position Model mock
    pos_model = MagicMock()
    pos_model.__name__ = "TrendPosition"
    pos_model.status = MagicMock()

    mgr = GmoCoinTrendManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=MagicMock(),
        cfd_position_model=pos_model,
    )

    # DB 기록 비활성화 (단위 테스트에서 DB 기록 불필요)
    mgr._record_open = AsyncMock(return_value=42)
    mgr._record_close = AsyncMock()
    mgr._update_trailing_stop_in_db = AsyncMock()
    mgr._update_position_in_db = AsyncMock()

    return mgr


_BASE_PARAMS = {
    "position_size_pct": 10.0,
    "min_order_jpy": 500,
    "atr_multiplier_stop": 2.0,
    "max_slippage_pct": 5.0,
    "min_coin_size": 0.0001,
    "max_leverage": 10.0,
}


# ──────────────────────────────────────────────────────────────
# SL-01: 진입 시 change_losscut_price 즉시 호출
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sl01_open_position_syncs_losscut_immediately():
    """SL-01: _open_position 완료 후 change_losscut_price가 즉시 호출된다."""
    fx_pos = _make_fx_pos(position_id=201)
    mgr = _make_gmoc_manager(fx_positions=[fx_pos])

    await mgr._open_position("btc_jpy", "buy", 11_500_000.0, 100_000.0, dict(_BASE_PARAMS))

    # change_losscut_price 최소 1회 호출됐는지 확인
    assert mgr._adapter.change_losscut_price.called, "진입 후 change_losscut_price가 호출되어야 함"
    call_args = mgr._adapter.change_losscut_price.call_args
    position_id_arg, new_sl_arg = call_args.args
    assert position_id_arg == 201
    # SL은 exec_price - atr * atr_mult  =  11_450_000 - 100_000 * 2.0 = 11_250_000
    assert new_sl_arg == pytest.approx(11_250_000.0, rel=1e-3)


# ──────────────────────────────────────────────────────────────
# SL-02: 피라미딩 후 change_losscut_price 호출
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sl02_add_to_position_syncs_losscut():
    """SL-02: _add_to_position 완료 후 change_losscut_price가 호출된다."""
    fx_pos = _make_fx_pos(position_id=202)
    mgr = _make_gmoc_manager(fx_positions=[fx_pos])
    mgr._position["btc_jpy"] = Position(
        pair="btc_jpy",
        entry_price=11_000_000.0,
        entry_amount=0.001,
        stop_loss_price=10_800_000.0,
        db_record_id=42,
        extra={"side": "buy", "pyramid_count": 0, "pyramid_entries": [], "total_size_pct": 0.1},
    )
    mgr._params["btc_jpy"] = dict(_BASE_PARAMS)

    await mgr._add_to_position("btc_jpy", "buy", 11_500_000.0, 100_000.0, dict(_BASE_PARAMS))

    assert mgr._adapter.change_losscut_price.called, "피라미딩 후 change_losscut_price가 호출되어야 함"


# ──────────────────────────────────────────────────────────────
# SL-03: _detect_existing_position — DB에서 stop_loss_price 복원
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sl03_detect_existing_restores_sl_from_db():
    """SL-03: DB에 stop_loss_price가 있으면 Position.stop_loss_price에 복원된다."""
    db_rec = _make_db_rec(rec_id=10, stop_loss_price=10_500_000.0, side="buy")
    mgr = _make_gmoc_manager(db_rec=db_rec, fx_positions=[_make_fx_pos(position_id=301)])

    # SQLAlchemy select를 mock — position_model이 MagicMock이라 실제 쿼리 빌딩 불가
    mock_stmt = MagicMock()
    mock_stmt.where.return_value = mock_stmt
    mock_stmt.order_by.return_value = mock_stmt
    mock_stmt.limit.return_value = mock_stmt

    with patch("sqlalchemy.select", return_value=mock_stmt):
        pos = await mgr._detect_existing_position("btc_jpy")

    assert pos is not None
    assert pos.stop_loss_price == pytest.approx(10_500_000.0)


# ──────────────────────────────────────────────────────────────
# SL-04: _detect_existing_position — DB 복원 후 거래소 재동기화
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sl04_detect_existing_resyncs_losscut_to_exchange():
    """SL-04: DB에서 SL 복원 후 change_losscut_price를 거래소에 재동기화한다."""
    db_rec = _make_db_rec(rec_id=10, stop_loss_price=10_500_000.0, side="buy")
    fx_pos = _make_fx_pos(position_id=302)
    mgr = _make_gmoc_manager(db_rec=db_rec, fx_positions=[fx_pos])

    mock_stmt = MagicMock()
    mock_stmt.where.return_value = mock_stmt
    mock_stmt.order_by.return_value = mock_stmt
    mock_stmt.limit.return_value = mock_stmt

    with patch("sqlalchemy.select", return_value=mock_stmt):
        await mgr._detect_existing_position("btc_jpy")

    assert mgr._adapter.change_losscut_price.called, "DB SL 복원 후 거래소 재동기화 필요"
    call_args = mgr._adapter.change_losscut_price.call_args
    position_id_arg, new_sl_arg = call_args.args
    assert position_id_arg == 302
    assert new_sl_arg == pytest.approx(10_500_000.0)


# ──────────────────────────────────────────────────────────────
# SL-05: _sync_losscut_price — 1회 실패 후 재시도 성공
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sl05_sync_retries_on_first_failure():
    """SL-05: change_losscut_price 1회 실패 후 재시도 시 성공하면 _handle_losscut_sync_failure 미호출."""
    fx_pos = _make_fx_pos(position_id=401)
    mgr = _make_gmoc_manager(fx_positions=[fx_pos])
    # 1차 False, 2차 True
    mgr._adapter.change_losscut_price = AsyncMock(side_effect=[False, True])
    mgr._handle_losscut_sync_failure = AsyncMock()

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await mgr._sync_losscut_price("btc_jpy", 10_800_000.0)

    assert mgr._adapter.change_losscut_price.call_count == 2, "2회 호출 (1차 실패 + 2차 성공)"
    mgr._handle_losscut_sync_failure.assert_not_called()


# ──────────────────────────────────────────────────────────────
# SL-06: _sync_losscut_price — 3회 모두 실패 시 _handle_losscut_sync_failure 호출
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sl06_sync_calls_failure_handler_after_max_retries():
    """SL-06: max_retries(3)회 모두 실패하면 _handle_losscut_sync_failure가 호출된다."""
    fx_pos = _make_fx_pos(position_id=402)
    mgr = _make_gmoc_manager(fx_positions=[fx_pos], change_losscut_ok=False)
    mgr._handle_losscut_sync_failure = AsyncMock()

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await mgr._sync_losscut_price("btc_jpy", 10_800_000.0, max_retries=3)

    assert mgr._adapter.change_losscut_price.call_count == 3, "max_retries=3회 모두 시도해야 함"
    mgr._handle_losscut_sync_failure.assert_called_once_with("btc_jpy", 10_800_000.0, 402)


# ──────────────────────────────────────────────────────────────
# SL-07: _handle_losscut_sync_failure — SL 뚫림 시 긴급 청산
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sl07_failure_handler_triggers_emergency_close_when_sl_breached():
    """SL-07: 현재가가 SL 아래(롱 side)면 긴급 청산 태스크가 생성된다."""
    mgr = _make_gmoc_manager(ticker_last=10_700_000.0)  # SL=10_800_000보다 낮음
    mgr._position["btc_jpy"] = Position(
        pair="btc_jpy",
        entry_price=11_000_000.0,
        entry_amount=0.001,
        stop_loss_price=10_800_000.0,
        extra={"side": "buy"},
    )
    mgr._close_position = MagicMock(return_value=None)  # coroutine 아닌 MagicMock — 경고 방지

    with patch("asyncio.create_task") as mock_create_task:
        await mgr._handle_losscut_sync_failure("btc_jpy", 10_800_000.0, position_id=501)

    # 긴급 청산 create_task 호출 확인
    assert mock_create_task.called, "SL 뚫림 시 create_task로 긴급 청산이 생성되어야 함"


# ──────────────────────────────────────────────────────────────
# SL-08: _handle_losscut_sync_failure — SL 미뚫림 시 긴급 청산 미발동
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sl08_failure_handler_no_emergency_close_when_sl_not_breached():
    """SL-08: 현재가가 SL 위(롱 side)면 긴급 청산을 발동하지 않는다."""
    mgr = _make_gmoc_manager(ticker_last=11_200_000.0)  # SL=10_800_000보다 높음
    mgr._position["btc_jpy"] = Position(
        pair="btc_jpy",
        entry_price=11_000_000.0,
        entry_amount=0.001,
        stop_loss_price=10_800_000.0,
        extra={"side": "buy"},
    )
    mgr._close_position = MagicMock(return_value=None)

    with patch("asyncio.create_task") as mock_create_task:
        await mgr._handle_losscut_sync_failure("btc_jpy", 10_800_000.0, position_id=502)

    mock_create_task.assert_not_called()
