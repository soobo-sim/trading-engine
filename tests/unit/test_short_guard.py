"""
BUG-031 회귀 테스트 — entry_short 방향 반전 방지.

롱 전용 매니저(TrendFollowingManager)에서 entry_short가 실행되면
MARKET_BUY가 발생하는 버그 수정 검증.

테스트:
  SG-01: _supports_short 기본값 False (BaseTrendManager)
  SG-02: CfdTrendFollowingManager._supports_short == True
  SG-03: 롱전용 매니저에서 entry_short → WARNING 로그 + _on_entry_signal 미호출
  SG-04: 롱전용 매니저에서 entry_long → _on_entry_signal("long_setup") 정상 호출
  SG-05: CFD 매니저에서 entry_short → _on_entry_signal("short_setup") 정상 호출
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

def _make_cfd_manager():
    """CfdTrendFollowingManager 최소 인스턴스."""
    from core.punisher.strategy.plugins.gmo_coin_trend.base import MarginTrendManager as CfdTrendFollowingManager

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
        raw_signal="short_setup",
    )


def _make_execution_result(action: str) -> ExecutionResult:
    return ExecutionResult(
        action=action,
        executed=False,
        decision=_make_decision(action),
        judgment_id=None,
    )


def _make_snapshot(signal: str = "short_setup") -> SignalSnapshot:
    return SignalSnapshot(
        pair="BTC_JPY",
        exchange="gmo_coin",
        timestamp=datetime(2026, 4, 13, 0, 20, 0, tzinfo=timezone.utc),
        signal=signal,
        current_price=11_399_360.0,
        exit_signal={"action": "hold"},
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
    from core.punisher.strategy.plugins.gmo_coin_trend.base import MarginTrendManager as CfdTrendFollowingManager
    assert CfdTrendFollowingManager._supports_short is True


# ──────────────────────────────────────────────────────────────
# SG-03: 롱전용 매니저 entry_short → 차단
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio

# ──────────────────────────────────────────────────────────────
# SG-04: 롱전용 매니저 entry_long → 정상 호출
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio

# ──────────────────────────────────────────────────────────────
# SG-05: CFD 매니저 entry_short → 정상 호출
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cfd_manager_allows_entry_short():
    """SG-05: CfdTrendFollowingManager._handle_execution_result(entry_short)
    → _on_entry_signal("short_setup") 정상 호출."""
    mgr = _make_cfd_manager()
    result = _make_execution_result("entry_short")
    snapshot = _make_snapshot("short_setup")
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
    assert call_args.args[1] == "short_setup"


# ──────────────────────────────────────────────────────────────
# SG-06: 롱전용 인스턴스도 _supports_short = False
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# SG-07: WARNING 메시지에 가이던스 포함
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio

# ──────────────────────────────────────────────────────────────
# SG-08: 롱전용 매니저 entry_short + is_preview=True → 차단
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio

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

def test_d35_02_gmofx_manager_supports_short():
    """D35-02: GMO FX용 CfdTrendFollowingManager._supports_short == True."""
    from core.punisher.strategy.plugins.gmo_coin_trend.base import MarginTrendManager as CfdTrendFollowingManager
    assert CfdTrendFollowingManager._supports_short is True

def test_d35_04_gmo_coin_still_uses_gmo_coin_manager():
    """D35-04: EXCHANGE=gmo_coin → GmoCoinTrendManager (회귀 없음)."""
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager
    from unittest.mock import MagicMock, AsyncMock

    adapter = MagicMock()
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    trend_manager = GmoCoinTrendManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )

    assert isinstance(trend_manager, GmoCoinTrendManager)


def test_d35_05_gmofx_log_prefix():
    """D35-05: GMO FX용 CfdTrendFollowingManager 프리픽스 [CfdMgr], [TrendMgr] 아님."""
    from core.punisher.strategy.plugins.gmo_coin_trend.base import MarginTrendManager as CfdTrendFollowingManager
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
    assert mgr._log_prefix == "[MarginMgr]"
    assert mgr._log_prefix != "[TrendMgr]"


# ══════════════════════════════════════════════════════════════
# BUG-034 회귀 테스트 — _open_position/pre_entry_checks 통합
# D34-01~08
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio

@pytest.mark.asyncio
async def test_d34_02_cfd_pre_entry_checks_ok_calls_open_position():
    """D34-02: CdfTF + long_setup → _pre_entry_checks → _open_position(side='buy') 호출."""
    from core.punisher.strategy.plugins.gmo_coin_trend.base import MarginTrendManager as CfdTrendFollowingManager
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
            await mgr._on_entry_signal("fx_btc_jpy", "long_setup", 5_000_000.0, 50000.0, {}, {})

    assert len(open_calls) == 1
    assert open_calls[0]["side"] == "buy"


@pytest.mark.asyncio
async def test_d34_03_cfd_pre_entry_checks_ok_calls_open_position_short():
    """D34-03: CdfTF + short_setup → _pre_entry_checks → _open_position(side='sell') 호출."""
    from core.punisher.strategy.plugins.gmo_coin_trend.base import MarginTrendManager as CfdTrendFollowingManager
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
            await mgr._on_entry_signal("fx_btc_jpy", "short_setup", 5_000_000.0, 50000.0, {}, {})

    assert len(open_calls) == 1
    assert open_calls[0]["side"] == "sell"


@pytest.mark.asyncio

@pytest.mark.asyncio
async def test_d34_05_cfd_pre_entry_checks_blocks_on_low_keep_rate():
    """D34-05: CdfTF + keep_rate < warn_threshold → _open_position 미호출."""
    from core.punisher.strategy.plugins.gmo_coin_trend.base import MarginTrendManager as CfdTrendFollowingManager
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
            await mgr._on_entry_signal("fx_btc_jpy", "long_setup", 5_000_000.0, 50000.0, {}, {})

    assert len(open_calls) == 0


@pytest.mark.asyncio
async def test_d34_06_pre_entry_checks_default_returns_true():
    """D34-06: BaseTrendManager._pre_entry_checks 기본 구현 → True 반환."""
    from core.punisher.strategy.plugins.gmo_coin_trend.base import MarginTrendManager
    from unittest.mock import MagicMock, AsyncMock

    adapter = MagicMock()
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = MarginTrendManager(
        adapter=adapter, supervisor=supervisor,
        session_factory=MagicMock(), candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )
    result = await mgr._pre_entry_checks("usd_jpy", "buy", {})
    assert result is True


@pytest.mark.asyncio
async def test_d34_07_try_paper_entry_short_sl_uses_short_formula():
    """D34-07: _try_paper_entry(direction='short') → SL = price + atr*mult (숏 공식 적용)."""
    from core.punisher.strategy.plugins.gmo_coin_trend.base import MarginTrendManager as CfdTrendFollowingManager
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
            await mgr._on_entry_signal("btc_jpy", "long_setup", 11_000_000.0, 100000.0, params, {})

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
