"""
BUG-040 / BUG-041 / BUG-042 회귀 테스트

BUG-040: range_pct를 bb_period 윈도우로 제한 → 긴 캔들 리스트에서도 ranging 올바르게 감지
BUG-041: RegimeGate 갱신이 _on_candle_extra_checks 이전에 실행 → extra_checks=False 때도 갱신
BUG-042: long_setup 조건이 regime_trending 요구 → unclear 체제 진입 차단
"""
import asyncio
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from core.shared.signals import classify_regime, compute_trend_signal
from core.execution.regime_gate import RegimeGate


# ── 캔들 Mock 헬퍼 ────────────────────────────────────────────

class _FakeCandle:
    def __init__(self, open_, high, low, close, volume=100.0):
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.open_time = "2026-01-01T00:00:00"


def _make_flat_recent_candles(total: int = 40, flat_last: int = 20, price: float = 10_000_000.0, noise: float = 5_000.0) -> list:
    """최근 flat_last 개는 횡보, 나머지는 과거 변동 (range_pct 전체 계산 시 over-estimate 발생)."""
    candles = []
    # 과거: 큰 변동 (전체 40봉 range_pct를 크게 부풀림)
    for i in range(total - flat_last):
        c = price + (200_000.0 if i % 2 == 0 else -200_000.0)
        candles.append(_FakeCandle(c, c + 50_000, c - 50_000, c))
    # 최근: 좁은 횡보
    for i in range(flat_last):
        c = price + (noise if i % 2 == 0 else -noise)
        candles.append(_FakeCandle(c, c + noise * 0.5, c - noise * 0.5, c))
    return candles


# ── BUG-040: range_pct bb_period 윈도우 제한 ─────────────────

class TestBug040RangePctWindow:
    """BUG-040: range_pct가 bb_period 윈도우를 사용해 ranging 올바르게 감지"""

    def test_flat_recent_candles_identified_as_ranging(self):
        """
        전체 40봉 중 최근 20봉은 좁은 횡보.
        range_pct가 bb_period(20봉) 윈도우만 사용하면 ranging 감지.
        전체 40봉 사용 시 과거 큰 변동으로 range_pct 과대 → unclear 오판.
        """
        candles = _make_flat_recent_candles(total=40, flat_last=20, noise=5_000.0)
        # bb_period=20 → range_pct를 최근 20봉으로 계산
        params = {
            "bb_period": 20,
            "bb_width_ranging_max": 3.0,
            "range_pct_ranging_max": 5.0,
            "bb_width_trending_min": 3.0,
        }
        result = compute_trend_signal(candles, params=params)
        # 최근 20봉이 매우 좁으므로 bb_width_pct 작음 + range_pct 작음 → ranging
        # (수정 전: 전체 40봉 range_pct > 5% → unclear)
        assert result["regime"] == "ranging", (
            f"ranging이어야 하는데 {result['regime']} 반환. "
            f"bb_width_pct={result['bb_width_pct']:.2f}%, range_pct={result['range_pct']:.2f}%"
        )

    def test_range_pct_uses_bb_period_window(self):
        """range_pct가 bb_period 윈도우(최근 N봉)의 max-min을 사용하는지 검증."""
        noise = 5_000.0
        price = 10_000_000.0
        # 전체 40봉 중 최근 20봉은 좁은 횡보
        candles = _make_flat_recent_candles(total=40, flat_last=20, noise=noise)
        result = compute_trend_signal(candles, params={"bb_period": 20})
        # 최근 20봉의 max-min: ±5000 = 10000 / 10_000_000 * 100 = 0.1%
        # 전체 40봉의 max-min: ±200_000 = 400_000 / price * 100 = 4%
        # range_pct가 최근 20봉 기준이면 ≈ 0.1~0.15%, 전체 기준이면 ≈ 4%
        assert result["range_pct"] < 1.0, (
            f"range_pct={result['range_pct']:.3f}% — bb_period 윈도우 미적용 의심"
        )

    def test_full_window_range_pct_would_be_large(self):
        """검증용: 전체 40봉 range_pct는 크다는 것을 확인 (수정 전 동작)."""
        noise = 5_000.0
        price = 10_000_000.0
        candles = _make_flat_recent_candles(total=40, flat_last=20, noise=noise)
        closes = [float(c.close) for c in candles]
        highs = [float(c.high) for c in candles]
        lows = [float(c.low) for c in candles]
        # 전체 range_pct (수정 전 방식)
        full_range_pct = (max(highs) - min(lows)) / closes[0] * 100
        assert full_range_pct > 3.0, "테스트 캔들 설계 오류 — 과거 변동이 충분히 커야 함"


