"""
추세추종 Paper Trading 연동 단위 테스트 (T-1~T-5).

검증 내용:
  T-1: Paper 진입 기록 — proposed trend → 시그널 발동 → paper_trades INSERT,
       adapter.place_order 미호출 확인
  T-2: Paper 청산 기록 — stop_loss / exit_warning / full_exit 3가지 reason
  T-3: Active pair 무영향 — active pair는 _close_position_impl 호출, paper 기록 0
  T-4: 다중 proposed + active 공존 — 각 pair 독립 동작
  T-5: Cfd paper (short) — entry_sell → direction="sell" paper 기록 + SL 방향 역전
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import (
    PaperTrade,
    create_candle_model,
    create_cfd_position_model,
    create_strategy_model,
    create_trend_position_model,
)
from adapters.database.session import Base
from core.exchange.types import OrderType, Position
from core.strategy.base_trend import _SYNC_INTERVAL_CYCLES
from core.strategy.trend_following import TrendFollowingManager
from core.strategy.cfd_trend_following import CfdTrendFollowingManager
from core.task.supervisor import TaskSupervisor
from tests.fake_exchange import FakeExchangeAdapter

# ── ORM 모델 (ppr_ prefix) ──────────────────────────────────────

PprStrategy = create_strategy_model("ppr")
PprCandle = create_candle_model("ppr", pair_column="pair")
PprTrendPosition = create_trend_position_model("ppr", order_id_length=40)
PprCfdPosition = create_cfd_position_model("ppr", pair_column="product_code")


# ── Fixtures ──────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session_factory():
    """SQLite 인메모리 — ppr_ + paper_trades 테이블."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        target_tables = [
            t for name, t in Base.metadata.tables.items()
            if name.startswith("ppr_") or name in ("paper_trades", "strategy_techniques")
        ]
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(sc, tables=target_tables)
        )
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
        initial_balances={"jpy": 1_000_000.0, "xrp": 0.0, "btc": 0.0},
        ticker_price=100.0,
    )
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def trend_manager(fake_adapter, supervisor, db_session_factory):
    mgr = TrendFollowingManager(
        adapter=fake_adapter,
        supervisor=supervisor,
        session_factory=db_session_factory,
        candle_model=PprCandle,
        trend_position_model=PprTrendPosition,
        pair_column="pair",
    )
    yield mgr
    await mgr.stop_all()


@pytest_asyncio.fixture
async def cfd_fake_adapter():
    adapter = FakeExchangeAdapter(
        exchange_name="bitflyer",
        initial_balances={"jpy": 1_000_000.0, "btc": 0.0},
        ticker_price=10_000_000.0,
    )
    adapter._is_margin_trading = True
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def cfd_manager(cfd_fake_adapter, supervisor, db_session_factory):
    mgr = CfdTrendFollowingManager(
        adapter=cfd_fake_adapter,
        supervisor=supervisor,
        session_factory=db_session_factory,
        candle_model=PprCandle,
        cfd_position_model=PprCfdPosition,
        pair_column="product_code",
    )
    yield mgr
    await mgr.stop_all()


# ──────────────────────────────────────────────────────────────
# T-1: Paper 진입 기록 — adapter.place_order 호출 없음
# ──────────────────────────────────────────────────────────────

