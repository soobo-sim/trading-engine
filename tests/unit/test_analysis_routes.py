"""
Analysis 라우트 테스트 — trade-stats + box-history에서 box + trend 포지션 통합 조회.

BUG-007: trade-stats와 box-history가 trend_positions 무시하던 버그의 수정 검증.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import (
    StrategyTechnique,
    create_balance_entry_model,
    create_box_model,
    create_box_position_model,
    create_candle_model,
    create_insight_model,
    create_strategy_model,
    create_summary_model,
    create_trade_model,
    create_trend_position_model,
)
from adapters.database.session import Base
from api.dependencies import AppState, ModelRegistry
from api.routes import analysis, system
from core.punisher.monitoring.health import HealthChecker
from core.strategy.gmo_coin_trend import GmoCoinTrendManager
from core.punisher.task.supervisor import TaskSupervisor
from tests.fake_exchange import FakeExchangeAdapter

# ── ORM 모델 (anl_ prefix) ───────────────────────

AnlStrategy = create_strategy_model("anl")
AnlTrade = create_trade_model("anl", order_id_length=40)
AnlBalanceEntry = create_balance_entry_model("anl")
AnlInsight = create_insight_model("anl")
AnlSummary = create_summary_model("anl")
AnlCandle = create_candle_model("anl", pair_column="pair")
AnlBox = create_box_model("anl", pair_column="pair")
AnlBoxPosition = create_box_position_model("anl", pair_column="pair", order_id_length=40)
AnlTrendPosition = create_trend_position_model("anl", order_id_length=40)


def _create_models() -> ModelRegistry:
    return ModelRegistry(
        strategy=AnlStrategy,
        trade=AnlTrade,
        balance_entry=AnlBalanceEntry,
        insight=AnlInsight,
        summary=AnlSummary,
        candle=AnlCandle,
        box=AnlBox,
        box_position=AnlBoxPosition,
        trend_position=AnlTrendPosition,
        technique=StrategyTechnique,
    )


@pytest_asyncio.fixture
async def setup():
    """DB + AppState + AsyncClient 통째로 제공."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    target_tables = [
        t for t in Base.metadata.tables.values()
        if t.name.startswith("anl_") or t.name == "strategy_techniques"
    ]
    async with engine.begin() as conn:
        for table in target_tables:
            await conn.run_sync(table.create)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    adapter = FakeExchangeAdapter(
        initial_balances={"jpy": 1_000_000.0, "xrp": 0.0},
        ticker_price=100.0,
    )
    await adapter.connect()

    supervisor = TaskSupervisor()
    models = _create_models()

    trend_mgr = GmoCoinTrendManager(
        adapter=adapter, supervisor=supervisor, session_factory=factory,
        candle_model=AnlCandle, cfd_position_model=AnlTrendPosition, pair_column="pair",
    )
    
    # DataHub 추가 (macro-brief API 테스트용)
    from core.shared.data.hub import DataHub
    trend_mgr._data_hub = DataHub(
        session_factory=factory,
        adapter=adapter,
        candle_model=AnlCandle,
        pair_column="pair",
    )
    
    health = HealthChecker(
        adapter=adapter, supervisor=supervisor, session_factory=factory,
        strategy_model=AnlStrategy, trend_position_model=AnlTrendPosition,
        box_position_model=AnlBoxPosition, pair_column="pair",
    )
    state = AppState(
        adapter=adapter, supervisor=supervisor, session_factory=factory,
        trend_manager=trend_mgr, health_checker=health,
        models=models, prefix="anl", pair_column="pair",
    )

    from fastapi import FastAPI
    app = FastAPI()
    app.state.app_state = state
    app.include_router(analysis.router)

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")

    yield client, factory

    await client.aclose()
    await supervisor.stop_all()
    await adapter.close()
    await engine.dispose()


