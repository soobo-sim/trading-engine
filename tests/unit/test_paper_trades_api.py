"""
Paper Trades API 단위 테스트.

T-PA-01: 빈 이력 조회 — 0건 반환
T-PA-02: strategy_id 필터 — 해당 전략만 반환
T-PA-03: closed_only 필터 — 청산 완료만 반환
T-PA-04: summary — 0건일 때 empty_summary
T-PA-05: summary — 복수 거래 WR/PnL 계산
T-PA-06: overview — proposed 전략 없으면 빈 목록
T-PA-07: overview — proposed 전략 있음 + paper_trades 집계
T-PA-08: max_drawdown 계산
T-PA-09: insufficient_data 플래그 (20거래 미만)
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from unittest.mock import AsyncMock, MagicMock

from adapters.database.models import PaperTrade, create_strategy_model
from adapters.database.session import Base
from api.routes.paper_trades import router, _calc_max_drawdown

# ── ORM 모델 ────────────────────────────────────────────────

PtStrategy = create_strategy_model("pt")


# ── Fixtures ────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_session_factory():
    """SQLite 인메모리 — pt_ + paper_trades 테이블."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        target_tables = [
            t for name, t in Base.metadata.tables.items()
            if name.startswith("pt_") or name in ("paper_trades", "strategy_techniques")
        ]
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(sc, tables=target_tables)
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_app_state(factory, prefix="pt"):
    """라우터 테스트용 AppState 모의 객체."""
    state = MagicMock()
    state.session_factory = factory
    state.models.strategy = PtStrategy
    state.prefix = prefix
    state.pair_column = "pair"
    state.normalize_pair = lambda p: p.lower()
    return state


def _build_client(factory) -> TestClient:
    """TestClient 빌드. app.state.app_state 주입."""
    from api.dependencies import get_db, get_state

    app = FastAPI()
    app.include_router(router)

    app_state = _make_app_state(factory)

    async def override_get_state():
        return app_state

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_state] = override_get_state
    app.dependency_overrides[get_db] = override_get_db

    return TestClient(app)


