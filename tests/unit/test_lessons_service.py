"""
P2 — Lessons 서비스 + API 단위 테스트.

LI-01~LI-03: LessonsService._next_id — ID 발급 규칙
LS-01~LS-08: LessonsService CRUD
LA-01~LA-07: API 엔드포인트 (TestClient)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.lesson_model import Lesson  # noqa: F401 — Base 등록 필수
from adapters.database.session import Base
from api.routes.evolution import router
from api.schemas.evolution import LessonCreate, LessonUpdate
from api.services.lessons_service import LessonsService

JST = timezone(timedelta(hours=9))

# ── DB 픽스처 ───────────────────────────────────────────────


@pytest_asyncio.fixture
async def db():
    """SQLite 인메모리 DB (lessons 테이블만)."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        target = [t for name, t in Base.metadata.tables.items() if name == "lessons"]
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=target))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def db_factory():
    """TestClient 용 세션 팩토리."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        target = [t for name, t in Base.metadata.tables.items() if name == "lessons"]
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=target))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _build_client(factory) -> TestClient:
    from api.dependencies import get_db

    _app = FastAPI()
    _app.include_router(router)

    mock_state = MagicMock()
    mock_state.session_factory = factory

    _app.state.app_state = mock_state
    return TestClient(_app)


# ── LI: ID 발급 ─────────────────────────────────────────────


class TestLessonIdGeneration:
    @pytest.mark.asyncio
    async def test_first_id_is_001(self, db: AsyncSession):
        """LI-01: 레코드 없을 때 첫 ID = L-{년}-001."""
        svc = LessonsService(db)
        generated = await svc._next_id()
        year = datetime.now(tz=JST).year
        assert generated == f"L-{year}-001"

    @pytest.mark.asyncio
    async def test_increments_sequentially(self, db: AsyncSession):
        """LI-02: 연속 생성 시 001 → 002."""
        svc = LessonsService(db)
        p = _base_payload()
        await svc.create(p)
        second_id = await svc._next_id()
        year = datetime.now(tz=JST).year
        assert second_id == f"L-{year}-002"

    @pytest.mark.asyncio
    async def test_ignores_other_prefix(self, db: AsyncSession):
        """LI-03: 다른 연도 프리픽스 레코드는 카운트에 영향 없음."""
        # 수동으로 다른 연도 레코드 삽입
        old = Lesson(
            id="L-2020-099",
            pattern_type="meta",
            observation="x" * 20,
            recommendation="y" * 20,
            conditions={},
        )
        db.add(old)
        await db.commit()

        svc = LessonsService(db)
        year = datetime.now(tz=JST).year
        next_id = await svc._next_id()
        assert next_id == f"L-{year}-001"


# ── LS: 서비스 CRUD ─────────────────────────────────────────


def _base_payload(**overrides) -> LessonCreate:
    defaults = dict(
        pattern_type="entry_condition",
        observation="EMA 기울기가 낮을 때 진입하면 손절 빈번.",
        recommendation="EMA 기울기 최소 0.05 이상일 때만 진입한다.",
        conditions={"ema_slope_min": 0.05},
    )
    defaults.update(overrides)
    return LessonCreate(**defaults)


class TestLessonsService:
    @pytest.mark.asyncio
    async def test_create_basic(self, db: AsyncSession):
        """LS-01: 기본 생성 → id 발급, status=active."""
        svc = LessonsService(db)
        lesson = await svc.create(_base_payload())
        assert lesson.id.startswith("L-")
        assert lesson.status == "active"
        assert lesson.confidence == 0.5

    @pytest.mark.asyncio
    async def test_create_invalid_pattern_type(self, db: AsyncSession):
        """LS-02: 잘못된 pattern_type → ValueError."""
        svc = LessonsService(db)
        with pytest.raises((ValueError, Exception)):
            await svc.create(_base_payload(pattern_type="invalid_type"))  # type: ignore

    @pytest.mark.asyncio
    async def test_get_returns_lesson(self, db: AsyncSession):
        """LS-03: create 후 get → 동일 레코드."""
        svc = LessonsService(db)
        created = await svc.create(_base_payload())
        fetched = await svc.get(created.id)
        assert fetched is not None
        assert fetched.id == created.id

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, db: AsyncSession):
        """LS-04: status 필터 — deprecated 레코드는 active 필터에서 제외."""
        svc = LessonsService(db)
        l1 = await svc.create(_base_payload())
        _l2 = await svc.create(_base_payload())
        await svc.delete(l1.id)   # l1 → deprecated

        total, rows = await svc.list(status="active")
        ids = [r.id for r in rows]
        assert l1.id not in ids
        assert total == 1

    @pytest.mark.asyncio
    async def test_list_filter_by_regime_includes_any(self, db: AsyncSession):
        """LS-05: market_regime 필터 시 "any" 레코드도 포함."""
        svc = LessonsService(db)
        specific = await svc.create(_base_payload(market_regime="trending"))
        any_lesson = await svc.create(_base_payload(market_regime="any"))

        total, rows = await svc.list(market_regime="trending")
        ids = [r.id for r in rows]
        assert specific.id in ids
        assert any_lesson.id in ids

    @pytest.mark.asyncio
    async def test_list_ordered_by_confidence_desc(self, db: AsyncSession):
        """LS-06: confidence 높은 순 정렬."""
        svc = LessonsService(db)
        await svc.create(_base_payload(confidence=0.3))
        await svc.create(_base_payload(confidence=0.9))
        await svc.create(_base_payload(confidence=0.6))

        _total, rows = await svc.list()
        confidences = [r.confidence for r in rows]
        assert confidences == sorted(confidences, reverse=True)

    @pytest.mark.asyncio
    async def test_update_partial(self, db: AsyncSession):
        """LS-07: 부분 업데이트 — confidence만 변경."""
        svc = LessonsService(db)
        lesson = await svc.create(_base_payload())
        updated = await svc.update(lesson.id, LessonUpdate(confidence=0.9))
        assert updated is not None
        assert updated.confidence == 0.9
        assert updated.observation == lesson.observation   # 변경 안됨

    @pytest.mark.asyncio
    async def test_update_superseded_requires_valid_target(self, db: AsyncSession):
        """LS-08: superseded_by에 존재하지 않는 ID → ValueError."""
        svc = LessonsService(db)
        lesson = await svc.create(_base_payload())
        with pytest.raises(ValueError, match="not found"):
            await svc.update(
                lesson.id,
                LessonUpdate(status="superseded", superseded_by="L-9999-999"),
            )

    @pytest.mark.asyncio
    async def test_delete_soft(self, db: AsyncSession):
        """LS-09: delete → status=deprecated, 물리 삭제 없음."""
        svc = LessonsService(db)
        lesson = await svc.create(_base_payload())
        result = await svc.delete(lesson.id)
        assert result is True
        still_exists = await svc.get(lesson.id)
        assert still_exists is not None
        assert still_exists.status == "deprecated"

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, db: AsyncSession):
        """LS-10: 없는 ID delete → False."""
        svc = LessonsService(db)
        assert await svc.delete("L-0000-000") is False

    @pytest.mark.asyncio
    async def test_stats_returns_breakdown(self, db: AsyncSession):
        """LS-11: stats() — by_status / by_pattern_type 포함."""
        svc = LessonsService(db)
        await svc.create(_base_payload(pattern_type="entry_condition"))
        await svc.create(_base_payload(pattern_type="exit_condition"))
        l3 = await svc.create(_base_payload(pattern_type="entry_condition"))
        await svc.delete(l3.id)

        stats = await svc.stats()
        assert stats["by_status"]["active"] == 2
        assert stats["by_status"]["deprecated"] == 1
        assert stats["by_pattern_type"]["entry_condition"] >= 1


# ── LA: API 엔드포인트 ──────────────────────────────────────


class TestLessonsAPI:
    def test_post_create_201(self, db_factory):
        """LA-01: POST /api/lessons → 201 + id."""
        client = _build_client(db_factory)
        resp = client.post("/api/lessons", json={
            "pattern_type": "entry_condition",
            "observation": "EMA 기울기가 낮을 때 진입하면 손절이 빈번하게 발생한다.",
            "recommendation": "EMA 기울기 0.05 이상일 때만 진입해야 한다.",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"].startswith("L-")
        assert data["status"] == "active"

    def test_post_validates_short_observation(self, db_factory):
        """LA-02: observation 20자 미만 → 422."""
        client = _build_client(db_factory)
        resp = client.post("/api/lessons", json={
            "pattern_type": "meta",
            "observation": "짧음",
            "recommendation": "이것은 긴 추천 문자열입니다 잘 작동합니다 테스트중.",
        })
        assert resp.status_code == 422

    def test_get_list_returns_lessons(self, db_factory):
        """LA-03: GET /api/lessons → 생성된 lesson 포함."""
        client = _build_client(db_factory)
        client.post("/api/lessons", json={
            "pattern_type": "regime_transition",
            "observation": "ranging에서 trending 전환 시 진입 시도 빈번하게 실패한다.",
            "recommendation": "체제 확정 3캔들 후에만 진입할 것.",
        })
        resp = client.get("/api/lessons")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_get_single(self, db_factory):
        """LA-04: GET /api/lessons/{id} → 단일 조회."""
        client = _build_client(db_factory)
        created = client.post("/api/lessons", json={
            "pattern_type": "risk_management",
            "observation": "포지션 크기 과대 시 심리적 압박으로 조기 청산 빈번 발생.",
            "recommendation": "최대 포지션 0.5 BTJ 제한을 유지한다.",
        }).json()
        resp = client.get(f"/api/lessons/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]

    def test_get_single_404(self, db_factory):
        """LA-05: 없는 ID → 404."""
        client = _build_client(db_factory)
        resp = client.get("/api/lessons/L-0000-000")
        assert resp.status_code == 404

    def test_patch_lesson(self, db_factory):
        """LA-06: PATCH /api/lessons/{id} → confidence 업데이트."""
        client = _build_client(db_factory)
        created = client.post("/api/lessons", json={
            "pattern_type": "macro_context",
            "observation": "FNG 25 이하 극공포 구간 진입 후 손실 확률이 높다.",
            "recommendation": "FNG 30 미만 시 포지션 크기를 절반으로 줄인다.",
        }).json()
        resp = client.patch(f"/api/lessons/{created['id']}", json={"confidence": 0.85})
        assert resp.status_code == 200
        assert resp.json()["confidence"] == 0.85

    def test_delete_returns_204(self, db_factory):
        """LA-07: DELETE → 204, 이후 GET에서 deprecated."""
        client = _build_client(db_factory)
        created = client.post("/api/lessons", json={
            "pattern_type": "workflow_process",
            "observation": "4H 캔들 교체 직후 advisory 요청 시 데이터 불일치 발생.",
            "recommendation": "4H 캔들 교체 후 5분 쿨링 후에 advisory를 요청한다.",
        }).json()
        del_resp = client.delete(f"/api/lessons/{created['id']}")
        assert del_resp.status_code == 204
        get_resp = client.get(f"/api/lessons/{created['id']}")
        assert get_resp.json()["status"] == "deprecated"

    def test_stats_endpoint(self, db_factory):
        """LA-08: GET /api/lessons/stats → by_status / by_pattern_type."""
        client = _build_client(db_factory)
        resp = client.get("/api/lessons/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "by_status" in data
        assert "by_pattern_type" in data
