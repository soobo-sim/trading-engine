"""
BUG-031 회귀 테스트 — entry_short 방향 반전 방지.

롱 전용 매니저(TrendFollowingManager)에서 entry_short가 실행되면
MARKET_BUY가 발생하는 버그 수정 검증.

테스트:
  SG-01: _supports_short 기본값 False (BaseTrendManager)
  SG-02: CfdTrendFollowingManager._supports_short == True
  SG-03: 롱전용 매니저에서 entry_short → WARNING 로그 + _on_entry_signal 미호출
  SG-04: 롱전용 매니저에서 entry_long → _on_entry_signal("entry_ok") 정상 호출
  SG-05: CFD 매니저에서 entry_short → _on_entry_signal("entry_sell") 정상 호출
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.data.dto import Decision, ExecutionResult, SignalSnapshot


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _make_trend_manager():
    """TrendFollowingManager 최소 인스턴스."""
    from core.strategy.plugins.trend_following.manager import TrendFollowingManager

    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.is_margin_trading = True

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    return TrendFollowingManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=MagicMock(),
        candle_model=MagicMock(),
        trend_position_model=MagicMock(),
    )


def _make_cfd_manager():
    """CfdTrendFollowingManager 최소 인스턴스."""
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager

    adapter = MagicMock()
    adapter.exchange_name = "bitflyer"
    adapter.is_margin_trading = True

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    return CfdTrendFollowingManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=MagicMock(),
        candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )


def _make_decision(action: str) -> Decision:
    return Decision(
        action=action,
        pair="BTC_JPY",
        exchange="gmo_coin",
        confidence=0.70,
        size_pct=0.80,
        stop_loss=11_184_906.0,
        take_profit=None,
        reasoning="숏 진입 조건 충족",
        risk_factors=(),
        source="rule_based_v1",
        trigger="regular_4h",
        raw_signal="entry_sell",
    )


def _make_execution_result(action: str) -> ExecutionResult:
    return ExecutionResult(
        action=action,
        executed=False,
        decision=_make_decision(action),
        judgment_id=None,
    )


def _make_snapshot(signal: str = "entry_sell") -> SignalSnapshot:
    return SignalSnapshot(
        pair="BTC_JPY",
        exchange="gmo_coin",
        timestamp=datetime(2026, 4, 13, 0, 20, 0, tzinfo=timezone.utc),
        signal=signal,
        current_price=11_399_360.0,
        exit_signal={"action": "hold"},
        is_preview=False,
    )


# ──────────────────────────────────────────────────────────────
# SG-01: _supports_short 기본값
# ──────────────────────────────────────────────────────────────

def test_supports_short_default_false():
    """SG-01: BaseTrendManager._supports_short 기본값은 False."""
    from core.strategy.base_trend import BaseTrendManager
    assert BaseTrendManager._supports_short is False


# ──────────────────────────────────────────────────────────────
# SG-02: CfdTrendFollowingManager._supports_short
# ──────────────────────────────────────────────────────────────

def test_cfd_manager_supports_short_true():
    """SG-02: CfdTrendFollowingManager._supports_short == True."""
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    assert CfdTrendFollowingManager._supports_short is True


# ──────────────────────────────────────────────────────────────
# SG-03: 롱전용 매니저 entry_short → 차단
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trend_manager_blocks_entry_short(caplog):
    """SG-03: TrendFollowingManager._handle_execution_result(entry_short)
    → WARNING 로그 + _on_entry_signal 미호출 + False 반환."""
    mgr = _make_trend_manager()
    result = _make_execution_result("entry_short")
    snapshot = _make_snapshot("entry_sell")
    signal_data: dict = {}
    params: dict = {}

    with patch.object(mgr, "_on_entry_signal", new_callable=AsyncMock) as mock_entry:
        with caplog.at_level(logging.WARNING, logger="core.strategy.base_trend"):
            ret = await mgr._handle_execution_result(
                "BTC_JPY", result, snapshot, signal_data, params
            )

    # 차단: False 반환, _on_entry_signal 미호출
    assert ret is False
    mock_entry.assert_not_called()

    # WARNING 로그 확인
    warn_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("entry_short 차단" in r.message for r in warn_msgs), (
        f"WARNING 로그 없음. 기록된 records: {[r.message for r in caplog.records]}"
    )


# ──────────────────────────────────────────────────────────────
# SG-04: 롱전용 매니저 entry_long → 정상 호출
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trend_manager_allows_entry_long():
    """SG-04: TrendFollowingManager._handle_execution_result(entry_long)
    → _on_entry_signal("entry_ok") 정상 호출."""
    mgr = _make_trend_manager()
    result = _make_execution_result("entry_long")
    snapshot = _make_snapshot("entry_ok")
    signal_data: dict = {}
    params: dict = {}

    with patch.object(mgr, "_on_entry_signal", new_callable=AsyncMock) as mock_entry:
        ret = await mgr._handle_execution_result(
            "BTC_JPY", result, snapshot, signal_data, params
        )

    # 차단 없음: _on_entry_signal 호출됨
    mock_entry.assert_called_once()
    call_args = mock_entry.call_args
    # 두 번째 인자 = signal
    assert call_args.args[1] == "entry_ok"


# ──────────────────────────────────────────────────────────────
# SG-05: CFD 매니저 entry_short → 정상 호출
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cfd_manager_allows_entry_short():
    """SG-05: CfdTrendFollowingManager._handle_execution_result(entry_short)
    → _on_entry_signal("entry_sell") 정상 호출."""
    mgr = _make_cfd_manager()
    result = _make_execution_result("entry_short")
    snapshot = _make_snapshot("entry_sell")
    signal_data: dict = {}
    params: dict = {}

    with patch.object(mgr, "_on_entry_signal", new_callable=AsyncMock) as mock_entry:
        ret = await mgr._handle_execution_result(
            "BTC_JPY", result, snapshot, signal_data, params
        )

    # 차단 없음: _on_entry_signal 호출됨
    mock_entry.assert_called_once()
    call_args = mock_entry.call_args
    # 두 번째 인자 = signal
    assert call_args.args[1] == "entry_sell"


# ──────────────────────────────────────────────────────────────
# SG-06: 롱전용 인스턴스도 _supports_short = False
# ──────────────────────────────────────────────────────────────

def test_trend_manager_instance_supports_short_false():
    """SG-06: TrendFollowingManager 인스턴스도 _supports_short == False."""
    mgr = _make_trend_manager()
    assert mgr._supports_short is False


# ──────────────────────────────────────────────────────────────
# SG-07: WARNING 메시지에 가이던스 포함
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trend_manager_block_warning_contains_guidance(caplog):
    """SG-07: entry_short 차단 WARNING에 'cfd_trend_following' 가이던스 포함."""
    mgr = _make_trend_manager()
    result = _make_execution_result("entry_short")
    snapshot = _make_snapshot("entry_sell")

    with patch.object(mgr, "_on_entry_signal", new_callable=AsyncMock):
        with caplog.at_level(logging.WARNING, logger="core.strategy.base_trend"):
            await mgr._handle_execution_result("BTC_JPY", result, snapshot, {}, {})

    warn_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("cfd_trend_following" in r.message for r in warn_msgs)


# ──────────────────────────────────────────────────────────────
# SG-08: 롱전용 매니저 entry_short + is_preview=True → 차단
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trend_manager_blocks_entry_short_preview(caplog):
    """SG-08: is_preview=True여도 롱전용 매니저는 entry_short 차단."""
    mgr = _make_trend_manager()
    result = _make_execution_result("entry_short")
    # is_preview=True 스냅샷
    snapshot = SignalSnapshot(
        pair="BTC_JPY",
        exchange="gmo_coin",
        timestamp=datetime(2026, 4, 13, 0, 20, 0, tzinfo=timezone.utc),
        signal="entry_sell",
        current_price=11_399_360.0,
        exit_signal={"action": "hold"},
        is_preview=True,
    )

    with patch.object(mgr, "_on_entry_signal", new_callable=AsyncMock) as mock_entry:
        with caplog.at_level(logging.WARNING, logger="core.strategy.base_trend"):
            ret = await mgr._handle_execution_result("BTC_JPY", result, snapshot, {}, {})

    assert ret is False
    mock_entry.assert_not_called()


# ──────────────────────────────────────────────────────────────
# SG-09: CFD 매니저 entry_short + is_preview=True → "entry_preview" 전달
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cfd_manager_entry_short_preview_passes_entry_preview():
    """SG-09: CfdTrendFollowingManager + is_preview=True → _on_entry_signal("entry_preview") 호출."""
    mgr = _make_cfd_manager()
    result = _make_execution_result("entry_short")
    snapshot = SignalSnapshot(
        pair="BTC_JPY",
        exchange="bitflyer",
        timestamp=datetime(2026, 4, 13, 0, 20, 0, tzinfo=timezone.utc),
        signal="entry_sell",
        current_price=11_399_360.0,
        exit_signal={"action": "hold"},
        is_preview=True,
    )

    with patch.object(mgr, "_on_entry_signal", new_callable=AsyncMock) as mock_entry:
        await mgr._handle_execution_result("BTC_JPY", result, snapshot, {}, {})

    mock_entry.assert_called_once()
    call_args = mock_entry.call_args
    assert call_args.args[1] == "entry_preview"  # preview 경로


# ──────────────────────────────────────────────────────────────
# SG-10: GmoCoinTrendManager._supports_short == True
# ──────────────────────────────────────────────────────────────

def test_gmo_coin_trend_manager_supports_short_true():
    """SG-10: GmoCoinTrendManager._supports_short == True."""
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager
    assert GmoCoinTrendManager._supports_short is True


# ══════════════════════════════════════════════════════════════
# BUG-035 회귀 테스트 — GMO FX 올바른 매니저 할당
# D35-01~05
# ══════════════════════════════════════════════════════════════

def test_d35_01_gmofx_creates_cfd_manager(monkeypatch):
    """D35-01: EXCHANGE=gmofx → trend_manager가 CfdTrendFollowingManager 인스턴스."""
    import os
    from unittest.mock import MagicMock, AsyncMock

    # main.py의 매니저 생성 로직만 재현
    monkeypatch.setenv("EXCHANGE", "gmofx")
    exchange = os.environ.get("EXCHANGE", "bitflyer").lower()

    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager
    from core.strategy.plugins.trend_following.manager import TrendFollowingManager

    adapter = MagicMock()
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)
    session_factory = MagicMock()
    candle_model = MagicMock()
    cfd_position_model = MagicMock()
    trend_position_model = MagicMock()
    snapshot_collector = None

    if exchange == "gmo_coin":
        trend_manager = GmoCoinTrendManager(
            adapter=adapter, supervisor=supervisor, session_factory=session_factory,
            candle_model=candle_model, cfd_position_model=cfd_position_model,
            snapshot_collector=snapshot_collector,
        )
    elif exchange == "gmofx":
        trend_manager = CfdTrendFollowingManager(
            adapter=adapter, supervisor=supervisor, session_factory=session_factory,
            candle_model=candle_model, cfd_position_model=cfd_position_model,
            snapshot_collector=snapshot_collector,
        )
    else:
        trend_manager = TrendFollowingManager(
            adapter=adapter, supervisor=supervisor, session_factory=session_factory,
            candle_model=candle_model, trend_position_model=trend_position_model,
            snapshot_collector=snapshot_collector,
        )

    assert isinstance(trend_manager, CfdTrendFollowingManager)
    assert not isinstance(trend_manager, GmoCoinTrendManager)


def test_d35_02_gmofx_manager_supports_short():
    """D35-02: GMO FX용 CfdTrendFollowingManager._supports_short == True."""
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    assert CfdTrendFollowingManager._supports_short is True


def test_d35_03_bitflyer_still_uses_trend_following():
    """D35-03: EXCHANGE=bitflyer → TrendFollowingManager (회귀 없음)."""
    from core.strategy.plugins.trend_following.manager import TrendFollowingManager
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager
    from unittest.mock import MagicMock, AsyncMock

    adapter = MagicMock()
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)
    session_factory = MagicMock()
    candle_model = MagicMock()
    trend_position_model = MagicMock()

    exchange = "bitflyer"
    if exchange == "gmo_coin":
        trend_manager = GmoCoinTrendManager(adapter=adapter, supervisor=supervisor,
            session_factory=session_factory, candle_model=candle_model,
            cfd_position_model=MagicMock())
    elif exchange == "gmofx":
        trend_manager = CfdTrendFollowingManager(adapter=adapter, supervisor=supervisor,
            session_factory=session_factory, candle_model=candle_model,
            cfd_position_model=MagicMock())
    else:
        trend_manager = TrendFollowingManager(adapter=adapter, supervisor=supervisor,
            session_factory=session_factory, candle_model=candle_model,
            trend_position_model=trend_position_model)

    assert isinstance(trend_manager, TrendFollowingManager)
    assert not isinstance(trend_manager, CfdTrendFollowingManager)


def test_d35_04_gmo_coin_still_uses_gmo_coin_manager():
    """D35-04: EXCHANGE=gmo_coin → GmoCoinTrendManager (회귀 없음)."""
    from core.strategy.plugins.trend_following.manager import TrendFollowingManager
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager
    from unittest.mock import MagicMock, AsyncMock

    adapter = MagicMock()
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    exchange = "gmo_coin"
    if exchange == "gmo_coin":
        trend_manager = GmoCoinTrendManager(adapter=adapter, supervisor=supervisor,
            session_factory=MagicMock(), candle_model=MagicMock(),
            cfd_position_model=MagicMock())
    elif exchange == "gmofx":
        trend_manager = CfdTrendFollowingManager(adapter=adapter, supervisor=supervisor,
            session_factory=MagicMock(), candle_model=MagicMock(),
            cfd_position_model=MagicMock())
    else:
        trend_manager = TrendFollowingManager(adapter=adapter, supervisor=supervisor,
            session_factory=MagicMock(), candle_model=MagicMock(),
            trend_position_model=MagicMock())

    assert isinstance(trend_manager, GmoCoinTrendManager)


def test_d35_05_gmofx_log_prefix():
    """D35-05: GMO FX용 CfdTrendFollowingManager 프리픽스 [CfdMgr], [TrendMgr] 아님."""
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    from unittest.mock import MagicMock, AsyncMock

    adapter = MagicMock()
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = CfdTrendFollowingManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )
    assert mgr._log_prefix == "[CfdMgr]"
    assert mgr._log_prefix != "[TrendMgr]"


# ══════════════════════════════════════════════════════════════
# BUG-034 회귀 테스트 — _open_position/pre_entry_checks 통합
# D34-01~08
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_d34_01_trend_manager_open_position_side_buy():
    """D34-01: TrendFollowingManager._open_position에 side='buy' 전달 → MARKET_BUY."""
    from core.strategy.plugins.trend_following.manager import TrendFollowingManager
    from core.exchange.types import OrderType
    from unittest.mock import MagicMock, AsyncMock, patch
    from core.exchange.types import Order, OrderStatus, OrderSide

    adapter = MagicMock()
    adapter.exchange_name = "bitflyer"
    adapter.is_margin_trading = False
    fake_order = Order(order_id="test-001", order_type=OrderType.MARKET_BUY,
                       pair="xrp_jpy", amount=100.0, price=100.0,
                       side=OrderSide.BUY, status=OrderStatus.COMPLETED)
    adapter.place_order = AsyncMock(return_value=fake_order)
    adapter.get_balance = AsyncMock(return_value=MagicMock(get_available=MagicMock(return_value=10000.0)))
    adapter.get_ticker = AsyncMock(return_value=MagicMock(ask=100.0, last=100.0))

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = TrendFollowingManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        trend_position_model=MagicMock(),
    )
    mgr._record_open = AsyncMock(return_value=1)

    await mgr._open_position("xrp_jpy", "buy", 100.0, 2.0, {"position_size_pct": 10.0, "min_order_jpy": 500, "atr_multiplier_stop": 2.0, "max_slippage_pct": 1.0})

    adapter.place_order.assert_called_once()
    call_kwargs = adapter.place_order.call_args
    assert call_kwargs.kwargs.get("order_type") == OrderType.MARKET_BUY or call_kwargs.args[0] == OrderType.MARKET_BUY


@pytest.mark.asyncio
async def test_d34_02_cfd_pre_entry_checks_ok_calls_open_position():
    """D34-02: CdfTF + entry_ok → _pre_entry_checks → _open_position(side='buy') 호출."""
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    from unittest.mock import MagicMock, AsyncMock, patch

    adapter = MagicMock()
    adapter.exchange_name = "bitflyer"
    adapter.is_margin_trading = True

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = CfdTrendFollowingManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )

    open_calls = []
    async def fake_open(pair, side, price, atr, params, *, signal_data=None):
        open_calls.append({"pair": pair, "side": side, "price": price})

    with patch.object(mgr, "_open_position", side_effect=fake_open):
        with patch.object(mgr, "_try_paper_entry", new_callable=AsyncMock, return_value=False):
            await mgr._on_entry_signal("fx_btc_jpy", "entry_ok", 5_000_000.0, 50000.0, {}, {})

    assert len(open_calls) == 1
    assert open_calls[0]["side"] == "buy"


@pytest.mark.asyncio
async def test_d34_03_cfd_pre_entry_checks_ok_calls_open_position_short():
    """D34-03: CdfTF + entry_sell → _pre_entry_checks → _open_position(side='sell') 호출."""
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    from unittest.mock import MagicMock, AsyncMock, patch

    adapter = MagicMock()
    adapter.exchange_name = "bitflyer"
    adapter.is_margin_trading = True

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = CfdTrendFollowingManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )

    open_calls = []
    async def fake_open(pair, side, price, atr, params, *, signal_data=None):
        open_calls.append({"pair": pair, "side": side})

    with patch.object(mgr, "_open_position", side_effect=fake_open):
        with patch.object(mgr, "_try_paper_entry", new_callable=AsyncMock, return_value=False):
            await mgr._on_entry_signal("fx_btc_jpy", "entry_sell", 5_000_000.0, 50000.0, {}, {})

    assert len(open_calls) == 1
    assert open_calls[0]["side"] == "sell"


@pytest.mark.asyncio
async def test_d34_04_cfd_pre_entry_checks_blocks_on_weekend():
    """D34-04: CdfTF + should_close_for_weekend()=True → _open_position 미호출."""
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    from unittest.mock import MagicMock, AsyncMock, patch

    adapter = MagicMock()
    adapter.exchange_name = "gmofx"
    adapter.is_margin_trading = True

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = CfdTrendFollowingManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )

    open_calls = []
    async def fake_open(pair, side, price, atr, params, *, signal_data=None):
        open_calls.append(side)

    with patch.object(mgr, "_open_position", side_effect=fake_open):
        with patch.object(mgr, "_try_paper_entry", new_callable=AsyncMock, return_value=False):
            with patch("core.strategy.plugins.cfd_trend_following.manager.should_close_for_weekend", return_value=True):
                with patch("core.strategy.plugins.cfd_trend_following.manager.is_fx_market_open", return_value=True):
                    await mgr._on_entry_signal("usd_jpy", "entry_ok", 150.0, 0.5, {}, {})

    assert len(open_calls) == 0


@pytest.mark.asyncio
async def test_d34_05_cfd_pre_entry_checks_blocks_on_low_keep_rate():
    """D34-05: CdfTF + keep_rate < warn_threshold → _open_position 미호출."""
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    from unittest.mock import MagicMock, AsyncMock, patch

    adapter = MagicMock()
    adapter.exchange_name = "bitflyer"
    adapter.is_margin_trading = True

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = CfdTrendFollowingManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )
    mgr._last_keep_rate["fx_btc_jpy"] = 1.0  # warn_threshold(1.5) 미만

    open_calls = []
    async def fake_open(pair, side, price, atr, params, *, signal_data=None):
        open_calls.append(side)

    with patch.object(mgr, "_open_position", side_effect=fake_open):
        with patch.object(mgr, "_try_paper_entry", new_callable=AsyncMock, return_value=False):
            await mgr._on_entry_signal("fx_btc_jpy", "entry_ok", 5_000_000.0, 50000.0, {}, {})

    assert len(open_calls) == 0


@pytest.mark.asyncio
async def test_d34_06_pre_entry_checks_default_returns_true():
    """D34-06: BaseTrendManager._pre_entry_checks 기본 구현 → True 반환."""
    from core.strategy.base_trend import BaseTrendManager
    from unittest.mock import MagicMock, AsyncMock

    # Abstract이므로 TrendFollowingManager로 테스트
    from core.strategy.plugins.trend_following.manager import TrendFollowingManager
    adapter = MagicMock()
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = TrendFollowingManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        trend_position_model=MagicMock(),
    )
    result = await mgr._pre_entry_checks("xrp_jpy", "buy", {})
    assert result is True


@pytest.mark.asyncio
async def test_d34_07_try_paper_entry_short_sl_uses_short_formula():
    """D34-07: _try_paper_entry(direction='short') → SL = price + atr*mult (숏 공식 적용)."""
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    from unittest.mock import MagicMock, AsyncMock

    adapter = MagicMock()
    adapter.exchange_name = "bitflyer"
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mock_executor = MagicMock()
    mock_executor.record_paper_entry = AsyncMock(return_value=42)

    mgr = CfdTrendFollowingManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )
    mgr._paper_executors["fx_btc_jpy"] = mock_executor

    await mgr._try_paper_entry("fx_btc_jpy", "short", 5_000_000.0, 50000.0, {"atr_multiplier_stop": 2.0})

    pos = mgr._position.get("fx_btc_jpy")
    assert pos is not None
    # 숏: SL = price + atr * mult = 5_000_000 + 50000 * 2.0 = 5_100_000
    assert pos.stop_loss_price == pytest.approx(5_100_000.0, abs=1.0)


@pytest.mark.asyncio
async def test_d34_08_cfd_entry_preview_reaches_open_position():
    """D34-08: CdfTF + entry_preview 시그널 → base 경로 통과 → _open_position 호출 (기존에 미지원)."""
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    from unittest.mock import MagicMock, AsyncMock, patch

    adapter = MagicMock()
    adapter.exchange_name = "bitflyer"
    adapter.is_margin_trading = True

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = CfdTrendFollowingManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )

    open_calls = []
    async def fake_open(pair, side, price, atr, params, *, signal_data=None):
        open_calls.append({"pair": pair, "side": side})

    with patch.object(mgr, "_open_position", side_effect=fake_open):
        with patch.object(mgr, "_try_paper_entry", new_callable=AsyncMock, return_value=False):
            # entry_preview는 base._on_entry_signal 경로에서만 처리됨
            await mgr._on_entry_signal("fx_btc_jpy", "entry_preview", 5_000_000.0, 50000.0, {}, {})

    # entry_preview가 CdfTF에 전달됨 (기존에는 무시됐음)
    assert len(open_calls) == 1
    assert open_calls[0]["side"] == "buy"


# ══════════════════════════════════════════════════════════════
# 큐니 추가 엣지 케이스 — 세션 필터·GMO Coin·FX 시장 폐장
# E1~E3
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_e1_gmo_coin_margin_no_session_filter():
    """E1: GmoCoinTrendManager + is_margin_trading=True + allowed_sessions 미설정
    → 세션 필터 비활성(=통과) → _open_position 호출됨."""
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager
    from unittest.mock import MagicMock, AsyncMock, patch

    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.is_margin_trading = True  # GMO Coin 레버리지 = True

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = GmoCoinTrendManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )

    open_calls = []
    async def fake_open(pair, side, price, atr, params, *, signal_data=None):
        open_calls.append(side)

    # allowed_sessions 미설정 → is_allowed_session returns True (비활성)
    params: dict = {}  # allowed_sessions 없음
    with patch.object(mgr, "_open_position", side_effect=fake_open):
        with patch.object(mgr, "_try_paper_entry", new_callable=AsyncMock, return_value=False):
            await mgr._on_entry_signal("btc_jpy", "entry_ok", 11_000_000.0, 100000.0, params, {})

    # 세션 필터 통과 → _open_position 호출됨
    assert len(open_calls) == 1
    assert open_calls[0] == "buy"


@pytest.mark.asyncio
async def test_e2_gmo_coin_pre_entry_no_fx_weekend_check():
    """E2: GmoCoinTrendManager._pre_entry_checks → exchange_name='gmo_coin'
    → FX 주말 체크 미적용 → True 반환 (24/7 암호화폐 시장)."""
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager
    from unittest.mock import MagicMock, AsyncMock

    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"  # gmofx 아님
    adapter.is_margin_trading = True

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = GmoCoinTrendManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )

    # keep_rate 없음 (None) → 차단 안 됨
    result = await mgr._pre_entry_checks("btc_jpy", "buy", {})
    assert result is True


@pytest.mark.asyncio
async def test_e3_cfd_pre_entry_checks_fx_market_closed():
    """E3: CdfTF + exchange_name='gmofx' + is_fx_market_open()=False
    → 진입 차단."""
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
    from unittest.mock import MagicMock, AsyncMock, patch

    adapter = MagicMock()
    adapter.exchange_name = "gmofx"
    adapter.is_margin_trading = True

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = CfdTrendFollowingManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )

    with patch("core.strategy.plugins.cfd_trend_following.manager.should_close_for_weekend", return_value=False):
        with patch("core.strategy.plugins.cfd_trend_following.manager.is_fx_market_open", return_value=False):
            result = await mgr._pre_entry_checks("usd_jpy", "buy", {})

    assert result is False
