"""
Paper Trading Executor 유닛 테스트 (Step 5).

PaperExecutor / RealExecutor / IExecutor Protocol 검증.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from core.execution.executor import (
    PaperExecutor,
    RealExecutor,
    IExecutor,
    _calc_pnl_pct,
    create_executor,
)
from core.exchange.types import Order, OrderSide, OrderStatus, OrderType


_UTC = timezone.utc


# ── 헬퍼 ──────────────────────────────────────────────────────

def _make_adapter(last_price: float = 150.0) -> MagicMock:
    adapter = MagicMock()
    ticker = MagicMock()
    ticker.last = last_price
    adapter.get_ticker = AsyncMock(return_value=ticker)
    adapter.place_order = AsyncMock(
        return_value=Order(
            order_id="real-001",
            pair="usd_jpy",
            order_type=OrderType.MARKET_BUY,
            side=OrderSide.BUY,
            price=last_price,
            amount=1000.0,
            status=OrderStatus.COMPLETED,
        )
    )
    return adapter


def _make_session_factory(paper_trade_row_id: int = 42) -> MagicMock:
    """PaperTrade row에 id를 부여하는 mock session_factory."""
    row = MagicMock()
    row.id = paper_trade_row_id

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()  # flush 후 row.id가 할당된 것으로 시뮬레이션
    session.commit = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=row)))))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = session
    return factory, session, row


# ── RealExecutor ──────────────────────────────────────────────

class TestRealExecutor:
    @pytest.mark.asyncio
    async def test_place_order_delegates_to_adapter(self):
        """RealExecutor.place_order 는 adapter.place_order를 위임한다."""
        adapter = _make_adapter(150.5)
        executor = RealExecutor()
        order = await executor.place_order(adapter, OrderType.MARKET_BUY, "usd_jpy", 1000.0)
        adapter.place_order.assert_called_once_with(OrderType.MARKET_BUY, "usd_jpy", 1000.0)
        assert order.order_id == "real-001"

    @pytest.mark.asyncio
    async def test_record_paper_entry_returns_none(self):
        """RealExecutor.record_paper_entry → None (paper 기록 없음)."""
        executor = RealExecutor()
        result = await executor.record_paper_entry(1, "usd_jpy", "long", 150.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_record_paper_exit_noop(self):
        """RealExecutor.record_paper_exit → no-op (예외 없이 반환)."""
        executor = RealExecutor()
        await executor.record_paper_exit(1, 151.0, "near_lower_exit", 150.0, 10000.0, "long")

    def test_is_iexecutor(self):
        """RealExecutor가 IExecutor Protocol을 만족한다."""
        assert isinstance(RealExecutor(), IExecutor)


# ── PaperExecutor ──────────────────────────────────────────────

class TestPaperExecutorPlaceOrder:
    @pytest.mark.asyncio
    async def test_place_order_no_real_order(self):
        """PaperExecutor.place_order 는 adapter.place_order를 호출하지 않는다."""
        factory, _, _ = _make_session_factory()
        adapter = _make_adapter(149.5)
        executor = PaperExecutor(factory, strategy_id=7)

        order = await executor.place_order(adapter, OrderType.MARKET_BUY, "usd_jpy", 1000.0)

        adapter.place_order.assert_not_called()
        assert order.price == 149.5
        assert order.amount == 1000.0
        assert order.status == OrderStatus.COMPLETED
        assert "paper" in order.order_id

    @pytest.mark.asyncio
    async def test_place_order_sell_returns_sell_side(self):
        """MARKET_SELL → OrderSide.SELL."""
        factory, _, _ = _make_session_factory()
        adapter = _make_adapter(149.5)
        executor = PaperExecutor(factory, strategy_id=7)

        order = await executor.place_order(adapter, OrderType.MARKET_SELL, "usd_jpy", 1000.0)
        assert order.side == OrderSide.SELL

    @pytest.mark.asyncio
    async def test_place_order_ticker_fail_price_zero(self):
        """ticker 조회 실패 시 price=0 반환 (예외 없이)."""
        factory, _, _ = _make_session_factory()
        adapter = MagicMock()
        adapter.get_ticker = AsyncMock(side_effect=Exception("timeout"))
        adapter.place_order = AsyncMock()

        executor = PaperExecutor(factory, strategy_id=7)
        order = await executor.place_order(adapter, OrderType.MARKET_BUY, "usd_jpy", 100.0)

        assert order.price == 0.0
        adapter.place_order.assert_not_called()

    def test_is_iexecutor(self):
        """PaperExecutor가 IExecutor Protocol을 만족한다."""
        factory, _, _ = _make_session_factory()
        assert isinstance(PaperExecutor(factory, 1), IExecutor)


class TestPaperExecutorRecordEntry:
    @pytest.mark.asyncio
    async def test_record_paper_entry_inserts_row(self):
        """record_paper_entry — DB 세션에 row add + commit."""
        factory, session, row = _make_session_factory(paper_trade_row_id=42)
        executor = PaperExecutor(factory, strategy_id=7)

        # flush 후 row.id가 세팅된다고 시뮬레이션
        async def _flush_side_effect():
            # PaperTrade row의 id는 mock이 이미 42로 설정됨
            pass
        session.flush.side_effect = _flush_side_effect

        with patch("core.execution.executor.PaperTrade") as MockPaperTrade:
            mock_row = MagicMock()
            mock_row.id = 42
            MockPaperTrade.return_value = mock_row

            result = await executor.record_paper_entry(
                strategy_id=7, pair="usd_jpy", direction="long", entry_price=150.0
            )

        assert result == 42
        session.add.assert_called_once()
        session.flush.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_paper_entry_db_error_returns_none(self):
        """DB 오류 시 None 반환 (예외 전파 없음)."""
        factory = MagicMock()
        session = AsyncMock()
        session.add = MagicMock(side_effect=Exception("DB error"))
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        factory.return_value = session

        executor = PaperExecutor(factory, strategy_id=7)
        result = await executor.record_paper_entry(7, "usd_jpy", "long", 150.0)
        assert result is None


class TestPaperExecutorRecordExit:
    @pytest.mark.asyncio
    async def test_record_paper_exit_updates_row(self):
        """record_paper_exit — 기존 row 업데이트 후 commit."""
        factory, session, row = _make_session_factory()
        executor = PaperExecutor(factory, strategy_id=7)

        await executor.record_paper_exit(
            paper_trade_id=42,
            exit_price=152.0,
            exit_reason="near_lower_exit",
            entry_price=150.0,
            invest_jpy=10000.0,
            direction="long",
        )

        session.commit.assert_called_once()
        assert row.exit_price == 152.0
        assert row.exit_reason == "near_lower_exit"

    @pytest.mark.asyncio
    async def test_record_paper_exit_row_not_found_noop(self):
        """청산할 row 없으면 commit 호출 없이 반환."""
        factory = MagicMock()
        session = AsyncMock()
        # scalars().first() → None
        mock_scalars = MagicMock()
        mock_scalars.first = MagicMock(return_value=None)
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=mock_scalars)
        session.execute = AsyncMock(return_value=mock_result)
        session.commit = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        factory.return_value = session

        executor = PaperExecutor(factory, strategy_id=7)
        await executor.record_paper_exit(99, 151.0, "timeout", 150.0, 10000.0, "long")

        session.commit.assert_not_called()


# ── PnL 계산 ──────────────────────────────────────────────────

class TestCalcPnlPct:
    def test_long_profit(self):
        assert abs(_calc_pnl_pct(150.0, 153.0, "long") - 2.0) < 0.01

    def test_long_loss(self):
        assert abs(_calc_pnl_pct(150.0, 147.0, "long") - (-2.0)) < 0.01

    def test_short_profit(self):
        """숏: 가격 하락 = 이익."""
        assert abs(_calc_pnl_pct(150.0, 147.0, "short") - 2.0) < 0.01

    def test_short_loss(self):
        """숏: 가격 상승 = 손실."""
        assert abs(_calc_pnl_pct(150.0, 153.0, "short") - (-2.0)) < 0.01

    def test_zero_entry_price_returns_zero(self):
        assert _calc_pnl_pct(0.0, 150.0, "long") == 0.0


# ── create_executor 팩토리 ─────────────────────────────────────

class TestCreateExecutor:
    def test_active_strategy_returns_real_executor(self):
        """is_proposed=False → RealExecutor."""
        factory = MagicMock()
        ex = create_executor(factory, strategy_id=1, is_proposed=False)
        assert isinstance(ex, RealExecutor)

    def test_proposed_strategy_returns_paper_executor(self):
        """is_proposed=True → PaperExecutor."""
        factory = MagicMock()
        ex = create_executor(factory, strategy_id=7, is_proposed=True)
        assert isinstance(ex, PaperExecutor)
        assert ex._strategy_id == 7
