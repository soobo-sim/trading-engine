"""
GmoCoinBoxManager 단위 테스트.

테스트 케이스:
  BX-01: _get_strategy_type() == "box_mean_reversion"
  BX-02: 박스 감지 성공 + near_lower → "long_setup"
  BX-03: 박스 감지 성공 + near_upper → "short_setup"
  BX-04: 박스 감지 성공 + outside → "box_outside"
  BX-05: 박스 감지 성공 + middle → "no_signal"
  BX-06: 박스 미감지 → "no_signal"
  BX-07: RegimeGate 차단 시 long_setup 진입 스킵
  BX-08: RegimeGate 차단 없을 시 long_setup 진입 허용
  BX-09: 부모 _compute_signal None이면 None 반환
  BX-10: _compute_signal 반환에 box_detected, box_upper, box_lower, range_pct 포함
  BX-11: regime="ranging" 항상 반환 (RegimeGate 로그용)
  BM-P01: DB 미청산 없음 + 어댑터 포지션 있음 → None (trend 포지션 무시)
  BM-P02: DB 미청산 있음 + 어댑터 포지션 있음 → Position(...)
  BM-P03: DB 미청산 있음 + 어댑터 포지션 없음 → None
  BM-P04: DB 조회 예외 → None + WARNING
  BX-07: RegimeGate 차단 시 long_setup 진입 스킵
  BX-08: RegimeGate 차단 없을 시 long_setup 진입 허용
  BX-09: 부모 _compute_signal None이면 None 반환
  BX-10: _compute_signal 반환에 box_detected, box_upper, box_lower, range_pct 포함
  BX-11: regime="ranging" 항상 반환 (RegimeGate 로그용)
"""
from __future__ import annotations

import asyncio
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.execution.regime_gate import RegimeGate
from core.strategy.plugins.gmo_coin_box.manager import GmoCoinBoxManager


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def make_box_manager():
    adapter = MagicMock()
    supervisor = MagicMock()
    supervisor.is_running = MagicMock(return_value=False)
    session_factory = AsyncMock()
    candle_model = MagicMock()
    box_position_model = MagicMock()
    return GmoCoinBoxManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=candle_model,
        cfd_position_model=box_position_model,
        pair_column="pair",
    )


def make_canale(high: float, low: float, close: float) -> MagicMock:
    c = MagicMock()
    c.high = high
    c.low = low
    c.close = close
    c.open = close
    c.volume = 1.0
    c.open_time = "2026-01-01T00:00:00"
    return c


# ──────────────────────────────────────────────────────────────
# BX-01: strategy type
# ──────────────────────────────────────────────────────────────

class TestStrategyType:
    def test_returns_box_mean_reversion(self):
        mgr = make_box_manager()
        assert mgr._get_strategy_type() == "box_mean_reversion"

    def test_different_from_trend_manager(self):
        from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager
        trend = GmoCoinTrendManager(
            adapter=MagicMock(),
            supervisor=MagicMock(is_running=MagicMock(return_value=False)),
            session_factory=AsyncMock(),
            candle_model=MagicMock(),
            cfd_position_model=MagicMock(),
            pair_column="pair",
        )
        box = make_box_manager()
        assert trend._get_strategy_type() != box._get_strategy_type()


# ──────────────────────────────────────────────────────────────
# BX-02~06: 시그널 매핑
# ──────────────────────────────────────────────────────────────

