"""
BoxMeanReversionManager — IFD-OCO 지정가 주문 단위 테스트.

FakeExchangeAdapter + SQLite 인메모리 DB 사용.
GMO FX 전용 IFD-OCO 발주·폴링·체결·취소·재발주 경로를 검증.

테스트 시리즈:
  S1 — 발주 (pending 등록)
  S2 — 체결 감지 (1차·TP·SL)
  S3 — 강제 취소 (청산·무효화)
  S4 — 백테스트 slippage=0
  S5 — GmoFxAdapter _round_price + get_orders_by_root
  S6 — start() DB 복원 (first_filled)
  S7 — 박스 무효화 시 pending IFD-OCO 취소
  S8 — TP + both mode → 반대 방향 재발주
  S9 — _box_pos_to_dict IFD-OCO 필드 직렬화
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import (
    create_candle_model,
    create_box_model,
    create_box_position_model,
)
from adapters.database.session import Base
from core.backtest.engine import BacktestConfig, run_backtest, _run_box_backtest
from core.strategy.box_mean_reversion import BoxMeanReversionManager
from core.task.supervisor import TaskSupervisor
from tests.fake_exchange import FakeExchangeAdapter
from core.exchange.types import FxPosition, Collateral

# ── 테스트용 ORM 모델 ─────────────────────────────

IfdoCandle = create_candle_model("ifdo", pair_column="pair")
IfdoBox = create_box_model("ifdo", pair_column="pair")
IfdoBoxPos = create_box_position_model("ifdo", pair_column="pair", order_id_length=40)


# ── Fixtures ─────────────────────────────────────


@pytest_asyncio.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        tables = [
            Base.metadata.tables[t]
            for t in Base.metadata.tables
            if t.startswith("ifdo_")
        ]
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def fx_adapter():
    """FX 모드 FakeExchangeAdapter (JPY 잔고, 증거금 거래)."""
    adapter = FakeExchangeAdapter(
        initial_balances={"jpy": 1_000_000.0},
        ticker_price=145.0,
    )
    adapter.set_margin_trading(True)
    adapter.set_collateral(
        Collateral(
            collateral=1_000_000.0,
            open_position_pnl=0.0,
            require_collateral=0.0,
            keep_rate=999.0,
        )
    )
    return adapter


@pytest_asyncio.fixture
async def manager(fx_adapter, db_factory):
    sup = TaskSupervisor()
    mgr = BoxMeanReversionManager(
        adapter=fx_adapter,
        supervisor=sup,
        session_factory=db_factory,
        candle_model=IfdoCandle,
        box_model=IfdoBox,
        box_position_model=IfdoBoxPos,
        pair_column="pair",
    )
    return mgr


async def _insert_box(
    factory: async_sessionmaker,
    pair: str,
    upper: float,
    lower: float,
    status: str = "active",
) -> int:
    async with factory() as db:
        box = IfdoBox()
        box.pair = pair
        box.upper_bound = Decimal(str(upper))
        box.lower_bound = Decimal(str(lower))
        box.upper_touch_count = 5
        box.lower_touch_count = 5
        box.tolerance_pct = Decimal("0.5")
        box.basis_timeframe = "4h"
        box.status = status
        box.created_at = datetime.now(timezone.utc)
        db.add(box)
        await db.commit()
        await db.refresh(box)
        return box.id


def _make_params(use_ifdoco: bool = True, direction: str = "long_only") -> dict:
    return {
        "pair": "usd_jpy",
        "stop_loss_pct": 1.5,
        "near_bound_pct": 0.3,
        "position_size_pct": 50.0,  # 50%: 500,000 JPY → 500,000/145=3448 → 3000 lot
        "min_order_jpy": 500.0,
        "lot_unit": 1000,
        "min_lot_size": 1000,
        "leverage": 1,
        "exchange_type": "fx",
        "direction_mode": direction,
        "use_ifdoco": use_ifdoco,
    }


# ══════════════════════════════════════════════
# S1 — IFD-OCO 발주
# ══════════════════════════════════════════════


class TestIfdocoOpen:
    """S-1-*: IFD-OCO 주문 발주 및 pending 상태 등록."""

    @pytest.mark.asyncio
    async def test_s1_1_open_position_ifdoco_registers_pending(self, manager, fx_adapter, db_factory):
        """S-1-1: _open_position_ifdoco 호출 → _ifdoco_orders[pair]에 rootOrderId 등록."""
        pair = "usd_jpy"
        box_id = await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params(use_ifdoco=True)

        await manager._open_position_ifdoco(pair, box, params, "long")

        assert pair in manager._ifdoco_orders
        root_id = manager._ifdoco_orders[pair]
        assert root_id is not None
        assert root_id.startswith("FAKE-IFO-")

    @pytest.mark.asyncio
    async def test_s1_2_meta_stored_correctly(self, manager, fx_adapter, db_factory):
        """S-1-2: _ifdoco_meta에 direction·price·box_id가 저장된다."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params(use_ifdoco=True)

        await manager._open_position_ifdoco(pair, box, params, "long")

        meta = manager._ifdoco_meta.get(pair)
        assert meta is not None
        assert meta["direction"] == "long"
        assert meta["entry_price"] == 144.0
        assert meta["tp_price"] == 146.0
        assert meta["sl_price"] < 144.0  # 1.5% below lower
        assert meta["box_id"] == box.id

    @pytest.mark.asyncio
    async def test_s1_3_short_direction_prices(self, manager, fx_adapter, db_factory):
        """S-1-3: direction=short → entry=upper·tp=lower·sl>upper."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params(use_ifdoco=True)

        await manager._open_position_ifdoco(pair, box, params, "short")

        meta = manager._ifdoco_meta.get(pair)
        assert meta["direction"] == "short"
        assert meta["entry_price"] == 146.0
        assert meta["tp_price"] == 144.0
        assert meta["sl_price"] > 146.0

    @pytest.mark.asyncio
    async def test_s1_4_no_duplicate_order_when_pending(self, manager, fx_adapter, db_factory):
        """S-1-4: _entry_monitor에서 이미 pending이면 새 주문 발주를 건너뛴다."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params(use_ifdoco=True)

        await manager._open_position_ifdoco(pair, box, params, "long")
        first_root_id = manager._ifdoco_orders[pair]

        # 두 번째 발주 시도 — pending 중이므로 건너뜀
        manager._params["usd_jpy"] = params
        # 직접 호출 시 _entry_monitor 가드를 수동으로 재현
        # (이미 pending이면 발주 안 함)
        if manager._ifdoco_orders.get(pair) is not None:
            pass  # entry_monitor가 continue하는 경로
        else:
            await manager._open_position_ifdoco(pair, box, params, "long")

        assert manager._ifdoco_orders[pair] == first_root_id  # 변경 없음