async def _seed_positions(factory: async_sessionmaker):
    """박스 1개 + 박스포지션 2개(1승1패) + 추세포지션 2개(2승) 시드."""
    now = datetime.now(timezone.utc)
    async with factory() as db:
        # 박스
        box = AnlBox(
            pair="xrp_jpy",
            upper_bound=Decimal("110"),
            lower_bound=Decimal("90"),
            upper_touch_count=3,
            lower_touch_count=3,
            tolerance_pct=Decimal("2.0"),
            status="active",
            created_at=now - timedelta(days=10),
        )
        db.add(box)
        await db.flush()

        # 박스 포지션 — 승
        bp_win = AnlBoxPosition(
            pair="xrp_jpy",
            box_id=box.id,
            side="buy",
            entry_order_id="box-entry-1",
            entry_price=Decimal("91"),
            entry_amount=Decimal("100"),
            entry_jpy=Decimal("9100"),
            exit_order_id="box-exit-1",
            exit_price=Decimal("109"),
            exit_amount=Decimal("100"),
            exit_jpy=Decimal("10900"),
            exit_reason="near_upper_exit",
            realized_pnl_jpy=Decimal("1800"),
            realized_pnl_pct=Decimal("19.78"),
            status="closed",
            created_at=now - timedelta(days=8),
            closed_at=now - timedelta(days=7),
        )
        # 박스 포지션 — 패
        bp_loss = AnlBoxPosition(
            pair="xrp_jpy",
            box_id=box.id,
            side="buy",
            entry_order_id="box-entry-2",
            entry_price=Decimal("91"),
            entry_amount=Decimal("100"),
            entry_jpy=Decimal("9100"),
            exit_order_id="box-exit-2",
            exit_price=Decimal("88"),
            exit_amount=Decimal("100"),
            exit_jpy=Decimal("8800"),
            exit_reason="stop_loss",
            realized_pnl_jpy=Decimal("-300"),
            realized_pnl_pct=Decimal("-3.30"),
            status="closed",
            created_at=now - timedelta(days=6),
            closed_at=now - timedelta(days=5),
        )
        db.add_all([bp_win, bp_loss])

        # 추세 포지션 — 승1
        tp1 = AnlTrendPosition(
            pair="xrp_jpy",
            side="buy",
            strategy_id=None,
            entry_order_id="trend-entry-1",
            entry_price=Decimal("95"),
            entry_size=Decimal("200"),
            entry_collateral_jpy=Decimal("19000"),
            stop_loss_price=Decimal("90"),
            exit_order_id="trend-exit-1",
            exit_price=Decimal("120"),
            exit_size=Decimal("200"),
            exit_reason="trailing_stop",
            realized_pnl_jpy=Decimal("5000"),
            realized_pnl_pct=Decimal("26.32"),
            status="closed",
            created_at=now - timedelta(days=4),
            closed_at=now - timedelta(days=3),
        )
        # 추세 포지션 — 승2
        tp2 = AnlTrendPosition(
            pair="xrp_jpy",
            side="buy",
            strategy_id=None,
            entry_order_id="trend-entry-2",
            entry_price=Decimal("100"),
            entry_size=Decimal("150"),
            entry_collateral_jpy=Decimal("15000"),
            stop_loss_price=Decimal("95"),
            exit_order_id="trend-exit-2",
            exit_price=Decimal("115"),
            exit_size=Decimal("150"),
            exit_reason="ema_breakdown",
            realized_pnl_jpy=Decimal("2250"),
            realized_pnl_pct=Decimal("15.00"),
            status="closed",
            created_at=now - timedelta(days=2),
            closed_at=now - timedelta(days=1),
        )
        db.add_all([tp1, tp2])
        await db.commit()


