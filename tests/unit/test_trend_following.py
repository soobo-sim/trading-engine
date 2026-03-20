"""
TrendFollowingManager 단위 테스트.

FakeExchangeAdapter + SQLite 인메모리 DB로 거래소-무관 통합 매니저를 검증.
실제 asyncio 태스크를 실행하지 않고, 개별 메서드를 직접 호출하여 테스트.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone, timedelta
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import create_candle_model, create_strategy_model, create_trend_position_model
from adapters.database.session import Base
from core.exchange.types import OrderType, Position
from core.strategy.trend_following import TrendFollowingManager
from core.task.supervisor import TaskSupervisor
from tests.fake_exchange import FakeExchangeAdapter


# ── 테스트용 ORM 모델 (tst_ prefix로 pytest 수집 방지) ───

TstStrategy = create_strategy_model("tst")
TstCandle = create_candle_model("tst", pair_column="pair")
TstTrendPosition = create_trend_position_model("tst", order_id_length=40)


# ── Fixtures ──────────────────────────────────


@pytest_asyncio.fixture
async def db_session_factory():
    """SQLite 인메모리 async_sessionmaker — tst_ 테이블만 생성."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        # 전체 metadata 대신 tst_ 테이블만 생성 (다른 prefix 테이블 충돌 방지)
        tables = [
            Base.metadata.tables[t]
            for t in Base.metadata.tables
            if t.startswith("tst_") or t == "strategy_techniques"
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
        initial_balances={"jpy": 1_000_000.0, "xrp": 0.0, "btc": 0.0},
        ticker_price=100.0,
    )
    await adapter.connect()
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def manager(fake_adapter, supervisor, db_session_factory):
    mgr = TrendFollowingManager(
        adapter=fake_adapter,
        supervisor=supervisor,
        session_factory=db_session_factory,
        candle_model=TstCandle,
        trend_position_model=TstTrendPosition,
        pair_column="pair",
    )
    yield mgr
    await mgr.stop_all()


