"""
BaseTrendManager + RegimeGate 연동 테스트.

테스트 케이스:
  BT-01: regime_gate=None이면 진입 차단 없음 (기존 동작 유지)
  BT-02: 4H 캔들 경계에서 update_regime 호출
  BT-03: should_allow_entry=False 시 진입 스킵
  BT-04: should_allow_entry=True 시 정상 진입 흐름 유지
  BT-05: range_pct가 signal_data에 포함됨
  BT-06: GmoCoinTrendManager._get_strategy_type() == "trend_following"

통합 시나리오 (IT-01~05):
  IT-01: 3캔들 ranging → BoxManager 허용, TrendManager 차단
  IT-02: 3캔들 trending → TrendManager 허용, BoxManager 차단
  IT-03: 추세 포지션 보유 중 ranging 전환 → exit 액션은 차단 없이 실행
  IT-04: unclear 캔들 → active_strategy=None → 양쪽 모두 차단
  IT-05: warm-up (캔들 미만) → active_strategy=None → 양쪽 모두 차단
"""
import asyncio
from typing import Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.execution.regime_gate import RegimeGate
from core.strategy.plugins.gmo_coin_box.manager import GmoCoinBoxManager
from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager


# ── ヘルパ ─────────────────────────────────────────────────────

def make_gmo_trend_manager():
    """GmoCoinTrendManager의 최소 인스턴스."""
    adapter = MagicMock()
    supervisor = MagicMock()
    supervisor.is_running = MagicMock(return_value=False)
    session_factory = AsyncMock()
    candle_model = MagicMock()
    trend_position_model = MagicMock()
    return GmoCoinTrendManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=candle_model,
        cfd_position_model=trend_position_model,
        pair_column="pair",
    )


# ── テスト ─────────────────────────────────────────────────────

class TestGetStrategyType:
    """BT-06: GmoCoinTrendManager._get_strategy_type()"""

    def test_returns_trend_following(self):
        mgr = make_gmo_trend_manager()
        assert mgr._get_strategy_type() == "trend_following"


class TestSetRegimeGate:
    """set_regime_gate DI setter 테스트"""

    def test_set_regime_gate(self):
        mgr = make_gmo_trend_manager()
        gate = RegimeGate("btc_jpy")
        mgr.set_regime_gate(gate)
        assert mgr._regime_gate is gate

    def test_default_regime_gate_is_none(self):
        mgr = make_gmo_trend_manager()
        assert mgr._regime_gate is None


class TestRegimeGateNoBlock:
    """BT-01: regime_gate=None이면 기존 동작 유지 (진입 차단 없음)"""

    @pytest.mark.asyncio
    async def test_no_gate_no_block(self):
        """regime_gate 없으면 entry_long 진입 스킵 없이 정상 진행."""
        mgr = make_gmo_trend_manager()
        assert mgr._regime_gate is None

        # _handle_execution_result에서 entry_long 처리 시 차단 없어야 함
        # _on_entry_signal이 호출되는지 확인
        mock_result = MagicMock()
        mock_result.action = "entry_long"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snapshot = MagicMock()
        mock_snapshot.current_price = 10000000.0
        mock_snapshot.atr = 100000.0
        mock_snapshot.ema_slope_pct = 0.1
        mock_snapshot.rsi = 55.0
        mock_snapshot.is_preview = False
        mock_snapshot.pair = "btc_jpy"

        mgr._on_entry_signal = AsyncMock()
        mgr._position["btc_jpy"] = None

        await mgr._handle_execution_result(
            "btc_jpy", mock_result, mock_snapshot, {}, {}
        )

        mgr._on_entry_signal.assert_called_once()


