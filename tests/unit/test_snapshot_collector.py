"""
SnapshotCollector 단위 테스트 (V-21~V-29).

T1/T2 트리거, 중복 방지, fail-safe, 스냅샷 저장 검증.
실제 DB 없이 AsyncMock / in-memory SQLite로 실행.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.strategy.snapshot_collector import SnapshotCollector, _T2_MIN_INTERVAL_SEC


# ══════════════════════════════════════════════════════════════
# Fixtures / helpers
# ══════════════════════════════════════════════════════════════

def _make_candle(close: float = 159.5, high: float = 160.0, low: float = 159.0) -> Any:
    """Duck-type 캔들 Fake."""
    c = MagicMock()
    c.close = close
    c.high = high
    c.low = low
    c.open_time = datetime(2026, 4, 4, 8, 0, 0, tzinfo=timezone.utc)
    c.timeframe = "4h"
    c.is_complete = True
    return c


def _make_strategy(
    sid: int = 1,
    pair: str = "usd_jpy",
    style: str = "box_mean_reversion",
    status: str = "proposed",
) -> Any:
    s = MagicMock()
    s.id = sid
    s.status = status
    s.parameters = {
        "pair": pair,
        "trading_style": style,
        "basis_timeframe": "4h",
        "near_bound_pct": 0.3,
        "trading_fee_rate": 0.001,
        "entry_rsi_min": 40.0,
        "entry_rsi_max": 65.0,
        "trailing_stop_atr_initial": 2.0,
    }
    return s


def _make_collector(
    strategies: list | None = None,
    candles: list | None = None,
    active_box: Any = None,
    paper_stats: tuple = (0, 0.0),
) -> SnapshotCollector:
    """SnapshotCollector with mocked internals."""
    session_factory = MagicMock()
    adapter = MagicMock()

    collector = SnapshotCollector(
        session_factory=session_factory,
        adapter=adapter,
        strategy_model=MagicMock(),
        candle_model=MagicMock(),
        box_model=MagicMock(),
        snapshot_model=MagicMock(),
        pair_column="pair",
    )

    # 내부 메서드 패치
    strategies = strategies or []
    candles = candles or [_make_candle() for _ in range(30)]

    collector._fetch_active_proposed = AsyncMock(return_value=strategies)  # type: ignore
    collector._fetch_candles = AsyncMock(return_value=candles)             # type: ignore
    collector._fetch_active_box = AsyncMock(return_value=active_box)       # type: ignore
    collector._get_paper_stats = AsyncMock(return_value=paper_stats)        # type: ignore
    collector._has_open_paper_position = AsyncMock(return_value=False)     # type: ignore
    collector._save_snapshot = AsyncMock()                                  # type: ignore

    return collector


# ══════════════════════════════════════════════════════════════
# V-21: box 포지션 청산 후 T1 발동 → collect_all_snapshots 호출
# ══════════════════════════════════════════════════════════════

# V-27: trend T1 훅 (_close_position wrapper)
# ══════════════════════════════════════════════════════════════

class TestTrendT1Hook:
    """V-27: base_trend.py _close_position에 T1 훅 삽입 확인."""

    @pytest.mark.asyncio
    async def test_v27_trend_close_fires_t1(self):
        """BaseTrendManager._close_position real 청산 후 T1 발동."""
        from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager

        collected: list = []

        class FakeCollector:
            async def collect_all_snapshots(self, trigger_type: str, trigger_pair: str = "") -> list:
                collected.append(trigger_type)
                return []

        adapter = MagicMock()
        session_factory = MagicMock()
        supervisor = MagicMock()
        candle_model = MagicMock()
        position_model = MagicMock()

        mgr = GmoCoinTrendManager(
            adapter=adapter,
            supervisor=supervisor,
            session_factory=session_factory,
            candle_model=candle_model,
            cfd_position_model=position_model,
            snapshot_collector=FakeCollector(),
        )

        # paper pair 아님
        pair = "btc_jpy"
        mgr._paper_executors.pop(pair, None)
        mgr._paper_positions.pop(pair, None)
        mgr._close_position_impl = AsyncMock()

        await mgr._close_position(pair, "exit_warning")

        await asyncio.sleep(0)
        assert "T1_position_close" in collected

    @pytest.mark.asyncio
    async def test_paper_trend_close_does_not_fire_t1(self):
        """paper pair 청산 시 T1 미발동 (paper return 분기)."""
        from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager
        from core.execution.executor import PaperExecutor

        collected: list = []

        class FakeCollector:
            async def collect_all_snapshots(self, trigger_type: str, trigger_pair: str = "") -> list:
                collected.append(trigger_type)
                return []

        session_factory = MagicMock()
        mgr = GmoCoinTrendManager(
            adapter=MagicMock(),
            supervisor=MagicMock(),
            session_factory=session_factory,
            candle_model=MagicMock(),
            cfd_position_model=MagicMock(),
            snapshot_collector=FakeCollector(),
        )

        pair = "btc_jpy"
        # paper pair 등록
        mgr._paper_executors[pair] = MagicMock()
        mgr._paper_positions[pair] = {
            "paper_trade_id": 10,
            "entry_price": 5_000_000.0,
            "direction": "long",
        }
        mgr._paper_executors[pair].record_paper_exit = AsyncMock()
        mgr._position[pair] = MagicMock()
        mgr._latest_price[pair] = 5_050_000.0

        await mgr._close_position(pair, "exit_warning")

        await asyncio.sleep(0)
        assert "T1_position_close" not in collected


# ══════════════════════════════════════════════════════════════
# V-29: 기존 동작 회귀 — SnapshotCollector 없을 때 정상 동작
# ══════════════════════════════════════════════════════════════

class TestNoCollectorRegression:
    """V-29: snapshot_collector=None이면 훅 발동 안 됨 (안전)."""

    @pytest.mark.asyncio
    async def test_v29_no_collector_margin_manager_safe(self):
        """snapshot_collector=None → T1 훅 없이 정상 청산."""
        from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager

        mgr = GmoCoinTrendManager(
            adapter=MagicMock(),
            supervisor=MagicMock(),
            session_factory=MagicMock(),
            candle_model=MagicMock(),
            cfd_position_model=MagicMock(),
            snapshot_collector=None,  # 없음
        )
        mgr._close_position_impl = AsyncMock()
        pair = "btc_jpy"
        mgr._paper_executors.pop(pair, None)
        mgr._paper_positions.pop(pair, None)

        # 예외 없이 정상 실행
        await mgr._close_position(pair, "stop_loss")
        mgr._close_position_impl.assert_awaited_once_with(pair, "stop_loss")

    @pytest.mark.asyncio
    async def test_v29_no_collector_margin_manager_safe2(self):
        """snapshot_collector=None → T1 훅 없이 정상 청산 (MarginTrendManager)."""
        from core.strategy.plugins.cfd_trend_following.manager import MarginTrendManager

        mgr = MarginTrendManager(
            adapter=MagicMock(),
            supervisor=MagicMock(),
            session_factory=MagicMock(),
            candle_model=MagicMock(),
            cfd_position_model=MagicMock(),
            snapshot_collector=None,
        )
        mgr._close_position_impl = AsyncMock()
        pair = "btc_jpy"
        mgr._paper_executors.pop(pair, None)
        mgr._paper_positions.pop(pair, None)

        await mgr._close_position(pair, "stop_loss")
        mgr._close_position_impl.assert_awaited_once_with(pair, "stop_loss")