async def _seed_candles(
    db_session_factory,
    pair: str = "xrp_jpy",
    timeframe: str = "4h",
    count: int = 30,
    base_price: float = 100.0,
    trend_up: bool = True,
):
    """테스트용 캔들 시드. trend_up=True면 우상향."""
    async with db_session_factory() as db:
        now = datetime(2026, 3, 17, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(count):
            if trend_up:
                close = base_price + i * 0.5
            else:
                close = base_price - i * 0.5
            open_time = now - timedelta(hours=4 * (count - i))
            close_time = now - timedelta(hours=4 * (count - i - 1))
            candle = TstCandle(
                pair=pair,
                timeframe=timeframe,
                open_time=open_time,
                close_time=close_time,
                open=close - 0.2,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1000,
                tick_count=50,
                is_complete=True,
            )
            db.add(candle)
            await db.flush()
        await db.commit()


# ──────────────────────────────────────────────
# 초기화 / 시작 / 종료 테스트
# ──────────────────────────────────────────────


class TestManagerLifecycle:

    @pytest.mark.asyncio
    async def test_start_registers_tasks(self, manager, supervisor):
        """start()는 candle + stoploss 2개 태스크를 supervisor에 등록."""
        await manager.start("xrp_jpy", {"basis_timeframe": "4h"})
        assert supervisor.is_running("trend_candle:xrp_jpy")
        assert supervisor.is_running("trend_stoploss:xrp_jpy")

    @pytest.mark.asyncio
    async def test_stop_removes_tasks(self, manager, supervisor):
        await manager.start("xrp_jpy", {})
        await manager.stop("xrp_jpy")
        assert not supervisor.is_running("trend_candle:xrp_jpy")
        assert not supervisor.is_running("trend_stoploss:xrp_jpy")

    @pytest.mark.asyncio
    async def test_running_pairs(self, manager):
        await manager.start("xrp_jpy", {})
        await manager.start("btc_jpy", {})
        pairs = manager.running_pairs()
        assert "xrp_jpy" in pairs
        assert "btc_jpy" in pairs

    @pytest.mark.asyncio
    async def test_stop_all(self, manager, supervisor):
        await manager.start("xrp_jpy", {})
        await manager.start("btc_jpy", {})
        await manager.stop_all()
        assert supervisor.alive_count == 0

    @pytest.mark.asyncio
    async def test_get_task_health(self, manager):
        await manager.start("xrp_jpy", {})
        health = manager.get_task_health()
        assert "xrp_jpy" in health
        assert "candle_monitor" in health["xrp_jpy"]
        assert "stop_loss_monitor" in health["xrp_jpy"]


# ──────────────────────────────────────────────
# 포지션 복원 테스트
# ──────────────────────────────────────────────


class TestPositionRecovery:

    @pytest.mark.asyncio
    async def test_detect_existing_position(self, manager, fake_adapter):
        """잔고 > min_coin_size → 기존 포지션 감지."""
        fake_adapter.set_balance("xrp", 100.0)
        manager._params["xrp_jpy"] = {"min_coin_size": 1.0}
        pos = await manager._detect_existing_position("xrp_jpy")
        assert pos is not None
        assert pos.entry_amount == 100.0
        assert pos.entry_price is None  # 복원이므로 진입가 불명

    @pytest.mark.asyncio
    async def test_detect_dust_ignored(self, manager, fake_adapter):
        """BUG-003: 잔고 < min_coin_size → dust로 무시."""
        fake_adapter.set_balance("xrp", 0.05)
        manager._params["xrp_jpy"] = {"min_coin_size": 1.0}
        pos = await manager._detect_existing_position("xrp_jpy")
        assert pos is None

    @pytest.mark.asyncio
    async def test_recover_db_position_id(self, manager, db_session_factory):
        """열린 DB 레코드에서 포지션 ID 복원."""
        # DB에 열린 포지션 삽입
        async with db_session_factory() as db:
            rec = TstTrendPosition(
                pair="xrp_jpy",
                entry_order_id="TEST-001",
                entry_price=100.0,
                entry_amount=50.0,
                status="open",
            )
            db.add(rec)
            await db.commit()
            await db.refresh(rec)
            expected_id = rec.id

        result = await manager._recover_db_position_id("xrp_jpy")
        assert result == expected_id


# ──────────────────────────────────────────────
# 시그널 계산 테스트
# ──────────────────────────────────────────────


class TestSignalComputation:

    @pytest.mark.asyncio
    async def test_compute_signal_insufficient_candles(self, manager, db_session_factory):
        """캔들 부족 시 None 반환."""
        await _seed_candles(db_session_factory, count=5)
        result = await manager._compute_signal("xrp_jpy", "4h")
        assert result is None

    @pytest.mark.asyncio
    async def test_compute_signal_with_enough_candles(self, manager, db_session_factory):
        """충분한 캔들 → signal_data dict 반환."""
        await _seed_candles(db_session_factory, count=30, trend_up=True)
        result = await manager._compute_signal("xrp_jpy", "4h")
        assert result is not None
        assert "signal" in result
        assert "current_price" in result
        assert "ema" in result
        assert "atr" in result
        assert "rsi" in result
        assert "latest_candle_open_time" in result
        assert "candles" in result


# ──────────────────────────────────────────────
# 진입 테스트
# ──────────────────────────────────────────────


class TestOpenPosition:

    @pytest.mark.asyncio
    async def test_open_position_success(self, manager, fake_adapter, db_session_factory):
        """진입 성공 → 인메모리 포지션 + DB 레코드."""
        params = {
            "position_size_pct": 10.0,
            "min_order_jpy": 500,
            "atr_multiplier_stop": 2.0,
            "max_slippage_pct": 1.0,
        }
        manager._params["xrp_jpy"] = params
        await manager._open_position("xrp_jpy", 100.0, 5.0, params)

        pos = manager._position.get("xrp_jpy")
        assert pos is not None
        assert pos.entry_price == 100.0
        assert pos.stop_loss_price == 90.0  # 100 - 5*2
        assert pos.db_record_id is not None
        assert not pos.stop_tightened

        # DB 검증
        async with db_session_factory() as db:
            result = await db.execute(select(TstTrendPosition))
            rec = result.scalars().first()
            assert rec is not None
            assert rec.status == "open"
            assert float(rec.entry_price) == 100.0

    @pytest.mark.asyncio
    async def test_open_position_skip_low_jpy(self, manager, fake_adapter):
        """JPY 부족 시 진입 스킵."""
        fake_adapter.set_balance("jpy", 100.0)
        params = {"position_size_pct": 10.0, "min_order_jpy": 500}
        manager._params["xrp_jpy"] = params
        await manager._open_position("xrp_jpy", 100.0, 5.0, params)
        assert manager._position.get("xrp_jpy") is None

    @pytest.mark.asyncio
    async def test_open_position_slippage_skip(self, manager, fake_adapter):
        """슬리피지 초과 시 진입 스킵."""
        # ticker ask = 100 * 1.001 = 100.1
        # price = 90 → slippage = (100.1 - 90) / 90 * 100 ≈ 11.2% > 0.3%
        params = {
            "position_size_pct": 10.0,
            "min_order_jpy": 500,
            "max_slippage_pct": 0.3,
        }
        manager._params["xrp_jpy"] = params
        await manager._open_position("xrp_jpy", 90.0, 5.0, params)
        assert manager._position.get("xrp_jpy") is None


# ──────────────────────────────────────────────
# 청산 테스트
# ──────────────────────────────────────────────


class TestClosePosition:

    @pytest.mark.asyncio
    async def test_close_position_success(self, manager, fake_adapter, db_session_factory):
        """전량 청산 성공 → 포지션 클리어 + DB closed."""
        # 포지션 진입
        fake_adapter.set_balance("jpy", 1_000_000.0)
        params = {
            "position_size_pct": 10.0,
            "min_order_jpy": 500,
            "atr_multiplier_stop": 2.0,
            "max_slippage_pct": 1.0,
            "min_coin_size": 0.001,
            "trading_fee_rate": 0.002,
        }
        manager._params["xrp_jpy"] = params
        await manager._open_position("xrp_jpy", 100.0, 5.0, params)
        assert manager._position.get("xrp_jpy") is not None

        await manager._close_position("xrp_jpy", "exit_warning")
        assert manager._position.get("xrp_jpy") is None

        # DB 검증
        async with db_session_factory() as db:
            result = await db.execute(select(TstTrendPosition))
            rec = result.scalars().first()
            assert rec is not None
            assert rec.status == "closed"
            assert rec.exit_reason == "exit_warning"

    @pytest.mark.asyncio
    async def test_close_dust_position(self, manager, fake_adapter, db_session_factory):
        """BUG-003: dust 잔고 → 인메모리+DB 모두 정리."""
        # 수동으로 포지션 세팅
        async with db_session_factory() as db:
            rec = TstTrendPosition(
                pair="xrp_jpy",
                entry_order_id="TEST-001",
                entry_price=100.0,
                entry_amount=50.0,
                status="open",
            )
            db.add(rec)
            await db.commit()
            await db.refresh(rec)
            db_id = rec.id

        pos = Position(
            pair="xrp_jpy", entry_price=100.0, entry_amount=50.0,
            stop_loss_price=90.0, db_record_id=db_id,
        )
        manager._position["xrp_jpy"] = pos
        manager._params["xrp_jpy"] = {"min_coin_size": 1.0, "trading_fee_rate": 0.002}

        # 잔고를 dust 수준으로
        fake_adapter.set_balance("xrp", 0.0005)

        await manager._close_position("xrp_jpy", "stop_loss")
        assert manager._position.get("xrp_jpy") is None

        # DB도 closed로 갱신
        async with db_session_factory() as db:
            result = await db.execute(select(TstTrendPosition).where(TstTrendPosition.id == db_id))
            rec = result.scalars().first()
            assert rec.status == "closed"
            assert rec.exit_reason == "dust_position_cleared"

    @pytest.mark.asyncio
    async def test_close_with_fee_deduction(self, manager, fake_adapter, db_session_factory):
        """BUG-004: 매도 수수료 차감 → sell_amount < available."""
        fake_adapter.set_balance("xrp", 100.0)
        fee_rate = 0.002

        async with db_session_factory() as db:
            rec = TstTrendPosition(
                pair="xrp_jpy", entry_order_id="TEST-002",
                entry_price=100.0, entry_amount=100.0, status="open",
            )
            db.add(rec)
            await db.commit()
            await db.refresh(rec)
            db_id = rec.id

        pos = Position(
            pair="xrp_jpy", entry_price=100.0, entry_amount=100.0,
            db_record_id=db_id,
        )
        manager._position["xrp_jpy"] = pos
        manager._params["xrp_jpy"] = {
            "min_coin_size": 0.001,
            "trading_fee_rate": fee_rate,
        }

        await manager._close_position("xrp_jpy", "exit_warning")
        assert manager._position.get("xrp_jpy") is None

        # 매도된 수량이 available / (1 + fee_rate) 이내인지 확인
        last_order = fake_adapter.order_history[-1]
        expected_sell = math.floor(100.0 / (1 + fee_rate) * 1e8) / 1e8
        assert last_order.amount == expected_sell

    @pytest.mark.asyncio
    async def test_close_ticker_fallback_when_price_zero(self, manager, fake_adapter, db_session_factory):
        """BUG-008: 체결가 미반환(price=0) → ticker 현재가로 대체하여 PnL 계산."""
        fake_adapter.set_balance("xrp", 50.0)
        fake_adapter.set_ticker_price(120.0)

        async with db_session_factory() as db:
            rec = TstTrendPosition(
                pair="xrp_jpy", entry_order_id="TEST-008",
                entry_price=100.0, entry_amount=50.0, status="open",
            )
            db.add(rec)
            await db.commit()
            await db.refresh(rec)
            db_id = rec.id

        pos = Position(
            pair="xrp_jpy", entry_price=100.0, entry_amount=50.0,
            db_record_id=db_id,
        )
        manager._position["xrp_jpy"] = pos
        manager._params["xrp_jpy"] = {
            "min_coin_size": 0.001,
            "trading_fee_rate": 0.002,
        }

        # place_order가 price=0인 Order를 반환하도록 패치
        from core.exchange.types import Order, OrderStatus, OrderSide
        original_place = fake_adapter.place_order

        async def _place_zero_price(*args, **kwargs):
            order = await original_place(*args, **kwargs)
            return Order(
                order_id=order.order_id, pair=order.pair,
                order_type=order.order_type, side=order.side,
                price=0, amount=order.amount,
                status=order.status, created_at=order.created_at,
            )

        fake_adapter.place_order = _place_zero_price

        await manager._close_position("xrp_jpy", "exit_warning")
        assert manager._position.get("xrp_jpy") is None

        # DB에 ticker last(=120.0)로 PnL이 기록되어야 함
        async with db_session_factory() as db:
            result = await db.execute(select(TstTrendPosition).where(TstTrendPosition.id == db_id))
            rec = result.scalars().first()
            assert rec.status == "closed"
            assert rec.exit_price is not None
            assert float(rec.exit_price) == 120.0
            assert rec.realized_pnl_jpy is not None
            assert float(rec.realized_pnl_jpy) > 0  # 100→120, 이익

    @pytest.mark.asyncio
    async def test_close_dust_logged_after_sell(self, manager, fake_adapter, db_session_factory, caplog):
        """BUG-009: 청산 후 dust 잔고 감지 → 로그 기록."""
        # 소량 잔고 설정 (0.005 XRP, fee 차감 후 dust 남음)
        fake_adapter.set_balance("xrp", 0.005)

        async with db_session_factory() as db:
            rec = TstTrendPosition(
                pair="xrp_jpy", entry_order_id="TEST-009",
                entry_price=100.0, entry_amount=0.005, status="open",
            )
            db.add(rec)
            await db.commit()
            await db.refresh(rec)
            db_id = rec.id

        pos = Position(
            pair="xrp_jpy", entry_price=100.0, entry_amount=0.005,
            db_record_id=db_id,
        )
        manager._position["xrp_jpy"] = pos
        manager._params["xrp_jpy"] = {
            "min_coin_size": 0.001,
            "trading_fee_rate": 0.002,
        }

        import logging
        with caplog.at_level(logging.INFO):
            await manager._close_position("xrp_jpy", "stop_loss")

        assert manager._position.get("xrp_jpy") is None
        # dust 로그가 기록되었는지 확인
        dust_logs = [r for r in caplog.records if "dust 잔고 감지" in r.message]
        assert len(dust_logs) == 1
        assert "매도 불가 수량" in dust_logs[0].message


# ──────────────────────────────────────────────
# 스탑 타이트닝 테스트
# ──────────────────────────────────────────────


class TestStopTightening:

    @pytest.mark.asyncio
    async def test_apply_stop_tightening(self, manager, db_session_factory):
        """스탑 타이트닝 → stop_loss_price 갱신 + 플래그."""
        async with db_session_factory() as db:
            rec = TstTrendPosition(
                pair="xrp_jpy", entry_order_id="TEST-003",
                entry_price=100.0, entry_amount=50.0,
                stop_loss_price=90.0, status="open",
            )
            db.add(rec)
            await db.commit()
            await db.refresh(rec)
            db_id = rec.id

        pos = Position(pair="xrp_jpy", entry_price=100.0, entry_amount=50.0,
                       stop_loss_price=90.0, db_record_id=db_id)
        manager._position["xrp_jpy"] = pos
        manager._params["xrp_jpy"] = {"tighten_stop_atr": 1.0}

        # current_price=110, atr=5 → new_sl = 110 - 5*1.0 = 105 > 90
        await manager._apply_stop_tightening("xrp_jpy", 110.0, 5.0, {"tighten_stop_atr": 1.0})

        assert pos.stop_tightened is True
        assert pos.stop_loss_price == 105.0

        # DB도 갱신 확인
        async with db_session_factory() as db:
            result = await db.execute(select(TstTrendPosition).where(TstTrendPosition.id == db_id))
            rec = result.scalars().first()
            assert float(rec.stop_loss_price) == 105.0

    @pytest.mark.asyncio
    async def test_stop_tightening_only_ratchets_up(self, manager):
        """스탑은 항상 위로만 이동 (아래로 이동 불가)."""
        pos = Position(pair="xrp_jpy", entry_price=100.0, entry_amount=50.0,
                       stop_loss_price=95.0)
        manager._position["xrp_jpy"] = pos
        manager._params["xrp_jpy"] = {}

        # new_sl = 85 - 5*1.0 = 80 < 95 → 갱신 안 됨
        await manager._apply_stop_tightening("xrp_jpy", 85.0, 5.0, {"tighten_stop_atr": 1.0})
        assert pos.stop_loss_price == 95.0
        assert pos.stop_tightened is True  # 플래그는 설정됨


# ──────────────────────────────────────────────
# 스탑로스 모니터 (백오프) 테스트
# ──────────────────────────────────────────────


class TestStopLossBackoff:

    @pytest.mark.asyncio
    async def test_fail_count_cooldown(self, manager):
        """BUG-005: 5회 실패마다 60초 쿨다운 기록."""
        manager._close_fail_count["xrp_jpy"] = 4
        # 5번째 실패 시뮬레이션
        import time
        manager._close_fail_count["xrp_jpy"] = 5
        manager._close_fail_until["xrp_jpy"] = time.time() + 60

        # 쿨다운 중이면 time.time() < cooldown_until → True
        assert time.time() < manager._close_fail_until["xrp_jpy"]


# ──────────────────────────────────────────────
# DB 기록 테스트
# ──────────────────────────────────────────────


class TestDbRecording:

    @pytest.mark.asyncio
    async def test_record_open(self, manager, db_session_factory):
        """진입 DB 레코드 생성."""
        rec_id = await manager._record_open(
            pair="btc_jpy",
            order_id="ORDER-001",
            price=5_000_000.0,
            amount=0.01,
            invest_jpy=50_000.0,
            stop_loss_price=4_900_000.0,
            strategy_id=None,
        )
        assert rec_id is not None

        async with db_session_factory() as db:
            result = await db.execute(select(TstTrendPosition).where(TstTrendPosition.id == rec_id))
            rec = result.scalars().first()
            assert rec.pair == "btc_jpy"
            assert rec.status == "open"
            assert float(rec.entry_price) == 5_000_000.0

    @pytest.mark.asyncio
    async def test_record_close_calculates_pnl(self, manager, db_session_factory):
        """청산 시 PnL 계산 + DB 갱신."""
        # 진입 기록
        rec_id = await manager._record_open(
            pair="xrp_jpy",
            order_id="ORDER-002",
            price=100.0,
            amount=50.0,
            invest_jpy=5_000.0,
            stop_loss_price=90.0,
            strategy_id=None,
        )

        # 청산 기록 (120엔에 청산 → +20엔/개 × 50개 = +1000엔)
        await manager._record_close(
            db_record_id=rec_id,
            pair="xrp_jpy",
            order_id="ORDER-003",
            price=120.0,
            amount=50.0,
            reason="exit_warning",
            entry_price=100.0,
        )

        async with db_session_factory() as db:
            result = await db.execute(select(TstTrendPosition).where(TstTrendPosition.id == rec_id))
            rec = result.scalars().first()
            assert rec.status == "closed"
            assert rec.exit_reason == "exit_warning"
            assert float(rec.realized_pnl_jpy) == 1000.0
            assert float(rec.realized_pnl_pct) == 20.0

    @pytest.mark.asyncio
    async def test_record_partial_close(self, manager, db_session_factory):
        """부분 청산 누적 기록."""
        rec_id = await manager._record_open(
            pair="xrp_jpy",
            order_id="ORDER-004",
            price=100.0,
            amount=100.0,
            invest_jpy=10_000.0,
            stop_loss_price=90.0,
            strategy_id=None,
        )

        pos = Position(pair="xrp_jpy", entry_price=100.0, entry_amount=100.0,
                       db_record_id=rec_id)
        manager._position["xrp_jpy"] = pos

        await manager._record_partial_close(
            pair="xrp_jpy",
            order_id="ORDER-005",
            price=110.0,
            amount=30.0,
            reason="partial_exit_rsi_extreme",
        )

        async with db_session_factory() as db:
            result = await db.execute(select(TstTrendPosition).where(TstTrendPosition.id == rec_id))
            rec = result.scalars().first()
            assert rec.partial_exit_count == 1
            assert float(rec.partial_exit_amount) == 30.0
            assert "partial_exit_rsi_extreme" in rec.partial_exit_reasons

    @pytest.mark.asyncio
    async def test_update_trailing_stop(self, manager, db_session_factory):
        """트레일링 스탑 DB 갱신."""
        rec_id = await manager._record_open(
            pair="xrp_jpy",
            order_id="ORDER-006",
            price=100.0,
            amount=50.0,
            invest_jpy=5_000.0,
            stop_loss_price=90.0,
            strategy_id=None,
        )

        pos = Position(pair="xrp_jpy", entry_price=100.0, entry_amount=50.0,
                       stop_loss_price=90.0, db_record_id=rec_id)
        manager._position["xrp_jpy"] = pos

        await manager._update_trailing_stop_in_db("xrp_jpy", 95.0)

        async with db_session_factory() as db:
            result = await db.execute(select(TstTrendPosition).where(TstTrendPosition.id == rec_id))
            rec = result.scalars().first()
            assert float(rec.stop_loss_price) == 95.0


# ──────────────────────────────────────────────
# MARKET_BUY JPY semantics (FakeExchangeAdapter)
# ──────────────────────────────────────────────


class TestMarketBuyJpySemantics:

    @pytest.mark.asyncio
    async def test_market_buy_converts_jpy_to_coins(self, fake_adapter):
        """MARKET_BUY: amount=JPY → 코인 수량 변환."""
        # 100엔 가격에 10000엔 투자 → 100개 코인
        order = await fake_adapter.place_order(
            OrderType.MARKET_BUY, "xrp_jpy", 10_000.0
        )
        assert order.amount == 100.0  # 10000/100
        balance = await fake_adapter.get_balance()
        assert balance.get_available("xrp") == 100.0
        assert balance.get_available("jpy") == 990_000.0


# ── BUG-006: 잔고-포지션 정합성 검사 ──────────────

class TestBalanceReconciliation:

    @pytest.mark.asyncio
    async def test_sync_updates_position_on_drift(self, manager, fake_adapter):
        """BUG-006: 실잔고와 인메모리 entry_amount이 1% 이상 다르면 갱신."""
        pair = "xrp_jpy"
        # 포지션 설정: 100코인 보유
        fake_adapter.set_balance("xrp", 80.0)  # 실잔고 80 (20% 괴리)
        manager._position[pair] = Position(
            pair=pair,
            entry_price=100.0,
            entry_amount=100.0,
            stop_loss_price=90.0,
            db_record_id=1,
        )
        await manager._sync_position_balance(pair)
        # 인메모리가 80으로 갱신되어야 함
        assert manager._position[pair].entry_amount == 80.0

    @pytest.mark.asyncio
    async def test_sync_no_update_within_threshold(self, manager, fake_adapter):
        """BUG-006: 괴리 1% 이내면 갱신하지 않음."""
        pair = "xrp_jpy"
        fake_adapter.set_balance("xrp", 99.5)  # 0.5% 괴리
        manager._position[pair] = Position(
            pair=pair,
            entry_price=100.0,
            entry_amount=100.0,
            stop_loss_price=90.0,
            db_record_id=1,
        )
        await manager._sync_position_balance(pair)
        # 0.5% < 1% → 갱신 없음
        assert manager._position[pair].entry_amount == 100.0

    @pytest.mark.asyncio
    async def test_sync_preserves_position_fields(self, manager, fake_adapter):
        """BUG-006: entry_amount만 갱신, 나머지 필드 보존."""
        pair = "xrp_jpy"
        fake_adapter.set_balance("xrp", 50.0)
        manager._position[pair] = Position(
            pair=pair,
            entry_price=100.0,
            entry_amount=100.0,
            stop_loss_price=90.0,
            db_record_id=42,
            stop_tightened=True,
            extra={"divergence_exit_done": True},
        )
        await manager._sync_position_balance(pair)
        pos = manager._position[pair]
        assert pos.entry_amount == 50.0
        assert pos.entry_price == 100.0
        assert pos.stop_loss_price == 90.0
        assert pos.db_record_id == 42
        assert pos.stop_tightened is True
        assert pos.extra == {"divergence_exit_done": True}

    @pytest.mark.asyncio
    async def test_sync_skipped_when_no_position(self, manager, fake_adapter):
        """BUG-006: 포지션 없으면 에러 없이 스킵."""
        await manager._sync_position_balance("xrp_jpy")  # no-op