class TestRegimeGateBlocks:
    """BT-03: should_allow_entry=False 시 진입 스킵"""

    @pytest.mark.asyncio
    async def test_gate_blocks_entry_long(self):
        """RegimeGate가 trend_following을 차단하면 _on_entry_signal 호출 안 됨."""
        mgr = make_gmo_trend_manager()
        gate = RegimeGate("btc_jpy")
        # 3캔들 ranging → box_mean_reversion 활성 → trend_following 차단
        for _ in range(3):
            gate.update_regime("ranging")
        assert gate.active_strategy == "box_mean_reversion"
        mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "entry_long"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snapshot = MagicMock()
        mock_snapshot.current_price = 10000000.0
        mock_snapshot.atr = 100000.0
        mock_snapshot.ema_slope_pct = 0.1
        mock_snapshot.rsi = 55.0
        mock_snapshot.is_preview = False
        mock_snapshot.pair = "btc_jpy"

        mgr._on_entry_signal = AsyncMock()
        mgr._position["btc_jpy"] = None

        result = await mgr._handle_execution_result(
            "btc_jpy", mock_result, mock_snapshot, {}, {}
        )

        mgr._on_entry_signal.assert_not_called()
        assert result is False

    @pytest.mark.asyncio
    async def test_gate_blocks_entry_short(self):
        """entry_short도 동일하게 차단."""
        mgr = make_gmo_trend_manager()
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("ranging")
        mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "entry_short"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snapshot = MagicMock()
        mock_snapshot.current_price = 10000000.0
        mock_snapshot.atr = 100000.0
        mock_snapshot.ema_slope_pct = -0.1
        mock_snapshot.rsi = 45.0
        mock_snapshot.is_preview = False
        mock_snapshot.pair = "btc_jpy"

        mgr._on_entry_signal = AsyncMock()
        mgr._position["btc_jpy"] = None
        mgr._supports_short = True

        await mgr._handle_execution_result(
            "btc_jpy", mock_result, mock_snapshot, {}, {}
        )

        mgr._on_entry_signal.assert_not_called()


class TestRegimeGateAllows:
    """BT-04: should_allow_entry=True 시 정상 진입"""

    @pytest.mark.asyncio
    async def test_gate_allows_when_matching(self):
        """RegimeGate가 trend_following을 허용하면 _on_entry_signal 호출됨."""
        mgr = make_gmo_trend_manager()
        gate = RegimeGate("btc_jpy")
        # 3캔들 trending → trend_following 활성
        for _ in range(3):
            gate.update_regime("trending")
        assert gate.active_strategy == "trend_following"
        mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "entry_long"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snapshot = MagicMock()
        mock_snapshot.current_price = 10000000.0
        mock_snapshot.atr = 100000.0
        mock_snapshot.ema_slope_pct = 0.1
        mock_snapshot.rsi = 55.0
        mock_snapshot.is_preview = False
        mock_snapshot.pair = "btc_jpy"

        mgr._on_entry_signal = AsyncMock()
        mgr._position["btc_jpy"] = None

        await mgr._handle_execution_result(
            "btc_jpy", mock_result, mock_snapshot, {}, {}
        )

        mgr._on_entry_signal.assert_called_once()


class TestExitNotBlocked:
    """exit/tighten_stop은 RegimeGate와 무관하게 항상 실행"""

    @pytest.mark.asyncio
    async def test_exit_not_blocked_by_gate(self):
        """exit 액션은 gate 차단 없이 실행."""
        mgr = make_gmo_trend_manager()
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("ranging")
        mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "exit"
        mock_result.decision = MagicMock()
        mock_result.decision.trigger = "test_exit"

        mock_snapshot = MagicMock()
        mock_snapshot.current_price = 10000000.0
        mock_snapshot.pair = "btc_jpy"
        mock_snapshot.atr = None

        mock_pos = MagicMock()
        mgr._position["btc_jpy"] = mock_pos

        mgr._close_position = AsyncMock()

        result = await mgr._handle_execution_result(
            "btc_jpy", mock_result, mock_snapshot, {}, {}
        )

        mgr._close_position.assert_called_once_with("btc_jpy", "test_exit")
        assert result is True


