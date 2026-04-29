"""
Limit Order 진입 단위 테스트.

커버:
  - _open_position_limit(): limit order 발주, PendingLimitOrder 반환
  - _check_pending_limit_order(): 체결 완료 → _finalize_limit_entry
  - _check_pending_limit_order(): 취소됨 → pending 제거
  - _check_pending_limit_order(): 시그널 변경 → cancel_order
  - _check_pending_limit_order(): 타임아웃 → cancel_order
  - _open_position_limit() 미구현(base) → None 반환
  - long_setup + pending 존재 → 진입 블록 (중복 방지)
  - entry_mode=limit_then_market: limit 실패 시 다음 사이클 market fallback
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import create_candle_model, create_cfd_position_model, create_strategy_model
from adapters.database.session import Base
from core.exchange.types import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PendingLimitOrder,
    Position,
)
from core.strategy.gmo_coin_trend import GmoCoinTrendManager
from core.punisher.task.supervisor import TaskSupervisor
from tests.fake_exchange import FakeExchangeAdapter

# ── 테스트용 ORM 모델 ──────────────────────────────────────────

TstStrategy = create_strategy_model("tlmt")
TstCandle = create_candle_model("tlmt", pair_column="pair")
TstTrendPosition = create_cfd_position_model("tlmt", pair_column="pair", order_id_length=40)


# ── Fixtures ──────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        tables = [
            Base.metadata.tables[t]
            for t in Base.metadata.tables
            if t.startswith("tlmt_") or t == "strategy_techniques"
        ]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def supervisor():
    sup = TaskSupervisor()
    yield sup
    await sup.stop_all()


@pytest_asyncio.fixture
async def fake_adapter():
    adapter = FakeExchangeAdapter(
        initial_balances={"jpy": 1_000_000.0, "btc": 0.0},
        ticker_price=10_000_000.0,
    )
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def manager(fake_adapter, supervisor, db_session_factory):
    mgr = GmoCoinTrendManager(
        adapter=fake_adapter,
        supervisor=supervisor,
        session_factory=db_session_factory,
        candle_model=TstCandle,
        cfd_position_model=TstTrendPosition,
        pair_column="pair",
    )
    yield mgr
    await mgr.stop_all()


def _pending(
    pair: str = "btc_jpy",
    order_id: str = "FAKE-001",
    limit_price: float = 9_985_000.0,
    placed_at: float | None = None,
) -> PendingLimitOrder:
    return PendingLimitOrder(
        order_id=order_id,
        pair=pair,
        limit_price=limit_price,
        amount=0.001,
        invest_jpy=10_000.0,
        placed_at=placed_at or time.time(),
        signal_at_placement="long_setup",
        params={"atr_multiplier_stop": 2.0, "strategy_id": None},
        atr=50_000.0,
        signal_data={},
    )


def _order(
    order_id: str = "FAKE-001",
    status: OrderStatus = OrderStatus.COMPLETED,
    price: float = 9_985_000.0,
    amount: float = 0.001,
) -> Order:
    return Order(
        order_id=order_id,
        pair="btc_jpy",
        order_type=OrderType.BUY,
        side=OrderSide.BUY,
        price=price,
        amount=amount,
        status=status,
    )


# ──────────────────────────────────────────────────────────────
# _open_position_limit()
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_limit_order_placed_on_long_setup(manager, fake_adapter):
    """
    Given: entry_mode=limit, ATR 있음, JPY 잔고 충분
    When:  _open_position_limit() 호출
    Then:  limit order 발주되고 PendingLimitOrder 반환
    """
    params = {
        "position_size_pct": 1.0,   # 투입 1%
        "min_order_jpy": 500,
        "limit_offset_atr_ratio": 0.15,
        "atr_multiplier_stop": 2.0,
    }
    result = await manager._open_position_limit(
        pair="btc_jpy",
        price=10_000_000.0,
        atr=50_000.0,
        params=params,
        signal_data={},
    )

    assert result is not None
    assert isinstance(result, PendingLimitOrder)
    # limit_price = 10_000_000 - 50_000 × 0.15 = 9_992_500
    assert result.limit_price == pytest.approx(9_992_500.0, rel=0.001)
    assert result.signal_at_placement == "long_setup"


@pytest.mark.asyncio
async def test_limit_order_skipped_when_jpy_insufficient(manager, fake_adapter):
    """
    Given: JPY 잔고 부족 (1백만 중 position_size_pct=0.03% → ~ 300 JPY < min 500)
    When:  _open_position_limit() 호출
    Then:  None 반환
    """
    params = {
        "position_size_pct": 0.03,  # ~ 300 JPY
        "min_order_jpy": 500,
    }
    result = await manager._open_position_limit(
        pair="btc_jpy",
        price=10_000_000.0,
        atr=50_000.0,
        params=params,
    )
    assert result is None


@pytest.mark.asyncio
async def test_base_class_open_position_limit_returns_none(manager):
    """
    Given: base_trend.py _open_position_limit (default 구현)
    When:  부모 메서드 직접 호출
    Then:  None 반환 (서브클래스 미구현 시 market fallback)
    """
    from core.strategy.base_trend import BaseTrendManager
    result = await BaseTrendManager._open_position_limit(
        manager, pair="btc_jpy", price=10_000_000.0, atr=50_000.0, params={}
    )
    assert result is None


# ──────────────────────────────────────────────────────────────
# _check_pending_limit_order()
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_limit_order_completed_calls_finalize(manager):
    """
    Given: limit order COMPLETED 상태
    When:  _check_pending_limit_order()
    Then:  _finalize_limit_entry() 호출, pending 제거, True 반환
    """
    pending = _pending()
    manager._pending_limit_orders["btc_jpy"] = pending

    completed_order = _order(status=OrderStatus.COMPLETED, price=9_985_000.0)
    manager._adapter.get_order = AsyncMock(return_value=completed_order)
    manager._finalize_limit_entry = AsyncMock()

    result = await manager._check_pending_limit_order(
        "btc_jpy", pending, "long_setup", {"limit_timeout_sec": 300}
    )

    assert result is True
    manager._finalize_limit_entry.assert_awaited_once_with("btc_jpy", completed_order, pending)
    assert "btc_jpy" not in manager._pending_limit_orders


@pytest.mark.asyncio
async def test_pending_limit_order_cancelled_removes_pending(manager):
    """
    Given: limit order CANCELLED 상태
    When:  _check_pending_limit_order()
    Then:  pending 제거, False 반환 (다음 사이클에서 재시도 가능)
    """
    pending = _pending()
    manager._pending_limit_orders["btc_jpy"] = pending

    cancelled_order = _order(status=OrderStatus.CANCELLED)
    manager._adapter.get_order = AsyncMock(return_value=cancelled_order)

    result = await manager._check_pending_limit_order(
        "btc_jpy", pending, "long_setup", {"limit_timeout_sec": 300}
    )

    assert result is False
    assert "btc_jpy" not in manager._pending_limit_orders


@pytest.mark.asyncio
async def test_pending_limit_order_signal_change_cancels_order(manager):
    """
    Given: limit order OPEN 상태, 시그널이 exit_warning으로 변경
    When:  _check_pending_limit_order()
    Then:  cancel_order 호출되고 pending 제거, False 반환
    """
    pending = _pending()
    manager._pending_limit_orders["btc_jpy"] = pending

    open_order = _order(status=OrderStatus.OPEN)
    manager._adapter.get_order = AsyncMock(return_value=open_order)
    manager._adapter.cancel_order = AsyncMock(return_value=True)

    result = await manager._check_pending_limit_order(
        "btc_jpy", pending, "exit_warning", {"limit_timeout_sec": 300}
    )

    assert result is False
    manager._adapter.cancel_order.assert_awaited_once()
    assert "btc_jpy" not in manager._pending_limit_orders


@pytest.mark.asyncio
async def test_pending_limit_order_timeout_cancels_order(manager):
    """
    Given: limit order OPEN 상태, 타임아웃 경과 (placed_at = 10분 전)
    When:  _check_pending_limit_order()
    Then:  cancel_order 호출되고 pending 제거, False 반환
    """
    old_time = time.time() - 700  # 700초 전 (타임아웃 300초보다 많이 경과)
    pending = _pending(placed_at=old_time)
    manager._pending_limit_orders["btc_jpy"] = pending

    open_order = _order(status=OrderStatus.OPEN)
    manager._adapter.get_order = AsyncMock(return_value=open_order)
    manager._adapter.cancel_order = AsyncMock(return_value=True)

    result = await manager._check_pending_limit_order(
        "btc_jpy", pending, "long_setup", {"limit_timeout_sec": 300}
    )

    assert result is False
    manager._adapter.cancel_order.assert_awaited_once()
    assert "btc_jpy" not in manager._pending_limit_orders


@pytest.mark.asyncio
async def test_pending_limit_order_open_waits(manager):
    """
    Given: limit order OPEN 상태, 타임아웃 미달, 시그널 유지
    When:  _check_pending_limit_order()
    Then:  대기 → True 반환, pending 유지
    """
    pending = _pending()  # placed_at = 지금
    manager._pending_limit_orders["btc_jpy"] = pending

    open_order = _order(status=OrderStatus.OPEN)
    manager._adapter.get_order = AsyncMock(return_value=open_order)

    result = await manager._check_pending_limit_order(
        "btc_jpy", pending, "long_setup", {"limit_timeout_sec": 300}
    )

    assert result is True
    assert "btc_jpy" in manager._pending_limit_orders


# ──────────────────────────────────────────────────────────────
# _finalize_limit_entry()
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_limit_entry_registers_position(manager):
    """
    Given: limit order 체결 완료
    When:  _finalize_limit_entry()
    Then:  manager._position[pair] 설정됨 (entry_price, stop_loss)
    """
    pending = _pending()
    completed_order = _order(status=OrderStatus.COMPLETED, price=9_985_000.0)

    # _record_open mocking
    manager._record_open = AsyncMock(return_value=42)

    await manager._finalize_limit_entry("btc_jpy", completed_order, pending)

    pos = manager._position.get("btc_jpy")
    assert pos is not None
    assert pos.entry_price == pytest.approx(9_985_000.0)
    assert pos.stop_loss_price is not None  # ATR × multiplier 로 계산됨


# ──────────────────────────────────────────────────────────────
# pending 존재 시 중복 진입 방지
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_limit_order_blocks_duplicate_entry(manager):
    """
    Given: _pending_limit_orders에 이미 pending이 있는 상태
    When:  _check_pending_limit_order()가 True 반환 → candle_monitor가 continue
    Then:  _open_position 호출 안 됨 (manager의 position은 None 유지)
    """
    pending = _pending()
    manager._pending_limit_orders["btc_jpy"] = pending

    # OPEN 상태 → 대기
    open_order = _order(status=OrderStatus.OPEN)
    manager._adapter.get_order = AsyncMock(return_value=open_order)
    manager._open_position = AsyncMock()

    result = await manager._check_pending_limit_order(
        "btc_jpy", pending, "long_setup", {"limit_timeout_sec": 300}
    )

    assert result is True
    manager._open_position.assert_not_called()


# ──────────────────────────────────────────────────────────────
# 엣지 케이스 보강
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_position_limit_no_atr_uses_fallback_price(manager, fake_adapter):
    """
    Given: ATR=None (캔들 부족 등으로 ATR 미계산)
    When:  _open_position_limit() 호출
    Then:  limit_price = price × 0.999 (0.1% 아래 fallback)
    """
    params = {
        "position_size_pct": 1.0,
        "min_order_jpy": 500,
        "limit_offset_atr_ratio": 0.15,
    }
    result = await manager._open_position_limit(
        pair="btc_jpy",
        price=10_000_000.0,
        atr=None,  # ATR 없음
        params=params,
        signal_data={},
    )

    assert result is not None
    # ATR 없으면 current_price × 0.999 = 9_990_000
    assert result.limit_price == pytest.approx(9_990_000.0, rel=0.001)


@pytest.mark.asyncio
async def test_check_pending_get_order_exception_returns_true(manager):
    """
    Given: get_order() 네트워크 오류 발생
    When:  _check_pending_limit_order()
    Then:  True 반환 (다음 사이클 재시도 — pending 유지)
    """
    pending = _pending()
    manager._pending_limit_orders["btc_jpy"] = pending
    manager._adapter.get_order = AsyncMock(side_effect=RuntimeError("network error"))

    result = await manager._check_pending_limit_order(
        "btc_jpy", pending, "long_setup", {"limit_timeout_sec": 300}
    )

    assert result is True
    assert "btc_jpy" in manager._pending_limit_orders


@pytest.mark.asyncio
async def test_check_pending_none_order_treated_as_cancelled(manager):
    """
    Given: get_order() → None (주문 미존재)
    When:  _check_pending_limit_order()
    Then:  CANCELLED처럼 처리 → pending 제거, False 반환
    """
    pending = _pending()
    manager._pending_limit_orders["btc_jpy"] = pending
    manager._adapter.get_order = AsyncMock(return_value=None)

    result = await manager._check_pending_limit_order(
        "btc_jpy", pending, "long_setup", {"limit_timeout_sec": 300}
    )

    assert result is False
    assert "btc_jpy" not in manager._pending_limit_orders


@pytest.mark.asyncio
async def test_open_position_limit_stop_loss_based_on_atr(manager, fake_adapter):
    """
    Given: ATR=50_000, atr_multiplier_stop=2.0
    When:  _finalize_limit_entry() 후 포지션 확인
    Then:  stop_loss_price = exec_price - ATR × multiplier
    """
    pending = _pending()  # atr=50_000, params multiplier=2.0
    completed_order = _order(status=OrderStatus.COMPLETED, price=9_985_000.0)
    manager._record_open = AsyncMock(return_value=1)

    await manager._finalize_limit_entry("btc_jpy", completed_order, pending)

    pos = manager._position.get("btc_jpy")
    expected_sl = round(9_985_000.0 - 50_000.0 * 2.0, 6)  # 9_885_000
    assert pos is not None
    assert pos.stop_loss_price == pytest.approx(expected_sl)