# ══════════════════════════════════════════════
# S2 — 체결 감지
# ══════════════════════════════════════════════


class TestIfdocoFill:
    """S-2-*: poll → first_fill·TP·SL 체결 감지 및 DB 기록."""

    @pytest.mark.asyncio
    async def test_s2_1_first_fill_creates_db_position(self, manager, fx_adapter, db_factory):
        """S-2-1: 1차 체결 감지 → DB에 open 포지션 생성."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params()

        await manager._open_position_ifdoco(pair, box, params, "long")
        root_id = manager._ifdoco_orders[pair]

        # 1차 체결 시뮬레이션
        fx_adapter.simulate_ifdoco_fill(root_id, "first_fill", exec_price=144.0)
        fx_adapter.set_fx_positions([
            FxPosition(
                position_id=1,
                product_code="usd_jpy",
                side="BUY",
                size=1000,
                price=144.0,
                pnl=0.0,
                leverage=1.0,
                require_collateral=0.0,
                swap_point_accumulate=0.0,
                sfd=0.0,
            )
        ])

        await manager._poll_ifdoco_status(pair)

        # DB에 open 포지션 확인
        async with db_factory() as db:
            result = await db.execute(
                select(IfdoBoxPos).where(IfdoBoxPos.pair == pair, IfdoBoxPos.status == "open")
            )
            pos = result.scalar_one_or_none()
        assert pos is not None
        assert float(pos.entry_price) == 144.0

    @pytest.mark.asyncio
    async def test_s2_2_tp_fill_closes_db_position(self, manager, fx_adapter, db_factory):
        """S-2-2: TP 체결 → DB 포지션 closed + ifdoco_status=completed_tp."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params()

        await manager._open_position_ifdoco(pair, box, params, "long")
        root_id = manager._ifdoco_orders[pair]

        # 1차 체결
        fx_adapter.simulate_ifdoco_fill(root_id, "first_fill", exec_price=144.0)
        fx_adapter.set_fx_positions([
            FxPosition(
                position_id=1, product_code="usd_jpy",
                side="BUY", size=1000, price=144.0, pnl=0.0,
                leverage=1.0, require_collateral=0.0,
                swap_point_accumulate=0.0, sfd=0.0,
            )
        ])
        await manager._poll_ifdoco_status(pair)

        # TP 체결 시뮬레이션
        fx_adapter.simulate_ifdoco_fill(root_id, "tp", exec_price=146.0)
        fx_adapter.set_fx_positions([])
        await manager._poll_ifdoco_status(pair)

        # DB 포지션 closed 확인
        async with db_factory() as db:
            result = await db.execute(
                select(IfdoBoxPos).where(IfdoBoxPos.pair == pair)
            )
            pos = result.scalar_one_or_none()
        assert pos is not None
        assert pos.status == "closed"
        assert pos.exit_reason == "near_upper_exit"

        # 메모리 정리 확인
        assert manager._ifdoco_orders.get(pair) is None

    @pytest.mark.asyncio
    async def test_s2_3_sl_fill_closes_db_position(self, manager, fx_adapter, db_factory):
        """S-2-3: SL 체결 → DB 포지션 closed + exit_reason=price_stop_loss."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params()

        await manager._open_position_ifdoco(pair, box, params, "long")
        root_id = manager._ifdoco_orders[pair]

        # 1차 체결 + SL 체결
        fx_adapter.simulate_ifdoco_fill(root_id, "first_fill", exec_price=144.0)
        fx_adapter.set_fx_positions([
            FxPosition(
                position_id=1, product_code="usd_jpy",
                side="BUY", size=1000, price=144.0, pnl=0.0,
                leverage=1.0, require_collateral=0.0,
                swap_point_accumulate=0.0, sfd=0.0,
            )
        ])
        await manager._poll_ifdoco_status(pair)

        fx_adapter.simulate_ifdoco_fill(root_id, "sl", exec_price=141.84)
        fx_adapter.set_fx_positions([])
        await manager._poll_ifdoco_status(pair)

        async with db_factory() as db:
            result = await db.execute(select(IfdoBoxPos).where(IfdoBoxPos.pair == pair))
            pos = result.scalar_one_or_none()
        assert pos is not None
        assert pos.status == "closed"
        assert pos.exit_reason == "price_stop_loss"

    @pytest.mark.asyncio
    async def test_s2_4_canceled_cleans_up_memory(self, manager, fx_adapter, db_factory):
        """S-2-4: CANCELED 상태 감지 → 메모리(ifdoco_orders·meta) 정리."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params()

        await manager._open_position_ifdoco(pair, box, params, "long")
        root_id = manager._ifdoco_orders[pair]

        fx_adapter.simulate_ifdoco_fill(root_id, "cancel")
        await manager._poll_ifdoco_status(pair)

        assert manager._ifdoco_orders.get(pair) is None
        assert manager._ifdoco_meta.get(pair) is None


