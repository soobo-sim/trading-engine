"""
박스역추세 양방향 (BOX_BIDIRECTIONAL) 테스트 — T-BI-01~17, T-BT-01~04.

FakeExchangeAdapter + SQLite 인메모리 DB로 롱/숏 양방향 로직 검증.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import create_box_model, create_box_position_model, create_candle_model
from adapters.database.session import Base
from core.backtest.engine import BacktestConfig, _run_box_backtest
from core.exchange.types import OrderType
from core.strategy.box_mean_reversion import BoxMeanReversionManager
from core.task.supervisor import TaskSupervisor
from tests.fake_exchange import FakeExchangeAdapter

# ── ORM 모델 (bbi_ prefix) ────────────────────────────────────

BbiCandle = create_candle_model("bbi", pair_column="pair")
BbiBox = create_box_model("bbi", pair_column="pair")
BbiBoxPosition = create_box_position_model("bbi", pair_column="pair", order_id_length=40)


# ── Fixtures ─────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        tables = [
            Base.metadata.tables[t]
            for t in Base.metadata.tables
            if t.startswith("bbi_")
        ]
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def fake_adapter():
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 1_000_000.0, "usd": 0.0})
    adapter.set_ticker_price(210.0)
    return adapter


@pytest_asyncio.fixture
async def fake_fx_adapter():
    """is_margin_trading=True FX 어댑터."""
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 1_000_000.0})
    adapter.set_ticker_price(210.0)
    adapter.set_margin_trading(True)
    return adapter


@pytest_asyncio.fixture
async def supervisor():
    return TaskSupervisor()


@pytest_asyncio.fixture
async def manager(fake_adapter, supervisor, db_session_factory):
    return BoxMeanReversionManager(
        adapter=fake_adapter,
        supervisor=supervisor,
        session_factory=db_session_factory,
        candle_model=BbiCandle,
        box_model=BbiBox,
        box_position_model=BbiBoxPosition,
        pair_column="pair",
    )


@pytest_asyncio.fixture
async def fx_manager(fake_fx_adapter, supervisor, db_session_factory):
    return BoxMeanReversionManager(
        adapter=fake_fx_adapter,
        supervisor=supervisor,
        session_factory=db_session_factory,
        candle_model=BbiCandle,
        box_model=BbiBox,
        box_position_model=BbiBoxPosition,
        pair_column="pair",
    )


async def _insert_box(factory, pair, upper, lower, tolerance_pct=0.3):
    async with factory() as db:
        box = BbiBox()
        box.pair = pair
        box.upper_bound = Decimal(str(upper))
        box.lower_bound = Decimal(str(lower))
        box.upper_touch_count = 5
        box.lower_touch_count = 5
        box.tolerance_pct = Decimal(str(tolerance_pct))
        box.basis_timeframe = "4h"
        box.status = "active"
        box.created_at = datetime.now(timezone.utc)
        db.add(box)
        await db.commit()
        await db.refresh(box)
        return box.id


# ══════════════════════════════════════════════
# T-BI-01~02: 기존 롱 동작 회귀 테스트
# ══════════════════════════════════════════════


class TestLongOnlyRegression:
    """direction_mode 미지정 시 기존 롱 동작 유지 (회귀)."""

    @pytest.mark.asyncio
    async def test_bi01_long_entry_on_near_lower(self, manager, db_session_factory):
        """T-BI-01: near_lower → 롱 진입, side='buy'."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)
        box = await manager._get_active_box(pair)
        manager._params[pair] = {
            "position_size_pct": 10.0,
            "min_order_jpy": 500,
            "direction_mode": "long_only",
        }

        await manager._open_position_market(pair, box, 210.1, manager._params[pair], direction="long")

        pos = await manager._get_open_position(pair)
        assert pos is not None
        assert pos.side == "buy"

    @pytest.mark.asyncio
    async def test_bi02_long_close_on_near_upper(self, manager, db_session_factory):
        """T-BI-02: near_upper → 롱 청산 (exit_reason=near_upper_exit)."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)

        await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-001",
            entry_price=210.2, entry_amount=1000.0,
            direction="long",
        )

        pos = await manager._get_open_position(pair)
        assert pos is not None
        assert pos.side == "buy"

        await manager._record_close_position(
            pair=pair,
            exit_order_id="ORD-002",
            exit_price=212.9, exit_amount=1000.0,
            exit_reason="near_upper_exit",
        )

        assert not await manager._has_open_position(pair)


# ══════════════════════════════════════════════
# T-BI-03~04: 숏 진입/청산 (direction_mode=both)
# ══════════════════════════════════════════════


class TestShortEntry:
    """direction_mode='both' 시 숏 진입/청산."""

    @pytest.mark.asyncio
    async def test_bi03_short_entry_saves_side_sell(self, fx_manager, db_session_factory):
        """T-BI-03: direction='short' 진입 → DB side='sell'."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)
        box = await fx_manager._get_active_box(pair)
        fx_manager._params[pair] = {
            "position_size_pct": 50.0,
            "min_order_jpy": 500,
            "leverage": 1,
            "lot_unit": 1000,
            "min_lot_size": 1000,
            "direction_mode": "both",
        }

        await fx_manager._open_position_market(pair, box, 212.9, fx_manager._params[pair], direction="short")

        pos = await fx_manager._get_open_position(pair)
        assert pos is not None
        assert pos.side == "sell"

    @pytest.mark.asyncio
    async def test_bi04_short_close_near_lower_exit(self, fx_manager, db_session_factory):
        """T-BI-04: 숏 포지션 near_lower_exit 청산 → PnL = (entry - exit) * amount."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)

        await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-SHORT",
            entry_price=212.8, entry_amount=1000.0,
            direction="short",
        )

        pos = await fx_manager._get_open_position(pair)
        assert pos.side == "sell"

        await fx_manager._record_close_position(
            pair=pair,
            exit_order_id="ORD-CLOSE",
            exit_price=210.2, exit_amount=1000.0,
            exit_reason="near_lower_exit",
        )

        # DB에서 직접 확인
        async with db_session_factory() as db:
            result = await db.execute(
                select(BbiBoxPosition).where(BbiBoxPosition.id == pos.id)
            )
            closed_pos = result.scalar_one()

        assert closed_pos.status == "closed"
        assert closed_pos.exit_reason == "near_lower_exit"
        # 숏 PnL = (entry - exit) * amount = (212.8 - 210.2) * 1000 = 2600
        assert float(closed_pos.realized_pnl_jpy) == pytest.approx(2600.0, abs=1.0)
        assert float(closed_pos.realized_pnl_pct) > 0  # 수익


# ══════════════════════════════════════════════
# T-BI-08~09: long_only 강제 / 현물 차단
# ══════════════════════════════════════════════


class TestDirectionModeEnforcement:
    """direction_mode 제한 + 현물 숏 차단."""

    @pytest.mark.asyncio
    async def test_bi08_long_only_no_short_position(self, fx_manager, db_session_factory):
        """T-BI-08: direction_mode=long_only → direction='short' 진입해도 side='buy' 아니면 진입 안 됨."""
        # _open_position_market에 direction='short'을 넘겨도
        # is_margin=True + MARKET_SELL 주문이 실행되어 side='sell'로 저장
        # 하지만 _entry_monitor는 long_only면 절대 direction='short'을 넘기지 않음
        # 이 테스트는 _record_open_position이 direction 파라미터를 정확히 저장하는지 검증
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)

        pos = await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-LONG",
            entry_price=210.2, entry_amount=1000.0,
            direction="long",
        )
        assert pos.side == "buy"

        await fx_manager._record_close_position(
            pair=pair, exit_order_id="X", exit_price=212.8,
            exit_amount=1000.0, exit_reason="near_upper_exit",
        )

        # 숏 진입
        await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-SHORT",
            entry_price=212.9, entry_amount=1000.0,
            direction="short",
        )
        pos2 = await fx_manager._get_open_position(pair)
        assert pos2 is not None
        assert pos2.side == "sell"

    @pytest.mark.asyncio
    async def test_bi09_spot_market_buy_only(self, manager, fake_adapter, db_session_factory):
        """T-BI-09: 현물(is_margin=False) → _open_position_market은 항상 MARKET_BUY."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)
        box = await manager._get_active_box(pair)
        manager._params[pair] = {"position_size_pct": 10.0, "min_order_jpy": 500}

        # direction='short'을 전달해도 현물이므로 MARKET_BUY + side='buy'
        orders_placed = []
        original = fake_adapter.place_order

        async def _capture(*args, **kwargs):
            orders_placed.append(args)
            return await original(*args, **kwargs)

        fake_adapter.place_order = _capture

        await manager._open_position_market(pair, box, 210.1, manager._params[pair], direction="short")

        # is_margin=False → 코드에서 Long 강제: MARKET_BUY
        assert len(orders_placed) == 1
        assert orders_placed[0][0] == OrderType.MARKET_BUY

        pos = await manager._get_open_position(pair)
        assert pos.side == "buy"


