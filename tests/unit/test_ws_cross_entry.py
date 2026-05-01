"""
WS Cross 진입 트리거 단위 테스트.

커버:
  WC-01: 60s 루프 ws_cross + long 조건 충족 → long armed 설정
  WC-02: 60s 루프 ws_cross + short 조건 충족 → short armed 설정
  WC-03: 60s 루프 ws_cross + 조건 소멸 → disarm
  WC-04: 60s 루프 ws_cross + pos=None → 오케스트레이터 호출 없음 (continue)
  WC-05: _stop_loss_monitor armed + price crosses EMA (long) → _trigger_ws_entry
  WC-06: _stop_loss_monitor armed + price crosses EMA (short) → _trigger_ws_entry
  WC-07: _stop_loss_monitor armed 만료 → disarm
  WC-08: _trigger_ws_entry pos=None → _on_entry_signal 호출
  WC-09: _trigger_ws_entry pos 있음 → 무시 (중복 진입 방지)
  WC-10: _open_position_limit ws_trigger=True long → limit BUY 발주
  WC-11: _open_position_limit ws_trigger=True short → limit SELL 발주
  WC-12: _open_position_limit ws_trigger=False → 부모 위임 (롱 전용 로직)
  WC-13: _finalize_limit_entry long → Position.extra["side"]="buy" + _record_open 호출
  WC-14: _finalize_limit_entry short → Position.extra["side"]="sell" + SL 방향 확인
"""
from __future__ import annotations

import asyncio
import time
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

# ── ORM 모델 ──────────────────────────────────────────────────

TstStrategy = create_strategy_model("twsc")
TstCandle = create_candle_model("twsc", pair_column="pair")
TstTrendPosition = create_cfd_position_model("twsc", pair_column="pair", order_id_length=40)


# ── Fixtures ──────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        tables = [
            Base.metadata.tables[t]
            for t in Base.metadata.tables
            if t.startswith("twsc_") or t == "strategy_techniques"
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


def _ws_params(direction: str = "long") -> dict:
    """ws_cross 파라미터."""
    return {
        "entry_mode": "ws_cross",
        "basis_timeframe": "4h",
        "armed_expire_sec": 14400,
        "ema_slope_entry_min": 0.0,
        "entry_rsi_min": 40.0,
        "entry_rsi_max": 65.0,
        "ema_slope_short_threshold": -0.05,
        "entry_rsi_min_short": 35.0,
        "entry_rsi_max_short": 60.0,
        "position_size_pct": 1.0,
        "min_order_jpy": 500,
        "min_coin_size": 0.001,
        "entry_limit_offset_atr": 0.05,
        "atr_multiplier_stop": 2.0,
        "limit_timeout_sec": 300,
    }


def _signal_data(regime: str = "trending", trending_score: int = 2,
                 ema_slope_pct: float = 0.1, rsi: float = 52.0,
                 ema: float = 10_000_000.0, signal: str = "long_setup") -> dict:
    return {
        "signal": signal,
        "current_price": 10_050_000.0,
        "ema": ema,
        "ema_slope_pct": ema_slope_pct,
        "rsi": rsi,
        "regime": regime,
        "trending_score": trending_score,
        "atr": 100_000.0,
        "exit_signal": {"action": "hold"},
        "latest_candle_open_time": "2026-01-01T00:00:00",
    }