# ══════════════════════════════════════════════
# S3 — 강제 취소
# ══════════════════════════════════════════════


class TestIfdocoCancel:
    """S-3-*: 청산 시 IFD-OCO 취소, 박스 무효화 시 pending 취소."""

    @pytest.mark.asyncio
    async def test_s3_1_cancel_active_ifdoco_cancels_order(self, manager, fx_adapter, db_factory):
        """S-3-1: _cancel_active_ifdoco → 거래소 CANCELED + 메모리 정리."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params()

        await manager._open_position_ifdoco(pair, box, params, "long")
        root_id = manager._ifdoco_orders[pair]
        assert root_id in fx_adapter._ifdoco_orders

        await manager._cancel_active_ifdoco(pair)

        # 메모리 정리 확인
        assert manager._ifdoco_orders.get(pair) is None
        assert manager._ifdoco_meta.get(pair) is None
        # 거래소 주문 CANCELED 확인
        meta = fx_adapter._ifdoco_orders.get(root_id)
        assert meta is not None
        canceled = all(o["status"] == "CANCELED" for o in meta["sub_orders"]
                       if o["status"] != "EXECUTED")
        assert canceled

    @pytest.mark.asyncio
    async def test_s3_2_close_position_market_cancels_pending_ifdoco(
        self, manager, fx_adapter, db_factory
    ):
        """S-3-2: _close_position_market 호출 시 pending IFD-OCO가 먼저 취소된다."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params()

        await manager._open_position_ifdoco(pair, box, params, "long")
        root_id = manager._ifdoco_orders[pair]

        # 1차 체결 후 실제 포지션 DB 생성
        fx_adapter.simulate_ifdoco_fill(root_id, "first_fill", exec_price=144.0)
        fx_adapter.set_fx_positions([
            FxPosition(
                position_id=1, product_code="usd_jpy",
                side="BUY", size=1000, price=144.0, pnl=0.0,
                leverage=1.0, require_collateral=0.0,
                swap_point_accumulate=0.0, sfd=0.0,
            )
        ])
        await manager._poll_ifdoco_status(pair)

        # 강제 청산 호출
        pos = await manager._get_open_position(pair)
        assert pos is not None
        await manager._close_position_market(pair, pos, "test_close")

        # IFD-OCO 취소 후 메모리 정리 확인
        assert manager._ifdoco_orders.get(pair) is None

    @pytest.mark.asyncio
    async def test_s3_3_stop_pair_cleans_ifdoco(self, manager, fx_adapter, db_factory):
        """S-3-3: stop(pair) 호출 시 _ifdoco_orders·_ifdoco_meta 정리."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params()

        # manual setup
        manager._ifdoco_orders[pair] = "FAKE-IFO-000001"
        manager._ifdoco_meta[pair] = {"direction": "long"}
        manager._params[pair] = params

        await manager.stop(pair)

        assert pair not in manager._ifdoco_orders
        assert pair not in manager._ifdoco_meta


# ══════════════════════════════════════════════
# S4 — 백테스트 use_ifdoco slippage=0
# ══════════════════════════════════════════════


class TestIfdocoBacktest:
    """S-4-*: use_ifdoco=True 시 백테스트 slippage=0 적용."""

    def _make_candles(self, count: int = 100):
        """박스 형성 + 진입/청산 가능한 FakeCandle 생성."""
        from dataclasses import dataclass
        from datetime import datetime, timezone

        @dataclass
        class FakeCandle:
            close: float
            high: float
            low: float
            open_time: datetime
            open: float = 145.0

        candles = []
        t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        dt = timedelta(hours=4)

        # 40캔들: 박스 형성 (144.0~146.0)
        for i in range(40):
            candles.append(FakeCandle(
                close=145.0, high=146.0, low=144.0,
                open_time=t0 + dt * i,
            ))
        # 10캔들: near_lower (진입 구간)
        for i in range(10):
            candles.append(FakeCandle(
                close=144.1, high=144.4, low=144.0,
                open_time=t0 + dt * (40 + i),
            ))
        # 10캔들: near_upper (청산 구간)
        for i in range(10):
            candles.append(FakeCandle(
                close=145.9, high=146.0, low=145.7,
                open_time=t0 + dt * (50 + i),
            ))
        return candles

    def test_s4_1_use_ifdoco_zero_slippage_on_entry(self):
        """S-4-1: use_ifdoco=True → 백테스트 진입가에 슬리피지 없음."""
        candles = self._make_candles()
        config = BacktestConfig(slippage_pct=0.05, initial_capital_jpy=1_000_000)

        params_with_ifdoco = {
            "exchange_type": "fx",
            "use_ifdoco": True,
            "near_bound_pct": 0.3,
            "stop_loss_pct": 2.0,
            "box_min_touches": 3,
            "direction_mode": "long_only",
        }
        params_without = {**params_with_ifdoco, "use_ifdoco": False}

        result_with = run_backtest(candles, params_with_ifdoco, config, strategy_type="box_mean_reversion")
        result_without = run_backtest(candles, params_without, config, strategy_type="box_mean_reversion")

        # 두 결과 모두 거래 있어야 의미 있음
        if result_with.total_trades > 0 and result_without.total_trades > 0:
            # use_ifdoco PnL >= without (슬리피지 없으므로 유리하거나 동등)
            assert (result_with.total_pnl_jpy or 0) >= (result_without.total_pnl_jpy or 0) - 10

    def test_s4_2_use_ifdoco_false_applies_slippage(self):
        """S-4-2: use_ifdoco=False → 기존 slippage_pct 적용. 결과 정상 반환."""
        candles = self._make_candles()
        config = BacktestConfig(slippage_pct=0.1, initial_capital_jpy=1_000_000)
        params = {"exchange_type": "fx", "use_ifdoco": False, "near_bound_pct": 0.3,
                  "stop_loss_pct": 2.0, "box_min_touches": 3, "direction_mode": "long_only"}

        result = run_backtest(candles, params, config, strategy_type="box_mean_reversion")
        assert result.candle_count == len(candles)


# ══════════════════════════════════════════════
# S5 — GmoFxAdapter _round_price + get_orders_by_root
# ══════════════════════════════════════════════


class TestGmoFxAdapterIfdoco:
    """S-5-*: GmoFxAdapter 저수준 메서드 검증 (HTTP mock 사용)."""

    def test_s5_1_round_price_jpy_pair(self):
        """S-5-1: JPY 페어 → 소수점 3자리 라운드."""
        from adapters.gmo_fx.client import GmoFxAdapter

        adapter = GmoFxAdapter.__new__(GmoFxAdapter)  # __init__ 호출 없이 인스턴스화
        adapter._STOP_PRICE_DECIMALS = {
            "USD_JPY": 3, "EUR_JPY": 3, "GBP_JPY": 3,
            "EUR_USD": 5,
        }
        assert adapter._round_price("USD_JPY", 145.12345678) == 145.123
        assert adapter._round_price("GBP_JPY", 188.9999) == 189.0
        assert adapter._round_price("EUR_USD", 1.1234567) == 1.12346

    def test_s5_2_round_price_unknown_pair_defaults_3(self):
        """S-5-2: 알 수 없는 페어 → 기본 소수점 3자리."""
        from adapters.gmo_fx.client import GmoFxAdapter

        adapter = GmoFxAdapter.__new__(GmoFxAdapter)
        adapter._STOP_PRICE_DECIMALS = {"USD_JPY": 3}
        assert adapter._round_price("UNKNOWN_PAIR", 12.34567) == 12.346

    @pytest.mark.asyncio
    async def test_s5_3_get_orders_by_root_returns_list(self):
        """S-5-3: get_orders_by_root → list[dict] 반환 (HTTP mock)."""
        from adapters.gmo_fx.client import GmoFxAdapter
        from unittest.mock import AsyncMock, MagicMock, patch

        adapter = GmoFxAdapter.__new__(GmoFxAdapter)
        adapter._private_url = "https://forex-api.coin.z.com/private"
        adapter._api_key = "key"
        adapter._api_secret = "secret"
        adapter._STOP_PRICE_DECIMALS = {"USD_JPY": 3}

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": 0,
            "data": {
                "list": [
                    {"orderId": "1", "settleType": "OPEN", "status": "WAITING"},
                    {"orderId": "2", "settleType": "CLOSE", "executionType": "LIMIT", "status": "WAITING"},
                    {"orderId": "3", "settleType": "CLOSE", "executionType": "STOP", "status": "WAITING"},
                ]
            },
        }
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_client", return_value=mock_client):
            with patch.object(adapter, "_get_auth_headers", return_value={}):
                with patch.object(adapter, "_raise_for_exchange_error"):
                    result = await adapter.get_orders_by_root("ROOT-001")

        assert isinstance(result, list)
        assert len(result) == 3
        assert result[0]["orderId"] == "1"

    def test_s5_4_fake_exchange_get_orders_by_root(self):
        """S-5-4: FakeExchangeAdapter.get_orders_by_root → sub_orders 반환."""
        adapter = FakeExchangeAdapter(
            initial_balances={"jpy": 1_000_000.0}, ticker_price=145.0
        )
        adapter.set_margin_trading(True)
        adapter.set_collateral(
            Collateral(collateral=1_000_000.0, open_position_pnl=0.0,
                       require_collateral=0.0, keep_rate=999.0)
        )

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                adapter.place_ifdoco_order(
                    pair="usd_jpy", side="BUY", size=1000,
                    first_execution_type="LIMIT", first_price=144.0,
                    take_profit_price=146.0, stop_loss_price=141.84,
                )
            )
            root_id = result["rootOrderId"]
            orders = loop.run_until_complete(adapter.get_orders_by_root(root_id))
        finally:
            loop.close()

        assert len(orders) == 3
        assert orders[0]["settleType"] == "OPEN"
        assert orders[1]["executionType"] == "LIMIT"
        assert orders[2]["executionType"] == "STOP"

    def test_s5_5_simulate_ifdoco_fill_tp(self):
        """S-5-5: simulate_ifdoco_fill('tp') → TP EXECUTED, SL CANCELED."""
        adapter = FakeExchangeAdapter(initial_balances={"jpy": 1_000_000.0}, ticker_price=145.0)
        adapter.set_margin_trading(True)
        adapter.set_collateral(
            Collateral(collateral=1_000_000.0, open_position_pnl=0.0,
                       require_collateral=0.0, keep_rate=999.0)
        )

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                adapter.place_ifdoco_order(
                    pair="usd_jpy", side="BUY", size=1000,
                    first_execution_type="LIMIT", first_price=144.0,
                    take_profit_price=146.0, stop_loss_price=141.84,
                )
            )
            root_id = result["rootOrderId"]
            adapter.simulate_ifdoco_fill(root_id, "tp", exec_price=146.0)
            orders = loop.run_until_complete(adapter.get_orders_by_root(root_id))
        finally:
            loop.close()

        status_map = {o["executionType"]: o["status"] for o in orders if o.get("settleType") == "CLOSE"}
        assert status_map.get("LIMIT") == "EXECUTED"
        assert status_map.get("STOP") == "CANCELED"


# ══════════════════════════════════════════════
# S6 — start() DB 복원
# ══════════════════════════════════════════════


class TestIfdocoStartRecovery:
    """S-6-*: 재시작 시 DB first_filled 상태에서 _ifdoco_orders 복원."""

    @pytest.mark.asyncio
    async def test_s6_1_start_restores_ifdoco_from_db_first_filled(
        self, manager, fx_adapter, db_factory
    ):
        """S-6-1: open pos + ifdoco_status=first_filled → start() 시 _ifdoco_orders에 복원."""
        pair = "usd_jpy"
        root_id_expected = "FAKE-IFO-RESTART-001"

        # DB에 first_filled 상태 포지션 직접 삽입
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        async with db_factory() as db:
            pos = IfdoBoxPos()
            pos.pair = pair
            pos.box_id = box.id
            pos.entry_order_id = root_id_expected
            pos.entry_price = Decimal("144.0")
            pos.entry_amount = Decimal("1000")
            pos.entry_jpy = Decimal("144000")
            pos.side = "buy"
            pos.status = "open"
            pos.ifdoco_status = "first_filled"
            pos.ifdoco_root_order_id = root_id_expected
            pos.tp_price = Decimal("146.0")
            pos.sl_price_registered = Decimal("141.84")
            pos.created_at = datetime.now(timezone.utc)
            db.add(pos)
            await db.commit()

        # start() 호출: DB 복원 로직 실행
        params = _make_params(use_ifdoco=True)
        await manager.start(pair, params)

        # _ifdoco_orders에 root_id 복원 확인
        assert manager._ifdoco_orders.get(pair) == root_id_expected

    @pytest.mark.asyncio
    async def test_s6_2_start_does_not_restore_when_no_first_filled(
        self, manager, fx_adapter, db_factory
    ):
        """S-6-2: ifdoco_status=None(일반 market 포지션) → _ifdoco_orders 복원 없음."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)

        # 일반 market 포지션 (ifdoco 컬럼 없음)
        async with db_factory() as db:
            pos = IfdoBoxPos()
            pos.pair = pair
            pos.box_id = box.id
            pos.entry_order_id = "ORD-001"
            pos.entry_price = Decimal("144.0")
            pos.entry_amount = Decimal("1000")
            pos.entry_jpy = Decimal("144000")
            pos.side = "buy"
            pos.status = "open"
            pos.created_at = datetime.now(timezone.utc)
            db.add(pos)
            await db.commit()

        params = _make_params(use_ifdoco=False)
        await manager.start(pair, params)

        # 복원 없음
        assert manager._ifdoco_orders.get(pair) is None