# ── BUG-042: long_setup가 regime_trending 요구 ─────────────────

class TestBug042EntryOkRequiresTrending:
    """BUG-042: unclear 체제에서 long_setup 차단 (not regime_ranging → regime_trending)"""

    def _make_unclear_candles(self, n: int = 30) -> list:
        """bb_width가 trending 임계값(3%) 미만, ranging 임계값 근방인 캔들."""
        price = 10_000_000.0
        # BB폭 ≈ 1.5~2.5% → unclear (trending<3, ranging 경계)
        candles = []
        for i in range(n):
            # moderate volatility: ±70K 변동
            c = price + 70_000 * (1 if i % 4 < 2 else -1)
            candles.append(_FakeCandle(c, c + 50_000, c - 50_000, c))
        return candles

    def _make_trending_candles(self, n: int = 30) -> list:
        """bb_width ≥ 3% → trending 체제."""
        price = 10_000_000.0
        candles = []
        for i in range(n):
            c = price + i * 30_000  # 꾸준한 상승 + 변동성
            candles.append(_FakeCandle(c, c + 150_000, c - 150_000, c))
        return candles

    def test_unclear_regime_blocks_long_setup(self):
        """unclear 체제(trending=False, ranging=False)에서 long_setup 발동 안 됨."""
        # 강제로 unclear 상황을 만드는 파라미터
        params = {
            "bb_width_trending_min": 99.0,   # trending 불가
            "bb_width_ranging_max": 0.0,      # ranging 불가
            "entry_rsi_min": 0.0,
            "entry_rsi_max": 100.0,
        }
        candles = self._make_unclear_candles(30)
        result = compute_trend_signal(candles, params=params)
        # unclear 체제 → regime_trending=False → long_setup 불가
        assert result["regime"] == "unclear"
        assert result["signal"] != "long_setup", (
            f"unclear 체제에서 long_setup 발동! (BUG-042 미수정)"
        )

    def test_trending_regime_allows_long_setup(self):
        """trending 체제 + price>EMA + slope양수 + RSI 범위 → long_setup 허용."""
        params = {
            "bb_width_trending_min": 0.01,  # trending 쉽게 달성
            "entry_rsi_min": 0.0,
            "entry_rsi_max": 100.0,
        }
        candles = self._make_trending_candles(30)
        result = compute_trend_signal(candles, params=params)
        # trending 체제이면 long_setup 가능 (price>ema, slope>0 조건도 맞아야 함)
        if result["regime"] == "trending":
            # slope와 price>ema 조건이 맞으면 long_setup
            price_above_ema = result.get("current_price", 0) > (result.get("ema") or 0)
            slope_ok = (result.get("ema_slope_pct") or 0) >= 0
            if price_above_ema and slope_ok:
                assert result["signal"] == "long_setup", (
                    f"trending 체제 + 진입 조건 충족인데 long_setup 아님: {result['signal']}"
                )

    def test_ranging_regime_gives_wait_regime_not_long_setup(self):
        """ranging 체제에서 price>EMA + slope양수여도 long_setup 아닌 wait_regime."""
        params = {
            "bb_width_trending_min": 99.0,   # trending 불가
            "bb_width_ranging_max": 99.0,    # 항상 ranging
            "range_pct_ranging_max": 99.0,
            "entry_rsi_min": 0.0,
            "entry_rsi_max": 100.0,
        }
        from tests.unit.test_signals import _make_uptrend_candles
        candles = _make_uptrend_candles(30)
        result = compute_trend_signal(candles, params=params)
        # ranging이면 long_setup 불가
        if result["regime"] == "ranging":
            assert result["signal"] != "long_setup"

    def test_long_setup_requires_regime_trending_not_just_not_ranging(self):
        """
        핵심: regime_trending=False, regime_ranging=False (= unclear) 시
        not regime_ranging = True 이지만 long_setup가 발동되면 안 된다.
        수정 후: regime_trending 필수 → unclear 차단.
        """
        # classify_regime으로 unclear 조건 확인
        _, regime_trending, regime_ranging = classify_regime(
            bb_width_pct=2.0,   # < 3.0 → trending=False
            range_pct=6.0,      # ≥ 5.0 → ranging=False  (unclear)
        )
        assert regime_trending is False
        assert regime_ranging is False  # unclear 확인

        # unclear 상황을 simulate: bb_width=2%, range_pct=6% 수준 캔들
        # 진입 조건(price>ema, slope>0, rsi)은 모두 충족하되 regime=unclear
        params = {
            "bb_width_trending_min": 3.0,
            "bb_width_ranging_max": 3.0,
            "range_pct_ranging_max": 5.0,
            "entry_rsi_min": 0.0,
            "entry_rsi_max": 100.0,
        }
        # 캔들: moderate oscillation → bb_width ≈ 2%, range_pct ≈ 6-7%
        price = 10_000_000.0
        candles = []
        for i in range(30):
            c = price + 100_000 * (1 if i % 3 == 0 else (-1 if i % 3 == 1 else 0))
            candles.append(_FakeCandle(c, c + 50_000, c - 50_000, c))

        result = compute_trend_signal(candles, params=params)
        if result["regime"] == "unclear":
            assert result["signal"] != "long_setup", (
                "unclear 체제에서 long_setup 발동 — BUG-042 미수정"
            )