async def _insert_strategy(factory, *, status="proposed", pair="USD_JPY", name="test-strategy"):
    async with factory() as db:
        row = PtStrategy(
            name=name, description="desc",
            parameters={"pair": pair},
            rationale="테스트용 전략 최소 20자 이상 rationale",
            status=status,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def _insert_trade(
    factory, *,
    strategy_id: int,
    pair: str = "USD_JPY",
    direction: str = "long",
    entry_price: float = 150.0,
    exit_price: float | None = None,
    pnl_pct: float | None = None,
    pnl_jpy: float | None = None,
):
    async with factory() as db:
        row = PaperTrade(
            strategy_id=strategy_id,
            pair=pair,
            direction=direction,
            entry_price=Decimal(str(entry_price)),
            entry_time=datetime.now(timezone.utc),
            exit_price=Decimal(str(exit_price)) if exit_price is not None else None,
            exit_time=datetime.now(timezone.utc) if exit_price is not None else None,
            paper_pnl_pct=Decimal(str(pnl_pct)) if pnl_pct is not None else None,
            paper_pnl_jpy=Decimal(str(pnl_jpy)) if pnl_jpy is not None else None,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


# ── Tests ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pa01_empty_list(db_session_factory):
    """T-PA-01: 거래 없으면 빈 배열."""
    client = _build_client(db_session_factory)
    resp = client.get("/api/paper-trades")
    assert resp.status_code == 200
    data = resp.json()
    assert data["trades"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_pa02_strategy_id_filter(db_session_factory):
    """T-PA-02: strategy_id 필터로 다른 전략 거래 미포함."""
    sid1 = await _insert_strategy(db_session_factory, name="s1")
    sid2 = await _insert_strategy(db_session_factory, name="s2")
    await _insert_trade(db_session_factory, strategy_id=sid1, pair="USD_JPY")
    await _insert_trade(db_session_factory, strategy_id=sid2, pair="GBP_JPY")

    client = _build_client(db_session_factory)
    resp = client.get(f"/api/paper-trades?strategy_id={sid1}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["trades"][0]["strategy_id"] == sid1


@pytest.mark.asyncio
async def test_pa03_closed_only_filter(db_session_factory):
    """T-PA-03: closed_only=true면 청산 완료 거래만 반환."""
    sid = await _insert_strategy(db_session_factory)
    await _insert_trade(db_session_factory, strategy_id=sid)               # 미청산
    await _insert_trade(db_session_factory, strategy_id=sid,
                        exit_price=151.0, pnl_pct=0.67, pnl_jpy=670.0)   # 청산

    client = _build_client(db_session_factory)
    resp = client.get("/api/paper-trades?closed_only=true")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["trades"][0]["exit_price"] is not None


@pytest.mark.asyncio
async def test_pa04_summary_empty(db_session_factory):
    """T-PA-04: 거래 없으면 empty summary (insufficient_data=True)."""
    sid = await _insert_strategy(db_session_factory)
    client = _build_client(db_session_factory)
    resp = client.get(f"/api/paper-trades/summary?strategy_id={sid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_trades"] == 0
    assert data["win_rate"] == 0.0
    assert data["insufficient_data"] is True


@pytest.mark.asyncio
async def test_pa05_summary_calculation(db_session_factory):
    """T-PA-05: 3건(2승1패) → WR 66.7%, total_pnl_pct = 5+5-3=7."""
    sid = await _insert_strategy(db_session_factory)
    for pnl in [5.0, 5.0, -3.0]:
        await _insert_trade(db_session_factory, strategy_id=sid,
                            exit_price=152.0, pnl_pct=pnl, pnl_jpy=pnl * 1000)

    client = _build_client(db_session_factory)
    resp = client.get(f"/api/paper-trades/summary?strategy_id={sid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_trades"] == 3
    assert data["win_rate"] == pytest.approx(66.7, abs=0.2)
    assert data["total_pnl_pct"] == pytest.approx(7.0, abs=0.01)
    assert data["insufficient_data"] is True  # 20 미만


@pytest.mark.asyncio
async def test_pa06_overview_no_proposed(db_session_factory):
    """T-PA-06: proposed 전략 없으면 빈 목록."""
    await _insert_strategy(db_session_factory, status="active")
    client = _build_client(db_session_factory)
    resp = client.get("/api/paper-trades/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["strategies"] == []


@pytest.mark.asyncio
async def test_pa07_overview_with_trades(db_session_factory):
    """T-PA-07: proposed 전략 1개 + trades 2건 → overview에 집계 포함."""
    sid = await _insert_strategy(db_session_factory, status="proposed", pair="USD_JPY")
    for pnl in [3.0, -1.0]:
        await _insert_trade(db_session_factory, strategy_id=sid,
                            exit_price=152.0, pnl_pct=pnl, pnl_jpy=pnl * 1000)

    client = _build_client(db_session_factory)
    resp = client.get("/api/paper-trades/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    item = data["strategies"][0]
    assert item["strategy_id"] == sid
    assert item["pair"] == "USD_JPY"
    assert item["status"] == "proposed"
    ps = item["paper_summary"]
    assert ps["total_trades"] == 2
    assert ps["win_rate"] == 50.0
    assert ps["total_pnl_pct"] == pytest.approx(2.0, abs=0.01)


@pytest.mark.asyncio
async def test_pa08_max_drawdown_calc():
    """T-PA-08: MDD 계산 — 누적 고점 대비 낙폭."""
    # 5% 오르다 8% 빠지면 MDD=3
    pnl_seq = [2.0, 3.0, -8.0, 4.0]  # cum: 2, 5, -3, 1
    mdd = _calc_max_drawdown(pnl_seq)
    assert mdd == pytest.approx(8.0, abs=0.01)


@pytest.mark.asyncio
async def test_pa09_insufficient_data_flag(db_session_factory):
    """T-PA-09: 거래 >= 20건이면 insufficient_data=False."""
    sid = await _insert_strategy(db_session_factory)
    for _ in range(20):
        await _insert_trade(db_session_factory, strategy_id=sid,
                            exit_price=151.0, pnl_pct=1.0, pnl_jpy=1000.0)

    client = _build_client(db_session_factory)
    resp = client.get(f"/api/paper-trades/summary?strategy_id={sid}")
    assert resp.status_code == 200
    assert resp.json()["insufficient_data"] is False


@pytest.mark.asyncio
async def test_pa10_open_trades_count(db_session_factory):
    """T-PA-10: 미청산 거래 수 open_trades 정확 집계.
    
    summary의 total_trades = 청산 완료 거래 수 (성과 집계 base).
    open_trades = 미청산 진행 중 거래 수 (별도 집계).
    """
    sid = await _insert_strategy(db_session_factory)
    # 청산 2건
    for pnl in [2.0, -1.0]:
        await _insert_trade(db_session_factory, strategy_id=sid,
                            exit_price=151.0, pnl_pct=pnl, pnl_jpy=pnl * 1000)
    # 미청산 3건
    for _ in range(3):
        await _insert_trade(db_session_factory, strategy_id=sid)

    client = _build_client(db_session_factory)
    resp = client.get(f"/api/paper-trades/summary?strategy_id={sid}")
    assert resp.status_code == 200
    data = resp.json()
    # total_trades = 청산 완료 건수 (성과 집계 기준), open_trades = 미청산 건수
    assert data["total_trades"] == 2   # 청산 완료
    assert data["open_trades"] == 3     # 미청산 진행 중


@pytest.mark.asyncio
async def test_pa11_pair_filter(db_session_factory):
    """T-PA-11: pair 필터 — 해당 통화 거래만 반환."""
    sid = await _insert_strategy(db_session_factory)
    await _insert_trade(db_session_factory, strategy_id=sid, pair="USD_JPY")
    await _insert_trade(db_session_factory, strategy_id=sid, pair="GBP_JPY")

    client = _build_client(db_session_factory)
    resp = client.get("/api/paper-trades?pair=USD_JPY")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["trades"][0]["pair"] == "USD_JPY"
