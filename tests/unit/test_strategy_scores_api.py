"""
Strategy Scores & Switch Recommendations API 단위 테스트 (V-40~V-48).

V-40: GET /api/strategy-scores — 스냅샷 없으면 빈 목록
V-41: GET /api/strategy-scores/{id} — 스냅샷 있으면 반환
V-42: GET /api/strategy-snapshots/latest — active+proposed 전 전략 최신 스냅샷
V-43: GET /api/strategy-snapshots/{id}?limit=5 — 이력 목록
V-44: GET /api/switch-recommendations?status=pending — 목록 필터
V-45: GET /api/switch-recommendations/{rec_id} — 상세 반환
V-46: GET /api/switch-recommendations/999 — 404
V-47: POST /api/switch-recommendations/{rec_id}/approve — 승인 처리
V-48: POST /api/switch-recommendations/{rec_id}/reject — 거부 처리
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from unittest.mock import MagicMock

from adapters.database.models import (
    create_strategy_model,
    create_strategy_snapshot_model,
    create_switch_recommendation_model,
)
from adapters.database.session import Base
from api.routes.strategy_scores import router

# ── ORM 모델 (tt_ prefix) ────────────────────────────────────

TtStrategy = create_strategy_model("tt")
TtSnapshot = create_strategy_snapshot_model("tt")
TtSwitchRec = create_switch_recommendation_model("tt")


# ── Fixtures ─────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_session_factory():
    """SQLite 인메모리 — tt_ 테이블."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        target_tables = [
            t for name, t in Base.metadata.tables.items()
            if name.startswith("tt_") or name in ("strategy_techniques",)
        ]
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(sc, tables=target_tables)
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_app_state(factory):
    """라우터 테스트용 AppState 모의 객체."""
    state = MagicMock()
    state.session_factory = factory
    state.models.strategy = TtStrategy
    state.models.strategy_snapshot = TtSnapshot
    state.models.switch_recommendation = TtSwitchRec
    state.prefix = "tt"
    state.pair_column = "pair"
    state.normalize_pair = lambda p: p.lower()
    # 매니저 mock (unregister_paper_pair 호출 확인용)
    state.trend_manager.unregister_paper_pair = MagicMock()
    state.box_manager.unregister_paper_pair = MagicMock()
    return state


def _build_client(factory) -> TestClient:
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

    return TestClient(app, raise_server_exceptions=False)


# ── DB 헬퍼 ──────────────────────────────────────────────────