class TestRangePctInSignalData:
    """BT-05: compute_trend_signal 반환에 range_pct 포함"""

    def test_range_pct_in_return(self):
        from core.strategy.signals import compute_trend_signal
        from unittest.mock import MagicMock

        # 최소한의 캔들 mock
        candle = MagicMock()
        candle.open_time = "2026-01-01T00:00:00"
        candle.open = 10000000.0
        candle.high = 10100000.0
        candle.low = 9900000.0
        candle.close = 10050000.0
        candle.volume = 1.0

        candles = [candle] * 30
        result = compute_trend_signal(
            candles=candles,
            params={},
        )
        assert "range_pct" in result
        assert isinstance(result["range_pct"], float)


# ── IT 통합 시나리오 ────────────────────────────────────────────

def make_gmo_box_manager():
    """GmoCoinBoxManager의 최소 인스턴스."""
    adapter = MagicMock()
    supervisor = MagicMock()
    supervisor.is_running = MagicMock(return_value=False)
    session_factory = AsyncMock()
    candle_model = MagicMock()
    trend_position_model = MagicMock()
    return GmoCoinBoxManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=candle_model,
        cfd_position_model=trend_position_model,
        pair_column="pair",
    )


def make_shared_gate_with_regime(regime: str, count: int = 3) -> RegimeGate:
    """지정 regime을 count회 업데이트한 RegimeGate."""
    gate = RegimeGate("btc_jpy")
    for _ in range(count):
        gate.update_regime(regime)
    return gate


class TestIT01RangingBoxAllowsTrendBlocks:
    """IT-01: 3캔들 ranging → BoxManager 허용, TrendManager 차단."""

    @pytest.mark.asyncio
    async def test_box_allowed_when_ranging(self):
        """ranging 3회 → active_strategy=box_mean_reversion → BoxMgr 허용."""
        gate = make_shared_gate_with_regime("ranging")
        assert gate.active_strategy == "box_mean_reversion"

        box_mgr = make_gmo_box_manager()
        box_mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "entry_long"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snap = MagicMock()
        mock_snap.current_price = 10_000_000.0
        mock_snap.atr = 100_000.0
        mock_snap.ema_slope_pct = 0.1
        mock_snap.rsi = 50.0
        mock_snap.is_preview = False
        mock_snap.pair = "btc_jpy"

        box_mgr._on_entry_signal = AsyncMock()
        box_mgr._position["btc_jpy"] = None

        await box_mgr._handle_execution_result("btc_jpy", mock_result, mock_snap, {}, {})

        box_mgr._on_entry_signal.assert_called_once()

    @pytest.mark.asyncio
    async def test_trend_blocked_when_ranging(self):
        """ranging 3회 → TrendMgr 진입 차단."""
        gate = make_shared_gate_with_regime("ranging")
        trend_mgr = make_gmo_trend_manager()
        trend_mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "entry_long"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snap = MagicMock()
        mock_snap.current_price = 10_000_000.0
        mock_snap.atr = 100_000.0
        mock_snap.ema_slope_pct = 0.1
        mock_snap.rsi = 50.0
        mock_snap.is_preview = False
        mock_snap.pair = "btc_jpy"

        trend_mgr._on_entry_signal = AsyncMock()
        trend_mgr._position["btc_jpy"] = None

        result = await trend_mgr._handle_execution_result("btc_jpy", mock_result, mock_snap, {}, {})

        trend_mgr._on_entry_signal.assert_not_called()
        assert result is False