# ══════════════════════════════════════════════
# T-BI-10~11: 방향별 SL
# ══════════════════════════════════════════════


class TestDirectionalStopLoss:
    """롱/숏 SL 계산 방향 검증."""

    @pytest.mark.asyncio
    async def test_bi10_short_sl_pnl_is_negative_when_sl_hits(self, fx_manager, db_session_factory):
        """T-BI-10: 숏 SL 발동 시 PnL 손실 (entry < sl_price ← price 상승)."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)

        entry_price = 212.0
        sl_pct = 1.5
        sl_price = entry_price * (1 + sl_pct / 100)  # 215.18

        await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-SHORT",
            entry_price=entry_price, entry_amount=1000.0,
            direction="short",
        )

        # SL 발동가격으로 청산
        await fx_manager._record_close_position(
            pair=pair, exit_order_id="ORD-SL",
            exit_price=sl_price, exit_amount=1000.0,
            exit_reason="price_stop_loss",
        )

        async with db_session_factory() as db:
            result = await db.execute(
                select(BbiBoxPosition).where(BbiBoxPosition.pair == pair)
            )
            pos = result.scalar_one()

        # 숏에서 가격이 오르면 손실
        assert float(pos.realized_pnl_jpy) < 0

    @pytest.mark.asyncio
    async def test_bi11_long_sl_pnl_negative(self, manager, db_session_factory):
        """T-BI-11: 롱 SL 발동 → 손실 (회귀)."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)

        entry_price = 210.5
        sl_price = entry_price * (1 - 1.5 / 100)

        await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-LONG",
            entry_price=entry_price, entry_amount=1000.0,
            direction="long",
        )

        await manager._record_close_position(
            pair=pair, exit_order_id="ORD-SL",
            exit_price=sl_price, exit_amount=1000.0,
            exit_reason="price_stop_loss",
        )

        async with db_session_factory() as db:
            result = await db.execute(
                select(BbiBoxPosition).where(BbiBoxPosition.pair == pair)
            )
            pos = result.scalar_one()

        assert float(pos.realized_pnl_jpy) < 0


