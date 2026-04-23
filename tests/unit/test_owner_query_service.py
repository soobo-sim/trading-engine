"""
P8 — OwnerQueryService + API 테스트.

OQ (Service): OQ-01~OQ-07
OA (API):     OA-01~OA-06
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database import owner_query_model  # noqa — Base 등록
from adapters.database.session import Base
from api.services.owner_query_service import (
    OwnerQueryService,
    infer_category,
)

JST = timezone(timedelta(hours=9))


# ── DB 픽스처 ────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        target = [Base.metadata.tables["owner_queries"]]
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
        target = [Base.metadata.tables["owner_queries"]]
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=target))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ── OQ: 서비스 단위 테스트 ────────────────────────────────────

class TestInferCategory:
    def test_no_trade_inferred(self):
        """OQ-01: '진입 없음' → no_trade."""
        assert infer_category("하루종일 거래 없음. 왜 진입이 안 되나?") == "no_trade"

    def test_regime_stuck_inferred(self):
        """OQ-02: '체제 전환' 키워드 → regime_stuck."""
        assert infer_category("왜 체제 전환이 안 되지?") == "regime_stuck"

    def test_general_fallback(self):
        """OQ-03: 키워드 없음 → general."""
        assert infer_category("안녕하세요 테스트 질문입니다.") == "general"


class TestOwnerQueryService:
    @pytest.mark.asyncio
    async def test_create_generates_id(self, db):
        """OQ-04: create() → OQ-YYYY-001 ID 생성."""
        svc = OwnerQueryService(db)
        q = await svc.create("하루종일 거래 없음. 왜 진입이 안 되나요?", priority="medium")
        assert q.id.startswith("OQ-")
        assert q.status == "open"

    @pytest.mark.asyncio
    async def test_create_infers_category(self, db):
        """OQ-05: category=None → 자동 추론."""
        svc = OwnerQueryService(db)
        q = await svc.create("하루종일 거래 없음. 왜 진입이 안 되나요?")
        assert q.category == "no_trade"

    @pytest.mark.asyncio
    async def test_close_requires_outcome(self, db):
        """OQ-06: outcome_summary 없으면 ValueError."""
        svc = OwnerQueryService(db)
        q = await svc.create("하루종일 거래 없음. 왜 진입이 안 되나요?")
        with pytest.raises(ValueError, match="outcome_summary"):
            await svc.close(q.id, cycle_id="CR-2026-001", outcome_summary="짧음")  # < 20자

    @pytest.mark.asyncio
    async def test_close_transitions_to_closed(self, db):
        """OQ-07: 정상 close → status=closed, closed_at 설정."""
        svc = OwnerQueryService(db)
        q = await svc.create("하루종일 거래 없음. 왜 진입이 안 되나요?")
        closed = await svc.close(
            q.id,
            cycle_id="CR-2026-001",
            outcome_summary="H-2026-007 가설로 연결 완료 — slope 파라미터 조정 예정",
        )
        assert closed.status == "closed"
        assert closed.closed_at is not None
        assert closed.addressed_in_cycle == "CR-2026-001"

    @pytest.mark.asyncio
    async def test_list_open_only(self, db):
        """OQ-08: list(status='open') → open 항목만."""
        svc = OwnerQueryService(db)
        q1 = await svc.create("하루종일 거래 없음. 왜 진입이 안 되나요?")
        await svc.create("왜 체제 전환이 이렇게 느리게 되나요?")
        await svc.close(
            q1.id, cycle_id="CR-2026-001",
            outcome_summary="로그 분석 결과 정상 범위 확인 — 변경 불필요로 판단",
        )
        total, rows = await svc.list(status="open")
        assert total == 1

    @pytest.mark.asyncio
    async def test_double_close_raises(self, db):
        """OQ-09: 이미 closed 항목 재close → ValueError."""
        svc = OwnerQueryService(db)
        q = await svc.create("하루종일 거래 없음. 왜 진입이 안 되나요?")
        await svc.close(
            q.id, cycle_id="CR-2026-001",
            outcome_summary="로그 분석 결과 정상 범위 확인 — 변경 불필요로 판단",
        )
        with pytest.raises(ValueError, match="already closed"):
            await svc.close(
                q.id, cycle_id="CR-2026-002",
                outcome_summary="또 다른 요약 내용인데 이렇게 처리됩니다",
            )


# ── OA: API 테스트 ────────────────────────────────────────────

def _oq_build_client(factory):
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


class TestOwnerQueryAPI:
    def test_create_201(self, db_factory):
        """OA-01: POST /api/owner-queries → 201."""
        client = _oq_build_client(db_factory)
        resp = client.post("/api/owner-queries", json={
            "content": "하루종일 거래 없음. 왜 진입이 안 되나요?",
            "priority": "high",
        })
        assert resp.status_code == 201
        assert resp.json()["status"] == "open"

    def test_create_too_short_400(self, db_factory):
        """OA-02: 10자 미만 content → 400/422."""
        client = _oq_build_client(db_factory)
        resp = client.post("/api/owner-queries", json={"content": "짧음"})
        assert resp.status_code in (400, 422)

    def test_list_returns_open(self, db_factory):
        """OA-03: GET /api/owner-queries → 200 + open 목록."""
        client = _oq_build_client(db_factory)
        client.post("/api/owner-queries", json={"content": "하루종일 거래 없음. 왜 진입이 안 되나요?"})
        resp = client.get("/api/owner-queries?status=open")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_close_200(self, db_factory):
        """OA-04: PATCH /close → 200 + status closed."""
        client = _oq_build_client(db_factory)
        create_resp = client.post("/api/owner-queries", json={
            "content": "하루종일 거래 없음. 왜 진입이 안 되나요?",
        })
        qid = create_resp.json()["id"]
        close_resp = client.patch(f"/api/owner-queries/{qid}/close", json={
            "cycle_id": "CR-2026-001",
            "outcome_summary": "H-2026-007 가설로 연결 완료 — slope 파라미터 조정 예정",
        })
        assert close_resp.status_code == 200
        assert close_resp.json()["status"] == "closed"

    def test_close_short_summary_400(self, db_factory):
        """OA-05: outcome_summary 20자 미만 → 400."""
        client = _oq_build_client(db_factory)
        create_resp = client.post("/api/owner-queries", json={"content": "하루종일 거래 없음. 왜 진입이 안 되나요?"})
        qid = create_resp.json()["id"]
        resp = client.patch(f"/api/owner-queries/{qid}/close", json={
            "cycle_id": "CR-2026-001",
            "outcome_summary": "짧음",  # < 20자 → Pydantic 검증 실패
        })
        assert resp.status_code in (400, 422)

    def test_category_auto_inferred(self, db_factory):
        """OA-06: category 미지정 → 자동 추론."""
        client = _oq_build_client(db_factory)
        resp = client.post("/api/owner-queries", json={
            "content": "하루종일 거래 없음. 왜 진입이 안 되나요?",
        })
        assert resp.status_code == 201
        assert resp.json()["category"] == "no_trade"