class TestIT02TrendingTrendAllowsBoxBlocks:
    """IT-02: 3캔들 trending → TrendManager 허용, BoxManager 차단."""

    @pytest.mark.asyncio
    async def test_trend_allowed_when_trending(self):
        """trending 3회 → TrendMgr 허용."""
        gate = make_shared_gate_with_regime("trending")
        assert gate.active_strategy == "trend_following"

        trend_mgr = make_gmo_trend_manager()
        trend_mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "entry_long"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snap = MagicMock()
        mock_snap.current_price = 10_000_000.0
        mock_snap.atr = 100_000.0
        mock_snap.ema_slope_pct = 0.1
        mock_snap.rsi = 50.0
        mock_snap.is_preview = False
        mock_snap.pair = "btc_jpy"

        trend_mgr._on_entry_signal = AsyncMock()
        trend_mgr._position["btc_jpy"] = None

        await trend_mgr._handle_execution_result("btc_jpy", mock_result, mock_snap, {}, {})

        trend_mgr._on_entry_signal.assert_called_once()

    @pytest.mark.asyncio
    async def test_box_blocked_when_trending(self):
        """trending 3회 → BoxMgr 진입 차단."""
        gate = make_shared_gate_with_regime("trending")
        box_mgr = make_gmo_box_manager()
        box_mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "entry_long"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snap = MagicMock()
        mock_snap.current_price = 10_000_000.0
        mock_snap.atr = 100_000.0
        mock_snap.ema_slope_pct = 0.1
        mock_snap.rsi = 50.0
        mock_snap.is_preview = False
        mock_snap.pair = "btc_jpy"

        box_mgr._on_entry_signal = AsyncMock()
        box_mgr._position["btc_jpy"] = None

        result = await box_mgr._handle_execution_result("btc_jpy", mock_result, mock_snap, {}, {})

        box_mgr._on_entry_signal.assert_not_called()
        assert result is False


class TestIT03ExitNotBlockedDuringSwitch:
    """IT-03: 추세 포지션 보유 중 ranging 전환 → exit는 차단 없이 실행."""

    @pytest.mark.asyncio
    async def test_exit_not_blocked_after_regime_switch(self):
        """ranging 체제로 전환 중에도 기존 추세 포지션 exit는 실행됨."""
        gate = make_shared_gate_with_regime("ranging")
        # ranging 활성 → TrendMgr 진입은 차단되지만 exit는 무관
        trend_mgr = make_gmo_trend_manager()
        trend_mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "exit"
        mock_result.decision = MagicMock()
        mock_result.decision.trigger = "regime_switch_exit"

        mock_snap = MagicMock()
        mock_snap.current_price = 10_000_000.0
        mock_snap.pair = "btc_jpy"
        mock_snap.atr = None

        mock_pos = MagicMock()
        trend_mgr._position["btc_jpy"] = mock_pos
        trend_mgr._close_position = AsyncMock()

        result = await trend_mgr._handle_execution_result("btc_jpy", mock_result, mock_snap, {}, {})

        trend_mgr._close_position.assert_called_once_with("btc_jpy", "regime_switch_exit")
        assert result is True


