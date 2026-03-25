"""
HealthChecker + API 라우트 + main.py 테스트.

테스트 대상:
  - core/monitoring/health.py (HealthChecker)
  - api/routes/ (system, trading, account, strategies, boxes, candles, techniques)
  - main.py (lifespan + 거래소 선택)
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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
from api.routes import system, trading, account, strategies, boxes, candles, techniques
from core.monitoring.health import HealthChecker, HealthReport
from core.strategy.box_mean_reversion import BoxMeanReversionManager
from core.strategy.trend_following import TrendFollowingManager
from core.task.supervisor import TaskSupervisor
from tests.fake_exchange import FakeExchangeAdapter

# ── 테스트용 ORM 모델 (hlth_ 프리픽스) ──────────────────────

HlthStrategy = create_strategy_model("hlth")
HlthTrade = create_trade_model("hlth", order_id_length=40)
HlthBalanceEntry = create_balance_entry_model("hlth")
HlthInsight = create_insight_model("hlth")
HlthSummary = create_summary_model("hlth")
HlthCandle = create_candle_model("hlth", pair_column="pair")
HlthBox = create_box_model("hlth", pair_column="pair")
HlthBoxPosition = create_box_position_model("hlth", pair_column="pair", order_id_length=40)
HlthTrendPosition = create_trend_position_model("hlth", order_id_length=40)


def _create_model_registry() -> ModelRegistry:
    return ModelRegistry(
        strategy=HlthStrategy,
        trade=HlthTrade,
        balance_entry=HlthBalanceEntry,
        insight=HlthInsight,
        summary=HlthSummary,
        candle=HlthCandle,
        box=HlthBox,
        box_position=HlthBoxPosition,
        trend_position=HlthTrendPosition,
        technique=StrategyTechnique,
    )


# ── Fixtures ─────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_session():
    """SQLite 인메모리 세션 + hlth_ 테이블 생성."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # hlth_ + strategy_techniques 테이블만
    target_tables = [
        t for t in Base.metadata.tables.values()
        if t.name.startswith("hlth_") or t.name == "strategy_techniques"
    ]
    async with engine.begin() as conn:
        for table in target_tables:
            await conn.run_sync(table.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session, factory
    await engine.dispose()


@pytest_asyncio.fixture
async def adapter():
    fa = FakeExchangeAdapter(
        initial_balances={"jpy": 1_000_000.0, "xrp": 100.0},
        ticker_price=80.0,
    )
    await fa.connect()
    yield fa
    await fa.close()


@pytest_asyncio.fixture
async def app_state(db_session, adapter):
    """테스트용 AppState 조립."""
    session, factory = db_session
    supervisor = TaskSupervisor()
    models = _create_model_registry()

    trend_manager = TrendFollowingManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=factory,
        candle_model=HlthCandle,
        trend_position_model=HlthTrendPosition,
        pair_column="pair",
    )
    box_manager = BoxMeanReversionManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=factory,
        candle_model=HlthCandle,
        box_model=HlthBox,
        box_position_model=HlthBoxPosition,
        pair_column="pair",
    )
    health_checker = HealthChecker(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=factory,
        strategy_model=HlthStrategy,
        trend_position_model=HlthTrendPosition,
        box_position_model=HlthBoxPosition,
        pair_column="pair",
    )
    state = AppState(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=factory,
        trend_manager=trend_manager,
        box_manager=box_manager,
        health_checker=health_checker,
        models=models,
        prefix="hlth",
        pair_column="pair",
    )
    yield state
    await supervisor.stop_all()


def _create_test_app(state: AppState):
    """테스트용 FastAPI 앱 생성."""
    from fastapi import FastAPI
    app = FastAPI()
    app.state.app_state = state
    app.include_router(system.router)
    app.include_router(trading.router)
    app.include_router(account.router)
    app.include_router(strategies.router)
    app.include_router(boxes.router)
    app.include_router(candles.router)
    app.include_router(techniques.router)
    return app


# ═══════════════════════════════════════════════════════════════
# 1. HealthChecker 유닛 테스트
# ═══════════════════════════════════════════════════════════════

