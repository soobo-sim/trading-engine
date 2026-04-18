"""
체제 시뮬레이션 단위 테스트.

테스트 케이스:
  RS-01: 전체 trending 캔들 → 3봉째부터 active_strategy="trend_following", switches 1건
  RS-02: unclear 포함 → active_strategy=None 구간, blocked_candles > 0
  RS-03: streak_required=5 → 4연속 trending 후에도 active_strategy=None
  RS-04: 캔들 부족 (limit 미만) → total_candles=0 (에러 없이)
  RS-05: compute_candle_limit 단위 테스트 — divergence_lookback=60 주면 60 반환
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.backtest.regime_simulator import simulate_regime
from core.shared.signals import compute_candle_limit

# ─── 상수 ────────────────────────────────────────────────────────
_MOCKED_LIMIT = 5  # 테스트에서 사용할 축소된 limit (실제 40 대신)


# ─── 헬퍼 ────────────────────────────────────────────────────────

def _candles(n: int) -> list:
    """open_time 속성을 가진 최소 SimpleNamespace 캔들 리스트."""
    return [
        SimpleNamespace(open_time=f"2024-01-01T{i:05d}Z", close=100.0, high=101.0, low=99.0)
        for i in range(n)
    ]


def _sig(regime: str, bb: float = 5.0, rng: float = 10.0) -> dict:
    return {
        "regime": regime,
        "bb_width_pct": bb,
        "range_pct": rng,
        "signal": "hold",
        "ema": 100.0,
        "atr": 1.0,
        "rsi": 50.0,
    }


# ─── RS-01 ────────────────────────────────────────────────────────

class TestRS01AllTrending:
    """RS-01: 전체 trending 캔들 → 3봉째부터 active_strategy="trend_following", switches 1건."""

    def test_third_snapshot_activates_trend_following(self):
        """streak_required=3, 3 trending 스냅샷 → 3번째에서 trend_following 전환."""
        # limit=5이면 range(4, 4+3)=range(4,7) → i=4,5,6 → 스냅샷 3개
        candles = _candles(_MOCKED_LIMIT + 3 - 1)  # 7개
        with (
            patch("core.backtest.regime_simulator.compute_candle_limit", return_value=_MOCKED_LIMIT),
            patch("core.backtest.regime_simulator.compute_trend_signal", return_value=_sig("trending")),
        ):
            result = simulate_regime(candles, {}, streak_required=3)

        assert result.total_candles == 3
        # 첫 2봉은 warm-up (active=None), 3봉째에서 전환
        assert result.snapshots[0].active_strategy is None
        assert result.snapshots[1].active_strategy is None
        assert result.snapshots[2].active_strategy == "trend_following"

    def test_switches_contains_exactly_one_entry(self):
        """switches 리스트에 정확히 1건 (None → trend_following)."""
        candles = _candles(_MOCKED_LIMIT + 3 - 1)
        with (
            patch("core.backtest.regime_simulator.compute_candle_limit", return_value=_MOCKED_LIMIT),
            patch("core.backtest.regime_simulator.compute_trend_signal", return_value=_sig("trending")),
        ):
            result = simulate_regime(candles, {}, streak_required=3)

        assert len(result.switches) == 1
        assert result.switches[0]["to"] == "trend_following"
        assert result.switches[0]["from"] is None

    def test_regime_counts_all_trending(self):
        """regime_counts["trending"] == total_candles."""
        candles = _candles(_MOCKED_LIMIT + 3 - 1)
        with (
            patch("core.backtest.regime_simulator.compute_candle_limit", return_value=_MOCKED_LIMIT),
            patch("core.backtest.regime_simulator.compute_trend_signal", return_value=_sig("trending")),
        ):
            result = simulate_regime(candles, {}, streak_required=3)

        assert result.regime_counts["trending"] == result.total_candles


# ─── RS-02 ────────────────────────────────────────────────────────

class TestRS02UnclearBlocksEntry:
    """RS-02: unclear 포함 → active_strategy=None 구간, blocked_candles > 0."""

    def test_unclear_sets_active_to_none(self):
        """trending×3 후 unclear 1캔들 → active_strategy=None."""
        candles = _candles(_MOCKED_LIMIT + 4 - 1)  # 스냅샷 4개
        signals = [
            _sig("trending"),   # 1번째: warm-up
            _sig("trending"),   # 2번째: warm-up
            _sig("trending"),   # 3번째: switches → trend_following
            _sig("unclear", bb=1.0, rng=5.5),  # 4번째: unclear → active=None
        ]
        with (
            patch("core.backtest.regime_simulator.compute_candle_limit", return_value=_MOCKED_LIMIT),
            patch("core.backtest.regime_simulator.compute_trend_signal", side_effect=signals),
        ):
            result = simulate_regime(candles, {}, streak_required=3)

        assert result.snapshots[2].active_strategy == "trend_following"
        assert result.snapshots[3].active_strategy is None

    def test_blocked_candles_positive(self):
        """unclear 포함 시 blocked_candles > 0."""
        candles = _candles(_MOCKED_LIMIT + 4 - 1)
        signals = [
            _sig("trending"),
            _sig("trending"),
            _sig("trending"),
            _sig("unclear", bb=1.0, rng=5.5),
        ]
        with (
            patch("core.backtest.regime_simulator.compute_candle_limit", return_value=_MOCKED_LIMIT),
            patch("core.backtest.regime_simulator.compute_trend_signal", side_effect=signals),
        ):
            result = simulate_regime(candles, {}, streak_required=3)

        assert result.blocked_candles > 0

    def test_unclear_regime_counted(self):
        """unclear 스냅샷이 regime_counts["unclear"]에 반영됨."""
        candles = _candles(_MOCKED_LIMIT + 4 - 1)
        signals = [
            _sig("trending"),
            _sig("trending"),
            _sig("trending"),
            _sig("unclear", bb=1.0, rng=5.5),
        ]
        with (
            patch("core.backtest.regime_simulator.compute_candle_limit", return_value=_MOCKED_LIMIT),
            patch("core.backtest.regime_simulator.compute_trend_signal", side_effect=signals),
        ):
            result = simulate_regime(candles, {}, streak_required=3)

        assert result.regime_counts.get("unclear", 0) == 1


# ─── RS-03 ────────────────────────────────────────────────────────

class TestRS03StreakRequired5:
    """RS-03: streak_required=5 → 4연속 trending 후에도 active_strategy=None."""

    def test_4_consecutive_trending_still_blocked(self):
        """streak_required=5이면 4번의 trending으로도 전환 없음."""
        candles = _candles(_MOCKED_LIMIT + 4 - 1)  # 스냅샷 4개
        with (
            patch("core.backtest.regime_simulator.compute_candle_limit", return_value=_MOCKED_LIMIT),
            patch("core.backtest.regime_simulator.compute_trend_signal", return_value=_sig("trending")),
        ):
            result = simulate_regime(candles, {}, streak_required=5)

        # 4 < 5 → 전환 없음
        assert result.snapshots[-1].active_strategy is None
        assert len(result.switches) == 0

    def test_5_consecutive_trending_activates(self):
        """streak_required=5이면 5번째 trending에서 전환 발생."""
        candles = _candles(_MOCKED_LIMIT + 5 - 1)  # 스냅샷 5개
        with (
            patch("core.backtest.regime_simulator.compute_candle_limit", return_value=_MOCKED_LIMIT),
            patch("core.backtest.regime_simulator.compute_trend_signal", return_value=_sig("trending")),
        ):
            result = simulate_regime(candles, {}, streak_required=5)

        assert result.snapshots[-1].active_strategy == "trend_following"
        assert len(result.switches) == 1


# ─── RS-04 ────────────────────────────────────────────────────────

class TestRS04InsufficientCandles:
    """RS-04: 캔들 부족 (limit 미만) → total_candles=0 (에러 없이)."""

    def test_zero_candles_returns_empty_result(self):
        """캔들 0개 → total_candles=0, 예외 없음."""
        with patch("core.backtest.regime_simulator.compute_candle_limit", return_value=_MOCKED_LIMIT):
            result = simulate_regime([], {})

        assert result.total_candles == 0
        assert result.switches == []
        assert result.blocked_candles == 0

    def test_candles_less_than_limit_returns_empty_result(self):
        """캔들 수 < limit → range(limit-1, len) = 빈 범위 → 스냅샷 0."""
        candles = _candles(_MOCKED_LIMIT - 1)  # 4개, limit=5 → 부족
        with patch("core.backtest.regime_simulator.compute_candle_limit", return_value=_MOCKED_LIMIT):
            result = simulate_regime(candles, {})

        assert result.total_candles == 0
        assert result.snapshots == []

    def test_exactly_limit_candles_gives_one_snapshot(self):
        """캔들 수 == limit → 스냅샷 정확히 1개."""
        candles = _candles(_MOCKED_LIMIT)
        with (
            patch("core.backtest.regime_simulator.compute_candle_limit", return_value=_MOCKED_LIMIT),
            patch("core.backtest.regime_simulator.compute_trend_signal", return_value=_sig("trending")),
        ):
            result = simulate_regime(candles, {})

        assert result.total_candles == 1


# ─── RS-05 ────────────────────────────────────────────────────────

class TestRS05ComputeCandleLimit:
    """RS-05: compute_candle_limit 단위 테스트."""

    def test_divergence_lookback_60_returns_60(self):
        """divergence_lookback=60 → max(40, 15, 60) = 60."""
        assert compute_candle_limit({"divergence_lookback": 60}) == 60

    def test_default_params_returns_40(self):
        """기본 파라미터 → max(20*2=40, 14+1=15, 40) = 40."""
        assert compute_candle_limit({}) == 40

    def test_none_params_returns_40(self):
        """params=None → 기본값 사용 → 40."""
        assert compute_candle_limit(None) == 40

    def test_large_ema_period_dominates(self):
        """ema_period=30 → 30*2=60 > lookback=40, atr+1=15 → 60."""
        assert compute_candle_limit({"ema_period": 30}) == 60

    def test_large_atr_period(self):
        """atr_period=50 → atr+1=51 < ema*2=40? No: 51 > 40 → 51."""
        # max(20*2=40, 50+1=51, 40=lookback) = 51
        assert compute_candle_limit({"atr_period": 50}) == 51