class TestIT04UnclearBlocksBoth:
    """IT-04: unclear 캔들 → active_strategy=None → 양쪽 모두 차단."""

    @pytest.mark.asyncio
    async def test_unclear_blocks_trend_manager(self):
        """unclear 체제 → TrendMgr 차단."""
        gate = RegimeGate("btc_jpy")
        # trending 2 + unclear 1 → active=None (unclear 끼임)
        gate.update_regime("trending")
        gate.update_regime("trending")
        gate.update_regime("unclear")
        assert gate.active_strategy is None

        trend_mgr = make_gmo_trend_manager()
        trend_mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "entry_long"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snap = MagicMock()
        mock_snap.current_price = 10_000_000.0
        mock_snap.atr = 100_000.0
        mock_snap.ema_slope_pct = 0.1
        mock_snap.rsi = 50.0
        mock_snap.is_preview = False
        mock_snap.pair = "btc_jpy"

        trend_mgr._on_entry_signal = AsyncMock()
        trend_mgr._position["btc_jpy"] = None

        result = await trend_mgr._handle_execution_result("btc_jpy", mock_result, mock_snap, {}, {})

        trend_mgr._on_entry_signal.assert_not_called()
        assert result is False

    @pytest.mark.asyncio
    async def test_unclear_blocks_box_manager(self):
        """unclear 체제 → BoxMgr도 차단."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("ranging")
        gate.update_regime("ranging")
        gate.update_regime("unclear")
        assert gate.active_strategy is None

        box_mgr = make_gmo_box_manager()
        box_mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "entry_long"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snap = MagicMock()
        mock_snap.current_price = 10_000_000.0
        mock_snap.atr = 100_000.0
        mock_snap.ema_slope_pct = 0.1
        mock_snap.rsi = 50.0
        mock_snap.is_preview = False
        mock_snap.pair = "btc_jpy"

        box_mgr._on_entry_signal = AsyncMock()
        box_mgr._position["btc_jpy"] = None

        result = await box_mgr._handle_execution_result("btc_jpy", mock_result, mock_snap, {}, {})

        box_mgr._on_entry_signal.assert_not_called()
        assert result is False


class TestIT05WarmUpBlocksBoth:
    """IT-05: warm-up (캔들 미만 3개) → active_strategy=None → 양쪽 모두 차단."""

    def test_warmup_active_strategy_none(self):
        """2캔들 이하 → active_strategy=None."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending")
        gate.update_regime("trending")
        # 2캔들 → warm-up 중
        assert gate.active_strategy is None

    @pytest.mark.asyncio
    async def test_warmup_blocks_trend_manager(self):
        """warm-up 중 TrendMgr 차단."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending")  # 1캔들, warm-up
        assert gate.active_strategy is None

        trend_mgr = make_gmo_trend_manager()
        trend_mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "entry_long"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snap = MagicMock()
        mock_snap.current_price = 10_000_000.0
        mock_snap.atr = 100_000.0
        mock_snap.ema_slope_pct = 0.1
        mock_snap.rsi = 50.0
        mock_snap.is_preview = False
        mock_snap.pair = "btc_jpy"

        trend_mgr._on_entry_signal = AsyncMock()
        trend_mgr._position["btc_jpy"] = None

        result = await trend_mgr._handle_execution_result("btc_jpy", mock_result, mock_snap, {}, {})

        trend_mgr._on_entry_signal.assert_not_called()
        assert result is False

    @pytest.mark.asyncio
    async def test_warmup_blocks_box_manager(self):
        """warm-up 중 BoxMgr도 차단."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("ranging")  # 1캔들, warm-up
        assert gate.active_strategy is None

        box_mgr = make_gmo_box_manager()
        box_mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "entry_long"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snap = MagicMock()
        mock_snap.current_price = 10_000_000.0
        mock_snap.atr = 100_000.0
        mock_snap.ema_slope_pct = 0.1
        mock_snap.rsi = 50.0
        mock_snap.is_preview = False
        mock_snap.pair = "btc_jpy"

        box_mgr._on_entry_signal = AsyncMock()
        box_mgr._position["btc_jpy"] = None

        result = await box_mgr._handle_execution_result("btc_jpy", mock_result, mock_snap, {}, {})

        box_mgr._on_entry_signal.assert_not_called()
        assert result is False


class TestIT06GetStrategyTypeBoxManager:
    """IT-06: GmoCoinBoxManager._get_strategy_type() == 'box_mean_reversion'."""

    def test_box_manager_strategy_type(self):
        box_mgr = make_gmo_box_manager()
        assert box_mgr._get_strategy_type() == "box_mean_reversion"

    def test_shared_gate_strategy_types_differ(self):
        """공유 gate에서 두 매니저의 strategy_type이 서로 다름."""
        gate = make_shared_gate_with_regime("trending")
        trend_mgr = make_gmo_trend_manager()
        box_mgr = make_gmo_box_manager()
        trend_mgr.set_regime_gate(gate)
        box_mgr.set_regime_gate(gate)

        # 동일 gate에서 각자 strategy_type 확인
        assert trend_mgr._get_strategy_type() == "trend_following"
        assert box_mgr._get_strategy_type() == "box_mean_reversion"
        # gate.active_strategy=trend_following이므로 허용/차단 분리
        assert gate.should_allow_entry("trend_following") is True
        assert gate.should_allow_entry("box_mean_reversion") is False