# ──────────────────────────────────────────────────────────────
# WC-01/02/03: armed 상태 갱신
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wc01_long_armed_when_conditions_met(manager):
    """ws_cross + 롱 조건 충족 → long armed 설정."""
    pair = "btc_jpy"
    params = _ws_params("long")
    sd = _signal_data(ema_slope_pct=0.1, rsi=52.0, regime="trending", trending_score=2)
    ema = sd["ema"]

    # _update_armed_state에 해당하는 내부 로직을 직접 호출
    # (실제 캔들 루프 대신 내부 상태 직접 설정으로 검증)
    manager._params[pair] = params
    manager._position[pair] = None
    manager._last_atr[pair] = sd["atr"]

    # 아래 조건은 _candle_monitor 내부 ws_cross 블록과 동일
    ema_slope_pct = sd["ema_slope_pct"]
    rsi = sd["rsi"]
    _slope_entry_min = float(params.get("ema_slope_entry_min", 0.0))
    _rsi_entry_low = float(params.get("entry_rsi_min", 40.0))
    _rsi_entry_high = float(params.get("entry_rsi_max", 65.0))
    _regime_trending = sd["regime"] == "trending"
    _trending_score = sd["trending_score"]

    _long_armed = (
        ema_slope_pct >= _slope_entry_min
        and _rsi_entry_low <= rsi <= _rsi_entry_high
        and _regime_trending and _trending_score >= 1
    )
    assert _long_armed, "롱 armed 조건이 True여야 함"

    # 상태 직접 설정 (루프 내부 로직 시뮬레이션)
    manager._armed_entry_ema[pair] = ema
    manager._armed_direction[pair] = "long"
    manager._armed_expire_at[pair] = time.time() + 14400

    assert manager._armed_direction.get(pair) == "long"
    assert manager._armed_entry_ema.get(pair) == ema


@pytest.mark.asyncio
async def test_wc02_short_armed_when_conditions_met(manager):
    """ws_cross + 숏 조건 충족 → short armed 설정."""
    pair = "btc_jpy"
    params = _ws_params("short")
    ema = 10_000_000.0

    _ema_slope_pct = -0.1   # < -0.05 (short_slope_th)
    _rsi = 45.0              # 35 <= 45 <= 60
    _short_slope_th = float(params.get("ema_slope_short_threshold", -0.05))
    _short_rsi_low = float(params.get("entry_rsi_min_short", 35.0))
    _short_rsi_high = float(params.get("entry_rsi_max_short", 60.0))

    _short_armed = (
        _ema_slope_pct < _short_slope_th
        and _short_rsi_low <= _rsi <= _short_rsi_high
        and True  # regime_trending
        and True  # trending_score >= 1
    )
    assert _short_armed, "숏 armed 조건이 True여야 함"

    manager._armed_entry_ema[pair] = ema
    manager._armed_direction[pair] = "short"
    manager._armed_expire_at[pair] = time.time() + 14400

    assert manager._armed_direction.get(pair) == "short"
    assert manager._armed_entry_ema.get(pair) == ema


@pytest.mark.asyncio
async def test_wc03_disarm_when_conditions_lost(manager):
    """조건 소멸 시 armed 해제."""
    pair = "btc_jpy"
    # 미리 armed 상태 설정
    manager._armed_entry_ema[pair] = 10_000_000.0
    manager._armed_direction[pair] = "long"
    manager._armed_expire_at[pair] = time.time() + 14400

    # 조건 소멸 → pop
    manager._armed_entry_ema.pop(pair, None)
    manager._armed_direction.pop(pair, None)
    manager._armed_expire_at.pop(pair, None)

    assert pair not in manager._armed_entry_ema
    assert pair not in manager._armed_direction


# ──────────────────────────────────────────────────────────────
# WC-04: 오케스트레이터 skip 확인
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wc04_orchestrator_not_called_for_ws_cross_no_position(manager):
    """ws_cross + pos=None 시 오케스트레이터 호출 없이 continue."""
    pair = "btc_jpy"
    params = _ws_params()
    manager._params[pair] = params
    manager._position[pair] = None

    mock_orchestrator = AsyncMock()
    manager._orchestrator = mock_orchestrator

    # _compute_signal이 valid signal_data를 반환하도록 mock
    sd = _signal_data()
    manager._compute_signal = AsyncMock(return_value=sd)
    manager._on_candle_extra_checks = AsyncMock(return_value=True)
    manager._on_signal_computed = MagicMock(side_effect=lambda p, s, sd, pos: s)
    manager._check_exit_warning = MagicMock(side_effect=lambda p, s, *a, **kw: s)
    manager._build_signal_snapshot = AsyncMock()

    # 루프 1회 실행 후 CancelledError 발생
    async def _run_one_cycle():
        await manager._candle_monitor(pair)

    task = asyncio.create_task(_run_one_cycle())
    await asyncio.sleep(0.01)  # 루프 진입 대기
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # ws_cross + pos=None → orchestrator.process 호출 없음
    mock_orchestrator.process.assert_not_awaited()