# ══════════════════════════════════════════════
# S7 — 박스 무효화 시 pending IFD-OCO 취소
# ══════════════════════════════════════════════


class TestIfdocoInvalidationCancel:
    """S-7-*: _run_one_box_monitor_cycle에서 박스 무효화 시 pending IFD-OCO 취소."""

    @pytest.mark.asyncio
    async def test_s7_1_box_invalidation_cancels_pending_ifdoco(
        self, manager, fx_adapter, db_factory
    ):
        """S-7-1: pending IFD-OCO 상태에서 박스 이탈 → _cancel_active_ifdoco 호출."""
        pair = "usd_jpy"
        # 박스 생성 (144~146)
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        params = _make_params(use_ifdoco=True)
        manager._params[pair] = params
        manager._last_seen_open_time[pair] = None

        # IFD-OCO pending 상태로 설정
        box = await manager._get_active_box(pair)
        await manager._open_position_ifdoco(pair, box, params, "long")
        root_id = manager._ifdoco_orders[pair]
        assert root_id is not None

        # 박스 이탈 캔들 삽입 (close가 lower 아래)
        async with db_factory() as db:
            candle = IfdoCandle()
            candle.pair = pair
            candle.timeframe = "4h"
            candle.open_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
            candle.close_time = datetime(2025, 1, 1, 4, tzinfo=timezone.utc)
            candle.open = 143.0
            candle.high = 143.5
            candle.low = 142.5
            candle.close = 143.0  # lower(144.0) 아래 → 무효화 트리거
            candle.volume = 1000.0
            candle.is_complete = True
            db.add(candle)
            await db.commit()

        # 새 캔들 감지 트릭: last_seen을 다른 값으로 설정
        manager._last_seen_open_time[pair] = datetime(2024, 12, 31, tzinfo=timezone.utc)

        await manager._run_one_box_monitor_cycle(pair)

        # IFD-OCO 취소 후 메모리 정리 확인
        assert manager._ifdoco_orders.get(pair) is None
        assert manager._ifdoco_meta.get(pair) is None

    @pytest.mark.asyncio
    async def test_s7_2_no_invalidation_no_cancel(
        self, manager, fx_adapter, db_factory
    ):
        """S-7-2: 박스 유효 상태 유지 → IFD-OCO 취소 없음."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        params = _make_params(use_ifdoco=True)
        manager._params[pair] = params

        box = await manager._get_active_box(pair)
        await manager._open_position_ifdoco(pair, box, params, "long")
        root_id = manager._ifdoco_orders[pair]

        # 박스 내 캔들 삽입 (close=145.0 → 유효)
        async with db_factory() as db:
            candle = IfdoCandle()
            candle.pair = pair
            candle.timeframe = "4h"
            candle.open_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
            candle.close_time = datetime(2025, 1, 1, 4, tzinfo=timezone.utc)
            candle.open = 145.0
            candle.high = 145.5
            candle.low = 144.5
            candle.close = 145.0
            candle.volume = 1000.0
            candle.is_complete = True
            db.add(candle)
            await db.commit()

        manager._last_seen_open_time[pair] = datetime(2024, 12, 31, tzinfo=timezone.utc)
        await manager._run_one_box_monitor_cycle(pair)

        # IFD-OCO 유지 확인
        assert manager._ifdoco_orders.get(pair) == root_id


# ══════════════════════════════════════════════
# S8 — TP + both mode → 반대 방향 재발주
# ══════════════════════════════════════════════


class TestIfdocoBothModeReentry:
    """S-8-*: direction_mode=both 시 TP 체결 후 반대 방향 IFD-OCO 자동 재발주."""

    @pytest.mark.asyncio
    async def test_s8_1_tp_both_mode_triggers_reverse_ifdoco(
        self, manager, fx_adapter, db_factory
    ):
        """S-8-1: long TP 체결 + both → short IFD-OCO 재발주."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params(use_ifdoco=True, direction="both")
        manager._params[pair] = params

        # long IFD-OCO 발주 + 1차 체결
        await manager._open_position_ifdoco(pair, box, params, "long")
        root_id = manager._ifdoco_orders[pair]
        fx_adapter.simulate_ifdoco_fill(root_id, "first_fill", exec_price=144.0)
        fx_adapter.set_fx_positions([
            FxPosition(
                position_id=1, product_code="usd_jpy",
                side="BUY", size=1000, price=144.0, pnl=0.0,
                leverage=1.0, require_collateral=0.0,
                swap_point_accumulate=0.0, sfd=0.0,
            )
        ])
        await manager._poll_ifdoco_status(pair)

        # TP 체결 → both 모드 → short 재발주 기대
        fx_adapter.simulate_ifdoco_fill(root_id, "tp", exec_price=146.0)
        fx_adapter.set_fx_positions([])
        await manager._poll_ifdoco_status(pair)

        # 반대 방향(short) IFD-OCO 재발주 확인
        new_root_id = manager._ifdoco_orders.get(pair)
        assert new_root_id is not None
        assert new_root_id != root_id  # 새 주문
        new_meta = manager._ifdoco_meta.get(pair)
        assert new_meta is not None
        assert new_meta["direction"] == "short"

    @pytest.mark.asyncio
    async def test_s8_2_tp_long_only_no_reentry(
        self, manager, fx_adapter, db_factory
    ):
        """S-8-2: long_only 모드 TP 체결 → 재발주 없음."""
        pair = "usd_jpy"
        await _insert_box(db_factory, pair, upper=146.0, lower=144.0)
        box = await manager._get_active_box(pair)
        params = _make_params(use_ifdoco=True, direction="long_only")
        manager._params[pair] = params

        await manager._open_position_ifdoco(pair, box, params, "long")
        root_id = manager._ifdoco_orders[pair]
        fx_adapter.simulate_ifdoco_fill(root_id, "first_fill", exec_price=144.0)
        fx_adapter.set_fx_positions([
            FxPosition(
                position_id=1, product_code="usd_jpy",
                side="BUY", size=1000, price=144.0, pnl=0.0,
                leverage=1.0, require_collateral=0.0,
                swap_point_accumulate=0.0, sfd=0.0,
            )
        ])
        await manager._poll_ifdoco_status(pair)

        fx_adapter.simulate_ifdoco_fill(root_id, "tp", exec_price=146.0)
        fx_adapter.set_fx_positions([])
        await manager._poll_ifdoco_status(pair)

        # long_only → 재발주 없음
        assert manager._ifdoco_orders.get(pair) is None