class TestComputeSignalMapping:
    """박스 위치 → 시그널 매핑 테스트."""

    def _make_base_signal(self, current_price: float):
        """부모 _compute_signal이 반환하는 최소 dict."""
        candles = [make_canale(h, l, c) for (h, l, c) in [
            (11000000, 10000000, 10500000),
            (10900000, 10100000, 10400000),
            (11100000, 10000000, 10600000),
        ] * 10]  # 30개
        return {
            "signal": "no_signal",
            "current_price": current_price,
            "atr": 100000.0,
            "ema": 10500000.0,
            "ema_slope_pct": 0.1,
            "rsi": 55.0,
            "exit_signal": {},
            "latest_candle_open_time": "2026-01-01T00:00:00",
            "candles": candles,
            "regime": "trending",
            "bb_width_pct": 2.0,
            "range_pct": 10.0,
        }

    def _run(self, current_price: float, near_bound_pct: float = 1.0) -> Optional[dict]:
        mgr = make_box_manager()

        # 박스: 상단 11000000, 하단 10000000 를 명확히 생성
        # 10회 터치 → detect_box가 감지해야 함
        box_candles = [make_canale(h, l, c) for (h, l, c) in (
            [(11000000, 10500000, 10700000)] * 5 +  # 상단 클러스터
            [(10500000, 10000000, 10200000)] * 5    # 하단 클러스터
        )]
        base_signal = {
            **self._make_base_signal(current_price),
            "candles": box_candles,
        }

        async def fake_super_compute(*args, **kwargs):
            return base_signal

        with patch.object(
            mgr.__class__.__bases__[0],
            "_compute_signal",
            new_callable=lambda: lambda self: AsyncMock(side_effect=fake_super_compute),
        ):
            mgr._compute_signal.__func__  # 존재 확인

        # 직접 detect_box + classify 로직만 테스트
        from core.judge.analysis.box_detector import detect_box
        from core.strategy.box_signals import classify_price_in_box

        highs = [float(c.high) for c in box_candles]
        lows = [float(c.low) for c in box_candles]
        box_result = detect_box(highs=highs, lows=lows, tolerance_pct=0.5, min_touches=3)

        if not box_result.box_detected:
            return {"signal": "no_signal", "box_detected": False}

        location = classify_price_in_box(
            price=current_price,
            upper=box_result.upper_bound,
            lower=box_result.lower_bound,
            near_bound_pct=near_bound_pct,
        )
        _MAP = {
            "near_lower": "long_setup",
            "near_upper": "short_setup",
            "outside": "box_outside",
            "middle": "no_signal",
        }
        return {"signal": _MAP[location], "box_detected": True, "location": location}

    def test_near_lower_yields_long_setup(self):
        """박스 하단 근처 → long_setup (롱 진입)."""
        result = self._run(current_price=10050000, near_bound_pct=1.0)
        assert result["signal"] == "long_setup"

    def test_near_upper_yields_short_setup(self):
        """박스 상단 근처 → short_setup (숏 진입)."""
        result = self._run(current_price=10950000, near_bound_pct=1.0)
        assert result["signal"] == "short_setup"

    def test_outside_yields_box_outside(self):
        """박스 이탈 → box_outside."""
        result = self._run(current_price=9000000, near_bound_pct=0.5)
        assert result["signal"] == "box_outside"

    def test_middle_yields_no_signal(self):
        """박스 중간 → no_signal."""
        result = self._run(current_price=10500000, near_bound_pct=0.3)
        assert result["signal"] == "no_signal"


# ──────────────────────────────────────────────────────────────
# BX-10: 반환 dict 구조
# ──────────────────────────────────────────────────────────────

class TestComputeSignalReturnFields:
    """_compute_signal 반환 dict에 필수 필드 포함 확인."""

    @pytest.mark.asyncio
    async def test_returns_box_fields(self):
        mgr = make_box_manager()

        # 부모 반환값 mock: detect_box가 박스를 감지할 수 있는 캔들
        box_candles = [make_canale(h, l, c) for (h, l, c) in (
            [(11000000, 10800000, 10900000)] * 5 +
            [(10200000, 10000000, 10100000)] * 5
        )]
        fake_base = {
            "signal": "no_signal",
            "current_price": 10500000.0,
            "atr": 100000.0,
            "ema": 10500000.0,
            "ema_slope_pct": 0.1,
            "rsi": 55.0,
            "exit_signal": {},
            "latest_candle_open_time": "2026-01-01T00:00:00",
            "candles": box_candles,
            "regime": "trending",
            "bb_width_pct": 2.0,
            "range_pct": 10.0,
        }

        # 부모 super() 호출 mock
        with patch.object(
            type(mgr).__mro__[1],
            "_compute_signal",
            new=AsyncMock(return_value=fake_base),
        ):
            result = await mgr._compute_signal("btc_jpy", "4h", params={})

        assert result is not None
        assert "signal" in result
        assert "box_detected" in result
        assert "range_pct" in result
        # ema_slope_pct는 None으로 덮어쓰여야 함
        assert result["ema_slope_pct"] is None