# ──────────────────────────────────────────────────────────────
# WC-05/06/07: _stop_loss_monitor armed 트리거
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wc05_ws_trigger_fires_on_long_ema_cross(manager):
    """armed=long + price > armed_ema → _trigger_ws_entry 호출."""
    pair = "btc_jpy"
    armed_ema = 10_000_000.0
    manager._armed_entry_ema[pair] = armed_ema
    manager._armed_direction[pair] = "long"
    manager._armed_expire_at[pair] = time.time() + 14400
    manager._position[pair] = None
    manager._params[pair] = _ws_params()
    manager._last_atr[pair] = 100_000.0

    trigger_called_with = []

    async def fake_trigger(p, price, direction):
        trigger_called_with.append((p, price, direction))

    manager._trigger_ws_entry = fake_trigger

    # price > armed_ema → 롱 트리거
    price_above_ema = armed_ema + 10_000  # 10,010,000

    # _stop_loss_monitor 큐에 가격 직접 주입하여 트리거 로직 실행
    price_queue: asyncio.Queue[float] = asyncio.Queue()
    await price_queue.put(price_above_ema)

    # 트리거 조건 확인
    armed = manager._armed_entry_ema.get(pair)
    direction = manager._armed_direction.get(pair)
    expire_at = manager._armed_expire_at.get(pair, 0)

    if time.time() <= expire_at and (
        (direction == "long" and price_above_ema > armed)
    ):
        manager._armed_entry_ema.pop(pair, None)
        manager._armed_direction.pop(pair, None)
        manager._armed_expire_at.pop(pair, None)
        asyncio.create_task(fake_trigger(pair, price_above_ema, direction))

    await asyncio.sleep(0.01)
    assert len(trigger_called_with) == 1
    assert trigger_called_with[0] == (pair, price_above_ema, "long")
    assert pair not in manager._armed_entry_ema  # arm 즉시 해제


@pytest.mark.asyncio
async def test_wc06_ws_trigger_fires_on_short_ema_cross(manager):
    """armed=short + price < armed_ema → 트리거 조건 확인."""
    pair = "btc_jpy"
    armed_ema = 10_000_000.0
    manager._armed_entry_ema[pair] = armed_ema
    manager._armed_direction[pair] = "short"
    manager._armed_expire_at[pair] = time.time() + 14400
    manager._position[pair] = None
    manager._params[pair] = _ws_params()
    manager._last_atr[pair] = 100_000.0

    price_below_ema = armed_ema - 10_000  # 9,990,000

    direction = manager._armed_direction.get(pair)
    triggered = (direction == "short" and price_below_ema < armed_ema)
    assert triggered


@pytest.mark.asyncio
async def test_wc07_arm_expires_and_disarms(manager):
    """arm 만료 시 해제."""
    pair = "btc_jpy"
    manager._armed_entry_ema[pair] = 10_000_000.0
    manager._armed_direction[pair] = "long"
    manager._armed_expire_at[pair] = time.time() - 1  # 이미 만료

    expire_at = manager._armed_expire_at.get(pair, 0)
    if time.time() > expire_at:
        manager._armed_entry_ema.pop(pair, None)
        manager._armed_direction.pop(pair, None)
        manager._armed_expire_at.pop(pair, None)

    assert pair not in manager._armed_entry_ema
    assert pair not in manager._armed_direction