async def _insert_strategy(factory, *, status="active", name="test"):
    async with factory() as db:
        row = TtStrategy(
            name=name, description="desc",
            parameters={"pair": "usd_jpy", "trading_style": "trend_following"},
            rationale="테스트용 전략 최소 20자 이상 rationale",
            status=status,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def _insert_snapshot(factory, strategy_id: int, score: float = 0.7):
    async with factory() as db:
        row = TtSnapshot(
            strategy_id=strategy_id,
            pair="usd_jpy",
            trading_style="trend_following",
            trigger_type="T2_candle_close",
            snapshot_time=datetime.now(timezone.utc),
            score=Decimal(str(score)),
            readiness=Decimal(str(score)),
            edge=Decimal(str(score)),
            regime_fit=Decimal(str(score)),
            regime="ranging",
            confidence="medium",
            has_position=False,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def _insert_rec(
    factory,
    *,
    current_sid: int,
    recommended_sid: int,
    decision: str = "pending",
    score_ratio: float = 1.8,
):
    async with factory() as db:
        row = TtSwitchRec(
            trigger_type="T2_candle_close",
            triggered_at=datetime.now(timezone.utc),
            current_strategy_id=current_sid,
            current_score=Decimal("0.3"),
            recommended_strategy_id=recommended_sid,
            recommended_score=Decimal("0.54"),
            score_ratio=Decimal(str(score_ratio)),
            confidence="high",
            reason="Score 1.8배 초과",
            decision=decision,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


# ══════════════════════════════════════════════════════════════
# V-40: strategy-scores 빈 목록
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v40_strategy_scores_empty(db_session_factory):
    """active 전략 없으면 빈 scores 반환."""
    client = _build_client(db_session_factory)
    resp = client.get("/api/strategy-scores")
    assert resp.status_code == 200
    assert resp.json()["scores"] == []


# ══════════════════════════════════════════════════════════════
# V-41: strategy-scores/{id} 스냅샷 있으면 반환
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v41_strategy_score_by_id(db_session_factory):
    """스냅샷 있으면 score 필드 정상 반환."""
    sid = await _insert_strategy(db_session_factory, status="active")
    await _insert_snapshot(db_session_factory, sid, score=0.65)

    client = _build_client(db_session_factory)
    resp = client.get(f"/api/strategy-scores/{sid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["strategy_id"] == sid
    assert abs(data["score"] - 0.65) < 0.001


@pytest.mark.asyncio
async def test_v41b_strategy_score_by_id_not_found(db_session_factory):
    """스냅샷 없으면 404."""
    client = _build_client(db_session_factory)
    resp = client.get("/api/strategy-scores/9999")
    assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════
# V-42: strategy-snapshots/latest — active+proposed 전 전략
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v42_snapshots_latest(db_session_factory):
    """active + proposed 전략 각각 최신 스냅샷 반환."""
    sid1 = await _insert_strategy(db_session_factory, status="active", name="s1")
    sid2 = await _insert_strategy(db_session_factory, status="proposed", name="s2")
    await _insert_snapshot(db_session_factory, sid1, score=0.5)
    await _insert_snapshot(db_session_factory, sid2, score=0.8)

    client = _build_client(db_session_factory)
    resp = client.get("/api/strategy-snapshots/latest")
    assert resp.status_code == 200
    snaps = resp.json()["snapshots"]
    assert len(snaps) == 2
    ids_in_resp = {s["strategy_id"] for s in snaps}
    assert ids_in_resp == {sid1, sid2}


# ══════════════════════════════════════════════════════════════
# V-43: strategy-snapshots/{id} 이력
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v43_snapshots_history(db_session_factory):
    """특정 전략의 최근 7일 스냅샷 이력 반환."""
    sid = await _insert_strategy(db_session_factory, status="active")
    for score in [0.6, 0.65, 0.7]:
        await _insert_snapshot(db_session_factory, sid, score=score)

    client = _build_client(db_session_factory)
    resp = client.get(f"/api/strategy-snapshots/{sid}?days=7&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["strategy_id"] == sid
    assert len(data["snapshots"]) == 3


# ══════════════════════════════════════════════════════════════
# V-44: switch-recommendations?status=pending
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v44_list_recommendations_filter(db_session_factory):
    """status=pending 필터 정상 동작."""
    sid1 = await _insert_strategy(db_session_factory, status="active", name="a1")
    sid2 = await _insert_strategy(db_session_factory, status="proposed", name="p1")
    await _insert_rec(db_session_factory, current_sid=sid1, recommended_sid=sid2, decision="pending")
    await _insert_rec(db_session_factory, current_sid=sid1, recommended_sid=sid2, decision="rejected")

    client = _build_client(db_session_factory)
    resp = client.get("/api/switch-recommendations?status=pending")
    assert resp.status_code == 200
    recs = resp.json()["recommendations"]
    assert len(recs) == 1
    assert recs[0]["decision"] == "pending"


# ══════════════════════════════════════════════════════════════
# V-45: switch-recommendations/{rec_id} 상세
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v45_get_recommendation(db_session_factory):
    """추천 상세 조회."""
    sid1 = await _insert_strategy(db_session_factory, status="active", name="a1")
    sid2 = await _insert_strategy(db_session_factory, status="proposed", name="p1")
    rec_id = await _insert_rec(db_session_factory, current_sid=sid1, recommended_sid=sid2)

    client = _build_client(db_session_factory)
    resp = client.get(f"/api/switch-recommendations/{rec_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == rec_id
    assert data["decision"] == "pending"
    assert abs(data["score_ratio"] - 1.8) < 0.01


# ══════════════════════════════════════════════════════════════
# V-46: switch-recommendations/999 — 404
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v46_get_recommendation_not_found(db_session_factory):
    """존재하지 않는 rec_id → 404."""
    client = _build_client(db_session_factory)
    resp = client.get("/api/switch-recommendations/999")
    assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════
# V-47: POST /approve — 승인 처리
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v47_approve_recommendation(db_session_factory):
    """추천 승인 → decision=approved, decided_by 반영."""
    sid1 = await _insert_strategy(db_session_factory, status="active", name="a1")
    sid2 = await _insert_strategy(db_session_factory, status="proposed", name="p1")
    rec_id = await _insert_rec(db_session_factory, current_sid=sid1, recommended_sid=sid2)

    client = _build_client(db_session_factory)
    resp = client.post(
        f"/api/switch-recommendations/{rec_id}/approve",
        json={"decided_by": "soobo"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "approved"
    assert data["decided_by"] == "soobo"
    assert data["decided_at"] is not None


@pytest.mark.asyncio
async def test_v47b_approve_already_processed(db_session_factory):
    """이미 approved인 추천 재승인 → 400."""
    sid1 = await _insert_strategy(db_session_factory, status="active", name="a1")
    sid2 = await _insert_strategy(db_session_factory, status="proposed", name="p1")
    rec_id = await _insert_rec(
        db_session_factory, current_sid=sid1, recommended_sid=sid2, decision="approved"
    )

    client = _build_client(db_session_factory)
    resp = client.post(
        f"/api/switch-recommendations/{rec_id}/approve",
        json={"decided_by": "soobo"},
    )
    assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════
# V-48: POST /reject — 거부 처리
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v48_reject_recommendation(db_session_factory):
    """추천 거부 → decision=rejected, reject_reason 저장."""
    sid1 = await _insert_strategy(db_session_factory, status="active", name="a1")
    sid2 = await _insert_strategy(db_session_factory, status="proposed", name="p1")
    rec_id = await _insert_rec(db_session_factory, current_sid=sid1, recommended_sid=sid2)

    client = _build_client(db_session_factory)
    resp = client.post(
        f"/api/switch-recommendations/{rec_id}/reject",
        json={"decided_by": "rachel", "reject_reason": "시장 불안정으로 보류"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "rejected"
    assert data["decided_by"] == "rachel"
    assert data["reject_reason"] == "시장 불안정으로 보류"