class TestTradeStats:
    """trade-stats 엔드포인트 기본 검증. 현재 API는 days 파라미터, flat dict 응답."""

    @pytest.mark.asyncio
    async def test_empty_returns_zero(self, setup):
        client, _ = setup
        resp = await client.get("/api/analysis/trade-stats?pair=xrp_jpy&days=365")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["total_trades"] == 0
        assert data["wins"] == 0
        assert data["losses"] == 0
        assert data["by_strategy"] == {}

    @pytest.mark.asyncio
    async def test_combined_stats(self, setup):
        """포지션 시드 후 trade-stats에서 trend_positions + box_positions 포함 검증 (BUG-027/028)."""
        client, factory = setup
        await _seed_positions(factory)

        # trade-stats는 Trade 테이블 + trend_positions + box_positions 모두 집계 (BUG-028 수정)
        resp = await client.get("/api/analysis/trade-stats?pair=xrp_jpy&days=365")
        assert resp.status_code == 200
        data = resp.json()
        # trend 2건(승2) + box 2건(승1,패1) = 4건
        assert data["total_trades"] == 4
        assert data["wins"] == 3
        assert data["losses"] == 1

        # 포지션 통합 검증은 box-history 엔드포인트
        resp = await client.get("/api/analysis/box-history?pair=xrp_jpy&days=30")
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert summary["closed_positions"] == 4  # box 2 + trend 2
        assert summary["wins"] == 3
        assert summary["losses"] == 1

    @pytest.mark.asyncio
    async def test_total_pnl_includes_trend(self, setup):
        """box-history summary에서 total_pnl_jpy가 박스+추세 합산."""
        client, factory = setup
        await _seed_positions(factory)

        resp = await client.get("/api/analysis/box-history?pair=xrp_jpy&days=30")
        summary = resp.json()["summary"]
        # box: 1800 + (-300) = 1500, trend: 5000 + 2250 = 7250 → total = 8750
        assert summary["total_pnl_jpy"] == 8750.0

    @pytest.mark.asyncio
    async def test_exit_reason_distribution(self, setup):
        """box-history에서 trend 청산 사유 포함."""
        client, factory = setup
        await _seed_positions(factory)

        resp = await client.get("/api/analysis/box-history?pair=xrp_jpy&days=30")
        reasons = resp.json()["summary"]["exit_reason_distribution"]
        assert "trend:trailing_stop" in reasons
        assert "trend:ema_breakdown" in reasons

    # ── BUG-028 검증 케이스 ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_trade_stats_includes_box_pnl(self, setup):
        """trade-stats total_pnl_jpy에 box_positions pnl 합산 (BUG-028)."""
        client, factory = setup
        await _seed_positions(factory)

        resp = await client.get("/api/analysis/trade-stats?pair=xrp_jpy&days=365")
        assert resp.status_code == 200
        data = resp.json()
        # box: 1800 + (-300) = 1500, trend: 5000 + 2250 = 7250 → total = 8750
        assert data["total_pnl_jpy"] == 8750.0
        # by_strategy에 box_mean_reversion 포함
        assert "box_mean_reversion" in data["by_strategy"]
        box_s = data["by_strategy"]["box_mean_reversion"]
        assert box_s["wins"] == 1
        assert box_s["losses"] == 1
        assert box_s["pnl_jpy"] == 1500.0
        assert box_s["trades"] == 2

    @pytest.mark.asyncio
    async def test_trade_stats_box_only(self, setup):
        """trade-stats: trend 없이 box_positions만 있어도 집계 (BUG-028)."""
        client, factory = setup
        now = datetime.now(timezone.utc)
        async with factory() as db:
            box = AnlBox(
                pair="xrp_jpy",
                upper_bound=Decimal("110"),
                lower_bound=Decimal("90"),
                upper_touch_count=3,
                lower_touch_count=3,
                tolerance_pct=Decimal("2.0"),
                status="active",
                created_at=now - timedelta(days=5),
            )
            db.add(box)
            await db.flush()
            bp = AnlBoxPosition(
                pair="xrp_jpy",
                box_id=box.id,
                side="buy",
                entry_order_id="only-entry",
                entry_price=Decimal("91"),
                entry_amount=Decimal("100"),
                exit_order_id="only-exit",
                exit_price=Decimal("109"),
                exit_amount=Decimal("100"),
                exit_jpy=Decimal("10900"),
                exit_reason="near_upper_exit",
                realized_pnl_jpy=Decimal("500"),
                realized_pnl_pct=Decimal("5.0"),
                status="closed",
                created_at=now - timedelta(days=4),
                closed_at=now - timedelta(days=3),
            )
            db.add(bp)
            await db.commit()

        resp = await client.get("/api/analysis/trade-stats?pair=xrp_jpy&days=365")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_trades"] == 1
        assert data["wins"] == 1
        assert data["losses"] == 0
        assert data["win_rate"] == 100.0
        assert data["total_pnl_jpy"] == 500.0

    @pytest.mark.asyncio
    async def test_trade_stats_box_null_pnl(self, setup):
        """trade-stats: box_position realized_pnl_jpy=NULL 시 카운트 포함 승/패 제외 (BUG-028)."""
        client, factory = setup
        now = datetime.now(timezone.utc)
        async with factory() as db:
            box = AnlBox(
                pair="xrp_jpy",
                upper_bound=Decimal("110"),
                lower_bound=Decimal("90"),
                upper_touch_count=3,
                lower_touch_count=3,
                tolerance_pct=Decimal("2.0"),
                status="active",
                created_at=now - timedelta(days=5),
            )
            db.add(box)
            await db.flush()
            bp = AnlBoxPosition(
                pair="xrp_jpy",
                box_id=box.id,
                side="buy",
                entry_order_id="null-pnl-entry",
                entry_price=Decimal("91"),
                entry_amount=Decimal("100"),
                realized_pnl_jpy=None,
                status="closed",
                created_at=now - timedelta(days=4),
                closed_at=now - timedelta(days=3),
            )
            db.add(bp)
            await db.commit()

        resp = await client.get("/api/analysis/trade-stats?pair=xrp_jpy&days=365")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_trades"] == 1   # 카운트는 포함
        assert data["wins"] == 0
        assert data["losses"] == 0
        assert data["win_rate"] is None    # valid_count == 0

    @pytest.mark.asyncio
    async def test_trade_stats_box_period_filter(self, setup):
        """trade-stats: closed_at < since인 box_position은 집계 제외 (기간 필터 검증, BUG-028)."""
        client, factory = setup
        now = datetime.now(timezone.utc)
        async with factory() as db:
            box = AnlBox(
                pair="xrp_jpy",
                upper_bound=Decimal("110"),
                lower_bound=Decimal("90"),
                upper_touch_count=3,
                lower_touch_count=3,
                tolerance_pct=Decimal("2.0"),
                status="active",
                created_at=now - timedelta(days=40),
            )
            db.add(box)
            await db.flush()
            # 30일 이전에 종료된 포지션 → days=7 쿼리에서 제외되어야 함
            bp_old = AnlBoxPosition(
                pair="xrp_jpy",
                box_id=box.id,
                side="buy",
                entry_order_id="old-entry",
                entry_price=Decimal("91"),
                entry_amount=Decimal("100"),
                realized_pnl_jpy=Decimal("999"),
                status="closed",
                created_at=now - timedelta(days=35),
                closed_at=now - timedelta(days=30),  # 30일 전
            )
            # 1일 전에 종료된 포지션 → 포함되어야 함
            bp_recent = AnlBoxPosition(
                pair="xrp_jpy",
                box_id=box.id,
                side="buy",
                entry_order_id="recent-entry",
                entry_price=Decimal("91"),
                entry_amount=Decimal("100"),
                realized_pnl_jpy=Decimal("200"),
                status="closed",
                created_at=now - timedelta(days=2),
                closed_at=now - timedelta(days=1),  # 1일 전
            )
            db.add_all([bp_old, bp_recent])
            await db.commit()

        resp = await client.get("/api/analysis/trade-stats?pair=xrp_jpy&days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_trades"] == 1        # recent만 포함
        assert data["total_pnl_jpy"] == 200.0   # old의 999는 제외

    @pytest.mark.asyncio
    async def test_trade_stats_box_pair_filter(self, setup):
        """trade-stats: 다른 pair의 box_position은 집계 제외 (pair 필터 검증, BUG-028)."""
        client, factory = setup
        now = datetime.now(timezone.utc)
        async with factory() as db:
            box = AnlBox(
                pair="btc_jpy",
                upper_bound=Decimal("5000000"),
                lower_bound=Decimal("4500000"),
                upper_touch_count=2,
                lower_touch_count=2,
                tolerance_pct=Decimal("1.0"),
                status="active",
                created_at=now - timedelta(days=5),
            )
            db.add(box)
            await db.flush()
            # btc_jpy pair 포지션 → xrp_jpy 쿼리에서 제외되어야 함
            bp_other = AnlBoxPosition(
                pair="btc_jpy",
                box_id=box.id,
                side="buy",
                entry_order_id="btc-entry",
                entry_price=Decimal("4600000"),
                entry_amount=Decimal("0.01"),
                realized_pnl_jpy=Decimal("50000"),
                status="closed",
                created_at=now - timedelta(days=4),
                closed_at=now - timedelta(days=3),
            )
            db.add(bp_other)
            await db.commit()

        resp = await client.get("/api/analysis/trade-stats?pair=xrp_jpy&days=365")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_trades"] == 0
        assert data["total_pnl_jpy"] == 0.0