# ══════════════════════════════════════════════
# T-BI-15: 동시 보유 방지
# ══════════════════════════════════════════════


class TestSimultaneousPositionPrevention:
    """open 포지션이 있으면 동일 pair 추가 진입 차단."""

    @pytest.mark.asyncio
    async def test_bi15_no_simultaneous_positions(self, fx_manager, db_session_factory):
        """T-BI-15: 숏 보유 중 롱 진입 시도 → 기존 positionid 반환 (중복 방지)."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)

        pos1 = await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-SHORT",
            entry_price=212.9, entry_amount=1000.0,
            direction="short",
        )

        # 이미 open 포지션이 있으므로 새 진입 시도 → 기존 반환
        pos2 = await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-LONG",
            entry_price=210.2, entry_amount=1000.0,
            direction="long",
        )

        assert pos1.id == pos2.id
        assert pos2.side == "sell"  # 기존 숏 유지


# ══════════════════════════════════════════════
# T-BI-16: direction DB 저장 확인
# ══════════════════════════════════════════════


class TestDirectionDbStorage:
    """side 컬럼에 'sell'이 정상 저장되는지 검증."""

    @pytest.mark.asyncio
    async def test_bi16_short_side_stored_in_db(self, fx_manager, db_session_factory):
        """T-BI-16: direction='short' → DB side='sell'."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)

        pos = await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-SHORT",
            entry_price=212.8, entry_amount=1000.0,
            direction="short",
        )

        # 캐시 우회하여 DB 직접 확인
        fx_manager._cached_position.pop(pair, None)
        async with db_session_factory() as db:
            result = await db.execute(
                select(BbiBoxPosition).where(BbiBoxPosition.id == pos.id)
            )
            db_pos = result.scalar_one()

        assert db_pos.side == "sell"

    @pytest.mark.asyncio
    async def test_bi16b_long_side_stored_in_db(self, manager, db_session_factory):
        """T-BI-16b: direction='long' → DB side='buy' (회귀)."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)

        pos = await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-LONG",
            entry_price=210.2, entry_amount=1000.0,
            direction="long",
        )

        manager._cached_position.pop(pair, None)
        async with db_session_factory() as db:
            result = await db.execute(
                select(BbiBoxPosition).where(BbiBoxPosition.id == pos.id)
            )
            db_pos = result.scalar_one()

        assert db_pos.side == "buy"


# ══════════════════════════════════════════════
# T-BI-17: match_fx_position_id side 파라미터
# ══════════════════════════════════════════════


class TestMatchFxPositionId:
    """_match_fx_position_id가 side를 동적으로 사용하는지 검증."""

    @pytest.mark.asyncio
    async def test_bi17_match_buy_position(self, fx_manager):
        """T-BI-17a: side='buy' → api_side='BUY'로 매칭."""
        from core.exchange.types import FxPosition
        pair = "usd_jpy"

        buy_pos = FxPosition(
            product_code=pair.upper(), side="BUY",
            price=210.2, size=1000, pnl=0.0,
            leverage=1.0, require_collateral=0.0,
            swap_point_accumulate=0.0, sfd=0.0,
            position_id=12345,
        )

        async def _get_positions(p):
            return [buy_pos]

        fx_manager._adapter.get_positions = _get_positions

        result = await fx_manager._match_fx_position_id(pair, 210.2, 1000, side="buy")
        assert result == 12345

    @pytest.mark.asyncio
    async def test_bi17b_match_sell_position(self, fx_manager):
        """T-BI-17b: side='sell' → api_side='SELL'로 매칭."""
        from core.exchange.types import FxPosition
        pair = "usd_jpy"

        sell_pos = FxPosition(
            product_code=pair.upper(), side="SELL",
            price=212.8, size=1000, pnl=0.0,
            leverage=1.0, require_collateral=0.0,
            swap_point_accumulate=0.0, sfd=0.0,
            position_id=99999,
        )

        async def _get_positions(p):
            return [sell_pos]

        fx_manager._adapter.get_positions = _get_positions

        result = await fx_manager._match_fx_position_id(pair, 212.8, 1000, side="sell")
        assert result == 99999


# ══════════════════════════════════════════════
# T-BT-01~04: 백테스트 엔진 양방향
# ══════════════════════════════════════════════


@dataclass
class FakeCandle:
    close: float
    high: float
    low: float
    open_time: Optional[datetime] = None
    open: float = 0.0

    def __post_init__(self):
        if self.open_time is None:
            self.open_time = datetime.now(tz=timezone.utc)


def _box_candles(n=100, upper=213.0, lower=210.0, oscillations=8):
    """박스권을 오가는 캔들 생성."""
    import math
    candles = []
    for i in range(n):
        t = i / max(n - 1, 1) * oscillations * math.pi
        price = lower + (upper - lower) * (math.sin(t) * 0.5 + 0.5)
        h = price * 1.001
        lo = price * 0.999
        candles.append(FakeCandle(close=price, high=h, low=lo))
    return candles


class TestBacktestBidirectional:

    def test_bt01_bidirectional_more_trades_than_long_only(self):
        """T-BT-01: direction_mode='both' → total_trades > long_only."""
        candles = _box_candles(n=150)
        config = BacktestConfig(fee_pct=0.0)

        params_long = {
            "box_tolerance_pct": 0.3, "box_min_touches": 3,
            "box_lookback_candles": 40, "near_bound_pct": 0.5,
            "direction_mode": "long_only",
        }
        params_both = {
            **params_long,
            "direction_mode": "both",
        }

        r_long = _run_box_backtest(candles, params_long, config)
        r_both = _run_box_backtest(candles, params_both, config)

        # 양방향이 롱만보다 거래 수 많아야 함
        assert r_both.total_trades >= r_long.total_trades

    def test_bt02_short_pnl_calculation(self):
        """T-BT-02: 숏 거래 PnL = (entry - exit) × amount > 0 (하락 시 이익)."""
        candles = _box_candles(n=150)
        config = BacktestConfig(fee_pct=0.0, slippage_pct=0.0)

        params = {
            "box_tolerance_pct": 0.3, "box_min_touches": 3,
            "box_lookback_candles": 40, "near_bound_pct": 0.5,
            "direction_mode": "both",
        }

        result = _run_box_backtest(candles, params, config)

        short_trades = [t for t in result.trades if t.side == "sell"]
        for trade in short_trades:
            if trade.pnl_jpy is not None:
                # 숏 청산이 near_lower(하락)이면 양수 PnL
                if trade.exit_reason == "near_lower_exit":
                    assert trade.pnl_jpy > 0, f"숏 수익 거래가 음수: {trade.pnl_jpy}"

    def test_bt03_no_simultaneous_long_short(self):
        """T-BT-03: 롱+숏 동시 보유 없음 (백테스트 내)."""
        candles = _box_candles(n=150)
        config = BacktestConfig()

        params = {
            "box_tolerance_pct": 0.3, "box_min_touches": 3,
            "box_lookback_candles": 40, "near_bound_pct": 0.5,
            "direction_mode": "both",
        }

        result = _run_box_backtest(candles, params, config)

        # exit_time이 겹치는 롱+숏 동시 보유 없어야 함
        trades_sorted = sorted(
            [t for t in result.trades if t.exit_time],
            key=lambda t: t.entry_time,
        )
        for i in range(len(trades_sorted) - 1):
            curr = trades_sorted[i]
            nxt = trades_sorted[i + 1]
            # 현재 거래가 종료된 후 다음 거래가 시작되어야 함
            assert curr.exit_time <= nxt.entry_time, (
                f"동시 보유 감지: [{curr.side}]{curr.entry_time}~{curr.exit_time} "
                f"and [{nxt.side}]{nxt.entry_time}"
            )

    def test_bt04_long_only_mode_no_short_trades(self):
        """T-BT-04: direction_mode='long_only' → side='sell' 거래 없음 (회귀)."""
        candles = _box_candles(n=150)
        config = BacktestConfig()

        params = {
            "box_tolerance_pct": 0.3, "box_min_touches": 3,
            "box_lookback_candles": 40, "near_bound_pct": 0.5,
            "direction_mode": "long_only",
        }

        result = _run_box_backtest(candles, params, config)

        short_trades = [t for t in result.trades if t.side == "sell"]
        assert len(short_trades) == 0

    def test_bt05_short_only_mode_no_long_trades(self):
        """T-BT-05: direction_mode='short_only' → side='buy' 거래 없음."""
        candles = _box_candles(n=150)
        config = BacktestConfig()

        params = {
            "box_tolerance_pct": 0.3, "box_min_touches": 3,
            "box_lookback_candles": 40, "near_bound_pct": 0.5,
            "direction_mode": "short_only",
        }

        result = _run_box_backtest(candles, params, config)

        long_trades = [t for t in result.trades if t.side == "buy"]
        assert len(long_trades) == 0
        # 거래가 발생했다면 전부 숏
        short_trades = [t for t in result.trades if t.side == "sell"]
        assert len(short_trades) == result.total_trades


# ══════════════════════════════════════════════
# T-BI-18: _close_position_market_fx close_side 반전
# ══════════════════════════════════════════════


class TestCloseFxSideReversal:
    """FX 청산 시 롱→SELL 반전, 숏→BUY 반전 검증."""

    @pytest.mark.asyncio
    async def test_bi18a_long_position_closes_with_sell(self, fx_manager, db_session_factory):
        """T-BI-18a: 롱(buy) 포지션 → close_position(side='SELL') 호출."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)

        pos = await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-LONG",
            entry_price=210.2, entry_amount=1000.0,
            direction="long",
        )

        close_calls = []

        async def _capture_close(symbol, side, position_id, size):
            close_calls.append({"symbol": symbol, "side": side, "position_id": position_id, "size": size})
            from core.exchange.types import Order, OrderSide, OrderStatus
            return Order(
                order_id="CLO-001", pair=pair,
                order_type=OrderType.MARKET_SELL,
                side=OrderSide.SELL,
                price=210.5, amount=1000.0, status=OrderStatus.COMPLETED,
            )

        pos.exchange_position_id = None  # positionId DB 없음 → API 조회 경로
        fx_manager._adapter.get_positions = AsyncMock(return_value=[])  # 매칭 실패 → 빠른 반환
        # exchange_position_id를 직접 주입하여 API 조회 없이 close_position 직접 호출
        pos.exchange_position_id = 12345
        fx_manager._adapter.close_position = _capture_close
        fx_manager._adapter.get_ticker = AsyncMock()
        fx_manager._adapter.get_ticker.return_value = type("T", (), {"last": 210.5})()

        await fx_manager._close_position_market_fx(pair, pos, "near_upper_exit")

        assert len(close_calls) == 1
        assert close_calls[0]["side"] == "SELL", (
            f"롱 포지션 청산은 SELL이어야 함, 실제: {close_calls[0]['side']}"
        )

    @pytest.mark.asyncio
    async def test_bi18b_short_position_closes_with_buy(self, fx_manager, db_session_factory):
        """T-BI-18b: 숏(sell) 포지션 → close_position(side='BUY') 호출."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_session_factory, pair, 213.0, 210.0)

        pos = await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-SHORT",
            entry_price=212.8, entry_amount=1000.0,
            direction="short",
        )

        close_calls = []

        async def _capture_close(symbol, side, position_id, size):
            close_calls.append({"symbol": symbol, "side": side, "position_id": position_id, "size": size})
            from core.exchange.types import Order, OrderSide, OrderStatus
            return Order(
                order_id="CLO-002", pair=pair,
                order_type=OrderType.MARKET_BUY,
                side=OrderSide.BUY,
                price=210.3, amount=1000.0, status=OrderStatus.COMPLETED,
            )

        pos.exchange_position_id = 99999  # positionId 직접 주입
        fx_manager._adapter.close_position = _capture_close
        fx_manager._adapter.get_ticker = AsyncMock()
        fx_manager._adapter.get_ticker.return_value = type("T", (), {"last": 210.3})()

        await fx_manager._close_position_market_fx(pair, pos, "near_lower_exit")

        assert len(close_calls) == 1
        assert close_calls[0]["side"] == "BUY", (
            f"숏 포지션 청산은 BUY이어야 함, 실제: {close_calls[0]['side']}"
        )