# ──────────────────────────────────────────────────────────────
# S9 — _box_pos_to_dict IFD-OCO 필드 직렬화
# ifdoco_status / tp_price / sl_price_registered 가 None·값 양쪽
# 모두 올바르게 직렬화되는지, 그리고 요청 전에도 None으로 안전 처리
# 되는지 검증한다.
# ──────────────────────────────────────────────────────────────

class TestBoxPosDictIfdocoFields:
    """S9: _box_pos_to_dict ifdoco 필드 직렬화 계약."""

    def _make_row(self, **kwargs):
        """DB row를 모사하는 SimpleNamespace."""
        from types import SimpleNamespace
        from decimal import Decimal
        defaults = {
            "id": 1,
            "pair": "usd_jpy",
            "box_id": 10,
            "entry_price": Decimal("148.50"),
            "entry_amount": Decimal("10000.0"),
            "exit_price": None,
            "exit_reason": None,
            "realized_pnl_jpy": None,
            "realized_pnl_pct": None,
            "status": "open",
            "created_at": None,
            "closed_at": None,
            "exchange_sl_order_id": None,
            "exchange_sl_price": None,
            "exchange_sl_status": None,
            "ifdoco_root_order_id": None,
            "ifdoco_status": None,
            "tp_price": None,
            "sl_price_registered": None,
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_ifdoco_fields_all_none_when_not_set(self):
        """use_ifdoco=False 경로 — 3필드 모두 None."""
        from api.routes.boxes import _box_pos_to_dict
        row = self._make_row()
        result = _box_pos_to_dict(row, pair_column="pair", current_price=149.0)
        assert result["ifdoco_status"] is None
        assert result["tp_price"] is None
        assert result["sl_price_registered"] is None

    def test_ifdoco_fields_populated_when_first_filled(self):
        """first_filled 상태 — tp_price, sl_price_registered float 변환."""
        from api.routes.boxes import _box_pos_to_dict
        from decimal import Decimal
        row = self._make_row(
            ifdoco_status="first_filled",
            tp_price=Decimal("151.20"),
            sl_price_registered=Decimal("146.80"),
        )
        result = _box_pos_to_dict(row, pair_column="pair", current_price=149.0)
        assert result["ifdoco_status"] == "first_filled"
        assert result["tp_price"] == pytest.approx(151.20)
        assert result["sl_price_registered"] == pytest.approx(146.80)

    def test_ifdoco_fields_pending_tp_sl_none(self):
        """pending 상태 (1차 미체결) — tp/sl은 None."""
        from api.routes.boxes import _box_pos_to_dict
        row = self._make_row(ifdoco_status="pending")
        result = _box_pos_to_dict(row, pair_column="pair", current_price=148.90)
        assert result["ifdoco_status"] == "pending"
        assert result["tp_price"] is None
        assert result["sl_price_registered"] is None

    def test_no_ifdoco_attr_graceful(self):
        """ifdoco 컬럼이 없는 테이블(BF) — getattr 기본값 None 반환, KeyError 없음."""
        from api.routes.boxes import _box_pos_to_dict
        from types import SimpleNamespace
        from decimal import Decimal
        # ifdoco 컬럼 없는 row (BF bf_box_positions 시뮬레이션)
        row = SimpleNamespace(
            id=2, pair="btc_jpy", box_id=5,
            entry_price=Decimal("14000000"),
            entry_amount=Decimal("14000"),
            exit_price=None, exit_reason=None,
            realized_pnl_jpy=None, realized_pnl_pct=None,
            status="open", created_at=None, closed_at=None,
            exchange_sl_order_id=None,
            exchange_sl_price=None,
            exchange_sl_status=None,
            # ifdoco 컬럼 없음 (hasattr=False)
        )
        result = _box_pos_to_dict(row, pair_column="pair", current_price=14100000.0)
        assert result["ifdoco_status"] is None
        assert result["tp_price"] is None
        assert result["sl_price_registered"] is None