class TestBoxHistory:
    """box-history 엔드포인트: 추세추종 별도 집계 검증."""

    @pytest.mark.asyncio
    async def test_empty(self, setup):
        client, _ = setup
        resp = await client.get("/api/analysis/box-history?pair=xrp_jpy&days=30")
        assert resp.status_code == 200
        data = resp.json()
        assert data["boxes"] == []
        assert data["summary"]["total_positions"] == 0

    @pytest.mark.asyncio
    async def test_trend_positions_section(self, setup):
        """trend_positions 섹션이 박스와 별도로 존재."""
        client, factory = setup
        await _seed_positions(factory)

        resp = await client.get("/api/analysis/box-history?pair=xrp_jpy&days=30")
        data = resp.json()

        assert "trend_positions" in data
        tp = data["trend_positions"]
        assert tp["total"] == 2
        assert tp["valid_trades"] == 2
        assert tp["wins"] == 2
        assert tp["losses"] == 0
        assert tp["unknown"] == 0
        assert tp["total_pnl_jpy"] == 7250.0

    @pytest.mark.asyncio
    async def test_summary_combined(self, setup):
        """summary가 박스+추세 합산."""
        client, factory = setup
        await _seed_positions(factory)

        resp = await client.get("/api/analysis/box-history?pair=xrp_jpy&days=30")
        summary = resp.json()["summary"]

        # 박스포지션 2 + 추세포지션 2 = 4
        assert summary["closed_positions"] == 4
        assert summary["valid_trades"] == 4
        assert summary["wins"] == 3
        assert summary["losses"] == 1
        assert summary["unknown"] == 0
        # box pnl 1500 + trend pnl 7250 = 8750
        assert summary["total_pnl_jpy"] == 8750.0

    @pytest.mark.asyncio
    async def test_trend_exit_reasons_prefixed(self, setup):
        """summary exit_reason_distribution에서 trend 사유는 trend: 접두사."""
        client, factory = setup
        await _seed_positions(factory)

        resp = await client.get("/api/analysis/box-history?pair=xrp_jpy&days=30")
        reasons = resp.json()["summary"]["exit_reason_distribution"]
        assert "trend:trailing_stop" in reasons
        assert "trend:ema_breakdown" in reasons