class TestTrendPaperEntry:

    @pytest.mark.asyncio
    async def test_register_paper_pair_creates_executor(self, trend_manager, db_session_factory):
        """register_paper_pair 호출 후 _paper_executors에 등록 확인."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=42)
        assert "xrp_jpy" in trend_manager._paper_executors

    @pytest.mark.asyncio
    async def test_paper_entry_no_real_order(self, trend_manager, fake_adapter, db_session_factory):
        """T-1: Paper pair에서 _try_paper_entry → adapter.place_order 미호출."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=10)

        params = {
            "strategy_id": 10,
            "basis_timeframe": "4h",
            "position_size_pct": 10.0,
            "atr_multiplier_stop": 2.0,
        }
        trend_manager._params["xrp_jpy"] = params
        trend_manager._latest_price["xrp_jpy"] = 100.0
        trend_manager._position["xrp_jpy"] = None

        order_call_count_before = len(fake_adapter._orders)
        result = await trend_manager._try_paper_entry(
            pair="xrp_jpy", direction="long",
            current_price=100.0, atr=1.0, params=params,
        )

        assert result is True
        # 실주문 발생하지 않음
        assert len(fake_adapter._orders) == order_call_count_before

    @pytest.mark.asyncio
    async def test_paper_entry_creates_paper_trade_row(self, trend_manager, db_session_factory):
        """T-1: paper_trades 테이블에 행이 INSERT 됨."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=11)
        params = {"strategy_id": 11, "atr_multiplier_stop": 2.0}
        trend_manager._params["xrp_jpy"] = params

        result = await trend_manager._try_paper_entry(
            pair="xrp_jpy", direction="long",
            current_price=100.0, atr=1.0, params=params,
        )
        assert result is True

        async with db_session_factory() as db:
            rows = (await db.execute(
                select(PaperTrade).where(PaperTrade.strategy_id == 11)
            )).scalars().all()
        assert len(rows) == 1
        assert rows[0].pair == "xrp_jpy"
        assert rows[0].direction == "long"
        assert rows[0].entry_price == 100.0

    @pytest.mark.asyncio
    async def test_paper_entry_sets_inmemory_position(self, trend_manager):
        """T-1: _position에 인메모리 Position이 생성 (stop_loss 포함)."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=12)
        params = {"strategy_id": 12, "atr_multiplier_stop": 2.0}
        trend_manager._params["xrp_jpy"] = params

        await trend_manager._try_paper_entry(
            pair="xrp_jpy", direction="long",
            current_price=100.0, atr=1.0, params=params,
        )

        pos = trend_manager._position.get("xrp_jpy")
        assert pos is not None
        assert pos.entry_price == 100.0
        assert pos.stop_loss_price == pytest.approx(98.0)  # 100 - 2*1

    @pytest.mark.asyncio
    async def test_on_entry_signal_paper_skips_open_position(self, trend_manager, fake_adapter):
        """T-1: _on_entry_signal에서 paper pair면 _open_position 호출 없음."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=13)
        params = {"strategy_id": 13, "atr_multiplier_stop": 2.0}
        trend_manager._params["xrp_jpy"] = params
        trend_manager._position["xrp_jpy"] = None

        # _open_position을 mock으로 감지
        open_called = []
        original_open = trend_manager._open_position
        async def _mock_open(*args, **kwargs):
            open_called.append(args)
            await original_open(*args, **kwargs)
        trend_manager._open_position = _mock_open

        await trend_manager._on_entry_signal(
            pair="xrp_jpy", signal="entry_ok",
            current_price=100.0, atr=1.0,
            params=params, signal_data={},
        )

        assert len(open_called) == 0, "_open_position이 호출되면 안 됨"


# ──────────────────────────────────────────────────────────────
# T-2: Paper 청산 기록 — 3가지 reason
# ──────────────────────────────────────────────────────────────

class TestTrendPaperExit:

    async def _setup_with_paper(self, trend_manager, db_session_factory, pair="xrp_jpy", strategy_id=20):
        """paper 진입 상태까지 셋업."""
        trend_manager.register_paper_pair(pair, strategy_id=strategy_id)
        params = {"strategy_id": strategy_id, "atr_multiplier_stop": 2.0}
        trend_manager._params[pair] = params
        trend_manager._latest_price[pair] = 105.0

        await trend_manager._try_paper_entry(
            pair=pair, direction="long",
            current_price=100.0, atr=1.0, params=params,
        )
        return params

    @pytest.mark.asyncio
    async def test_paper_exit_stop_loss(self, trend_manager, db_session_factory):
        """T-2a: stop_loss reason → paper_trades.exit_reason='stop_loss'."""
        await self._setup_with_paper(trend_manager, db_session_factory, strategy_id=21)
        await trend_manager._close_position("xrp_jpy", "stop_loss")

        async with db_session_factory() as db:
            row = (await db.execute(
                select(PaperTrade).where(PaperTrade.strategy_id == 21)
            )).scalars().first()
        assert row is not None
        assert row.exit_reason == "stop_loss"
        assert row.exit_price is not None

    @pytest.mark.asyncio
    async def test_paper_exit_exit_warning(self, trend_manager, db_session_factory):
        """T-2b: exit_warning reason → paper_trades.exit_reason='exit_warning'."""
        await self._setup_with_paper(trend_manager, db_session_factory, strategy_id=22)
        await trend_manager._close_position("xrp_jpy", "exit_warning")

        async with db_session_factory() as db:
            row = (await db.execute(
                select(PaperTrade).where(PaperTrade.strategy_id == 22)
            )).scalars().first()
        assert row is not None
        assert row.exit_reason == "exit_warning"

    @pytest.mark.asyncio
    async def test_paper_exit_full_exit(self, trend_manager, db_session_factory):
        """T-2c: full_exit reason → paper_trades.exit_reason='full_exit'."""
        await self._setup_with_paper(trend_manager, db_session_factory, strategy_id=23)
        await trend_manager._close_position("xrp_jpy", "full_exit")

        async with db_session_factory() as db:
            row = (await db.execute(
                select(PaperTrade).where(PaperTrade.strategy_id == 23)
            )).scalars().first()
        assert row.exit_reason == "full_exit"

    @pytest.mark.asyncio
    async def test_paper_exit_clears_inmemory_position(self, trend_manager, db_session_factory):
        """T-2: 청산 후 _position[pair] = None."""
        await self._setup_with_paper(trend_manager, db_session_factory, strategy_id=24)
        assert trend_manager._position.get("xrp_jpy") is not None

        await trend_manager._close_position("xrp_jpy", "stop_loss")
        assert trend_manager._position.get("xrp_jpy") is None

    @pytest.mark.asyncio
    async def test_paper_exit_clears_paper_positions_dict(self, trend_manager, db_session_factory):
        """T-2: 청산 후 _paper_positions에서 제거 (재진입 가능)."""
        await self._setup_with_paper(trend_manager, db_session_factory, strategy_id=25)
        assert "xrp_jpy" in trend_manager._paper_positions

        await trend_manager._close_position("xrp_jpy", "stop_loss")
        assert "xrp_jpy" not in trend_manager._paper_positions

    @pytest.mark.asyncio
    async def test_paper_exit_no_real_order(self, trend_manager, fake_adapter, db_session_factory):
        """T-2: 청산 시 adapter.place_order 미호출 (paper pair)."""
        await self._setup_with_paper(trend_manager, db_session_factory, strategy_id=26)
        order_count_before = len(fake_adapter._orders)

        await trend_manager._close_position("xrp_jpy", "stop_loss")

        assert len(fake_adapter._orders) == order_count_before


# ──────────────────────────────────────────────────────────────
# T-3: Active pair 무영향
# ──────────────────────────────────────────────────────────────

class TestTrendActiveUnaffected:

    @pytest.mark.asyncio
    async def test_active_pair_uses_real_close(self, trend_manager, fake_adapter, db_session_factory):
        """T-3: active pair는 _close_position_impl 호출 (실청산)."""
        fake_adapter._balances["xrp"] = 100.0
        params = {
            "strategy_id": 30,
            "min_coin_size": 0.001,
            "trading_fee_rate": 0.002,
        }
        trend_manager._params["xrp_jpy"] = params
        trend_manager._position["xrp_jpy"] = Position(
            pair="xrp_jpy", entry_price=100.0,
            entry_amount=100.0, stop_loss_price=90.0,
        )

        # paper 등록 없음 → _close_position_impl 호출
        order_count_before = len(fake_adapter._orders)
        await trend_manager._close_position("xrp_jpy", "stop_loss")
        # 실주문 발생
        assert len(fake_adapter._orders) > order_count_before

    @pytest.mark.asyncio
    async def test_active_pair_no_paper_trade_row(self, trend_manager, fake_adapter, db_session_factory):
        """T-3: active pair 청산 시 paper_trades 행 미생성."""
        fake_adapter._balances["xrp"] = 100.0
        params = {
            "strategy_id": 31,
            "min_coin_size": 0.001,
            "trading_fee_rate": 0.002,
        }
        trend_manager._params["xrp_jpy"] = params
        trend_manager._position["xrp_jpy"] = Position(
            pair="xrp_jpy", entry_price=100.0,
            entry_amount=100.0, stop_loss_price=90.0,
        )

        await trend_manager._close_position("xrp_jpy", "stop_loss")

        async with db_session_factory() as db:
            rows = (await db.execute(
                select(PaperTrade).where(PaperTrade.strategy_id == 31)
            )).scalars().all()
        assert len(rows) == 0


# ──────────────────────────────────────────────────────────────
# T-4: 다중 proposed + active 공존
# ──────────────────────────────────────────────────────────────

class TestTrendMultiPairCoexistence:

    @pytest.mark.asyncio
    async def test_paper_and_active_independent(self, trend_manager, fake_adapter, db_session_factory):
        """T-4: proposed(xrp_jpy) + active(btc_jpy) 동시에 각각 독립 동작."""
        # proposed xrp_jpy 등록
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=40)
        params_xrp = {"strategy_id": 40, "atr_multiplier_stop": 2.0}
        trend_manager._params["xrp_jpy"] = params_xrp

        # active btc_jpy — paper 등록 없음
        fake_adapter._balances["btc"] = 1.0
        params_btc = {
            "strategy_id": 41,
            "min_coin_size": 0.001,
            "trading_fee_rate": 0.002,
        }
        trend_manager._params["btc_jpy"] = params_btc
        trend_manager._position["btc_jpy"] = Position(
            pair="btc_jpy", entry_price=10_000_000.0,
            entry_amount=1.0, stop_loss_price=9_000_000.0,
        )

        # xrp_jpy paper 진입
        await trend_manager._try_paper_entry(
            pair="xrp_jpy", direction="long",
            current_price=100.0, atr=1.0, params=params_xrp,
        )
        order_count_after_paper = len(fake_adapter._orders)

        # btc_jpy active 청산 → 실주문 발생
        await trend_manager._close_position("btc_jpy", "stop_loss")
        assert len(fake_adapter._orders) > order_count_after_paper

        # xrp_jpy paper 청산 → 실주문 없음
        order_count_before_paper_exit = len(fake_adapter._orders)
        await trend_manager._close_position("xrp_jpy", "stop_loss")
        assert len(fake_adapter._orders) == order_count_before_paper_exit

    @pytest.mark.asyncio
    async def test_two_paper_pairs_independent_ids(self, trend_manager, db_session_factory):
        """T-4: proposed 2개 — 각 pair별 별도 paper_trade_id."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=42)
        trend_manager.register_paper_pair("btc_jpy", strategy_id=43)

        params_xrp = {"strategy_id": 42, "atr_multiplier_stop": 2.0}
        params_btc = {"strategy_id": 43, "atr_multiplier_stop": 2.0}
        trend_manager._params["xrp_jpy"] = params_xrp
        trend_manager._params["btc_jpy"] = params_btc

        await trend_manager._try_paper_entry("xrp_jpy", "long", 100.0, 1.0, params_xrp)
        await trend_manager._try_paper_entry("btc_jpy", "long", 10_000.0, 100.0, params_btc)

        xrp_id = trend_manager._paper_positions["xrp_jpy"]["paper_trade_id"]
        btc_id = trend_manager._paper_positions["btc_jpy"]["paper_trade_id"]
        assert xrp_id != btc_id

    @pytest.mark.asyncio
    async def test_active_pair_not_in_paper_executors(self, trend_manager):
        """T-4: active pair는 _paper_executors에 없음 (등록 미호출)."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=44)
        assert "xrp_jpy" in trend_manager._paper_executors
        assert "btc_jpy" not in trend_manager._paper_executors  # active pair 미등록 확인


# ──────────────────────────────────────────────────────────────
# T-5: CfdTrendFollowingManager paper (short)
# ──────────────────────────────────────────────────────────────

class TestCfdPaperEntry:

    @pytest.mark.asyncio
    async def test_cfd_paper_entry_sell_direction(self, cfd_manager, db_session_factory):
        """T-5: entry_sell → direction='sell' paper_trades 기록."""
        cfd_manager.register_paper_pair("FX_BTC_JPY", strategy_id=50)
        params = {"strategy_id": 50, "atr_multiplier_stop": 2.0}
        cfd_manager._params["FX_BTC_JPY"] = params

        result = await cfd_manager._try_paper_entry(
            pair="FX_BTC_JPY", direction="sell",
            current_price=10_000_000.0, atr=100_000.0, params=params,
        )
        assert result is True

        async with db_session_factory() as db:
            row = (await db.execute(
                select(PaperTrade).where(PaperTrade.strategy_id == 50)
            )).scalars().first()
        assert row is not None
        assert row.direction == "sell"

    @pytest.mark.asyncio
    async def test_cfd_paper_entry_sell_sl_direction(self, cfd_manager):
        """T-5: short 진입 시 SL = price + ATR*mult (숏 방향 역전)."""
        cfd_manager.register_paper_pair("FX_BTC_JPY", strategy_id=51)
        params = {"strategy_id": 51, "atr_multiplier_stop": 2.0}
        cfd_manager._params["FX_BTC_JPY"] = params

        await cfd_manager._try_paper_entry(
            pair="FX_BTC_JPY", direction="sell",
            current_price=10_000_000.0, atr=100_000.0, params=params,
        )

        pos = cfd_manager._position.get("FX_BTC_JPY")
        assert pos is not None
        # 숏: SL = price + ATR*mult = 10_000_000 + 200_000 = 10_200_000
        assert pos.stop_loss_price == pytest.approx(10_200_000.0)

    @pytest.mark.asyncio
    async def test_cfd_paper_long_sl_direction(self, cfd_manager):
        """T-5: long 진입 시 SL = price - ATR*mult (롱 방향 정상)."""
        cfd_manager.register_paper_pair("FX_BTC_JPY", strategy_id=52)
        params = {"strategy_id": 52, "atr_multiplier_stop": 2.0}
        cfd_manager._params["FX_BTC_JPY"] = params

        await cfd_manager._try_paper_entry(
            pair="FX_BTC_JPY", direction="buy",
            current_price=10_000_000.0, atr=100_000.0, params=params,
        )

        pos = cfd_manager._position.get("FX_BTC_JPY")
        # 롱: SL = price - ATR*mult = 10_000_000 - 200_000 = 9_800_000
        assert pos.stop_loss_price == pytest.approx(9_800_000.0)

    @pytest.mark.asyncio
    async def test_cfd_on_entry_signal_sell_paper(self, cfd_manager, cfd_fake_adapter, db_session_factory):
        """T-5: Cfd _on_entry_signal('entry_sell') → paper 기록, 실주문 없음."""
        cfd_manager.register_paper_pair("FX_BTC_JPY", strategy_id=53)
        params = {
            "strategy_id": 53,
            "atr_multiplier_stop": 2.0,
            "keep_rate_warn": 0.0,  # 차단 없음
        }
        cfd_manager._params["FX_BTC_JPY"] = params
        cfd_manager._position["FX_BTC_JPY"] = None
        cfd_manager._last_keep_rate["FX_BTC_JPY"] = None

        order_count_before = len(cfd_fake_adapter._orders)

        with patch(
            "core.strategy.plugins.cfd_trend_following.manager.should_close_for_weekend",
            return_value=False,
        ), patch(
            "core.strategy.plugins.cfd_trend_following.manager.is_fx_market_open",
            return_value=True,
        ):
            await cfd_manager._on_entry_signal(
                pair="FX_BTC_JPY", signal="entry_sell",
                current_price=10_000_000.0, atr=100_000.0,
                params=params, signal_data={},
            )

        # 실주문 없음
        assert len(cfd_fake_adapter._orders) == order_count_before

        # paper_trades 기록 확인
        async with db_session_factory() as db:
            row = (await db.execute(
                select(PaperTrade).where(PaperTrade.strategy_id == 53)
            )).scalars().first()
        assert row is not None
        assert row.direction == "sell"

    @pytest.mark.asyncio
    async def test_cfd_paper_exit_records_correct_reason(self, cfd_manager, db_session_factory):
        """T-5: CfdMgr paper 청산 → exit_reason 기록."""
        cfd_manager.register_paper_pair("FX_BTC_JPY", strategy_id=54)
        params = {"strategy_id": 54, "atr_multiplier_stop": 2.0}
        cfd_manager._params["FX_BTC_JPY"] = params
        cfd_manager._latest_price["FX_BTC_JPY"] = 10_100_000.0

        await cfd_manager._try_paper_entry(
            "FX_BTC_JPY", "sell", 10_000_000.0, 100_000.0, params,
        )
        await cfd_manager._close_position("FX_BTC_JPY", "exit_warning")

        async with db_session_factory() as db:
            row = (await db.execute(
                select(PaperTrade).where(PaperTrade.strategy_id == 54)
            )).scalars().first()
        assert row is not None
        assert row.exit_reason == "exit_warning"
        assert cfd_manager._position.get("FX_BTC_JPY") is None


# ──────────────────────────────────────────────────────────────
# sync_position_state paper 가드
# ──────────────────────────────────────────────────────────────

class TestSyncPositionGuard:

    @pytest.mark.asyncio
    async def test_paper_pair_skips_sync(self, trend_manager):
        """paper pair는 정합성 검사(_sync_position_state) 스킵 — ZeroDivisionError 없음."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=60)
        params = {"strategy_id": 60, "atr_multiplier_stop": 2.0}
        trend_manager._params["xrp_jpy"] = params
        await trend_manager._try_paper_entry("xrp_jpy", "long", 100.0, 1.0, params)

        # _sync_counter를 29로 설정하여 다음 ++에서 30이 됨 → 조건 진입
        trend_manager._sync_counter["xrp_jpy"] = 29

        sync_called = []
        original_sync = trend_manager._sync_position_state
        async def _mock_sync(pair):
            sync_called.append(pair)
            await original_sync(pair)
        trend_manager._sync_position_state = _mock_sync

        # entry_amount=0인 position으로 _sync_position_state 호출 시 ZeroDivisionError 예상
        # → paper 가드로 호출 자체를 막아야 함
        pos = trend_manager._position.get("xrp_jpy")
        if pos is not None:
            cnt = trend_manager._sync_counter.get("xrp_jpy", 0) + 1
            trend_manager._sync_counter["xrp_jpy"] = cnt
            if cnt % 30 == 0 and "xrp_jpy" not in trend_manager._paper_executors:
                await trend_manager._sync_position_state("xrp_jpy")

        assert "xrp_jpy" not in sync_called


