"""
Hot Strategy Activation 단위 테스트 (T-01~T-08 + R-01 + E-01~E-05).

T-01: activate → 신규 전략 즉시 기동
T-02: activate → 기존 active 전략 중단 후 신규 기동
T-03: activate → paper 모드로 실행 중인 전략 중단 후 기동
T-04: archive → 실행 중인 전략 즉시 중단
T-05: archive → 이미 중단된 전략 (stop 호출 안 됨)
T-06: activate → 런타임 에러 시 DB 상태 유지
T-07: stop_pair_all_managers → 실행 중인 매니저만 중단
T-08: strategy_registry가 None이면 런타임 기동 스킵 (에러 없음)
R-01: stop_pair_all_managers → 한 매니저 stop 에러가 다른 매니저 중단 방해 안 함
E-01: activate → product_code 키 사용 (BF 스타일)
E-02: activate → parameters에 pair/trading_style 없으면 런타임 기동 스킵 (에러 없음)
E-03: archive → proposed 전략 (activated_at 없음)도 Hot Deactivation 실행
E-04: archive → hot_style 미등록 전략도 에러 없이 처리
E-05: stop_pair_all_managers → 매니저 없으면 아무 일 없음
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import create_strategy_model
from adapters.database.session import Base
from api.routes.strategies import router
from core.strategy.registry import StrategyRegistry

# ── ORM 모델 (hot_ prefix) ───────────────────────────────────

HotStrategy = create_strategy_model("hot")


# ── Fixtures ─────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_factory():
    """SQLite 인메모리 — hot_ 테이블."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        target_tables = [
            t for name, t in Base.metadata.tables.items()
            if name.startswith("hot_") or name in ("strategy_techniques",)
        ]
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(sc, tables=target_tables)
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_manager(is_running: bool = False) -> MagicMock:
    """매니저 mock. start/stop/is_running 포함."""
    manager = MagicMock()
    manager.is_running = MagicMock(return_value=is_running)
    manager.start = AsyncMock()
    manager.stop = AsyncMock()
    return manager


def _make_registry(trend_running: bool = False, box_running: bool = False):
    """StrategyRegistry with mock managers."""
    trend = _make_manager(trend_running)
    box = _make_manager(box_running)
    registry = StrategyRegistry()
    registry.register("trend_following", trend)
    registry.register("box_mean_reversion", box)
    return registry, trend, box


def _build_client(db_factory, registry=None) -> tuple[TestClient, MagicMock]:
    from api.dependencies import get_db, get_state

    app = FastAPI()
    app.include_router(router)

    state = MagicMock()
    state.models.strategy = HotStrategy
    state.pair_column = "pair"
    state.prefix = "hot"
    state.normalize_pair = lambda p: p.lower()
    state.strategy_registry = registry

    async def override_get_state():
        return state

    async def override_get_db():
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_state] = override_get_state
    app.dependency_overrides[get_db] = override_get_db

    return TestClient(app, raise_server_exceptions=False), state


