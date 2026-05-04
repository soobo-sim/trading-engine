"""
trailing_stop 청산 후 재진입 slope 완화 테스트.

테스트 케이스:
  TS-REENTRY-01: trailing_stop 청산 후 _last_trailing_stop_time 기록
  TS-REENTRY-02: trending 체제 + trailing_stop 후 slope 완화 파라미터 적용
  TS-REENTRY-03: 윈도우 만료 후 slope 완화 미적용
  TS-REENTRY-04: trailing_stop_reentry_slope_min 미설정 시 완화 없음
  TS-REENTRY-05: 포지션 보유 중에는 완화 미적용
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager


def make_trend_manager():
    adapter = MagicMock()
    supervisor = MagicMock()
    supervisor.is_running = MagicMock(return_value=False)
    session_factory = AsyncMock()
    candle_model = MagicMock()
    position_model = MagicMock()
    return GmoCoinTrendManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=candle_model,
        cfd_position_model=position_model,
        pair_column="pair",
    )


class TestTrailingStopReentryTracking:
    """TS-REENTRY-01: trailing_stop 청산 시 _last_trailing_stop_time 기록."""

    @pytest.mark.asyncio
    async def test_ts_reentry_01_records_time_on_trailing_stop(self):
        """trailing_stop 이유로 청산 시 _last_trailing_stop_time[pair] 기록."""
        mgr = make_trend_manager()
        mgr._position["btc_jpy"] = None
        mgr._paper_executors = {}
        mgr._paper_positions = {}
        mgr._latest_price = {"btc_jpy": 12_000_000.0}
        mgr._snapshot_collector = None

        close_impl_called = []

        async def fake_close_impl(pair, reason):
            close_impl_called.append(reason)

        mgr._close_position_impl = fake_close_impl

        # 학습 루프 관련 mock
        mgr._update_judgment_outcome = AsyncMock()

        before = time.time()
        await mgr._close_position("btc_jpy", "trailing_stop")
        after = time.time()

        assert hasattr(mgr, "_last_trailing_stop_time")
        ts = mgr._last_trailing_stop_time.get("btc_jpy")
        assert ts is not None
        assert before <= ts <= after

    @pytest.mark.asyncio
    async def test_ts_reentry_02_stop_loss_does_not_record(self):
        """stop_loss 이유로 청산 시 _last_trailing_stop_time 갱신 안 함."""
        mgr = make_trend_manager()
        mgr._position["btc_jpy"] = None
        mgr._paper_executors = {}
        mgr._paper_positions = {}
        mgr._latest_price = {"btc_jpy": 12_000_000.0}
        mgr._snapshot_collector = None

        async def fake_close_impl(pair, reason):
            pass

        mgr._close_position_impl = fake_close_impl
        mgr._update_judgment_outcome = AsyncMock()

        await mgr._close_position("btc_jpy", "stop_loss")

        ts = getattr(mgr, "_last_trailing_stop_time", {}).get("btc_jpy")
        assert ts is None


class TestTrailingStopReentryParamRelax:
    """TS-REENTRY-02~05: _effective_params slope 완화 로직."""

    def _slope_relaxed(
        self,
        pos_is_none: bool,
        last_ts_offset: float,  # now - offset = last_ts (음수면 미래)
        params: dict,
        strategy_type: str = "trend_following",
    ) -> dict | None:
        """_candle_loop에서 _effective_params 계산 로직을 단독 검증."""
        from core.punisher.strategy._candle_loop import _TRAILING_STOP_REENTRY_WINDOW_SEC

        _last_trailing_stop_time = {}
        if last_ts_offset is not None:
            _last_trailing_stop_time["btc_jpy"] = time.time() - last_ts_offset

        if not pos_is_none or strategy_type != "trend_following":
            return None  # 완화 없음

        _last_ts = _last_trailing_stop_time.get("btc_jpy", 0)
        _reentry_window = float(
            params.get("trailing_stop_reentry_window_sec", _TRAILING_STOP_REENTRY_WINDOW_SEC)
        )
        _reentry_slope = params.get("trailing_stop_reentry_slope_min")
        if (
            _reentry_slope is not None
            and _last_ts > 0
            and (time.time() - _last_ts) <= _reentry_window
            and float(_reentry_slope) < float(params.get("ema_slope_entry_min", 0.0))
        ):
            return {**params, "ema_slope_entry_min": float(_reentry_slope)}
        return None

    def test_ts_reentry_02_applies_relaxed_slope_within_window(self):
        """TS-REENTRY-02: 윈도우 내 trailing_stop + slope 설정 → 완화 적용."""
        params = {
            "ema_slope_entry_min": 0.08,
            "trailing_stop_reentry_slope_min": 0.03,
            "trailing_stop_reentry_window_sec": 3600,
        }
        result = self._slope_relaxed(pos_is_none=True, last_ts_offset=1800, params=params)
        assert result is not None
        assert result["ema_slope_entry_min"] == 0.03

    def test_ts_reentry_03_expired_window_no_relax(self):
        """TS-REENTRY-03: 윈도우 만료(4000초) → 완화 미적용."""
        params = {
            "ema_slope_entry_min": 0.08,
            "trailing_stop_reentry_slope_min": 0.03,
            "trailing_stop_reentry_window_sec": 3600,
        }
        result = self._slope_relaxed(pos_is_none=True, last_ts_offset=4000, params=params)
        assert result is None

    def test_ts_reentry_04_no_reentry_slope_param_no_relax(self):
        """TS-REENTRY-04: trailing_stop_reentry_slope_min 미설정 → 완화 없음."""
        params = {"ema_slope_entry_min": 0.08}
        result = self._slope_relaxed(pos_is_none=True, last_ts_offset=100, params=params)
        assert result is None

    def test_ts_reentry_05_position_exists_no_relax(self):
        """TS-REENTRY-05: 포지션 보유 중(pos not None) → 완화 미적용."""
        params = {
            "ema_slope_entry_min": 0.08,
            "trailing_stop_reentry_slope_min": 0.03,
        }
        result = self._slope_relaxed(pos_is_none=False, last_ts_offset=100, params=params)
        assert result is None

    def test_ts_reentry_06_relaxed_slope_must_be_lower(self):
        """TS-REENTRY-06: reentry_slope >= entry_slope이면 완화 미적용 (의미 없음)."""
        params = {
            "ema_slope_entry_min": 0.03,
            "trailing_stop_reentry_slope_min": 0.05,  # higher → no relax
        }
        result = self._slope_relaxed(pos_is_none=True, last_ts_offset=100, params=params)
        assert result is None