class TestHealthChecker:

    @pytest.mark.asyncio
    async def test_healthy_when_no_issues(self, app_state):
        """이슈 없으면 healthy=True."""
        report = await app_state.health_checker.check()
        assert report.healthy is True
        assert report.issues == []
        assert report.ws_connected is True

    @pytest.mark.asyncio
    async def test_unhealthy_ws_disconnected(self, app_state, adapter):
        """WS 끊기면 healthy=False."""
        await adapter.close()
        report = await app_state.health_checker.check()
        assert report.healthy is False
        assert any("ws" in i for i in report.issues)

    @pytest.mark.asyncio
    async def test_unhealthy_dead_task(self, app_state):
        """죽은 태스크가 있으면 healthy=False."""
        # 즉시 예외 던지는 태스크 등록
        async def failing():
            raise RuntimeError("test crash")

        await app_state.supervisor.register(
            "failing_task", failing, max_restarts=0, auto_restart=False
        )
        await asyncio.sleep(0.1)  # 태스크가 죽을 시간

        report = await app_state.health_checker.check()
        assert report.healthy is False
        assert any("failing_task" in i for i in report.issues)

    @pytest.mark.asyncio
    async def test_position_balance_consistency_ok(self, app_state, db_session):
        """오픈 포지션 없으면 discrepancy 없음."""
        report = await app_state.health_checker.check()
        assert report.position_balance == []

    @pytest.mark.asyncio
    async def test_position_balance_mismatch(self, app_state, db_session, adapter):
        """DB 포지션 vs 실잔고 불일치 감지."""
        session, factory = db_session
        # DB에 오픈 포지션 기록 (50 XRP) — grace period 회피를 위해 2분 전 생성
        pos = HlthTrendPosition(
            pair="xrp_jpy",
            entry_order_id="FAKE-001",
            entry_price=80.0,
            entry_amount=50.0,
            status="open",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        )
        session.add(pos)
        await session.commit()

        # 실잔고를 0으로 설정 → 불일치
        adapter.set_balance("xrp", 0.0)
        report = await app_state.health_checker.check()
        assert len(report.position_balance) > 0
        assert report.position_balance[0]["currency"] == "xrp"

    @pytest.mark.asyncio
    async def test_active_strategies_listed(self, app_state, db_session):
        """활성 전략이 헬스 리포트에 포함."""
        session, factory = db_session
        st = HlthStrategy(
            name="test",
            description="test strategy",
            parameters={"pair": "xrp_jpy", "trading_style": "trend_following"},
            rationale="test rationale for the strategy",
            status="active",
        )
        session.add(st)
        await session.commit()

        report = await app_state.health_checker.check()
        assert len(report.active_strategies) == 1
        assert report.active_strategies[0]["name"] == "test"


# ═══════════════════════════════════════════════════════════════
# 2. system.py 라우트 테스트
# ═══════════════════════════════════════════════════════════════

class TestSystemRoute:

    @pytest.mark.asyncio
    async def test_health_200_when_healthy(self, app_state):
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["healthy"] is True

    @pytest.mark.asyncio
    async def test_health_503_when_ws_down(self, app_state, adapter):
        await adapter.close()
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/system/health")
        assert resp.status_code == 503
        assert resp.json()["healthy"] is False


# ═══════════════════════════════════════════════════════════════
# 3. trading.py 라우트 테스트
# ═══════════════════════════════════════════════════════════════

class TestTradingRoute:

    @pytest.mark.asyncio
    async def test_get_constraints(self, app_state):
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/exchange/constraints")
        assert resp.status_code == 200
        data = resp.json()
        assert "min_order_sizes" in data
        assert data["exchange"] == "fake"

    @pytest.mark.asyncio
    async def test_create_order(self, app_state):
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/exchange/orders", json={
                "pair": "xrp_jpy",
                "order_type": "market_buy",
                "amount": 1000.0,
                "reasoning": "Testing market buy order execution",
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["order_id"].startswith("FAKE-")
        assert data["pair"] == "xrp_jpy"

    @pytest.mark.asyncio
    async def test_create_order_invalid_type(self, app_state):
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/exchange/orders", json={
                "pair": "xrp_jpy",
                "order_type": "invalid_type",
                "amount": 100.0,
                "reasoning": "this should fail validation",
            })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_cancel_order(self, app_state):
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 먼저 주문 생성
            resp = await client.post("/api/exchange/orders", json={
                "pair": "xrp_jpy",
                "order_type": "buy",
                "amount": 10.0,
                "price": 80.0,
                "reasoning": "Testing order cancel workflow",
            })
            order_id = resp.json()["order_id"]
            # 취소
            resp = await client.delete(f"/api/exchange/orders/{order_id}")
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is True

    @pytest.mark.asyncio
    async def test_get_open_orders(self, app_state):
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/exchange/orders/opens?pair=xrp_jpy")
        assert resp.status_code == 200
        assert "orders" in resp.json()