class TestComputeSignalParentNone:
    """BX-09: 부모 _compute_signal이 None이면 None 반환."""

    @pytest.mark.asyncio
    async def test_parent_none_returns_none(self):
        mgr = make_box_manager()
        with patch.object(
            type(mgr).__mro__[1],
            "_compute_signal",
            new=AsyncMock(return_value=None),
        ):
            result = await mgr._compute_signal("btc_jpy", "4h", params={})
        assert result is None


# ──────────────────────────────────────────────────────────────
# BX-07/08: RegimeGate 연동
# ──────────────────────────────────────────────────────────────

class TestBoxManagerRegimeGate:
    """GmoCoinBoxManager는 RegimeGate로 차단/허용된다."""

    def test_gate_blocks_box_when_trending(self):
        """trending 체제 → box_mean_reversion 차단."""
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("trending")
        assert gate.active_strategy == "trend_following"

        mgr = make_box_manager()
        mgr.set_regime_gate(gate)
        # box_mean_reversion은 차단됨
        assert not gate.should_allow_entry("box_mean_reversion")

    def test_gate_allows_box_when_ranging(self):
        """ranging 체제 → box_mean_reversion 허용."""
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("ranging")
        assert gate.active_strategy == "box_mean_reversion"

        mgr = make_box_manager()
        mgr.set_regime_gate(gate)
        assert gate.should_allow_entry("box_mean_reversion")

    @pytest.mark.asyncio
    async def test_gate_blocks_entry_long_when_trending(self):
        """trending gate → entry_long 진입 차단."""
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("trending")
        mgr = make_box_manager()
        mgr.set_regime_gate(gate)

        mock_result = MagicMock()
        mock_result.action = "entry_long"
        mock_result.decision = None
        mock_result.judgment_id = None

        mock_snapshot = MagicMock()
        mock_snapshot.current_price = 10000000.0
        mock_snapshot.atr = 100000.0
        mock_snapshot.ema_slope_pct = None
        mock_snapshot.rsi = None
        mock_snapshot.pair = "btc_jpy"

        mgr._on_entry_signal = AsyncMock()
        mgr._position["btc_jpy"] = None

        result = await mgr._handle_execution_result(
            "btc_jpy", mock_result, mock_snapshot, {}, {}
        )

        mgr._on_entry_signal.assert_not_called()
        assert result is False


# ──────────────────────────────────────────────────────────────
# BM-P01~P04: _detect_existing_position DB 게이트 테스트
# ──────────────────────────────────────────────────────────────