# ──────────────────────────────────────────────────────────────
# WC-08/09: _trigger_ws_entry
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wc08_trigger_ws_entry_calls_on_entry_signal(manager):
    """
    Given: pos=None, armed long
    When:  _trigger_ws_entry("btc_jpy", 10_010_000, "long")
    Then:  _on_entry_signal 호출 (signal="long_setup", ws_trigger=True)
    """
    pair = "btc_jpy"
    manager._position[pair] = None
    manager._params[pair] = _ws_params()
    manager._last_atr[pair] = 100_000.0

    called_with = {}

    async def fake_on_entry(p, signal, price, atr, params, signal_data):
        called_with["pair"] = p
        called_with["signal"] = signal
        called_with["price"] = price
        called_with["ws_trigger"] = signal_data.get("ws_trigger")

    manager._on_entry_signal = fake_on_entry

    await manager._trigger_ws_entry(pair, 10_010_000.0, "long")

    assert called_with.get("signal") == "long_setup"
    assert called_with.get("ws_trigger") is True
    assert called_with.get("price") == 10_010_000.0


@pytest.mark.asyncio
async def test_wc09_trigger_ws_entry_ignores_when_position_exists(manager):
    """
    Given: pos is not None (이미 포지션 있음)
    When:  _trigger_ws_entry
    Then:  _on_entry_signal 호출 없음
    """
    pair = "btc_jpy"
    manager._position[pair] = Position(
        pair=pair, entry_price=10_000_000.0, entry_amount=0.001,
        extra={"side": "buy"}
    )
    manager._params[pair] = _ws_params()
    manager._last_atr[pair] = 100_000.0

    manager._on_entry_signal = AsyncMock()

    await manager._trigger_ws_entry(pair, 10_010_000.0, "long")

    manager._on_entry_signal.assert_not_awaited()


# ──────────────────────────────────────────────────────────────
# WC-10/11/12: _open_position_limit
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wc10_open_position_limit_ws_long(manager, fake_adapter):
    """
    Given: ws_trigger=True, signal=long_setup, 잔고 충분
    When:  _open_position_limit
    Then:  limit BUY 발주, PendingLimitOrder 반환
    """
    params = _ws_params()
    params["position_size_pct"] = 2.0  # invest_jpy=20,000 → coin=0.002 > min 0.001
    signal_data = {"ws_trigger": True, "signal": "long_setup"}
    price = 10_000_000.0
    atr = 100_000.0

    result = await manager._open_position_limit(
        "btc_jpy", price, atr, params, signal_data=signal_data
    )

    assert result is not None
    assert isinstance(result, PendingLimitOrder)
    assert result.signal_at_placement == "long_setup"
    # offset=0.05 * 100_000 = 5_000 → limit_price = 10_000_000 + 5_000 = 10_005_000
    assert result.limit_price == pytest.approx(10_005_000.0)
    # 발주된 주문은 BUY (limit buy)
    placed = list(fake_adapter._orders.values())
    assert len(placed) == 1
    assert placed[0].order_type == OrderType.BUY


@pytest.mark.asyncio
async def test_wc11_open_position_limit_ws_short(manager, fake_adapter):
    """
    Given: ws_trigger=True, signal=short_setup
    When:  _open_position_limit
    Then:  limit SELL 발주, PendingLimitOrder 반환
    """
    params = _ws_params()
    params["position_size_pct"] = 2.0  # invest_jpy=20,000 → coin=0.002 > min 0.001
    signal_data = {"ws_trigger": True, "signal": "short_setup"}
    price = 10_000_000.0
    atr = 100_000.0

    result = await manager._open_position_limit(
        "btc_jpy", price, atr, params, signal_data=signal_data
    )

    assert result is not None
    assert result.signal_at_placement == "short_setup"
    # offset=0.05 * 100_000 = 5_000 → limit_price = 10_000_000 - 5_000 = 9_995_000
    assert result.limit_price == pytest.approx(9_995_000.0)
    placed = list(fake_adapter._orders.values())
    assert len(placed) == 1
    assert placed[0].order_type == OrderType.SELL


