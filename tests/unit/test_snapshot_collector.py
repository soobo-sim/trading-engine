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

class TestT1Trigger:
    """V-21, V-28: T1 트리거 동작."""

    @pytest.mark.asyncio
    async def test_v21_t1_calls_collect_on_box_close(self):
        """BoxMeanReversionManager _close_position_market real 청산 후 T1 발동."""
        from core.strategy.plugins.box_mean_reversion.manager import BoxMeanReversionManager
        from core.task.supervisor import TaskSupervisor
        from core.exchange.types import Order, OrderSide, OrderStatus, OrderType

        collected: list = []

        class FakeCollector:
            async def collect_all_snapshots(self, trigger_type: str, trigger_pair: str = "") -> list:
                collected.append((trigger_type, trigger_pair))
                return []

        adapter = MagicMock()
        adapter.is_margin_trading = False

        # get_balance 반환
        balance = MagicMock()
        balance.get_available = MagicMock(return_value=0.01)
        adapter.get_balance = AsyncMock(return_value=balance)

        # place_order 반환
        order = Order(
            order_id="ord-001", pair="gbp_jpy",
            order_type=OrderType.MARKET_SELL, side=OrderSide.SELL,
            price=210.5, amount=0.01, status=OrderStatus.COMPLETED,
        )
        adapter.place_order = AsyncMock(return_value=order)
        adapter.get_ticker = AsyncMock(return_value=MagicMock(last=210.5))

        supervisor = MagicMock()
        session_factory = MagicMock()
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        session_factory.return_value = mock_session

        box_model = MagicMock()
        box_model.__tablename__ = "gmo_boxes"
        box_pos_model = MagicMock()
        box_pos_model.__tablename__ = "gmo_box_positions"
        candle_model = MagicMock()

        mgr = BoxMeanReversionManager(
            adapter=adapter,
            supervisor=supervisor,
            session_factory=session_factory,
            candle_model=candle_model,
            box_model=box_model,
            box_position_model=box_pos_model,
            snapshot_collector=FakeCollector(),
        )
        mgr._params["gbp_jpy"] = {"min_coin_size": 0.001, "trading_fee_rate": 0.001}

        # _record_close_position stub
        mgr._record_close_position = AsyncMock()

        # 포지션 obj (real, not paper)
        pos = MagicMock()
        pos.side = "buy"
        pos.entry_price = 210.0
        mgr._cached_position["gbp_jpy"] = None  # not paper — paper_trade_id 없음

        await mgr._close_position_market("gbp_jpy", pos, "near_upper_exit")

        # asyncio.create_task 가 즉시 실행되도록 양보
        await asyncio.sleep(0)
        assert any(t == "T1_position_close" for t, _ in collected)

    @pytest.mark.asyncio
    async def test_v28_paper_close_does_not_fire_t1(self):
        """paper 포지션 청산 시 T1 미발동."""
        from core.strategy.plugins.box_mean_reversion.manager import BoxMeanReversionManager

        collected: list = []

        class FakeCollector:
            async def collect_all_snapshots(self, trigger_type: str, trigger_pair: str = "") -> list:
                collected.append(trigger_type)
                return []

        adapter = MagicMock()
        adapter.is_margin_trading = False
        adapter.get_ticker = AsyncMock(return_value=MagicMock(last=159.5))

        session_factory = MagicMock()

        mgr = BoxMeanReversionManager(
            adapter=adapter,
            supervisor=MagicMock(),
            session_factory=session_factory,
            candle_model=MagicMock(),
            box_model=MagicMock(),
            box_position_model=MagicMock(),
            snapshot_collector=FakeCollector(),
        )

        # paper cached_position 세팅
        mgr._cached_position["usd_jpy"] = {
            "paper_trade_id": 99,
            "entry_price": 159.0,
            "invest_jpy": 100_000.0,
            "direction": "long",
        }
        exec_mock = AsyncMock()
        mgr._paper_executors["usd_jpy"] = MagicMock()
        mgr._get_executor = MagicMock(return_value=exec_mock)
        exec_mock.record_paper_exit = AsyncMock()

        pos = MagicMock()
        await mgr._close_position_market("usd_jpy", pos, "near_upper_exit")

        await asyncio.sleep(0)
        assert "T1_position_close" not in collected


