"""
P7 — LessonDecayService + monthly-meta API 테스트.

LD (Lesson Decay): LD-01~LD-06
MA (API):          MA-01~MA-04
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from api.services.lesson_decay_service import (
    LessonDecayService,
    DEPRECATE_THRESHOLD,
    DECAY_RULES,
    format_decay_report,
    DecayReport,
)

JST = timezone(timedelta(hours=9))


# ── 헬퍼 ────────────────────────────────────────────────────

def _make_lesson(
    lesson_id: str,
    confidence: float,
    days_since_ref: int,
    status: str = "active",
    last_decay_days: int | None = None,
):
    lesson = MagicMock()
    lesson.id = lesson_id
    lesson.confidence = confidence
    lesson.status = status
    now = datetime.now(tz=JST)
    lesson.created_at = now - timedelta(days=days_since_ref)
    lesson.last_referenced_at = now - timedelta(days=days_since_ref)
    lesson.last_decay_at = (now - timedelta(days=last_decay_days)) if last_decay_days is not None else None
    return lesson


def _make_db_with_lessons(lessons, hyps=None):
    db = AsyncMock()
    _call_count = [0]

    async def _execute(stmt, params=None):
        result = MagicMock()
        _call_count[0] += 1
        # 첫 번째 호출 = lessons, 두 번째 = hypotheses
        if _call_count[0] == 1:
            result.scalars.return_value.all.return_value = lessons
        else:
            result.scalars.return_value.all.return_value = hyps or []
        result.scalar.return_value = None
        return result

    db.execute = _execute
    db.commit = AsyncMock()
    return db


# ── LD: 감쇠 로직 ────────────────────────────────────────────

class TestDecayRules:
    def test_decay_rules_ordered_by_threshold(self):
        """LD-01: DECAY_RULES가 큰 임계값(더 엄격한)이 먼저."""
        thresholds = [r[0] for r in DECAY_RULES]
        assert thresholds[0] >= thresholds[-1]

    def test_deprecate_threshold_is_positive(self):
        """LD-02: DEPRECATE_THRESHOLD > 0."""
        assert 0 < DEPRECATE_THRESHOLD < 1.0


class TestLessonDecayService:
    @pytest.mark.asyncio
    async def test_active_90day_lesson_decayed(self):
        """LD-03: 90일 이상 미참조 → 감쇠 발생."""
        lesson = _make_lesson("L-2026-001", confidence=0.8, days_since_ref=100)
        db = _make_db_with_lessons([lesson], hyps=[])
        svc = LessonDecayService(db)
        report = await svc.run()
        assert len(report.decayed) == 1
        assert report.decayed[0][0] == "L-2026-001"
        assert report.decayed[0][2] < 0.8  # confidence 감소

    @pytest.mark.asyncio
    async def test_recent_lesson_not_decayed(self):
        """LD-04: 60일 미만 참조 → 감쇠 안 함."""
        lesson = _make_lesson("L-2026-002", confidence=0.8, days_since_ref=30)
        db = _make_db_with_lessons([lesson], hyps=[])
        svc = LessonDecayService(db)
        report = await svc.run()
        assert len(report.decayed) == 0

    @pytest.mark.asyncio
    async def test_low_confidence_becomes_deprecated(self):
        """LD-05: 감쇠 후 confidence < 0.3 → deprecated."""
        lesson = _make_lesson("L-2026-003", confidence=0.31, days_since_ref=200)
        db = _make_db_with_lessons([lesson], hyps=[])
        svc = LessonDecayService(db)
        report = await svc.run()
        assert "L-2026-003" in report.deprecated
        assert lesson.status == "deprecated"

    @pytest.mark.asyncio
    async def test_already_decayed_this_cycle_skipped(self):
        """LD-06: last_decay_at 10일 이내 → skip."""
        lesson = _make_lesson("L-2026-004", confidence=0.8, days_since_ref=200, last_decay_days=10)
        db = _make_db_with_lessons([lesson], hyps=[])
        svc = LessonDecayService(db)
        report = await svc.run()
        assert len(report.decayed) == 0  # skip됨

    def test_format_decay_report(self):
        """LD-07: format_decay_report 형식 확인."""
        report = DecayReport(
            run_at=datetime.now(tz=JST),
            decayed=[("L-001", 0.8, 0.72)],
            deprecated=["L-002"],
            archived_hypotheses=["H-2026-001"],
            total_active_after=20,
        )
        text = format_decay_report(report)
        assert "감쇠: 1건" in text
        assert "deprecated: 1건" in text


# ── MA: Monthly Meta API ─────────────────────────────────────

@pytest_asyncio.fixture
async def meta_db_factory():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from adapters.database import hypothesis_model, lesson_model  # noqa
    from adapters.database.session import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        wanted = {"lessons", "hypotheses"}
        target = [t for name, t in Base.metadata.tables.items() if name in wanted]
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=target))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _meta_build_client(factory):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.evolution import router
    from api.dependencies import get_db

    app = FastAPI()
    app.include_router(router)

    async def _override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app, raise_server_exceptions=False)


class TestMonthlyMetaAPI:
    def test_decay_stale_returns_200(self, meta_db_factory):
        """MA-01: POST /api/lessons/decay-stale → 200."""
        client = _meta_build_client(meta_db_factory)
        resp = client.post("/api/lessons/decay-stale")
        assert resp.status_code == 200
        data = resp.json()
        assert "decayed" in data
        assert "deprecated" in data

    def test_audit_tunables_returns_200(self, meta_db_factory):
        """MA-02: GET /api/tunables/audit → 200."""
        client = _meta_build_client(meta_db_factory)
        resp = client.get("/api/tunables/audit")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_tunables" in data
        assert "unused_count" in data

    def test_evolution_health_returns_200(self, meta_db_factory):
        """MA-03: GET /api/evolution/health → 200."""
        client = _meta_build_client(meta_db_factory)
        resp = client.get("/api/evolution/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "cycles_total" in data
        assert "adoption_rate" in data

    def test_tunables_audit_since_days_query(self, meta_db_factory):
        """MA-04: since_days 파라미터 반영."""
        client = _meta_build_client(meta_db_factory)
        resp = client.get("/api/tunables/audit?since_days=30")
        assert resp.status_code == 200
        assert resp.json()["period_days"] == 30