async def _insert_strategy(db_factory, *, status: str, pair: str = "btc_jpy",
                            style: str = "trend_following", name: str = "test") -> int:
    async with db_factory() as db:
        row = HotStrategy(
            name=name,
            description="테스트 전략",
            parameters={"pair": pair, "trading_style": style},
            rationale="테스트 목적 핫 활성화 최소 20자 rationale",
            status=status,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


# ── T-01: activate → 신규 전략 즉시 기동 ─────────────────────

@pytest.mark.asyncio
async def test_t01_activate_starts_new_strategy(db_factory):
    strategy_id = await _insert_strategy(db_factory, status="proposed")
    registry, trend, _ = _make_registry(trend_running=False)

    client, _ = _build_client(db_factory, registry=registry)
    resp = client.put(f"/api/strategies/{strategy_id}/activate")

    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    trend.start.assert_awaited_once()
    called_pair = trend.start.call_args[0][0]
    assert called_pair == "btc_jpy"


# ── T-02: activate → 기존 active 중단 후 신규 기동 ───────────

@pytest.mark.asyncio
async def test_t02_activate_stops_existing_then_starts(db_factory):
    # 기존 active 전략 삽입
    await _insert_strategy(db_factory, status="active", pair="btc_jpy", name="old")
    new_id = await _insert_strategy(db_factory, status="proposed", pair="btc_jpy", name="new")
    registry, trend, _ = _make_registry(trend_running=True)

    client, _ = _build_client(db_factory, registry=registry)
    resp = client.put(f"/api/strategies/{new_id}/activate")

    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    # 기존 실행 중이므로 stop 호출됨
    trend.stop.assert_awaited_once_with("btc_jpy")
    # 신규 전략 기동됨
    trend.start.assert_awaited_once()


# ── T-03: activate → paper 모드 중단 후 실전 기동 ────────────

@pytest.mark.asyncio
async def test_t03_activate_stops_paper_then_starts_live(db_factory):
    strategy_id = await _insert_strategy(db_factory, status="proposed")
    # paper 모드로 이미 실행 중
    registry, trend, _ = _make_registry(trend_running=True)

    client, _ = _build_client(db_factory, registry=registry)
    resp = client.put(f"/api/strategies/{strategy_id}/activate")

    assert resp.status_code == 200
    # paper 포함 실행 중이므로 stop 먼저
    trend.stop.assert_awaited_once_with("btc_jpy")
    # 실전 모드로 재기동
    trend.start.assert_awaited_once()
    # start에 strategy_id가 포함되어야 함
    start_params = trend.start.call_args[0][1]
    assert start_params["strategy_id"] == strategy_id


# ── T-04: archive → 실행 중 전략 즉시 중단 ───────────────────

@pytest.mark.asyncio
async def test_t04_archive_stops_running_strategy(db_factory):
    strategy_id = await _insert_strategy(db_factory, status="active")
    registry, trend, _ = _make_registry(trend_running=True)

    client, _ = _build_client(db_factory, registry=registry)
    resp = client.put(f"/api/strategies/{strategy_id}/archive")

    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    trend.stop.assert_awaited_once_with("btc_jpy")


# ── T-05: archive → 이미 중단됨 (stop 호출 안 됨) ────────────

@pytest.mark.asyncio
async def test_t05_archive_skips_stop_when_not_running(db_factory):
    strategy_id = await _insert_strategy(db_factory, status="active")
    registry, trend, _ = _make_registry(trend_running=False)

    client, _ = _build_client(db_factory, registry=registry)
    resp = client.put(f"/api/strategies/{strategy_id}/archive")

    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    trend.stop.assert_not_awaited()


# ── T-06: activate → 런타임 에러 시 DB 상태 유지 ─────────────

@pytest.mark.asyncio
async def test_t06_activate_runtime_error_keeps_db_active(db_factory):
    strategy_id = await _insert_strategy(db_factory, status="proposed")
    registry, trend, _ = _make_registry(trend_running=False)
    # start()가 예외를 던짐
    trend.start.side_effect = RuntimeError("테스트 에러")

    client, _ = _build_client(db_factory, registry=registry)
    resp = client.put(f"/api/strategies/{strategy_id}/activate")

    # 200으로 응답 (에러를 caller에게 전파 안 함)
    assert resp.status_code == 200
    # DB status는 active 유지
    assert resp.json()["status"] == "active"


# ── T-07: stop_pair_all_managers → 실행 중인 것만 중단 ────────

@pytest.mark.asyncio
async def test_t07_stop_pair_all_managers_only_running():
    trend = _make_manager(is_running=True)
    box = _make_manager(is_running=False)
    registry = StrategyRegistry()
    registry.register("trend_following", trend)
    registry.register("box_mean_reversion", box)

    await registry.stop_pair_all_managers("btc_jpy")

    trend.stop.assert_awaited_once_with("btc_jpy")
    box.stop.assert_not_awaited()


# ── T-08: strategy_registry가 None이면 스킵 ──────────────────

@pytest.mark.asyncio
async def test_t08_activate_skips_hot_when_registry_none(db_factory):
    strategy_id = await _insert_strategy(db_factory, status="proposed")

    # registry=None 전달
    client, _ = _build_client(db_factory, registry=None)
    resp = client.put(f"/api/strategies/{strategy_id}/activate")

    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


# ── R-01: stop 에러가 다른 매니저 중단 방해 안 함 ─────────────

@pytest.mark.asyncio
async def test_r01_stop_pair_continues_after_error():
    trend = _make_manager(is_running=True)
    trend.stop.side_effect = RuntimeError("stop 중 에러")
    box = _make_manager(is_running=True)
    registry = StrategyRegistry()
    registry.register("trend_following", trend)
    registry.register("box_mean_reversion", box)

    # 예외가 전파되지 않아야 함
    await registry.stop_pair_all_managers("btc_jpy")

    trend.stop.assert_awaited_once_with("btc_jpy")
    # trend가 에러나도 box는 중단됨
    box.stop.assert_awaited_once_with("btc_jpy")


# ── E-01: product_code 키 사용 (BF 스타일) ────────────────────

@pytest.mark.asyncio
async def test_e01_activate_product_code_pair(db_factory):
    """BF 스타일 전략은 pair 대신 product_code 키 사용 — 정상 기동돼야 함."""
    async with db_factory() as db:
        row = HotStrategy(
            name="bf-test",
            description="BF 전략",
            parameters={"product_code": "BTC_JPY", "trading_style": "trend_following"},
            rationale="BF product_code 기반 핫 활성화 테스트 최소 20자",
            status="proposed",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        strategy_id = row.id

    registry, trend, _ = _make_registry(trend_running=False)
    client, state = _build_client(db_factory, registry=registry)
    # BF는 대문자 반환
    state.normalize_pair = lambda p: p.upper()

    resp = client.put(f"/api/strategies/{strategy_id}/activate")

    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    trend.start.assert_awaited_once()
    called_pair = trend.start.call_args[0][0]
    assert called_pair == "BTC_JPY"


# ── E-02: parameters에 pair/trading_style 없으면 스킵 ────────

@pytest.mark.asyncio
async def test_e02_activate_missing_pair_skips_hot(db_factory):
    """pair/trading_style 없는 전략: DB 활성화는 되고 런타임 기동은 스킵."""
    async with db_factory() as db:
        row = HotStrategy(
            name="no-pair",
            description="페어 없는 전략",
            parameters={},  # pair, trading_style 없음
            rationale="페어 없는 전략 핫 활성화 최소 20자 rationale",
            status="proposed",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        strategy_id = row.id

    registry, trend, _ = _make_registry()
    client, _ = _build_client(db_factory, registry=registry)

    resp = client.put(f"/api/strategies/{strategy_id}/activate")

    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    trend.start.assert_not_awaited()
    trend.stop.assert_not_awaited()


# ── E-03: proposed 전략 archive → Hot Deactivation 실행 ──────

@pytest.mark.asyncio
async def test_e03_archive_proposed_strategy_stops_if_running(db_factory):
    """proposed(paper 실행 중) 전략 archive 시 Hot Deactivation 실행."""
    strategy_id = await _insert_strategy(db_factory, status="proposed")
    registry, trend, _ = _make_registry(trend_running=True)

    client, _ = _build_client(db_factory, registry=registry)
    resp = client.put(f"/api/strategies/{strategy_id}/archive")

    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    # proposed도 실행 중이면 stop 호출됨
    trend.stop.assert_awaited_once_with("btc_jpy")


# ── E-04: archive → hot_style 미등록 전략도 에러 없음 ─────────

@pytest.mark.asyncio
async def test_e04_archive_unknown_style_no_error(db_factory):
    """registry에 없는 trading_style 전략 archive — 에러 없이 DB 상태 반영."""
    async with db_factory() as db:
        row = HotStrategy(
            name="unknown-style",
            description="알 수 없는 스타일",
            parameters={"pair": "eth_jpy", "trading_style": "unknown_strategy"},
            rationale="미등록 전략 스타일 핫 디액티베이션 테스트 최소 20자",
            status="active",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        strategy_id = row.id

    registry, trend, _ = _make_registry()
    client, _ = _build_client(db_factory, registry=registry)

    resp = client.put(f"/api/strategies/{strategy_id}/archive")

    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    trend.stop.assert_not_awaited()


# ── E-05: stop_pair_all_managers → 빈 registry도 에러 없음 ───

@pytest.mark.asyncio
async def test_e05_stop_pair_all_managers_empty_registry():
    """매니저가 하나도 없는 registry에 stop_pair_all_managers 호출 — 에러 없음."""
    registry = StrategyRegistry()
    # 예외 없이 완료되어야 함
    await registry.stop_pair_all_managers("btc_jpy")


# ── DA-01: 듀얼 활성 — 다른 스타일은 archive 안 됨 ───────────

@pytest.mark.asyncio
async def test_da01_dual_active_different_style_coexists(db_factory):
    """trend_following active 상태에서 box_mean_reversion proposed 활성화.
    → trend_following DB 레코드는 archive되지 않아야 한다 (스타일 다름)."""
    trend_id = await _insert_strategy(
        db_factory, status="active", pair="btc_jpy",
        style="trend_following", name="trend-active"
    )
    box_id = await _insert_strategy(
        db_factory, status="proposed", pair="btc_jpy",
        style="box_mean_reversion", name="box-proposed"
    )

    registry, trend, box = _make_registry(trend_running=True, box_running=False)
    client, _ = _build_client(db_factory, registry=registry)
    resp = client.put(f"/api/strategies/{box_id}/activate")

    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    # trend_following은 archive되지 않아야 함 — 스타일이 다르므로
    async with db_factory() as db:
        trend_row = await db.get(HotStrategy, trend_id)
        assert trend_row.status == "active", "trend_following이 잘못 archive됨"
        box_row = await db.get(HotStrategy, box_id)
        assert box_row.status == "active"

    # trend 매니저는 stop되지 않아야 함
    trend.stop.assert_not_awaited()
    # box 매니저는 start됨 (실행 중 아니었으므로 stop 없이 start)
    box.start.assert_awaited_once()


# ── DA-02: 듀얼 활성 — 같은 스타일은 여전히 archive됨 ────────

@pytest.mark.asyncio
async def test_da02_same_style_still_archives_old(db_factory):
    """trend_following v1 active + trend_following v2 proposed 활성화.
    → v1은 archive (같은 스타일 충돌). v2는 start됨."""
    old_id = await _insert_strategy(
        db_factory, status="active", pair="btc_jpy",
        style="trend_following", name="trend-v1"
    )
    new_id = await _insert_strategy(
        db_factory, status="proposed", pair="btc_jpy",
        style="trend_following", name="trend-v2"
    )

    registry, trend, _ = _make_registry(trend_running=True)
    client, _ = _build_client(db_factory, registry=registry)
    resp = client.put(f"/api/strategies/{new_id}/activate")

    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    # 같은 스타일 기존 전략은 archive
    async with db_factory() as db:
        old_row = await db.get(HotStrategy, old_id)
        assert old_row.status == "archived"

    # 동일 스타일 매니저는 stop → start
    trend.stop.assert_awaited_once_with("btc_jpy")
    trend.start.assert_awaited_once()


# ── DA-03: 듀얼 활성 Hot — box 활성화 시 trend 매니저 유지 ────

@pytest.mark.asyncio
async def test_da03_hot_activation_box_does_not_stop_trend(db_factory):
    """box_mean_reversion 활성화 시 trend_following 매니저는 중단되지 않아야 함."""
    box_id = await _insert_strategy(
        db_factory, status="proposed", pair="btc_jpy",
        style="box_mean_reversion", name="box"
    )

    # 양쪽 매니저 모두 실행 중
    registry, trend, box = _make_registry(trend_running=True, box_running=True)
    client, _ = _build_client(db_factory, registry=registry)
    resp = client.put(f"/api/strategies/{box_id}/activate")

    assert resp.status_code == 200
    # trend 매니저는 건드리지 않음
    trend.stop.assert_not_awaited()
    trend.start.assert_not_awaited()
    # box 매니저는 기존 것 stop 후 재기동
    box.stop.assert_awaited_once_with("btc_jpy")
    box.start.assert_awaited_once()