# ══════════════════════════════════════════════════════════════
# V-22/V-23: T2 트리거 — 무포지션 시만 발동
# ══════════════════════════════════════════════════════════════

class TestT2Trigger:
    """V-22, V-23: T2 트리거 조건."""

    @pytest.mark.asyncio
    async def test_v22_t2_fires_when_no_position(self):
        """새 캔들 감지 + 무포지션 → T2 발동."""
        from core.strategy.plugins.box_mean_reversion.manager import BoxMeanReversionManager
        import asyncio as aio

        collected: list = []

        class FakeCollector:
            async def collect_all_snapshots(self, trigger_type: str, trigger_pair: str = "") -> list:
                collected.append((trigger_type, trigger_pair))
                return []

        adapter = MagicMock()
        adapter.is_margin_trading = False

        session_factory = MagicMock()
        supervisor = MagicMock()
        supervisor.is_running = MagicMock(return_value=False)

        mgr = BoxMeanReversionManager(
            adapter=adapter,
            supervisor=supervisor,
            session_factory=session_factory,
            candle_model=MagicMock(),
            box_model=MagicMock(),
            box_position_model=MagicMock(),
            snapshot_collector=FakeCollector(),
        )

        # 무포지션 상태
        mgr._get_open_position = AsyncMock(return_value=None)
        mgr._validate_active_box = AsyncMock(return_value=None)
        mgr._detect_and_create_box = AsyncMock(return_value=None)

        # 새 캔들 감지 시뮬레이션 (last_seen 없음 → 새 캔들)
        pair = "gbp_jpy"
        mgr._params[pair] = {"basis_timeframe": "4h"}
        mgr._last_seen_open_time[pair] = None
        mgr._last_invalidation_time[pair] = None
        new_open_time = "2026-04-04T08:00:00+00:00"
        mgr._get_latest_candle_open_time = AsyncMock(return_value=new_open_time)

        # 한 사이클 직접 실행
        await mgr._run_one_box_monitor_cycle(pair)

        await aio.sleep(0)
        assert any(t == "T2_candle_close" for t, _ in collected)

    @pytest.mark.asyncio
    async def test_v23_t2_does_not_fire_with_position(self):
        """새 캔들 감지 + 포지션 보유 중 → T2 미발동."""
        from core.strategy.plugins.box_mean_reversion.manager import BoxMeanReversionManager

        collected: list = []

        class FakeCollector:
            async def collect_all_snapshots(self, trigger_type: str, trigger_pair: str = "") -> list:
                collected.append(trigger_type)
                return []

        adapter = MagicMock()
        adapter.is_margin_trading = False

        mgr = BoxMeanReversionManager(
            adapter=adapter,
            supervisor=MagicMock(),
            session_factory=MagicMock(),
            candle_model=MagicMock(),
            box_model=MagicMock(),
            box_position_model=MagicMock(),
            snapshot_collector=FakeCollector(),
        )

        # 포지션 보유 중
        pos = MagicMock()
        mgr._get_open_position = AsyncMock(return_value=pos)
        mgr._validate_active_box = AsyncMock(return_value=None)
        mgr._detect_and_create_box = AsyncMock(return_value=None)

        pair = "gbp_jpy"
        mgr._params[pair] = {"basis_timeframe": "4h"}
        mgr._last_seen_open_time[pair] = None
        mgr._last_invalidation_time[pair] = None
        new_open_time = "2026-04-04T08:00:00+00:00"
        mgr._get_latest_candle_open_time = AsyncMock(return_value=new_open_time)

        await mgr._run_one_box_monitor_cycle(pair)

        await asyncio.sleep(0)
        assert "T2_candle_close" not in collected


# ══════════════════════════════════════════════════════════════
# V-24: 전략 3개 → 스냅샷 3행
# ══════════════════════════════════════════════════════════════

