"""
P3 — Lessons Recall + Advisory Validator 단위 테스트.

SC-01~SC-05: score() 점수 계산
CM-01~CM-03: _match_conditions() 조건 매칭
RC-01~RC-04: recall_lessons() 비동기 (DB 포함)
AV-01~AV-04: advisory_validator.validate_lesson_citations()
RA-01~RA-02: POST /api/lessons/recall API
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.lesson_model import Lesson  # noqa: F401 — Base 등록
from adapters.database.session import Base
from api.routes.evolution import router
from api.services.lessons_recall import (
    RecallContext,
    _match_conditions,
    recall_lessons,
    score,
)

JST = timezone(timedelta(hours=9))

# ── DB 픽스처 ───────────────────────────────────────────────


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
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
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        target = [t for name, t in Base.metadata.tables.items() if name == "lessons"]
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=target))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_lesson(
    db_session,
    *,
    pair="btc_jpy",
    market_regime="trending",
    confidence=0.7,
    conditions: dict | None = None,
    status="active",
    observation="EMA 기울기가 낮을 때 진입하면 손절이 빈번하게 발생한다는 교훈.",
    recommendation="EMA 기울기 0.05 이상일 때만 진입하는 것을 권고한다.",
    lesson_id: str | None = None,
) -> Lesson:
    import uuid
    short = lesson_id or f"L-2026-{str(uuid.uuid4())[:3]}"
    now = datetime.now(tz=JST).replace(tzinfo=None)
    l = Lesson(
        id=short,
        pair=pair,
        market_regime=market_regime,
        pattern_type="entry_condition",
        conditions=conditions or {},
        observation=observation,
        recommendation=recommendation,
        confidence=confidence,
        status=status,
        source="manual",
        updated_at=now,
        created_at=now,
    )
    db_session.add(l)
    return l


def _ctx(**overrides) -> RecallContext:
    defaults = dict(
        pair="btc_jpy",
        market_regime="trending",
        top_k=5,
    )
    defaults.update(overrides)
    return RecallContext(**defaults)


# ── SC: score() ─────────────────────────────────────────────


class TestScoring:
    def test_exact_pair_higher_than_any(self):
        """SC-01: pair 정확 일치가 any보다 점수 높음."""
        ctx = _ctx(pair="btc_jpy", market_regime="trending")

        l_exact = MagicMock(spec=Lesson)
        l_exact.pair = "btc_jpy"
        l_exact.market_regime = "trending"
        l_exact.conditions = {}
        l_exact.confidence = 0.7
        l_exact.updated_at = datetime.now().replace(tzinfo=None)

        l_any = MagicMock(spec=Lesson)
        l_any.pair = "any"
        l_any.market_regime = "trending"
        l_any.conditions = {}
        l_any.confidence = 0.7
        l_any.updated_at = datetime.now().replace(tzinfo=None)

        assert score(l_exact, ctx) > score(l_any, ctx)

    def test_regime_mismatch_zeros(self):
        """SC-02: 체제 불일치 → 0.0."""
        ctx = _ctx(market_regime="trending")

        l = MagicMock(spec=Lesson)
        l.pair = "btc_jpy"
        l.market_regime = "ranging"  # 불일치
        l.conditions = {}
        l.confidence = 0.9
        l.updated_at = datetime.now().replace(tzinfo=None)

        assert score(l, ctx) == 0.0

    def test_confidence_weighting(self):
        """SC-03: confidence 높을수록 점수 높음."""
        ctx = _ctx()

        def _l(conf):
            l = MagicMock(spec=Lesson)
            l.pair = "btc_jpy"
            l.market_regime = "trending"
            l.conditions = {}
            l.confidence = conf
            l.updated_at = datetime.now().replace(tzinfo=None)
            return l

        assert score(_l(0.9), ctx) > score(_l(0.3), ctx)

    def test_recency_bonus_within_30d(self):
        """SC-04: 30일 이내 lesson은 점수 보너스."""
        ctx = _ctx()

        now = datetime.now().replace(tzinfo=None)
        recent = MagicMock(spec=Lesson)
        recent.pair = "btc_jpy"
        recent.market_regime = "trending"
        recent.conditions = {}
        recent.confidence = 0.7
        recent.updated_at = now

        old = MagicMock(spec=Lesson)
        old.pair = "btc_jpy"
        old.market_regime = "trending"
        old.conditions = {}
        old.confidence = 0.7
        old.updated_at = now - timedelta(days=120)

        assert score(recent, ctx) > score(old, ctx)

    def test_pair_wrong_returns_zero(self):
        """SC-05: 다른 페어는 0.0."""
        ctx = _ctx(pair="btc_jpy")
        l = MagicMock(spec=Lesson)
        l.pair = "eth_jpy"
        l.market_regime = "trending"
        l.conditions = {}
        l.confidence = 1.0
        l.updated_at = datetime.now().replace(tzinfo=None)
        assert score(l, ctx) == 0.0


# ── CM: _match_conditions() ─────────────────────────────────


class TestConditionMatching:
    def test_no_conditions_returns_half(self):
        """CM-01: conditions 비어있으면 0.5 반환."""
        ctx = _ctx()
        assert _match_conditions({}, ctx) == 0.5

    def test_bb_width_in_range(self):
        """CM-02: bb_width 조건 일치 시 ratio=1.0."""
        ctx = _ctx(bb_width_pct=5.0)
        cond = {"bb_width_min": 3.0, "bb_width_max": 7.0}
        ratio = _match_conditions(cond, ctx)
        assert ratio == 1.0

    def test_partial_match_ratio(self):
        """CM-03: 일부만 일치 → 0.5 (2개 중 1개)."""
        ctx = _ctx(bb_width_pct=2.0)  # min=3.0 불충족
        cond = {"bb_width_min": 3.0, "bb_width_max": 7.0}
        ratio = _match_conditions(cond, ctx)
        assert ratio == 0.5


# ── RC: recall_lessons() ────────────────────────────────────


class TestRecallLessons:
    @pytest.mark.asyncio
    async def test_returns_top_k(self, db: AsyncSession):
        """RC-01: 여러 lesson 중 top_k만 반환."""
        for i in range(5):
            l = _make_lesson(db, confidence=0.5 + i * 0.05, lesson_id=f"L-2026-{100+i:03d}")
        await db.commit()

        ctx = _ctx(top_k=3)
        results = await recall_lessons(db, ctx)
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_excludes_deprecated(self, db: AsyncSession):
        """RC-02: deprecated lesson은 제외."""
        active = _make_lesson(db, status="active", lesson_id="L-2026-A01")
        dep = _make_lesson(db, status="deprecated", lesson_id="L-2026-D01")
        await db.commit()

        ctx = _ctx(top_k=10)
        results = await recall_lessons(db, ctx)
        ids = [l.id for l, _ in results]
        assert active.id in ids
        assert dep.id not in ids

    @pytest.mark.asyncio
    async def test_increments_reference_count(self, db: AsyncSession):
        """RC-03: recall 후 reference_count +1."""
        l = _make_lesson(db, lesson_id="L-2026-R01", confidence=0.8)
        await db.commit()

        ctx = _ctx(top_k=10)
        await recall_lessons(db, ctx)

        # DB에서 직접 확인
        from sqlalchemy import select
        row = (await db.execute(select(Lesson).where(Lesson.id == l.id))).scalar_one()
        assert row.reference_count == 1

    @pytest.mark.asyncio
    async def test_threshold_filters_low_score(self, db: AsyncSession):
        """RC-04: 점수가 임계값(0.3) 미만인 lesson은 제외 (다른 regime)."""
        # 이 lesson은 pair 일치하지만 regime 불일치 → score=0.0
        _make_lesson(
            db, market_regime="ranging", lesson_id="L-2026-X01",
        )
        await db.commit()

        ctx = _ctx(market_regime="trending", top_k=10)
        results = await recall_lessons(db, ctx)
        ids = [l.id for l, _ in results]
        assert "L-2026-X01" not in ids


# ── AV: advisory_validator ──────────────────────────────────


class TestAdvisoryValidator:
    def test_no_lessons_passes(self):
        """AV-01: recalled_lesson_ids 비어있으면 통과."""
        from api.services.advisory_validator import validate_lesson_citations
        validate_lesson_citations(reasoning="아무 내용이나", recalled_lesson_ids=[])

    def test_all_cited_passes(self):
        """AV-02: 모든 lesson이 인용되면 통과."""
        from api.services.advisory_validator import validate_lesson_citations
        validate_lesson_citations(
            reasoning="L-2026-001 검토 완료. L-2026-002 적용 제외.",
            recalled_lesson_ids=["L-2026-001", "L-2026-002"],
        )

    def test_missing_citation_raises_in_strict_mode(self, monkeypatch):
        """AV-03: strict 모드에서 미인용 → AdvisoryValidationError."""
        import api.services.advisory_validator as av
        monkeypatch.setattr(av, "_MODE", "strict")
        from api.services.advisory_validator import AdvisoryValidationError
        with pytest.raises(AdvisoryValidationError):
            av.validate_lesson_citations(
                reasoning="L-2026-001 인용함.",
                recalled_lesson_ids=["L-2026-001", "L-2026-002"],  # 002 누락
            )

    def test_warn_mode_does_not_raise(self, monkeypatch):
        """AV-04: warn 모드는 예외 없이 로그만."""
        import api.services.advisory_validator as av
        monkeypatch.setattr(av, "_MODE", "warn")
        # 예외 없이 통과해야 함
        av.validate_lesson_citations(
            reasoning="아무 인용도 없음.",
            recalled_lesson_ids=["L-2026-001"],
        )


# ── RA: Recall API ───────────────────────────────────────────


class TestRecallAPI:
    def test_recall_empty_db(self, db_factory):
        """RA-01: lesson 없으면 matched_count=0."""
        app = FastAPI()
        app.include_router(router)
        mock_state = MagicMock()
        mock_state.session_factory = db_factory
        app.state.app_state = mock_state

        client = TestClient(app)
        resp = client.post("/api/lessons/recall", json={
            "pair": "btc_jpy",
            "market_regime": "trending",
        })
        assert resp.status_code == 200
        assert resp.json()["matched_count"] == 0

    def test_recall_returns_matching_lessons(self, db_factory):
        """RA-02: 매칭되는 lesson 반환."""
        import asyncio

        async def _seed():
            async with db_factory() as s:
                now = datetime.now().replace(tzinfo=None)
                l = Lesson(
                    id="L-2026-S01",
                    pair="btc_jpy",
                    market_regime="trending",
                    pattern_type="entry_condition",
                    conditions={},
                    observation="EMA 기울기가 낮을 때 진입하면 손절이 빈번하게 발생한다.",
                    recommendation="EMA 기울기 0.05 이상일 때만 진입한다.",
                    confidence=0.8,
                    status="active",
                    source="manual",
                    created_at=now,
                    updated_at=now,
                )
                s.add(l)
                await s.commit()

        asyncio.get_event_loop().run_until_complete(_seed())

        app = FastAPI()
        app.include_router(router)
        mock_state = MagicMock()
        mock_state.session_factory = db_factory
        app.state.app_state = mock_state

        client = TestClient(app)
        resp = client.post("/api/lessons/recall", json={
            "pair": "btc_jpy",
            "market_regime": "trending",
            "top_k": 5,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_count"] >= 1
        assert data["lessons"][0]["id"] == "L-2026-S01"