class TestInvalidPeriod:
    """잘못된 days 파라미터 검증."""

    @pytest.mark.asyncio
    async def test_invalid_days_422(self, setup):
        """days에 음수/문자열 → 422 Validation Error."""
        client, _ = setup
        resp = await client.get("/api/analysis/trade-stats?pair=xrp_jpy&days=-1")
        assert resp.status_code == 422


async def _seed_with_unknown_pnl(factory: async_sessionmaker):
    """PnL null 포지션 포함 시드: 박스 1승 + 추세 1건(PnL null)."""
    now = datetime.now(timezone.utc)
    async with factory() as db:
        box = AnlBox(
            pair="xrp_jpy",
            upper_bound=Decimal("110"),
            lower_bound=Decimal("90"),
            upper_touch_count=3,
            lower_touch_count=3,
            tolerance_pct=Decimal("2.0"),
            status="active",
            created_at=now - timedelta(days=10),
        )
        db.add(box)
        await db.flush()

        bp = AnlBoxPosition(
            pair="xrp_jpy",
            box_id=box.id,
            side="buy",
            entry_order_id="box-e-1",
            entry_price=Decimal("91"),
            entry_amount=Decimal("100"),
            entry_jpy=Decimal("9100"),
            exit_order_id="box-x-1",
            exit_price=Decimal("109"),
            exit_amount=Decimal("100"),
            exit_jpy=Decimal("10900"),
            exit_reason="near_upper_exit",
            realized_pnl_jpy=Decimal("1800"),
            realized_pnl_pct=Decimal("19.78"),
            status="closed",
            created_at=now - timedelta(days=8),
            closed_at=now - timedelta(days=7),
        )
        db.add(bp)

        # PnL null 추세 포지션 (BUG-008 수정 이전 청산건)
        tp_unknown = AnlTrendPosition(
            pair="xrp_jpy",
            side="buy",
            strategy_id=None,
            entry_order_id="trend-null-1",
            entry_price=Decimal("95"),
            entry_size=Decimal("200"),
            entry_collateral_jpy=Decimal("19000"),
            stop_loss_price=Decimal("90"),
            exit_order_id="trend-null-x1",
            exit_price=None,
            exit_size=None,
            exit_reason="trailing_stop",
            realized_pnl_jpy=None,
            realized_pnl_pct=None,
            status="closed",
            created_at=now - timedelta(days=4),
            closed_at=now - timedelta(days=3),
        )
        db.add(tp_unknown)
        await db.commit()