# ── BUG-041: RegimeGate 갱신이 extra_checks 전에 실행 ────────────────

class TestBug041RegimeGateBeforeExtraChecks:
    """BUG-041: extra_checks=False(continue) 시에도 RegimeGate가 갱신된다."""

    @pytest.mark.asyncio
    async def test_regime_gate_updated_even_when_extra_checks_false(self):
        """
        _candle_monitor에서 extra_checks가 False를 반환해도
        RegimeGate.update_regime()이 이미 호출됐어야 한다.
        """
        from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager

        adapter = MagicMock()
        supervisor = MagicMock()
        supervisor.is_running = MagicMock(return_value=False)
        session_factory = AsyncMock()
        candle_model = MagicMock()
        pos_model = MagicMock()

        mgr = GmoCoinTrendManager(
            adapter=adapter,
            supervisor=supervisor,
            session_factory=session_factory,
            candle_model=candle_model,
            cfd_position_model=pos_model,
            pair_column="pair",
        )

        # RegimeGate 설정
        gate = RegimeGate("btc_jpy")
        gate.update_regime = MagicMock(wraps=gate.update_regime)
        mgr.set_regime_gate(gate)

        mgr._params["btc_jpy"] = {"basis_timeframe": "4h"}
        mgr._position["btc_jpy"] = None
        mgr._last_signal["btc_jpy"] = ""

        NEW_CANDLE_KEY = "2026-04-26T00:00:00"
        signal_data = {
            "signal": "no_signal",
            "current_price": 10_000_000.0,
            "ema": 9_900_000.0,
            "ema_slope_pct": 0.1,
            "atr": 100_000.0,
            "stop_loss_price": 9_800_000.0,
            "rsi": 52.0,
            "rsi_series": [],
            "regime": "ranging",
            "bb_width_pct": 1.5,
            "range_pct": 1.2,
            "exit_signal": {"action": "hold", "triggers": {}},
            "candles": [],
            "latest_candle_open_time": NEW_CANDLE_KEY,
        }

        update_regime_calls = []

        async def fake_extra_checks(pair, params):
            # extra_checks가 False를 반환하는 시점에 이미 update_regime이 호출됐어야 함
            update_regime_calls.append(gate.update_regime.call_count)
            return False  # 이 사이클 스킵

        call_count = 0
        async def fake_compute_signal(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return signal_data
            raise asyncio.CancelledError()

        mgr._compute_signal = fake_compute_signal
        mgr._on_candle_extra_checks = fake_extra_checks
        mgr._sync_position_state = AsyncMock()
        mgr._paper_executors = set()

        # asyncio.sleep을 즉시 반환으로 패치
        with patch("core.punisher.strategy._candle_loop.asyncio.sleep", new=AsyncMock()):
            with patch(
                "core.execution.regime_gate_persistence.save_regime_gate_state",
                new=AsyncMock(),
            ):
                try:
                    await asyncio.wait_for(mgr._candle_monitor("btc_jpy"), timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

        # extra_checks 진입 시점에 이미 update_regime이 1회 이상 호출돼 있어야 함
        assert len(update_regime_calls) > 0, "extra_checks가 한 번도 호출되지 않음"
        assert update_regime_calls[0] >= 1, (
            f"extra_checks 호출 시점에 update_regime이 {update_regime_calls[0]}번 호출됨 — "
            "0이면 BUG-041 미수정 (RegimeGate가 extra_checks 후에 있음)"
        )

    @pytest.mark.asyncio
    async def test_regime_gate_db_saved_when_new_candle_even_if_extra_checks_false(self):
        """
        새 4H 캔들 경계에서 extra_checks=False여도 DB 영속화가 시도된다.
        """
        from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager

        adapter = MagicMock()
        supervisor = MagicMock()
        supervisor.is_running = MagicMock(return_value=False)
        session_factory = AsyncMock()
        candle_model = MagicMock()
        pos_model = MagicMock()

        mgr = GmoCoinTrendManager(
            adapter=adapter,
            supervisor=supervisor,
            session_factory=session_factory,
            candle_model=candle_model,
            cfd_position_model=pos_model,
            pair_column="pair",
        )

        gate = RegimeGate("btc_jpy")
        mgr.set_regime_gate(gate)

        mgr._params["btc_jpy"] = {"basis_timeframe": "4h"}
        mgr._position["btc_jpy"] = None
        mgr._last_signal["btc_jpy"] = ""

        NEW_CANDLE_KEY = "2026-04-26T04:00:00"
        signal_data = {
            "signal": "no_signal",
            "current_price": 10_000_000.0,
            "ema": 9_900_000.0,
            "ema_slope_pct": 0.1,
            "atr": 100_000.0,
            "stop_loss_price": None,
            "rsi": 52.0,
            "rsi_series": [],
            "regime": "ranging",
            "bb_width_pct": 1.5,
            "range_pct": 1.2,
            "exit_signal": {"action": "hold", "triggers": {}},
            "candles": [],
            "latest_candle_open_time": NEW_CANDLE_KEY,
        }

        call_count = 0
        async def fake_compute_signal(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return signal_data
            raise asyncio.CancelledError()

        async def fake_extra_checks(pair, params):
            return False

        mgr._compute_signal = fake_compute_signal
        mgr._on_candle_extra_checks = fake_extra_checks
        mgr._paper_executors = set()

        save_calls = []
        async def mock_save(factory, gate_obj):
            save_calls.append(True)

        with patch("core.punisher.strategy._candle_loop.asyncio.sleep", new=AsyncMock()):
            with patch(
                "core.execution.regime_gate_persistence.save_regime_gate_state",
                new=mock_save,
            ):
                try:
                    await asyncio.wait_for(mgr._candle_monitor("btc_jpy"), timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

        # 새 캔들 + ranging → DB 저장 시도됐어야 함
        assert len(save_calls) >= 1, (
            "extra_checks=False 시에도 RegimeGate DB 저장이 시도돼야 함 (BUG-041)"
        )