# ═══════════════════════════════════════════════════════════════
# 4. account.py 라우트 테스트
# ═══════════════════════════════════════════════════════════════

class TestAccountRoute:

    @pytest.mark.asyncio
    async def test_get_balance(self, app_state):
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/accounts/balance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exchange"] == "fake"
        assert "jpy" in data["currencies"]
        assert data["currencies"]["jpy"]["available"] == 1_000_000.0


# ═══════════════════════════════════════════════════════════════
# 5. strategies.py 라우트 테스트
# ═══════════════════════════════════════════════════════════════

class TestStrategiesRoute:

    @pytest.mark.asyncio
    async def test_create_and_list_strategy(self, app_state):
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 생성
            resp = await client.post("/api/strategies", json={
                "name": "XRP Box Strategy",
                "description": "박스권 역추세 전략",
                "parameters": {"pair": "xrp_jpy", "trading_style": "box_mean_reversion"},
                "rationale": "XRP가 박스권 안에서 횡보 중이므로 역추세 매매",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "proposed"
            strategy_id = data["id"]

            # 목록
            resp = await client.get("/api/strategies")
            assert resp.status_code == 200
            assert resp.json()["total"] >= 1

    @pytest.mark.asyncio
    async def test_activate_and_archive_strategy(self, app_state):
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 생성
            resp = await client.post("/api/strategies", json={
                "name": "Test Trend",
                "description": "추세추종 전략 테스트",
                "parameters": {"pair": "btc_jpy", "trading_style": "trend_following"},
                "rationale": "BTC 추세가 강하게 형성되고 있어 진입 적합",
            })
            sid = resp.json()["id"]

            # 활성화
            resp = await client.put(f"/api/strategies/{sid}/activate")
            assert resp.status_code == 200
            assert resp.json()["status"] == "active"

            # active 목록
            resp = await client.get("/api/strategies/active")
            assert resp.status_code == 200
            assert any(s["id"] == sid for s in resp.json())

            # 아카이브
            resp = await client.put(f"/api/strategies/{sid}/archive")
            assert resp.status_code == 200
            assert resp.json()["status"] == "archived"

    @pytest.mark.asyncio
    async def test_reject_strategy(self, app_state):
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/strategies", json={
                "name": "Bad Strategy",
                "description": "거부될 전략",
                "parameters": {"pair": "xrp_jpy"},
                "rationale": "이 전략은 테스트를 위한 거부 대상 전략입니다",
            })
            sid = resp.json()["id"]
            resp = await client.put(f"/api/strategies/{sid}/reject", json={
                "rejection_reason": "리스크가 너무 높아서 거부함",
            })
            assert resp.status_code == 200
            assert resp.json()["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_activate_archives_same_pair(self, app_state):
        """동일 pair 기존 active 전략이 자동 archive."""
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 전략 1 생성 + 활성화
            resp = await client.post("/api/strategies", json={
                "name": "V1",
                "description": "첫 번째 전략",
                "parameters": {"pair": "eth_jpy", "trading_style": "trend_following"},
                "rationale": "ETH 추세추종 전략 첫 번째 버전으로 진입",
            })
            sid1 = resp.json()["id"]
            await client.put(f"/api/strategies/{sid1}/activate")

            # 전략 2 생성 + 활성화 → 전략 1 자동 archive
            resp = await client.post("/api/strategies", json={
                "name": "V2",
                "description": "두 번째 전략",
                "parameters": {"pair": "eth_jpy", "trading_style": "box_mean_reversion"},
                "rationale": "ETH 박스권 역추세로 전략 전환이 필요합니다",
            })
            sid2 = resp.json()["id"]
            await client.put(f"/api/strategies/{sid2}/activate")

            # 전략 1 확인 → archived
            resp = await client.get(f"/api/strategies/{sid1}")
            assert resp.json()["status"] == "archived"


# ═══════════════════════════════════════════════════════════════
# 6. boxes.py 라우트 테스트
# ═══════════════════════════════════════════════════════════════

class TestBoxesRoute:

    @pytest.mark.asyncio
    async def test_no_active_box(self, app_state):
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/boxes/xrp_jpy")
        assert resp.status_code == 200
        assert resp.json()["box"] is None

    @pytest.mark.asyncio
    async def test_active_box_found(self, app_state, db_session):
        session, _ = db_session
        box = HlthBox(
            pair="xrp_jpy",
            upper_bound=90.0,
            lower_bound=70.0,
            upper_touch_count=4,
            lower_touch_count=3,
            status="active",
        )
        session.add(box)
        await session.commit()

        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/boxes/xrp_jpy")
        assert resp.status_code == 200
        data = resp.json()
        assert data["box"] is not None
        assert data["box"]["upper_bound"] == 90.0



    @pytest.mark.asyncio
    async def test_box_history(self, app_state, db_session):
        session, _ = db_session
        for status in ["active", "invalidated", "invalidated"]:
            b = HlthBox(
                pair="btc_jpy",
                upper_bound=50000.0,
                lower_bound=48000.0,
                status=status,
            )
            session.add(b)
        await session.commit()

        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/boxes/btc_jpy/history")
        assert resp.status_code == 200
        assert len(resp.json()["boxes"]) == 3

    @pytest.mark.asyncio
    async def test_price_position_no_box(self, app_state):
        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/boxes/xrp_jpy/position")
        assert resp.status_code == 200
        assert resp.json()["position"] == "no_box"


# ═══════════════════════════════════════════════════════════════
# 7. techniques.py 라우트 테스트
# ═══════════════════════════════════════════════════════════════

class TestTechniquesRoute:

    @pytest.mark.asyncio
    async def test_list_and_update_technique(self, app_state, db_session):
        session, _ = db_session
        tech = StrategyTechnique(
            code="box_mean_reversion",
            name="박스권 역추세",
            description="박스권 안에서 하단 매수 상단 매도",
            risk_level="medium",
        )
        session.add(tech)
        await session.commit()

        app = _create_test_app(app_state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 목록
            resp = await client.get("/api/techniques")
            assert resp.status_code == 200
            assert len(resp.json()) >= 1

            # 상세
            resp = await client.get("/api/techniques/box_mean_reversion")
            assert resp.status_code == 200
            assert resp.json()["code"] == "box_mean_reversion"

            # 노트 업데이트
            resp = await client.patch("/api/techniques/box_mean_reversion/notes", json={
                "experience_notes": "3월 실전에서 승률 60% 달성, 비트코인 횡보장에 효과적",
            })
            assert resp.status_code == 200
            assert "60%" in resp.json()["experience_notes"]


# ═══════════════════════════════════════════════════════════════
# 8. main.py 설정 테스트
# ═══════════════════════════════════════════════════════════════

class TestMainConfig:

    def test_exchange_config_bitflyer(self):
        from main import _EXCHANGE_CONFIG
        cfg = _EXCHANGE_CONFIG["bitflyer"]
        assert cfg["prefix"] == "bf"
        assert cfg["pair_column"] == "product_code"
        assert cfg["order_id_length"] == 40

    def test_create_models_bitflyer(self):
        """ck/bf prefix는 test_models.py와 충돌하므로 tmc/tmb 사용."""
        from main import _create_models
        models = _create_models("tmb", "product_code", 40)
        assert models.strategy.__tablename__ == "tmb_strategies"
        assert models.candle.__tablename__ == "tmb_candles"
        assert models.box.__tablename__ == "tmb_boxes"

    def test_create_adapter_bitflyer(self, monkeypatch):
        monkeypatch.setenv("BITFLYER_API_KEY", "test_key")
        monkeypatch.setenv("BITFLYER_API_SECRET", "test_secret")
        from main import _create_adapter
        adapter = _create_adapter("bitflyer")
        assert adapter.exchange_name == "bitflyer"
