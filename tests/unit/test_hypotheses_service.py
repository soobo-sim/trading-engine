"""
P4 — Hypotheses 생애주기 + API 테스트.

LC (Lifecycle Transitions): HT-01~HT-14
EC (Escalation Track):       ET-01~ET-05
SE (Service extras):         SV-01~SV-04
HA (API):                    HA-01~HA-08
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database import hypothesis_model  # noqa: F401 — Base 등록
from adapters.database import lesson_model  # noqa: F401
from adapters.database.hypothesis_model import Hypothesis
from adapters.database.session import Base
from api.routes.evolution import router
from api.services.hypotheses_service import HypothesesService
from api.schemas.evolution import HypothesisCreate, HypothesisTransition, TunableChange
from core.judge.evolution.lifecycle import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    validate_transition,
    check_promotion_to_paper,
    check_promotion_to_canary,
    check_promotion_to_adopted,
)
from core.shared.tunable_registry import register_all
from core.shared.tunable_catalog import TunableCatalog

JST = timezone(timedelta(hours=9))


@pytest.fixture(autouse=True)
def populate_catalog():
    """모든 테스트에서 TunableCatalog가 등록돼 있어야 함."""
    register_all()
    yield

# ── DB 픽스처 ───────────────────────────────────────────────

ENGINE_OPTS = dict(connect_args={"check_same_thread": False})


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", **ENGINE_OPTS)
    async with engine.begin() as conn:
        target = [
            t for name, t in Base.metadata.tables.items()
            if name in ("lessons", "hypotheses")
        ]
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=target))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", **ENGINE_OPTS)
    async with engine.begin() as conn:
        target = [
            t for name, t in Base.metadata.tables.items()
            if name in ("lessons", "hypotheses")
        ]
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=target))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _auto_change() -> TunableChange:
    """autonomy=auto Tunable 변경 (standard track)."""
    return TunableChange(
        tunable_key="trend.ema_slope_entry_min",
        current_value=0.05,
        proposed_value=0.07,
        rationale="최근 30일 진입 6건 중 4건 손실, 모두 slope < 0.07",
    )


def _escalation_change() -> TunableChange:
    """autonomy=escalation Tunable 변경 (escalation track 강제)."""
    return TunableChange(
        tunable_key="gate.kill_consec_loss_threshold",
        current_value=5,
        proposed_value=3,
        rationale="연속 손실 임계값을 보수적으로 낮춰 포지션 조기 청산",
    )


async def _create_standard(svc: HypothesesService) -> Hypothesis:
    payload = HypothesisCreate(
        title="EMA slope 기준 상향 테스트",
        description="ema_slope_entry_min을 0.05 → 0.07로 상향해 손실 빈도를 낮추는 가설이다.",
        changes=[_auto_change()],
        proposer="alice",
    )
    return await svc.create(payload)


async def _create_escalation(svc: HypothesesService) -> Hypothesis:
    payload = HypothesisCreate(
        title="Kill 임계값 조정",
        description="연속 손실 임계값을 5→3으로 낮춰 연속 손실 손상을 최소화하려는 가설이다.",
        changes=[_escalation_change()],
        proposer="rachel",
    )
    return await svc.create(payload)


# ── LC: 생애주기 유닛 테스트 ──────────────────────────────────

class TestLifecycleMethods:
    def test_validate_transition_allowed(self):
        """HT-01: proposed → backtested 허용."""
        validate_transition("proposed", "backtested")  # no exception

    def test_validate_transition_invalid_raises(self):
        """HT-02: proposed → canary는 불가."""
        with pytest.raises(ValueError, match="not allowed"):
            validate_transition("proposed", "canary")

    def test_terminal_states_have_no_transitions(self):
        """HT-03: terminal 상태(adopted/rejected/archived)는 전이 없음."""
        for s in ("adopted", "rejected", "archived"):
            assert not ALLOWED_TRANSITIONS[s]

    def test_check_paper_ok(self):
        """HT-04: 정상 backtest_result → paper 승격 허용."""
        h = MagicMock()
        h.backtest_result = {"trades": 50, "sharpe": 1.5}
        h.baseline_metrics = {"sharpe": 1.0}
        check_promotion_to_paper(h)  # no exception

    def test_check_paper_insufficient_trades(self):
        """HT-05: backtest trades < 30 → ValueError."""
        h = MagicMock()
        h.backtest_result = {"trades": 10, "sharpe": 1.5}
        h.baseline_metrics = {"sharpe": 1.0}
        with pytest.raises(ValueError, match="< 30"):
            check_promotion_to_paper(h)

    def test_check_paper_sharpe_below_baseline(self):
        """HT-06: sharpe < baseline → ValueError."""
        h = MagicMock()
        h.backtest_result = {"trades": 40, "sharpe": 0.8}
        h.baseline_metrics = {"sharpe": 1.0}
        with pytest.raises(ValueError, match="sharpe"):
            check_promotion_to_paper(h)

    def test_check_canary_ok(self):
        """HT-07: 정상 paper_result → canary 승격 허용."""
        h = MagicMock()
        h.paper_result = {"trades": 10, "win_rate": 0.6}
        h.baseline_metrics = {"win_rate": 0.6}
        check_promotion_to_canary(h)

    def test_check_canary_insufficient_trades(self):
        """HT-08: paper trades < 5 → ValueError."""
        h = MagicMock()
        h.paper_result = {"trades": 3, "win_rate": 0.7}
        h.baseline_metrics = {"win_rate": 0.6}
        with pytest.raises(ValueError, match="< 5"):
            check_promotion_to_canary(h)

    def test_check_adopted_ok(self):
        """HT-09: rollback_triggered=False, trades≥3, sharpe OK → adopted 허용."""
        h = MagicMock()
        h.canary_result = {"rollback_triggered": False, "trades": 5, "sharpe": 1.0}
        h.baseline_metrics = {"sharpe": 1.0}
        check_promotion_to_adopted(h)

    def test_check_adopted_rollback_triggered(self):
        """HT-10: rollback_triggered=True → ValueError."""
        h = MagicMock()
        h.canary_result = {"rollback_triggered": True, "trades": 5, "sharpe": 1.2}
        h.baseline_metrics = {"sharpe": 1.0}
        with pytest.raises(ValueError, match="rollback_triggered"):
            check_promotion_to_adopted(h)


# ── Service 통합 테스트 ─────────────────────────────────────

class TestHypothesesService:
    @pytest.mark.asyncio
    async def test_create_standard_track(self, db: AsyncSession):
        """HT-11: auto tunable → standard track."""
        svc = HypothesesService(db)
        h = await _create_standard(svc)
        assert h.status == "proposed"
        assert h.track == "standard"
        assert h.expires_at is None

    @pytest.mark.asyncio
    async def test_proposed_to_backtested(self, db: AsyncSession):
        """HT-12: proposed → backtested + backtest_result 저장."""
        svc = HypothesesService(db)
        h = await _create_standard(svc)
        h2 = await svc.transition(
            h.id, "backtested",
            actor="alice",
            payload={"backtest_result": {
                "trades": 55, "win_rate": 0.62, "sharpe": 1.45, "total_pnl_jpy": 38000,
            }},
        )
        assert h2.status == "backtested"
        assert h2.backtest_result["trades"] == 55

    @pytest.mark.asyncio
    async def test_backtested_to_paper(self, db: AsyncSession):
        """HT-13: backtested → paper (standard, sharpe≥baseline)."""
        svc = HypothesesService(db)
        h = await _create_standard(svc)
        # baseline_metrics 직접 세팅 (업데이트 없이 DB에서)
        h.baseline_metrics = {"sharpe": 1.0, "win_rate": 0.58}
        await db.commit()

        await svc.transition(h.id, "backtested", actor="alice",
                             payload={"backtest_result": {"trades": 40, "sharpe": 1.3}})
        h3 = await svc.transition(h.id, "paper", actor="alice")
        assert h3.status == "paper"

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self, db: AsyncSession):
        """HT-14: 불허 전이 → ValueError."""
        svc = HypothesesService(db)
        h = await _create_standard(svc)
        with pytest.raises(ValueError, match="not allowed"):
            await svc.transition(h.id, "adopted", actor="alice")


# ── ET: Escalation 트랙 ──────────────────────────────────────

class TestEscalationTrack:
    @pytest.mark.asyncio
    async def test_create_with_escalation_tunable_sets_track(self, db: AsyncSession):
        """ET-01: escalation autonomy → track=escalation + expires_at 세팅."""
        svc = HypothesesService(db)
        h = await _create_escalation(svc)
        assert h.track == "escalation"
        assert h.expires_at is not None

    @pytest.mark.asyncio
    async def test_expires_at_roughly_7_days(self, db: AsyncSession):
        """ET-02: expires_at ≈ now+7일."""
        svc = HypothesesService(db)
        h = await _create_escalation(svc)
        assert h.expires_at is not None
        # SQLite returns naive datetimes; normalize to UTC for comparison
        exp = h.expires_at
        if exp.tzinfo is not None:
            exp = exp.replace(tzinfo=None)
        now_naive = datetime.now()
        diff = (exp - now_naive).total_seconds()
        # 7일(초) ± 60초 허용
        assert abs(diff - 7 * 86400) < 60

    @pytest.mark.asyncio
    async def test_escalation_canary_requires_sub_actor(self, db: AsyncSession):
        """ET-03: escalation → canary는 sub* actor만 허용."""
        svc = HypothesesService(db)
        h = await _create_escalation(svc)
        # proposed → backtested
        await svc.transition(h.id, "backtested", actor="rachel",
                             payload={"backtest_result": {"trades": 40, "sharpe": 1.3}})
        # backtested → paper (escalation 이므로 sharpe 검증 없음)
        await svc.transition(h.id, "paper", actor="rachel")
        # paper → canary with non-sub actor → PermissionError
        with pytest.raises(PermissionError, match="sub"):
            await svc.transition(h.id, "canary", actor="rachel")

    @pytest.mark.asyncio
    async def test_escalation_canary_ok_with_sub_actor(self, db: AsyncSession):
        """ET-04: sub* actor면 escalation canary 승격 허용."""
        svc = HypothesesService(db)
        h = await _create_escalation(svc)
        await svc.transition(h.id, "backtested", actor="rachel",
                             payload={"backtest_result": {"trades": 40, "sharpe": 1.3}})
        await svc.transition(h.id, "paper", actor="rachel")
        h_c = await svc.transition(h.id, "canary", actor="sub_owner")
        assert h_c.status == "canary"
        assert h_c.approver == "sub_owner"

    @pytest.mark.asyncio
    async def test_expire_overdue_marks_rejected(self, db: AsyncSession):
        """ET-05: expires_at 지난 escalation → expire_overdue() → rejected."""
        svc = HypothesesService(db)
        h = await _create_escalation(svc)
        # 강제로 expires_at을 과거로 변경
        h.expires_at = datetime.now(tz=JST) - timedelta(days=1)
        await db.commit()

        expired = await svc.expire_overdue()
        assert h.id in expired

        refreshed = await svc.get(h.id)
        assert refreshed.status == "rejected"
        assert "도과" in (refreshed.rejection_reason or "")


# ── SV: 서비스 추가 기능 테스트 ──────────────────────────────

class TestHypothesesServiceExtras:
    @pytest.mark.asyncio
    async def test_stats_counts(self, db: AsyncSession):
        """SV-01: stats() 카운트 정확."""
        svc = HypothesesService(db)
        await _create_standard(svc)
        await _create_standard(svc)
        data = await svc.stats()
        assert data["total"] == 2
        assert data["by_status"]["proposed"] == 2

    @pytest.mark.asyncio
    async def test_list_status_filter(self, db: AsyncSession):
        """SV-02: list() status 필터."""
        svc = HypothesesService(db)
        h = await _create_standard(svc)
        await svc.transition(h.id, "rejected", actor="alice", payload={"reason": "테스트"})
        total, rows = await svc.list(status="rejected")
        assert total == 1
        assert rows[0].status == "rejected"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self, db: AsyncSession):
        """SV-03: 없는 ID → None."""
        svc = HypothesesService(db)
        assert await svc.get("H-9999-000") is None

    @pytest.mark.asyncio
    async def test_rejected_is_terminal(self, db: AsyncSession):
        """SV-04: rejected 이후 전이 → ValueError."""
        svc = HypothesesService(db)
        h = await _create_standard(svc)
        await svc.transition(h.id, "rejected", actor="alice", payload={"reason": "x"})
        with pytest.raises(ValueError):
            await svc.transition(h.id, "proposed", actor="alice")


# ── HA: API 테스트 ───────────────────────────────────────────

def _build_client(db_factory) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    mock_state = MagicMock()
    mock_state.session_factory = db_factory
    app.state.app_state = mock_state
    return TestClient(app)


class TestHypothesesAPI:
    def test_create_hypothesis_201(self, db_factory):
        """HA-01: POST /api/hypotheses → 201."""
        client = _build_client(db_factory)
        resp = client.post("/api/hypotheses", json={
            "title": "EMA slope 상향 검증",
            "description": "ema_slope_entry_min 0.05→0.07로 변경하여 손절 빈도를 낮추는 가설이다.",
            "changes": [{
                "tunable_key": "trend.ema_slope_entry_min",
                "current_value": 0.05,
                "proposed_value": 0.07,
                "rationale": "최근 30일 진입 6건 중 4건 손실",
            }],
            "proposer": "alice",
        })
        assert resp.status_code == 201
        assert resp.json()["status"] == "proposed"
        assert resp.json()["track"] == "standard"

    def test_create_unknown_tunable_422(self, db_factory):
        """HA-02: 알 수 없는 tunable_key → 422."""
        client = _build_client(db_factory)
        resp = client.post("/api/hypotheses", json={
            "title": "Unknown key test",
            "description": "존재하지 않는 tunable_key를 사용하는 가설이다.",
            "changes": [{
                "tunable_key": "no_such.key",
                "current_value": 1,
                "proposed_value": 2,
                "rationale": "테스트용 잘못된 키",
            }],
            "proposer": "alice",
        })
        assert resp.status_code == 422

    def test_get_hypothesis_200(self, db_factory):
        """HA-03: GET /api/hypotheses/{id} → 200."""
        client = _build_client(db_factory)
        created = client.post("/api/hypotheses", json={
            "title": "조회 테스트 가설",
            "description": "단순 조회 목적의 가설이다. 내용은 중요하지 않다. 테스트용.",
            "changes": [{
                "tunable_key": "trend.ema_slope_entry_min",
                "current_value": 0.05,
                "proposed_value": 0.06,
                "rationale": "소폭 상향 테스트용 변경 사유",
            }],
            "proposer": "alice",
        }).json()
        resp = client.get(f"/api/hypotheses/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]

    def test_get_hypothesis_404(self, db_factory):
        """HA-04: 없는 ID → 404."""
        client = _build_client(db_factory)
        assert client.get("/api/hypotheses/H-9999-000").status_code == 404

    def test_list_hypotheses(self, db_factory):
        """HA-05: GET /api/hypotheses 목록 반환."""
        client = _build_client(db_factory)
        for _ in range(3):
            client.post("/api/hypotheses", json={
                "title": "목록 테스트 가설",
                "description": "목록 조회를 위한 테스트 가설이다. 반복 등록한다.",
                "changes": [{
                    "tunable_key": "trend.ema_slope_entry_min",
                    "current_value": 0.05,
                    "proposed_value": 0.07,
                    "rationale": "목록 테스트용 변경 사유 설명",
                }],
                "proposer": "alice",
            })
        resp = client.get("/api/hypotheses")
        assert resp.status_code == 200
        assert resp.json()["total"] == 3

    def test_transition_endpoint(self, db_factory):
        """HA-06: POST /api/hypotheses/{id}/transition → backtested."""
        client = _build_client(db_factory)
        h = client.post("/api/hypotheses", json={
            "title": "전이 테스트 가설",
            "description": "상태 전이 테스트를 위한 가설이다. 허용 경로를 검증한다.",
            "changes": [{
                "tunable_key": "trend.ema_slope_entry_min",
                "current_value": 0.05,
                "proposed_value": 0.07,
                "rationale": "전이 테스트용 변경 사유 설명",
            }],
            "proposer": "alice",
        }).json()
        resp = client.post(f"/api/hypotheses/{h['id']}/transition", json={
            "new_status": "backtested",
            "actor": "alice",
            "payload": {"backtest_result": {"trades": 55, "win_rate": 0.62, "sharpe": 1.45}},
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "backtested"

    def test_stats_endpoint(self, db_factory):
        """HA-07: GET /api/hypotheses/stats."""
        client = _build_client(db_factory)
        client.post("/api/hypotheses", json={
            "title": "통계 테스트 가설",
            "description": "통계 엔드포인트를 검증하기 위한 가설이다.",
            "changes": [{
                "tunable_key": "trend.ema_slope_entry_min",
                "current_value": 0.05,
                "proposed_value": 0.07,
                "rationale": "통계 테스트용 변경 사유 설명",
            }],
            "proposer": "alice",
        })
        resp = client.get("/api/hypotheses/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["by_status"]["proposed"] == 1

    def test_expire_overdue_endpoint(self, db_factory):
        """HA-08: POST /api/hypotheses/expire-overdue → expired_count."""
        client = _build_client(db_factory)
        resp = client.post("/api/hypotheses/expire-overdue")
        assert resp.status_code == 200
        assert "expired_count" in resp.json()