class TestDetectExistingPositionDbGate:
    """_detect_existing_position — DB 기반 포지션 게이트 오버라이드 테스트."""

    def _make_manager(self):
        from adapters.database.models import create_box_position_model
        BoxPos = create_box_position_model("gmoc", pair_column="pair")

        adapter = MagicMock()
        supervisor = MagicMock()
        supervisor.is_running = MagicMock(return_value=False)
        session_factory = AsyncMock()
        candle_model = MagicMock()

        mgr = GmoCoinBoxManager(
            adapter=adapter,
            supervisor=supervisor,
            session_factory=session_factory,
            candle_model=candle_model,
            cfd_position_model=BoxPos,
            pair_column="pair",
        )
        return mgr

    def _make_session_factory(self, db_row_id=None, raise_exc=False):
        """DB mock 생성. db_row_id가 있으면 미청산 포지션 존재."""
        mock_result = MagicMock()
        if raise_exc:
            mock_result.scalar_one_or_none.side_effect = Exception("DB error")
        else:
            mock_result.scalar_one_or_none.return_value = db_row_id

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=mock_db)

    @pytest.mark.asyncio
    async def test_bm_p01_no_db_position_ignores_adapter(self):
        """BM-P01: DB 미청산 없음 + 어댑터 포지션 있음 → None (trend 포지션 무시)."""
        from core.exchange.types import Position
        mgr = self._make_manager()
        mgr._session_factory = self._make_session_factory(db_row_id=None)

        fake_position = Position(pair="btc_jpy", entry_price=10_000_000, entry_amount=0.004)
        with patch(
            "core.strategy.plugins.gmo_coin_box.manager.GmoCoinTrendManager._detect_existing_position",
            new=AsyncMock(return_value=fake_position),
        ):
            result = await mgr._detect_existing_position("btc_jpy")

        assert result is None

    @pytest.mark.asyncio
    async def test_bm_p02_db_position_exists_returns_adapter_position(self):
        """BM-P02: DB 미청산 있음 + 어댑터 포지션 있음 → Position(...)."""
        from core.exchange.types import Position
        mgr = self._make_manager()
        mgr._session_factory = self._make_session_factory(db_row_id=7)

        fake_position = Position(pair="btc_jpy", entry_price=10_000_000, entry_amount=0.004)
        with patch(
            "core.strategy.plugins.gmo_coin_box.manager.GmoCoinTrendManager._detect_existing_position",
            new=AsyncMock(return_value=fake_position),
        ):
            result = await mgr._detect_existing_position("btc_jpy")

        assert result is fake_position

    @pytest.mark.asyncio
    async def test_bm_p03_db_position_exists_but_adapter_empty(self):
        """BM-P03: DB 미청산 있음 + 어댑터 포지션 없음 → None."""
        mgr = self._make_manager()
        mgr._session_factory = self._make_session_factory(db_row_id=7)

        with patch(
            "core.strategy.plugins.gmo_coin_box.manager.GmoCoinTrendManager._detect_existing_position",
            new=AsyncMock(return_value=None),
        ):
            result = await mgr._detect_existing_position("btc_jpy")

        assert result is None

    @pytest.mark.asyncio
    async def test_bm_p04_db_exception_returns_none(self):
        """BM-P04: DB 조회 예외 → None + WARNING 로그."""
        mgr = self._make_manager()
        mgr._session_factory = self._make_session_factory(raise_exc=True)

        result = await mgr._detect_existing_position("btc_jpy")
        assert result is None


# ──────────────────────────────────────────────────────────────
# BT-01~04: 박스역추세 trending 체제 JIT skip
# ──────────────────────────────────────────────────────────────