class TestUnknownPnl:
    """PnL null 포지션이 box-history에서 unknown 분류 검증."""

    @pytest.mark.asyncio
    async def test_trade_stats_unknown(self, setup):
        """PnL null → box-history unknown 카운트."""
        client, factory = setup
        await _seed_with_unknown_pnl(factory)

        resp = await client.get("/api/analysis/box-history?pair=xrp_jpy&days=30")
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        # box 1 (PnL 있음) + trend 1 (PnL null) = 2 포지션
        assert summary["closed_positions"] == 2
        assert summary["valid_trades"] == 1
        assert summary["wins"] == 1
        assert summary["unknown"] == 1
        assert summary["win_rate"] == 100.0  # 1/1 * 100

        # trend_positions 별도 집계
        trend = resp.json()["trend_positions"]
        assert trend["total"] == 1
        assert trend["valid_trades"] == 0
        assert trend["unknown"] == 1
        assert trend["win_rate"] is None

    @pytest.mark.asyncio
    async def test_box_history_unknown(self, setup):
        """box-history summary에서 unknown 분리."""
        client, factory = setup
        await _seed_with_unknown_pnl(factory)

        resp = await client.get("/api/analysis/box-history?pair=xrp_jpy&days=30")
        data = resp.json()

        tp = data["trend_positions"]
        assert tp["total"] == 1
        assert tp["valid_trades"] == 0
        assert tp["unknown"] == 1
        assert tp["win_rate"] is None

        summary = data["summary"]
        assert summary["valid_trades"] == 1  # box 1만
        assert summary["unknown"] == 1
        assert summary["wins"] == 1
        assert summary["losses"] == 0
        assert summary["win_rate"] == 100.0


# ──────────────────────────────────────────────────────────────
# Macro Brief API 테스트
# ──────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def setup_macro():
    """macro-brief 전용 — (client, trend_manager) yield."""
    from core.shared.data.hub import DataHub

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    target_tables = [
        t for t in Base.metadata.tables.values()
        if t.name.startswith("anl_") or t.name == "strategy_techniques"
    ]
    async with engine.begin() as conn:
        for table in target_tables:
            await conn.run_sync(table.create)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 1_000_000.0}, ticker_price=100.0)
    await adapter.connect()
    supervisor = TaskSupervisor()
    models = _create_models()

    trend_mgr = GmoCoinTrendManager(
        adapter=adapter, supervisor=supervisor, session_factory=factory,
        candle_model=AnlCandle, cfd_position_model=AnlTrendPosition, pair_column="pair",
    )
    trend_mgr._data_hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=AnlCandle, pair_column="pair",
    )

    health = HealthChecker(
        adapter=adapter, supervisor=supervisor, session_factory=factory,
        strategy_model=AnlStrategy, trend_position_model=AnlTrendPosition,
        box_position_model=AnlBoxPosition, pair_column="pair",
    )
    state = AppState(
        adapter=adapter, supervisor=supervisor, session_factory=factory,
        trend_manager=trend_mgr, health_checker=health,
        models=models, prefix="anl", pair_column="pair",
    )

    from fastapi import FastAPI
    app = FastAPI()
    app.state.app_state = state
    app.include_router(analysis.router)

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")

    yield client, trend_mgr

    await client.aclose()
    await supervisor.stop_all()
    await adapter.close()
    await engine.dispose()


