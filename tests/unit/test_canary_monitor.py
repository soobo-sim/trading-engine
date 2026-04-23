"""
P6 — CanaryMonitor + guardrails 테스트.

GR (Guardrails):     GR-01~GR-07
CM (CanaryMonitor):  CM-01~CM-04
CA (Canary API):     CA-01~CA-03
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from core.judge.evolution.guardrails import (
    GuardrailViolation,
    _count_trailing_losses,
    _calculate_max_drawdown,
    check_guardrails,
)

JST = timezone(timedelta(hours=9))


# ── 헬퍼 ────────────────────────────────────────────────────

def _canary_hypothesis(**kwargs):
    h = MagicMock()
    h.status = "canary"
    h.id = "H-2026-001"
    h.approved_at = datetime.now(tz=JST) - timedelta(hours=2)
    h.canary_result = {}
    h.changes = []
    for k, v in kwargs.items():
        setattr(h, k, v)
    return h


def _make_db(trades: list[dict] | None = None, balance: float = 5_000_000.0):
    db = AsyncMock()

    async def _execute(stmt, params=None):
        """realized_pnl 쿼리를 모킹."""
        result = MagicMock()
        if trades is not None:
            result.all.return_value = [(t["realized_pnl"],) for t in trades]
            result.first.return_value = (balance,) if balance else None
            result.scalar.return_value = None
            result.scalars.return_value.all.return_value = []
        else:
            result.all.return_value = []
            result.first.return_value = (balance,)
            result.scalar.return_value = None
            result.scalars.return_value.all.return_value = []
        return result

    db.execute = _execute
    return db


# ── GR: 가드레일 단위 테스트 ─────────────────────────────────

class TestGuardrailHelpers:
    def test_count_trailing_losses_3_consecutive(self):
        """GR-01: 마지막 3건 손실 → 3."""
        trades = [
            {"realized_pnl": 1000},
            {"realized_pnl": -500},
            {"realized_pnl": -300},
            {"realized_pnl": -200},
        ]
        assert _count_trailing_losses(trades) == 3

    def test_count_trailing_losses_profit_breaks_streak(self):
        """GR-02: 중간 이익 → 연속 카운트 리셋."""
        trades = [
            {"realized_pnl": -500},
            {"realized_pnl": 100},   # profit breaks streak
            {"realized_pnl": -300},
            {"realized_pnl": -200},
        ]
        assert _count_trailing_losses(trades) == 2

    def test_calculate_max_drawdown_single_loss(self):
        """GR-03: 단일 손실 drawdown 계산."""
        trades = [{"realized_pnl": -100_000}]
        dd = _calculate_max_drawdown(trades, 1_000_000.0)
        assert abs(dd - 10.0) < 0.01  # 10%

    def test_calculate_max_drawdown_no_trades(self):
        """GR-04: 거래 없음 → 0."""
        assert _calculate_max_drawdown([], 1_000_000.0) == 0.0

    @pytest.mark.asyncio
    async def test_check_guardrails_pnl_violation(self):
        """GR-05: 누적 PnL ≤ -3000 → pnl_jpy 위반."""
        h = _canary_hypothesis()
        db = _make_db(trades=[{"realized_pnl": -3500}], balance=5_000_000.0)
        violation = await check_guardrails(
            db, h,
            current_balance_jpy=5_000_000.0,
            canary_start_balance_jpy=5_000_000.0,
            canary_start_at=h.approved_at,
        )
        assert violation is not None
        assert violation.trigger == "pnl_jpy"

    @pytest.mark.asyncio
    async def test_check_guardrails_no_violation(self):
        """GR-06: 모든 기준 통과 → None."""
        h = _canary_hypothesis()
        db = _make_db(trades=[{"realized_pnl": 500}], balance=5_010_000.0)
        violation = await check_guardrails(
            db, h,
            current_balance_jpy=5_010_000.0,
            canary_start_balance_jpy=5_000_000.0,
            canary_start_at=h.approved_at,
        )
        assert violation is None

    @pytest.mark.asyncio
    async def test_check_guardrails_non_canary_skipped(self):
        """GR-07: status != canary → None 즉시 반환."""
        h = _canary_hypothesis(status="paper")
        db = _make_db(trades=[{"realized_pnl": -9999}])
        violation = await check_guardrails(
            db, h,
            current_balance_jpy=1.0,
            canary_start_balance_jpy=1_000_000.0,
            canary_start_at=datetime.now(tz=JST),
        )
        assert violation is None


# ── GR: 가드레일 직렬화 ──────────────────────────────────────

class TestGuardrailViolation:
    def test_to_dict_serializable(self):
        """GR-08: GuardrailViolation.to_dict() → dict with trigger key."""
        v = GuardrailViolation(
            trigger="pnl_jpy",
            actual_value=-3500.0,
            threshold=-3000.0,
            detected_at=datetime.now(tz=JST),
            description="누적 손실 ¥-3,500 ≤ 임계 ¥-3,000",
        )
        d = v.to_dict()
        assert d["trigger"] == "pnl_jpy"
        assert d["actual_value"] == -3500.0


# ── CM: CanaryMonitor ────────────────────────────────────────

class TestCanaryMonitor:
    def test_singleton(self):
        """CM-01: get_canary_monitor() 동일 인스턴스 반환."""
        from core.judge.evolution.canary_monitor import get_canary_monitor
        m1 = get_canary_monitor()
        m2 = get_canary_monitor()
        assert m1 is m2

    def test_not_running_initially(self):
        """CM-02: 생성 직후 is_running=False."""
        from core.judge.evolution.canary_monitor import CanaryMonitor
        monitor = CanaryMonitor()
        assert monitor.is_running is False

    @pytest.mark.asyncio
    async def test_start_stop(self):
        """CM-03: start() → is_running=True, stop() → False."""
        from core.judge.evolution.canary_monitor import CanaryMonitor
        monitor = CanaryMonitor()
        await monitor.start()
        assert monitor.is_running is True
        await monitor.stop()
        assert monitor.is_running is False

    @pytest.mark.asyncio
    async def test_double_start_idempotent(self):
        """CM-04: start() 두 번 호출해도 task 하나만."""
        from core.judge.evolution.canary_monitor import CanaryMonitor
        monitor = CanaryMonitor()
        await monitor.start()
        task1 = monitor._task
        await monitor.start()  # should be no-op
        task2 = monitor._task
        assert task1 is task2
        await monitor.stop()


# ── CA: Canary API ───────────────────────────────────────────

@pytest_asyncio.fixture
async def canary_db_factory():
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


def _canary_build_client(factory):
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


class TestCanaryAPI:
    def test_list_active_canaries_empty(self, canary_db_factory):
        """CA-01: canary 가설 없을 때 빈 배열 반환."""
        client = _canary_build_client(canary_db_factory)
        resp = client.get("/api/canary/active")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_active_canaries_returns_list(self, canary_db_factory):
        """CA-02: 응답이 배열 형식."""
        client = _canary_build_client(canary_db_factory)
        resp = client.get("/api/canary/active")
        assert isinstance(resp.json(), list)

    def test_tunables_accessible(self, canary_db_factory):
        """CA-03: GET /api/tunables 여전히 200 — 회귀 없음."""
        client = _canary_build_client(canary_db_factory)
        resp = client.get("/api/tunables")
        assert resp.status_code == 200