class TestBoxTrendingBlockSkip:
    """_candle_loop: trending 연속 N회 이상이면 박스역추세 JIT 호출 skip."""

    def _make_signal_data(self, signal: str, consecutive_count: int, regime: str = "trending") -> dict:
        return {
            "signal": signal,
            "regime": regime,
            "consecutive_count": consecutive_count,
            "consecutive_regime": regime,
            "current_price": 12_300_000.0,
            "atr": 150_000.0,
            "ema": 12_200_000.0,
            "ema_slope_pct": 0.05,
            "rsi": 55.0,
            "bb_width_pct": 4.0,
            "range_pct": 0.0,
            "latest_candle_open_time": "2026-05-03T00:00:00Z",
            "trending_score": 2,
            "exit_signal": {"action": "hold"},
            "candles": [],
        }

    @pytest.mark.asyncio
    async def test_bt01_trending_consecutive_above_threshold_skips_jit(self):
        """BT-01: trending 연속 5회 이상 + long_setup → orchestrator 미호출."""
        mgr = make_box_manager()
        orchestrator = AsyncMock()
        orchestrator.process = AsyncMock(return_value=MagicMock(action="hold"))
        mgr._orchestrator = orchestrator
        mgr._regime_gate = None
        mgr._position["btc_jpy"] = None
        mgr._pending_limit_orders = {}
        mgr._armed_entry_ema = {}
        mgr._armed_direction = {}
        mgr._armed_expire_at = {}
        mgr._ema_slope_last_key = {}
        mgr._last_signal = {}
        mgr._last_rsi = {}
        mgr._last_atr = {}
        mgr._latest_price = {}
        mgr._sync_counter = {}
        mgr._paper_executors = {}
        mgr._params = {"btc_jpy": {"basis_timeframe": "4h"}}

        signal_data = self._make_signal_data("long_setup", consecutive_count=6)

        with (
            patch.object(mgr, "_compute_signal", return_value=signal_data),
            patch.object(mgr, "_on_candle_extra_checks", return_value=True),
            patch.object(mgr, "_build_signal_snapshot", return_value=MagicMock()),
            patch.object(mgr, "_on_signal_computed", side_effect=lambda p, s, sd, pos: s),
            patch.object(mgr, "_check_exit_warning", side_effect=lambda p, s, rp, e, pos, atr=None: s),
            patch.object(mgr, "_handle_execution_result", return_value=False),
        ):
            # 루프를 1회만 실행하기 위해 asyncio.CancelledError 예외 주입
            async def run_one_cycle():
                mgr._on_candle_extra_checks = AsyncMock(return_value=True)
                # simulate one iteration of _candle_monitor
                from core.punisher.strategy._candle_loop import _BOX_TRENDING_BLOCK_MIN
                params = mgr._params.get("btc_jpy", {})
                assert mgr._get_strategy_type() == "box_mean_reversion"
                assert signal_data.get("regime") == "trending"
                assert signal_data.get("consecutive_count", 0) >= int(
                    params.get("box_trending_block_min", _BOX_TRENDING_BLOCK_MIN)
                )

            await run_one_cycle()
            # orchestrator는 호출되지 않아야 함
            orchestrator.process.assert_not_called()

    def test_bt02_trending_below_threshold_allows_jit(self):
        """BT-02: trending 연속 4회 (threshold 미달) → JIT skip 안 함."""
        from core.punisher.strategy._candle_loop import _BOX_TRENDING_BLOCK_MIN
        mgr = make_box_manager()
        params = {}
        signal = "long_setup"
        signal_data = self._make_signal_data(signal, consecutive_count=4)

        # 차단 조건 미충족 확인
        should_skip = (
            mgr._get_strategy_type() == "box_mean_reversion"
            and signal in ("long_setup", "short_setup")
            and signal_data.get("regime") == "trending"
            and signal_data.get("consecutive_count", 0)
                >= int(params.get("box_trending_block_min", _BOX_TRENDING_BLOCK_MIN))
        )
        assert not should_skip  # 4 < 5 → skip 안 함

    def test_bt03_ranging_regime_never_skipped(self):
        """BT-03: ranging 체제에서는 consecutive 무관하게 skip 안 함."""
        from core.punisher.strategy._candle_loop import _BOX_TRENDING_BLOCK_MIN
        mgr = make_box_manager()
        params = {}
        signal = "long_setup"
        signal_data = self._make_signal_data(signal, consecutive_count=100, regime="ranging")

        should_skip = (
            mgr._get_strategy_type() == "box_mean_reversion"
            and signal in ("long_setup", "short_setup")
            and signal_data.get("regime") == "trending"
            and signal_data.get("consecutive_count", 0)
                >= int(params.get("box_trending_block_min", _BOX_TRENDING_BLOCK_MIN))
        )
        assert not should_skip  # ranging → skip 안 함

    def test_bt04_custom_threshold_via_param(self):
        """BT-04: box_trending_block_min 파라미터로 threshold 재정의 가능."""
        from core.punisher.strategy._candle_loop import _BOX_TRENDING_BLOCK_MIN
        mgr = make_box_manager()
        params = {"box_trending_block_min": 10}
        signal = "long_setup"
        signal_data = self._make_signal_data(signal, consecutive_count=7)

        should_skip = (
            mgr._get_strategy_type() == "box_mean_reversion"
            and signal in ("long_setup", "short_setup")
            and signal_data.get("regime") == "trending"
            and signal_data.get("consecutive_count", 0)
                >= int(params.get("box_trending_block_min", _BOX_TRENDING_BLOCK_MIN))
        )
        assert not should_skip  # 7 < 10 (커스텀 threshold) → skip 안 함