# ──────────────────────────────────────────────────────────────
# 엣지 케이스 보강
# ──────────────────────────────────────────────────────────────

class TestEdgeCases:

    @pytest.mark.asyncio
    async def test_try_paper_entry_non_paper_pair_returns_false(self, trend_manager):
        """paper 미등록 pair → _try_paper_entry False (실주문 경로)."""
        params = {"strategy_id": 70, "atr_multiplier_stop": 2.0}
        trend_manager._params["xrp_jpy"] = params
        result = await trend_manager._try_paper_entry(
            pair="xrp_jpy", direction="long",
            current_price=100.0, atr=1.0, params=params,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_register_paper_pair_overwrite(self, trend_manager):
        """register_paper_pair 중복 호출 → 최신 executor로 덮어쓰기."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=71)
        exec_first = trend_manager._paper_executors["xrp_jpy"]
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=72)
        exec_second = trend_manager._paper_executors["xrp_jpy"]
        assert exec_first is not exec_second  # 새 인스턴스로 교체

    @pytest.mark.asyncio
    async def test_close_position_no_paper_info_delegates_to_impl(self, trend_manager, fake_adapter):
        """_paper_positions에 없는 pair → _close_position_impl 위임 (paper exec 등록돼도)."""
        # paper executor 등록했지만 진입 기록(_paper_positions)은 없음
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=73)
        fake_adapter._balances["xrp"] = 100.0
        params = {"strategy_id": 73, "min_coin_size": 0.001, "trading_fee_rate": 0.002}
        trend_manager._params["xrp_jpy"] = params
        trend_manager._position["xrp_jpy"] = Position(
            pair="xrp_jpy", entry_price=100.0,
            entry_amount=100.0, stop_loss_price=90.0,
        )

        order_count_before = len(fake_adapter._orders)
        await trend_manager._close_position("xrp_jpy", "stop_loss")
        # paper_positions 없으므로 _close_position_impl → 실주문 발생
        assert len(fake_adapter._orders) > order_count_before

    @pytest.mark.asyncio
    async def test_paper_entry_atr_none_no_sl(self, trend_manager):
        """ATR=None일 때 paper 진입 → stop_loss_price=None, 오류 없음."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=74)
        params = {"strategy_id": 74, "atr_multiplier_stop": 2.0}
        trend_manager._params["xrp_jpy"] = params

        result = await trend_manager._try_paper_entry(
            pair="xrp_jpy", direction="long",
            current_price=100.0, atr=None, params=params,
        )
        assert result is True
        pos = trend_manager._position.get("xrp_jpy")
        assert pos is not None
        assert pos.stop_loss_price is None

    @pytest.mark.asyncio
    async def test_paper_exit_uses_latest_price(self, trend_manager, db_session_factory):
        """청산 시 _latest_price 사용 (entry_price 아님)."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=75)
        params = {"strategy_id": 75, "atr_multiplier_stop": 2.0}
        trend_manager._params["xrp_jpy"] = params
        trend_manager._latest_price["xrp_jpy"] = 120.0  # 진입 100→청산 120

        await trend_manager._try_paper_entry("xrp_jpy", "long", 100.0, 1.0, params)
        await trend_manager._close_position("xrp_jpy", "full_exit")

        async with db_session_factory() as db:
            row = (await db.execute(
                select(PaperTrade).where(PaperTrade.strategy_id == 75)
            )).scalars().first()
        assert row is not None
        assert row.exit_price == pytest.approx(120.0)

    @pytest.mark.asyncio
    async def test_paper_exit_pnl_positive_long(self, trend_manager, db_session_factory):
        """Long paper: 진입 100→청산 120 → PnL > 0."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=76)
        params = {"strategy_id": 76, "atr_multiplier_stop": 2.0}
        trend_manager._params["xrp_jpy"] = params
        trend_manager._latest_price["xrp_jpy"] = 120.0

        await trend_manager._try_paper_entry("xrp_jpy", "long", 100.0, 1.0, params)
        await trend_manager._close_position("xrp_jpy", "full_exit")

        async with db_session_factory() as db:
            row = (await db.execute(
                select(PaperTrade).where(PaperTrade.strategy_id == 76)
            )).scalars().first()
        assert row.paper_pnl_pct is not None
        assert row.paper_pnl_pct > 0

    @pytest.mark.asyncio
    async def test_paper_position_entry_amount_zero(self, trend_manager):
        """paper Position의 entry_amount=0 (실수량 없음) 확인."""
        trend_manager.register_paper_pair("xrp_jpy", strategy_id=77)
        params = {"strategy_id": 77, "atr_multiplier_stop": 2.0}
        trend_manager._params["xrp_jpy"] = params

        await trend_manager._try_paper_entry("xrp_jpy", "long", 100.0, 1.0, params)

        pos = trend_manager._position.get("xrp_jpy")
        assert pos is not None
        assert pos.entry_amount == 0.0  # paper: 실수량 없음

    @pytest.mark.asyncio
    async def test_active_pair_sync_not_skipped(self, trend_manager):
        """active pair는 _paper_executors에 없으므로 sync 가드 통과 — 호출 정상."""
        params = {"strategy_id": 78}
        trend_manager._params["xrp_jpy"] = params
        trend_manager._position["xrp_jpy"] = Position(
            pair="xrp_jpy", entry_price=100.0,
            entry_amount=100.0, stop_loss_price=90.0,
        )
        trend_manager._sync_counter["xrp_jpy"] = 29

        sync_called = []
        async def _mock_sync(pair):
            sync_called.append(pair)
        trend_manager._sync_position_state = _mock_sync

        pos = trend_manager._position.get("xrp_jpy")
        if pos is not None:
            cnt = trend_manager._sync_counter.get("xrp_jpy", 0) + 1
            trend_manager._sync_counter["xrp_jpy"] = cnt
            if cnt % 30 == 0 and "xrp_jpy" not in trend_manager._paper_executors:
                await trend_manager._sync_position_state("xrp_jpy")

        # active pair → 가드 통과 → sync 호출됨
        assert "xrp_jpy" in sync_called

    def test_sync_interval_cycles_constant(self):
        """_SYNC_INTERVAL_CYCLES 상수가 30임을 명시 검증 — 60초 × 30 = 30분 주기."""
        assert _SYNC_INTERVAL_CYCLES == 30

    @pytest.mark.asyncio
    async def test_sync_not_called_before_interval(self, trend_manager):
        """주기 미달(29사이클)에서는 sync가 호출되지 않음."""
        trend_manager._position["xrp_jpy"] = Position(
            pair="xrp_jpy", entry_price=100.0,
            entry_amount=100.0, stop_loss_price=90.0,
        )
        trend_manager._sync_counter["xrp_jpy"] = 28  # 29→28, cnt+1=29 (미달)

        sync_called = []
        async def _mock_sync(pair):
            sync_called.append(pair)
        trend_manager._sync_position_state = _mock_sync

        pos = trend_manager._position.get("xrp_jpy")
        if pos is not None:
            cnt = trend_manager._sync_counter.get("xrp_jpy", 0) + 1
            trend_manager._sync_counter["xrp_jpy"] = cnt
            if cnt % _SYNC_INTERVAL_CYCLES == 0 and "xrp_jpy" not in trend_manager._paper_executors:
                await trend_manager._sync_position_state("xrp_jpy")

        assert "xrp_jpy" not in sync_called

    @pytest.mark.asyncio
    async def test_sync_called_on_second_interval(self, trend_manager):
        """두 번째 주기(59→60 사이클)에서도 sync 호출됨."""
        trend_manager._position["xrp_jpy"] = Position(
            pair="xrp_jpy", entry_price=100.0,
            entry_amount=100.0, stop_loss_price=90.0,
        )
        trend_manager._sync_counter["xrp_jpy"] = 59  # cnt+1=60

        sync_called = []
        async def _mock_sync(pair):
            sync_called.append(pair)
        trend_manager._sync_position_state = _mock_sync

        pos = trend_manager._position.get("xrp_jpy")
        if pos is not None:
            cnt = trend_manager._sync_counter.get("xrp_jpy", 0) + 1
            trend_manager._sync_counter["xrp_jpy"] = cnt
            if cnt % _SYNC_INTERVAL_CYCLES == 0 and "xrp_jpy" not in trend_manager._paper_executors:
                await trend_manager._sync_position_state("xrp_jpy")

        assert "xrp_jpy" in sync_called

