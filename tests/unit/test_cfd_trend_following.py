"""
CfdTrendFollowingManager 단위 테스트.

FakeExchangeAdapter (get_collateral/get_positions 포함) + SQLite 인메모리 DB.
실제 asyncio 태스크를 실행하지 않고, 개별 메서드를 직접 호출하여 테스트.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import create_candle_model, create_cfd_position_model, create_strategy_model
from adapters.database.session import Base
from core.exchange.types import Collateral, FxPosition, OrderType, Position
from core.strategy.cfd_trend_following import CfdTrendFollowingManager
from core.task.supervisor import TaskSupervisor
from tests.fake_exchange import FakeExchangeAdapter


# ── 테스트용 ORM 모델 (cft_ prefix) ───

CftStrategy = create_strategy_model("cft")
CftCandle = create_candle_model("cft", pair_column="product_code")
CftCfdPosition = create_cfd_position_model("cft", pair_column="product_code", order_id_length=40)


# ── Fixtures ──────────────────────────────────


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        tables = [
            Base.metadata.tables[t]
            for t in Base.metadata.tables
            if t.startswith("cft_") or t == "strategy_techniques"
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
        ticker_price=15_000_000.0,
    )
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def manager(fake_adapter, supervisor, db_session_factory):
    mgr = CfdTrendFollowingManager(
        adapter=fake_adapter,
        supervisor=supervisor,
        session_factory=db_session_factory,
        candle_model=CftCandle,
        cfd_position_model=CftCfdPosition,
        pair_column="product_code",
    )
    yield mgr
    await mgr.stop_all()


# ── 유틸리티 ──────────────────────────────────

_DEFAULT_PARAMS = {
    "basis_timeframe": "4h",
    "trading_style": "cfd_trend_following",
    "position_size_pct": 30,
    "max_leverage": 2.0,
    "keep_rate_warn": 250,
    "keep_rate_critical": 120,
    "max_holding_hours": 72,
    "atr_multiplier_stop": 2.0,
    "trailing_stop_atr_initial": 2.0,
    "trailing_stop_atr_mature": 1.2,
    "tighten_stop_atr": 1.0,
    "min_coin_size": 0.001,
    "min_order_jpy": 500,
    "max_slippage_pct": 0.5,
}


async def _seed_candles(
    db_session_factory,
    product_code: str = "FX_BTC_JPY",
    timeframe: str = "4h",
    count: int = 30,
    base_price: float = 15_000_000.0,
    trend_up: bool = True,
):
    async with db_session_factory() as db:
        now = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(count):
            if trend_up:
                close = base_price + i * 50_000
            else:
                close = base_price - i * 50_000
            open_time = now - timedelta(hours=4 * (count - i))
            close_time = now - timedelta(hours=4 * (count - i - 1))
            candle = CftCandle(
                product_code=product_code,
                timeframe=timeframe,
                open_time=open_time,
                close_time=close_time,
                open=close - 10000,
                high=close + 50000,
                low=close - 50000,
                close=close,
                volume=100,
                tick_count=50,
                is_complete=True,
            )
            db.add(candle)
            await db.flush()
        await db.commit()


# ──────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────


class TestCfdLifecycle:

    @pytest.mark.asyncio
    async def test_start_registers_tasks(self, manager, supervisor):
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)
        assert supervisor.is_running("cfd_candle:FX_BTC_JPY")
        assert supervisor.is_running("cfd_stoploss:FX_BTC_JPY")

    @pytest.mark.asyncio
    async def test_stop_removes_tasks(self, manager, supervisor):
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)
        await manager.stop("FX_BTC_JPY")
        assert not supervisor.is_running("cfd_candle:FX_BTC_JPY")
        assert not supervisor.is_running("cfd_stoploss:FX_BTC_JPY")

    @pytest.mark.asyncio
    async def test_running_pairs(self, manager):
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)
        assert "FX_BTC_JPY" in manager.running_pairs()

    @pytest.mark.asyncio
    async def test_stop_all(self, manager, supervisor):
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)
        await manager.stop_all()
        assert supervisor.alive_count == 0

    @pytest.mark.asyncio
    async def test_get_task_health(self, manager):
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)
        health = manager.get_task_health()
        assert "FX_BTC_JPY" in health
        assert "candle_monitor" in health["FX_BTC_JPY"]
        assert "stop_loss_monitor" in health["FX_BTC_JPY"]


# ──────────────────────────────────────────────
# Position Recovery
# ──────────────────────────────────────────────


class TestPositionRecovery:

    @pytest.mark.asyncio
    async def test_detect_existing_position(self, manager, fake_adapter):
        """getpositions에 포지션이 있으면 인메모리 복원."""
        fake_adapter.set_fx_positions([
            FxPosition(
                product_code="FX_BTC_JPY",
                side="BUY",
                price=15_000_000.0,
                size=0.01,
                pnl=5000.0,
                leverage=2.0,
                require_collateral=75000.0,
                swap_point_accumulate=-50.0,
                sfd=0.0,
                open_date="2026-03-19",
            ),
        ])
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)
        pos = manager.get_position("FX_BTC_JPY")
        assert pos is not None
        assert pos.entry_price == 15_000_000.0
        assert pos.entry_amount == 0.01
        assert pos.extra["side"] == "buy"

    @pytest.mark.asyncio
    async def test_no_position_recovers_none(self, manager, fake_adapter):
        """getpositions가 빈 배열이면 포지션 없음."""
        fake_adapter.set_fx_positions([])
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)
        assert manager.get_position("FX_BTC_JPY") is None


# ──────────────────────────────────────────────
# Open Position
# ──────────────────────────────────────────────


class TestOpenPosition:

    @pytest.mark.asyncio
    async def test_open_long_position(self, manager, fake_adapter, db_session_factory):
        """증거금 기반 롱 포지션 열기."""
        await manager.start("FX_BTC_JPY", {**_DEFAULT_PARAMS, "strategy_id": 1})
        await manager._open_position(
            "FX_BTC_JPY", "buy", 15_000_000.0, 500_000.0, {**_DEFAULT_PARAMS, "strategy_id": 1}
        )
        pos = manager.get_position("FX_BTC_JPY")
        assert pos is not None
        assert pos.entry_price > 0
        assert pos.extra["side"] == "buy"
        assert pos.stop_loss_price is not None

        # DB 확인
        async with db_session_factory() as db:
            result = await db.execute(select(CftCfdPosition))
            rec = result.scalars().first()
            assert rec is not None
            assert rec.status == "open"
            assert rec.side == "buy"

    @pytest.mark.asyncio
    async def test_open_skips_when_no_collateral(self, manager, fake_adapter):
        """여유 증거금이 없으면 진입 스킵."""
        fake_adapter.set_collateral(Collateral(
            collateral=100_000.0,
            open_position_pnl=0.0,
            require_collateral=100_000.0,
            keep_rate=100.0,
        ))
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)
        await manager._open_position(
            "FX_BTC_JPY", "buy", 15_000_000.0, 500_000.0, _DEFAULT_PARAMS
        )
        assert manager.get_position("FX_BTC_JPY") is None

    @pytest.mark.asyncio
    async def test_open_limits_leverage(self, manager, fake_adapter, db_session_factory):
        """max_leverage를 초과하면 수량이 줄어든다."""
        # 작은 담보에 높은 position_size_pct → 레버리지 제한 작동
        fake_adapter.set_collateral(Collateral(
            collateral=100_000.0,
            open_position_pnl=0.0,
            require_collateral=0.0,
            keep_rate=999.0,
        ))
        # position_size_pct=200 → invest_jpy=200K → coin_size=200K/15M=0.01333
        # eff_lev=0.01333*15M/100K=2.0 > max_leverage=1.5 → 제한 작동
        params = {**_DEFAULT_PARAMS, "position_size_pct": 200, "max_leverage": 1.5, "strategy_id": 1}
        await manager.start("FX_BTC_JPY", params)
        await manager._open_position("FX_BTC_JPY", "buy", 15_000_000.0, 500_000.0, params)

        pos = manager.get_position("FX_BTC_JPY")
        assert pos is not None
        # 레버리지 = pos.entry_amount * price / collateral ≤ 1.5
        eff_lev = pos.entry_amount * 15_000_000.0 / 100_000.0
        assert eff_lev <= 1.5 + 0.01  # 부동소수 오차 허용
        # 레버리지 제한이 실제로 수량을 줄였는지 확인
        unreduced_coin = 200_000.0 / 15_000_000.0  # 0.01333
        assert pos.entry_amount < unreduced_coin


# ──────────────────────────────────────────────
# Close Position
# ──────────────────────────────────────────────


class TestClosePosition:

    @pytest.mark.asyncio
    async def test_close_clears_position(self, manager, fake_adapter, db_session_factory):
        """청산 시 인메모리 포지션 제거 + DB 기록."""
        params = {**_DEFAULT_PARAMS, "strategy_id": 1}
        await manager.start("FX_BTC_JPY", params)
        await manager._open_position("FX_BTC_JPY", "buy", 15_000_000.0, 500_000.0, params)
        assert manager.get_position("FX_BTC_JPY") is not None

        await manager._close_position("FX_BTC_JPY", "stop_loss")
        assert manager.get_position("FX_BTC_JPY") is None

        async with db_session_factory() as db:
            result = await db.execute(select(CftCfdPosition))
            rec = result.scalars().first()
            assert rec is not None
            assert rec.status == "closed"
            assert rec.exit_reason == "stop_loss"

    @pytest.mark.asyncio
    async def test_close_noop_when_no_position(self, manager):
        """포지션 없을 때 청산은 no-op."""
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)
        await manager._close_position("FX_BTC_JPY", "stop_loss")
        # 예외 없이 정상 종료


# ──────────────────────────────────────────────
# Keep Rate
# ──────────────────────────────────────────────


class TestKeepRate:

    @pytest.mark.asyncio
    async def test_critical_keep_rate_forces_close(self, manager, fake_adapter, db_session_factory):
        """keep_rate < critical이면 긴급 청산."""
        params = {**_DEFAULT_PARAMS, "keep_rate_critical": 130, "strategy_id": 1}
        await manager.start("FX_BTC_JPY", params)
        await manager._open_position("FX_BTC_JPY", "buy", 15_000_000.0, 500_000.0, params)
        assert manager.get_position("FX_BTC_JPY") is not None

        # keep_rate를 위험 수준으로 설정
        fake_adapter.set_collateral(Collateral(
            collateral=50_000.0,
            open_position_pnl=-30_000.0,
            require_collateral=50_000.0,
            keep_rate=100.0,  # < 130
        ))
        kr = await manager._check_keep_rate("FX_BTC_JPY")
        assert kr == 100.0
        # 포지션이 청산됐는지 확인
        assert manager.get_position("FX_BTC_JPY") is None

    @pytest.mark.asyncio
    async def test_normal_keep_rate_no_action(self, manager, fake_adapter):
        """keep_rate가 정상이면 아무 조치 없음."""
        params = {**_DEFAULT_PARAMS, "keep_rate_critical": 130}
        await manager.start("FX_BTC_JPY", params)
        # 기본 collateral: keep_rate=999.0 — 정상
        kr = await manager._check_keep_rate("FX_BTC_JPY")
        assert kr == 999.0


# ──────────────────────────────────────────────
# Position Sync
# ──────────────────────────────────────────────


class TestPositionSync:

    @pytest.mark.asyncio
    async def test_sync_updates_on_drift(self, manager, fake_adapter, db_session_factory):
        """실 포지션과 인메모리가 1% 이상 괴리하면 갱신."""
        params = {**_DEFAULT_PARAMS, "strategy_id": 1}
        await manager.start("FX_BTC_JPY", params)
        await manager._open_position("FX_BTC_JPY", "buy", 15_000_000.0, 500_000.0, params)

        pos = manager.get_position("FX_BTC_JPY")
        assert pos is not None
        original_amount = pos.entry_amount

        # 실 포지션을 다르게 설정 (5% 차이)
        fake_adapter.set_fx_positions([
            FxPosition(
                product_code="FX_BTC_JPY",
                side="BUY",
                price=15_000_000.0,
                size=original_amount * 1.05,
                pnl=0.0,
                leverage=2.0,
                require_collateral=0.0,
                swap_point_accumulate=0.0,
                sfd=0.0,
                open_date="2026-03-20",
            ),
        ])
        await manager._sync_position_state("FX_BTC_JPY")
        updated_pos = manager.get_position("FX_BTC_JPY")
        assert updated_pos is not None
        assert abs(updated_pos.entry_amount - original_amount * 1.05) < 0.00001

    @pytest.mark.asyncio
    async def test_sync_clears_when_external_close(self, manager, fake_adapter, db_session_factory):
        """실 포지션이 0이면 인메모리 포지션 제거."""
        params = {**_DEFAULT_PARAMS, "strategy_id": 1}
        await manager.start("FX_BTC_JPY", params)
        await manager._open_position("FX_BTC_JPY", "buy", 15_000_000.0, 500_000.0, params)
        assert manager.get_position("FX_BTC_JPY") is not None

        fake_adapter.set_fx_positions([])
        await manager._sync_position_state("FX_BTC_JPY")
        assert manager.get_position("FX_BTC_JPY") is None


# ──────────────────────────────────────────────
# Stop Tightening
# ──────────────────────────────────────────────


class TestStopTightening:

    @pytest.mark.asyncio
    async def test_tighten_raises_stop_for_long(self, manager, fake_adapter, db_session_factory):
        """롱 포지션: 스탑 타이트닝은 스탑을 올린다."""
        params = {**_DEFAULT_PARAMS, "tighten_stop_atr": 1.0, "strategy_id": 1}
        await manager.start("FX_BTC_JPY", params)
        await manager._open_position("FX_BTC_JPY", "buy", 15_000_000.0, 500_000.0, params)

        pos = manager.get_position("FX_BTC_JPY")
        original_sl = pos.stop_loss_price

        await manager._apply_stop_tightening("FX_BTC_JPY", 16_000_000.0, 500_000.0, params)

        pos = manager.get_position("FX_BTC_JPY")
        assert pos.stop_tightened is True
        # 타이트닝 후 스탑 = 16_000_000 - 500_000 * 1.0 = 15_500_000
        assert pos.stop_loss_price == 15_500_000.0


# ──────────────────────────────────────────────
# Signal Computation
# ──────────────────────────────────────────────


class TestCfdSignal:

    @pytest.mark.asyncio
    async def test_compute_signal_with_candles(self, manager, db_session_factory):
        """캔들 데이터가 있을 때 시그널 계산 성공."""
        await _seed_candles(db_session_factory, count=30, trend_up=True)
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)

        result = await manager._compute_signal("FX_BTC_JPY", "4h", params=_DEFAULT_PARAMS)
        assert result is not None
        assert "signal" in result
        assert "current_price" in result
        assert "ema" in result

    @pytest.mark.asyncio
    async def test_compute_signal_insufficient_candles(self, manager, db_session_factory):
        """캔들 부족하면 None 반환."""
        await _seed_candles(db_session_factory, count=5)
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)

        result = await manager._compute_signal("FX_BTC_JPY", "4h", params=_DEFAULT_PARAMS)
        assert result is None


# ──────────────────────────────────────────────
# DB Record
# ──────────────────────────────────────────────


class TestDbRecord:

    @pytest.mark.asyncio
    async def test_record_open_creates_row(self, manager, db_session_factory):
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)
        rec_id = await manager._record_open(
            product_code="FX_BTC_JPY",
            side="buy",
            order_id="test-order-123",
            price=15_000_000.0,
            size=0.01,
            collateral_jpy=100_000.0,
            stop_loss_price=14_000_000.0,
            strategy_id=1,
        )
        assert rec_id is not None

        async with db_session_factory() as db:
            result = await db.execute(select(CftCfdPosition).where(CftCfdPosition.id == rec_id))
            rec = result.scalars().first()
            assert rec.side == "buy"
            assert rec.status == "open"
            assert float(rec.entry_price) == 15_000_000.0

    @pytest.mark.asyncio
    async def test_record_close_updates_row(self, manager, db_session_factory):
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)
        rec_id = await manager._record_open(
            product_code="FX_BTC_JPY",
            side="buy",
            order_id="open-001",
            price=15_000_000.0,
            size=0.01,
            collateral_jpy=100_000.0,
            stop_loss_price=14_000_000.0,
            strategy_id=1,
        )
        await manager._record_close(
            db_record_id=rec_id,
            product_code="FX_BTC_JPY",
            side="buy",
            order_id="close-001",
            price=15_500_000.0,
            size=0.01,
            reason="stop_loss",
            entry_price=15_000_000.0,
        )

        async with db_session_factory() as db:
            result = await db.execute(select(CftCfdPosition).where(CftCfdPosition.id == rec_id))
            rec = result.scalars().first()
            assert rec.status == "closed"
            assert rec.exit_reason == "stop_loss"
            # P&L: (15_500_000 - 15_000_000) * 0.01 = 5000
            assert float(rec.realized_pnl_jpy) == 5000.0

    @pytest.mark.asyncio
    async def test_record_close_short_pnl(self, manager, db_session_factory):
        """숏 포지션 P&L 계산 (진입가 - 청산가)."""
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)
        rec_id = await manager._record_open(
            product_code="FX_BTC_JPY",
            side="sell",
            order_id="open-002",
            price=15_000_000.0,
            size=0.01,
            collateral_jpy=100_000.0,
            stop_loss_price=16_000_000.0,
            strategy_id=1,
        )
        await manager._record_close(
            db_record_id=rec_id,
            product_code="FX_BTC_JPY",
            side="sell",
            order_id="close-002",
            price=14_500_000.0,
            size=0.01,
            reason="exit_warning",
            entry_price=15_000_000.0,
        )

        async with db_session_factory() as db:
            result = await db.execute(select(CftCfdPosition).where(CftCfdPosition.id == rec_id))
            rec = result.scalars().first()
            # 숏 P&L: (15m - 14.5m) * 0.01 = 5000
            assert float(rec.realized_pnl_jpy) == 5000.0


# ──────────────────────────────────────────────
# Short Entry / Close / Stop
# ──────────────────────────────────────────────


class TestShortPosition:

    @pytest.mark.asyncio
    async def test_open_short_position(self, manager, fake_adapter, db_session_factory):
        """숏 포지션 열기 — side=sell + stop_loss 가격 위에."""
        params = {**_DEFAULT_PARAMS, "strategy_id": 1}
        await manager.start("FX_BTC_JPY", params)
        await manager._open_position("FX_BTC_JPY", "sell", 15_000_000.0, 500_000.0, params)

        pos = manager.get_position("FX_BTC_JPY")
        assert pos is not None
        assert pos.extra["side"] == "sell"
        # 숏 스탑로스 = price + atr * mult = 15M + 500K * 2 = 16M
        assert pos.stop_loss_price == 16_000_000.0

        async with db_session_factory() as db:
            result = await db.execute(select(CftCfdPosition))
            rec = result.scalars().first()
            assert rec.side == "sell"
            assert rec.status == "open"

    @pytest.mark.asyncio
    async def test_close_short_position(self, manager, fake_adapter, db_session_factory):
        """숏 청산 — 반대 매매(MARKET_BUY) + 코인 수량 전달."""
        params = {**_DEFAULT_PARAMS, "strategy_id": 1}
        await manager.start("FX_BTC_JPY", params)
        await manager._open_position("FX_BTC_JPY", "sell", 15_000_000.0, 500_000.0, params)
        assert manager.get_position("FX_BTC_JPY") is not None

        await manager._close_position("FX_BTC_JPY", "exit_warning")
        assert manager.get_position("FX_BTC_JPY") is None

        async with db_session_factory() as db:
            result = await db.execute(select(CftCfdPosition))
            rec = result.scalars().first()
            assert rec.status == "closed"
            assert rec.exit_reason == "exit_warning"

    @pytest.mark.asyncio
    async def test_tighten_stop_lowers_for_short(self, manager, fake_adapter, db_session_factory):
        """숏 포지션: 스탑 타이트닝은 스탑을 내린다."""
        params = {**_DEFAULT_PARAMS, "tighten_stop_atr": 1.0, "strategy_id": 1}
        await manager.start("FX_BTC_JPY", params)
        await manager._open_position("FX_BTC_JPY", "sell", 15_000_000.0, 500_000.0, params)

        pos = manager.get_position("FX_BTC_JPY")
        # 초기 스탑 = 15M + 500K * 2 = 16M
        assert pos.stop_loss_price == 16_000_000.0

        # 가격이 14M으로 하락 → 타이트닝 = 14M + 500K * 1.0 = 14.5M (< 16M)
        await manager._apply_stop_tightening("FX_BTC_JPY", 14_000_000.0, 500_000.0, params)

        pos = manager.get_position("FX_BTC_JPY")
        assert pos.stop_tightened is True
        assert pos.stop_loss_price == 14_500_000.0

    @pytest.mark.asyncio
    async def test_short_signal_downtrend_candles(self, manager, db_session_factory):
        """하락 추세 캔들 → entry_sell 시그널."""
        await _seed_candles(db_session_factory, count=30, trend_up=False, base_price=15_000_000.0)
        await manager.start("FX_BTC_JPY", _DEFAULT_PARAMS)

        params = {**_DEFAULT_PARAMS, "ema_slope_short_threshold": -0.01}
        result = await manager._compute_signal("FX_BTC_JPY", "4h", params=params)
        assert result is not None
        # 하락 추세에서 entry_sell 또는 exit_warning
        assert result["signal"] in ("entry_sell", "exit_warning")