class TestCollectAllSnapshots:
    """V-24~V-26: collect_all_snapshots 핵심 로직."""

    @pytest.mark.asyncio
    async def test_v24_three_strategies_three_snapshots(self):
        """전략 3개 → 각각 Score 계산 + 저장 3회."""
        box1 = MagicMock()
        box1.id = 10
        box1.upper_bound = 160.0
        box1.lower_bound = 159.0

        strategies = [
            _make_strategy(sid=1, pair="usd_jpy", style="box_mean_reversion"),
            _make_strategy(sid=2, pair="gbp_jpy", style="trend_following"),
            _make_strategy(sid=3, pair="gbp_jpy", style="box_mean_reversion"),
        ]
        collector = _make_collector(strategies=strategies, active_box=box1)

        with patch(
            "core.strategy.snapshot_collector.compute_trend_signal",
            return_value={
                "signal": "entry_ok",
                "current_price": 159.5,
                "ema": 159.0,
                "ema_slope_pct": 0.02,
                "atr": 0.3,
                "rsi": 50.0,
                "regime": "ranging",
                "bb_width_pct": 1.2,
                "exit_signal": {},
            },
        ):
            results = await collector.collect_all_snapshots("T1_position_close", "gbp_jpy")

        assert len(results) == 3
        assert collector._save_snapshot.call_count == 3

    @pytest.mark.asyncio
    async def test_v25_one_strategy_fails_others_succeed(self):
        """특정 전략 Score 계산 실패 → 나머지 정상 저장."""
        strategies = [
            _make_strategy(sid=1, style="box_mean_reversion"),
            _make_strategy(sid=2, style="trend_following"),
        ]
        collector = _make_collector(strategies=strategies)

        call_count = 0

        async def flaky_fetch(pair, tf):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("DB 오류 시뮬레이션")
            return [_make_candle() for _ in range(30)]

        collector._fetch_candles = flaky_fetch  # type: ignore

        with patch(
            "core.strategy.snapshot_collector.compute_trend_signal",
            return_value={
                "signal": "entry_ok",
                "current_price": 159.5,
                "ema": 159.0,
                "ema_slope_pct": 0.02,
                "atr": 0.3,
                "rsi": 50.0,
                "regime": "ranging",
                "bb_width_pct": 1.2,
                "exit_signal": {},
            },
        ):
            results = await collector.collect_all_snapshots("T1_position_close")

        # 1개 실패, 1개 성공
        assert len(results) == 1
        assert collector._save_snapshot.call_count == 1

    @pytest.mark.asyncio
    async def test_v26_t2_dedup_within_interval(self):
        """T2를 58분 이내에 재호출 → 두 번째는 빈 리스트 반환."""
        collector = _make_collector(strategies=[_make_strategy()])

        with patch(
            "core.strategy.snapshot_collector.compute_trend_signal",
            return_value={
                "signal": "wait_dip",
                "current_price": 159.5,
                "ema": 159.0,
                "ema_slope_pct": 0.02,
                "atr": 0.3,
                "rsi": 62.0,
                "regime": "unclear",
                "bb_width_pct": 2.0,
                "exit_signal": {},
            },
        ):
            r1 = await collector.collect_all_snapshots("T2_candle_close")
            r2 = await collector.collect_all_snapshots("T2_candle_close")  # 즉시 재호출

        assert len(r1) == 1
        assert r2 == []  # 중복 방지


# ══════════════════════════════════════════════════════════════
# V-27: trend T1 훅 (_close_position wrapper)
# ══════════════════════════════════════════════════════════════