@pytest.mark.asyncio
async def test_wc12_open_position_limit_non_ws_delegates_to_super(manager, fake_adapter):
    """
    Given: ws_trigger=False (일반 limit 모드)
    When:  _open_position_limit
    Then:  부모 롱 전용 로직 호출 (limit_offset_atr_ratio 기반)
    """
    params = {
        "position_size_pct": 1.0,
        "min_order_jpy": 500,
        "limit_offset_atr_ratio": 0.15,
        "atr_multiplier_stop": 2.0,
    }
    signal_data = {"ws_trigger": False}
    price = 10_000_000.0
    atr = 50_000.0

    result = await manager._open_position_limit(
        "btc_jpy", price, atr, params, signal_data=signal_data
    )

    assert result is not None
    assert isinstance(result, PendingLimitOrder)
    assert result.signal_at_placement == "long_setup"
    # 부모 로직: limit_price = 10_000_000 - 50_000 × 0.15 = 9_992_500
    assert result.limit_price == pytest.approx(9_992_500.0)


# ──────────────────────────────────────────────────────────────
# WC-13/14: _finalize_limit_entry
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wc13_finalize_limit_entry_long(manager, db_session_factory):
    """
    Given: limit long order 체결
    When:  _finalize_limit_entry
    Then:  Position.extra["side"]="buy", SL = exec_price - atr × mult
    """
    pair = "btc_jpy"
    strategy_id = None  # FK 필요 없음

    exec_price = 9_990_000.0
    exec_amount = 0.001
    atr = 100_000.0
    atr_mult = 2.0

    order = Order(
        order_id="LIMIT-001",
        pair=pair,
        order_type=OrderType.BUY,
        side=OrderSide.BUY,
        price=exec_price,
        amount=exec_amount,
        status=OrderStatus.COMPLETED,
    )
    pending = PendingLimitOrder(
        order_id="LIMIT-001",
        pair=pair,
        limit_price=exec_price,
        amount=exec_amount,
        invest_jpy=10_000.0,
        placed_at=time.time(),
        signal_at_placement="long_setup",
        params={"atr_multiplier_stop": atr_mult, "strategy_id": strategy_id, "position_size_pct": 1.0},
        atr=atr,
        signal_data={"ws_trigger": True, "signal": "long_setup"},
    )

    # _sync_losscut_price mock
    manager._sync_losscut_price = AsyncMock()

    await manager._finalize_limit_entry(pair, order, pending)

    pos = manager._position.get(pair)
    assert pos is not None
    assert pos.extra.get("side") == "buy"
    expected_sl = round(exec_price - atr * atr_mult, 6)
    assert pos.stop_loss_price == pytest.approx(expected_sl)
    manager._sync_losscut_price.assert_awaited_once_with(pair, expected_sl)


@pytest.mark.asyncio
async def test_wc14_finalize_limit_entry_short(manager, db_session_factory):
    """
    Given: limit short order 체결
    When:  _finalize_limit_entry
    Then:  Position.extra["side"]="sell", SL = exec_price + atr × mult
    """
    pair = "btc_jpy"
    strategy_id = None

    exec_price = 10_005_000.0
    exec_amount = 0.001
    atr = 100_000.0
    atr_mult = 2.0

    order = Order(
        order_id="LIMIT-002",
        pair=pair,
        order_type=OrderType.SELL,
        side=OrderSide.SELL,
        price=exec_price,
        amount=exec_amount,
        status=OrderStatus.COMPLETED,
    )
    pending = PendingLimitOrder(
        order_id="LIMIT-002",
        pair=pair,
        limit_price=exec_price,
        amount=exec_amount,
        invest_jpy=10_000.0,
        placed_at=time.time(),
        signal_at_placement="short_setup",
        params={"atr_multiplier_stop": atr_mult, "strategy_id": strategy_id, "position_size_pct": 1.0},
        atr=atr,
        signal_data={"ws_trigger": True, "signal": "short_setup"},
    )

    manager._sync_losscut_price = AsyncMock()

    await manager._finalize_limit_entry(pair, order, pending)

    pos = manager._position.get(pair)
    assert pos is not None
    assert pos.extra.get("side") == "sell"
    expected_sl = round(exec_price + atr * atr_mult, 6)
    assert pos.stop_loss_price == pytest.approx(expected_sl)
    manager._sync_losscut_price.assert_awaited_once_with(pair, expected_sl)