class TestMacroBrief:
    """macro-brief API 테스트 — DataHub 모킹으로 컨텍스트 조합 검증."""

    @pytest.mark.asyncio
    async def test_mb01_all_data_present(self, setup_macro):
        """
        MB-01: 모든 데이터 존재
        Given: FNG, 뉴스, 이벤트, VIX/DXY 모두 있음
        When:  GET /api/analysis/macro-brief
        Then:  200, context_summary에 모든 항목 포함
        """
        from datetime import datetime, timedelta, timezone
        from unittest.mock import AsyncMock
        from core.shared.data.dto import SentimentDTO, NewsDTO, EconomicEventDTO, MacroSnapshotDTO

        client, trend_mgr = setup_macro

        mock_hub = AsyncMock()
        now = datetime.now(timezone.utc)
        
        mock_hub.get_sentiment.return_value = SentimentDTO(
            source="fear_and_greed",
            score=27,
            classification="Fear",
            timestamp=now,
        )
        mock_hub.get_sentiment_history.return_value = (
            SentimentDTO(source="alternative_me_fng", score=27, classification="Fear", timestamp=now),
            SentimentDTO(source="alternative_me_fng", score=35, classification="Fear", timestamp=now),
        )
        mock_hub.get_news_summary.return_value = (
            NewsDTO(title="BTC crashes 10%", source="cryptonews", published_at=now,
                    category="market", sentiment_score=-0.8),
            NewsDTO(title="Market panic spreads", source="reuters", published_at=now,
                    category="market", sentiment_score=-0.6),
        )
        event_time = now + timedelta(hours=3)
        mock_hub.get_upcoming_events.return_value = (
            EconomicEventDTO(name="FOMC", datetime_jst=event_time + timedelta(hours=9),
                             importance="High", currency="USD"),
        )
        mock_hub.get_macro_snapshot.return_value = MacroSnapshotDTO(
            vix=22.5, dxy=104.2, us_10y=4.3, fetched_at=now,
        )

        trend_mgr._data_hub = mock_hub
        resp = await client.get("/api/analysis/macro-brief?pair=btc_jpy")

        assert resp.status_code == 200
        data = resp.json()
        assert data["fng"]["score"] == 27
        assert data["fng"]["label"] == "Fear"
        assert data["fng_history"] is not None
        assert len(data["fng_history"]) == 2
        assert data["fng_history"][0]["score"] == 27
        assert data["news"]["count"] == 2
        assert data["news"]["avg_sentiment"] < 0
        assert len(data["events"]["high_within_6h"]) == 1
        assert data["macro"]["vix"] == 22.5
        assert data["macro"]["dxy"] == 104.2

        summary = data["context_summary"]
        assert "FNG 27(Fear)" in summary
        assert "뉴스 부정적" in summary
        assert "고영향 이벤트 1개(6시간 내)" in summary
        assert "VIX 22.5" in summary
        assert "DXY 104.2" in summary

    @pytest.mark.asyncio
    async def test_mb02_partial_data_none_graceful(self, setup_macro):
        """
        MB-02: 일부 데이터 None — graceful 처리
        Given: FNG만 있고 나머지 None
        When:  GET /api/analysis/macro-brief
        Then:  200, 해당 항목만 context_summary에 포함
        """
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock
        from core.shared.data.dto import SentimentDTO

        client, trend_mgr = setup_macro
        now = datetime.now(timezone.utc)

        mock_hub = AsyncMock()
        mock_hub.get_sentiment.return_value = SentimentDTO(
            source="fear_and_greed", score=50, classification="Neutral", timestamp=now,
        )
        mock_hub.get_sentiment_history.return_value = ()
        mock_hub.get_news_summary.return_value = ()
        mock_hub.get_upcoming_events.return_value = ()
        mock_hub.get_macro_snapshot.return_value = None

        trend_mgr._data_hub = mock_hub
        resp = await client.get("/api/analysis/macro-brief?pair=btc_jpy")

        assert resp.status_code == 200
        data = resp.json()
        assert data["fng"]["score"] == 50
        assert data["news"] is None or data["news"]["count"] == 0
        assert data["events"]["high_within_6h"] == []
        assert data["macro"] is None

        summary = data["context_summary"]
        assert "FNG 50(Neutral)" in summary
        assert "뉴스" not in summary
        assert "이벤트" not in summary
        assert "VIX" not in summary

    @pytest.mark.asyncio
    async def test_mb03_all_none_returns_no_data(self, setup_macro):
        """
        MB-03: 모든 데이터 None
        Given: 모든 소스 None
        When:  GET /api/analysis/macro-brief
        Then:  200, context_summary="데이터 없음"
        """
        from unittest.mock import AsyncMock

        client, trend_mgr = setup_macro

        mock_hub = AsyncMock()
        mock_hub.get_sentiment.return_value = None
        mock_hub.get_sentiment_history.return_value = ()
        mock_hub.get_news_summary.return_value = ()
        mock_hub.get_upcoming_events.return_value = ()
        mock_hub.get_macro_snapshot.return_value = None

        trend_mgr._data_hub = mock_hub
        resp = await client.get("/api/analysis/macro-brief?pair=btc_jpy")

        assert resp.status_code == 200
        data = resp.json()
        assert data["fng"] is None
        assert data["fng_history"] is None
        assert data["news"] is None or data["news"]["count"] == 0
        assert data["macro"] is None

        summary = data["context_summary"]
        assert summary == "데이터 없음"

    @pytest.mark.asyncio
    async def test_mb04_fng_history_trend_text(self, setup_macro):
        """
        MB-04: FNG 이력 추이 텍스트 생성
        Given: FNG 이력 [45, 38, 29] (하락추세)
        When:  GET /api/analysis/macro-brief
        Then:  context_summary에 "↓하락" 포함
               FNG 이력 [20, 25, 29] (회복)라면 "↑회복" 포함
               FNG 이력 [27, 28, 29] (보합)라면 "→보합" 포함
        """
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock
        from core.shared.data.dto import SentimentDTO

        client, trend_mgr = setup_macro
        now = datetime.now(timezone.utc)

        def make_dto(score: int, label: str) -> SentimentDTO:
            return SentimentDTO(
                source="alternative_me_fng", score=score,
                classification=label, timestamp=now,
            )

        # 시나리오 A: 하락 (45→38→29)
        mock_hub = AsyncMock()
        mock_hub.get_sentiment.return_value = make_dto(29, "Fear")
        mock_hub.get_sentiment_history.return_value = (
            make_dto(29, "Fear"), make_dto(38, "Fear"), make_dto(45, "Fear"),
        )
        mock_hub.get_news_summary.return_value = ()
        mock_hub.get_upcoming_events.return_value = ()
        mock_hub.get_macro_snapshot.return_value = None
        trend_mgr._data_hub = mock_hub

        resp = await client.get("/api/analysis/macro-brief?pair=btc_jpy")
        assert resp.status_code == 200
        data = resp.json()
        assert "↓하락" in data["context_summary"]
        assert data["fng_history"][0]["score"] == 29
        assert data["fng_history"][2]["score"] == 45

        # 시나리오 B: 회복 (20→25→29)
        mock_hub2 = AsyncMock()
        mock_hub2.get_sentiment.return_value = make_dto(29, "Fear")
        mock_hub2.get_sentiment_history.return_value = (
            make_dto(29, "Fear"), make_dto(25, "Fear"), make_dto(20, "Extreme Fear"),
        )
        mock_hub2.get_news_summary.return_value = ()
        mock_hub2.get_upcoming_events.return_value = ()
        mock_hub2.get_macro_snapshot.return_value = None
        trend_mgr._data_hub = mock_hub2

        resp2 = await client.get("/api/analysis/macro-brief?pair=btc_jpy")
        assert resp2.status_code == 200
        assert "↑회복" in resp2.json()["context_summary"]

        # 시나리오 C: 보합 (27→28→29)
        mock_hub3 = AsyncMock()
        mock_hub3.get_sentiment.return_value = make_dto(29, "Fear")
        mock_hub3.get_sentiment_history.return_value = (
            make_dto(29, "Fear"), make_dto(28, "Fear"), make_dto(27, "Fear"),
        )
        mock_hub3.get_news_summary.return_value = ()
        mock_hub3.get_upcoming_events.return_value = ()
        mock_hub3.get_macro_snapshot.return_value = None
        trend_mgr._data_hub = mock_hub3

        resp3 = await client.get("/api/analysis/macro-brief?pair=btc_jpy")
        assert resp3.status_code == 200
        assert "→보합" in resp3.json()["context_summary"]