class TestTrendT1Hook:
    """V-27: base_trend.py _close_position에 T1 훅 삽입 확인."""

    @pytest.mark.asyncio
    async def test_v27_trend_close_fires_t1(self):
        """BaseTrendManager._close_position real 청산 후 T1 발동."""
        from core.strategy.plugins.trend_following.manager import TrendFollowingManager

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

        mgr = TrendFollowingManager(
            adapter=adapter,
            supervisor=supervisor,
            session_factory=session_factory,
            candle_model=candle_model,
            trend_position_model=position_model,
            snapshot_collector=FakeCollector(),
        )

        # paper pair 아님
        pair = "gbp_jpy"
        mgr._paper_executors.pop(pair, None)
        mgr._paper_positions.pop(pair, None)
        mgr._close_position_impl = AsyncMock()

        await mgr._close_position(pair, "exit_warning")

        await asyncio.sleep(0)
        assert "T1_position_close" in collected

    @pytest.mark.asyncio
    async def test_paper_trend_close_does_not_fire_t1(self):
        """paper pair 청산 시 T1 미발동 (paper return 분기)."""
        from core.strategy.plugins.trend_following.manager import TrendFollowingManager
        from core.execution.executor import PaperExecutor

        collected: list = []

        class FakeCollector:
            async def collect_all_snapshots(self, trigger_type: str, trigger_pair: str = "") -> list:
                collected.append(trigger_type)
                return []

        session_factory = MagicMock()
        mgr = TrendFollowingManager(
            adapter=MagicMock(),
            supervisor=MagicMock(),
            session_factory=session_factory,
            candle_model=MagicMock(),
            trend_position_model=MagicMock(),
            snapshot_collector=FakeCollector(),
        )

        pair = "usd_jpy"
        # paper pair 등록
        mgr._paper_executors[pair] = MagicMock()
        mgr._paper_positions[pair] = {
            "paper_trade_id": 10,
            "entry_price": 150.0,
            "direction": "long",
        }
        mgr._paper_executors[pair].record_paper_exit = AsyncMock()
        mgr._position[pair] = MagicMock()
        mgr._latest_price[pair] = 151.0

        await mgr._close_position(pair, "exit_warning")

        await asyncio.sleep(0)
        assert "T1_position_close" not in collected


# ══════════════════════════════════════════════════════════════
# V-29: 기존 동작 회귀 — SnapshotCollector 없을 때 정상 동작
# ══════════════════════════════════════════════════════════════

class TestNoCollectorRegression:
    """V-29: snapshot_collector=None이면 훅 발동 안 됨 (안전)."""

    @pytest.mark.asyncio
    async def test_v29_no_collector_box_manager_safe(self):
        """snapshot_collector=None → T1 훅 없이 정상 청산."""
        from core.strategy.plugins.box_mean_reversion.manager import BoxMeanReversionManager
        from core.exchange.types import Order, OrderSide, OrderStatus, OrderType

        adapter = MagicMock()
        adapter.is_margin_trading = False
        balance = MagicMock()
        balance.get_available = MagicMock(return_value=0.01)
        adapter.get_balance = AsyncMock(return_value=balance)
        order = Order(
            order_id="ord-001", pair="gbp_jpy",
            order_type=OrderType.MARKET_SELL, side=OrderSide.SELL,
            price=210.5, amount=0.01, status=OrderStatus.COMPLETED,
        )
        adapter.place_order = AsyncMock(return_value=order)
        adapter.get_ticker = AsyncMock(return_value=MagicMock(last=210.5))

        session_factory = MagicMock()
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        session_factory.return_value = mock_session

        mgr = BoxMeanReversionManager(
            adapter=adapter,
            supervisor=MagicMock(),
            session_factory=session_factory,
            candle_model=MagicMock(),
            box_model=MagicMock(),
            box_position_model=MagicMock(),
            snapshot_collector=None,  # 없음
        )
        mgr._params["gbp_jpy"] = {"min_coin_size": 0.001, "trading_fee_rate": 0.001}
        mgr._record_close_position = AsyncMock()
        mgr._cached_position["gbp_jpy"] = None

        pos = MagicMock()
        pos.side = "buy"
        pos.entry_price = 210.0

        # 예외 없이 정상 실행
        await mgr._close_position_market("gbp_jpy", pos, "near_upper_exit")

    @pytest.mark.asyncio
    async def test_v29_no_collector_trend_manager_safe(self):
        """snapshot_collector=None → T1 훅 없이 정상 청산."""
        from core.strategy.plugins.trend_following.manager import TrendFollowingManager

        mgr = TrendFollowingManager(
            adapter=MagicMock(),
            supervisor=MagicMock(),
            session_factory=MagicMock(),
            candle_model=MagicMock(),
            trend_position_model=MagicMock(),
            snapshot_collector=None,
        )
        mgr._close_position_impl = AsyncMock()
        pair = "btc_jpy"
        mgr._paper_executors.pop(pair, None)
        mgr._paper_positions.pop(pair, None)

        await mgr._close_position(pair, "stop_loss")
        mgr._close_position_impl.assert_awaited_once_with(pair, "stop_loss")
