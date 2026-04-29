"""
ExecutionOrchestrator AI 판단 기록 단위 테스트.

session_factory + judgment_model 조합으로 ai_judgments 테이블에 INSERT되는지 검증.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import AiJudgment
from adapters.database.session import Base
from core.data.dto import Decision, GuardrailResult, SignalSnapshot
from core.execution.orchestrator import ExecutionOrchestrator


# ── 헬퍼 ──────────────────────────────────────────────────────


def _snapshot() -> SignalSnapshot:
    return SignalSnapshot(
        pair="USD_JPY",
        exchange="gmo_fx",
        timestamp=datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
        signal="long_setup",
        current_price=150.50,
        exit_signal={"action": "hold"},
    )


def _decision(
    action: str = "entry_long",
    source: str = "ai_v2",
    meta: dict | None = None,
) -> Decision:
    return Decision(
        action=action,
        pair="USD_JPY",
        exchange="gmo_fx",
        confidence=0.75,
        size_pct=0.40,
        stop_loss=149.0,
        take_profit=152.0,
        reasoning="[Rachel] 상승 추세\n[Alice] 가격 돌파\n[위험] 낮음",
        risk_factors=("뉴스 없음",),
        source=source,
        trigger="regular_4h",
        raw_signal="long_setup",
        timestamp=datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
        meta=meta or {},
    )


def _guardrail_result(approved: bool, decision: Decision) -> GuardrailResult:
    return GuardrailResult(
        approved=approved,
        violations=() if approved else ("GR-01: 일일 한도 초과",),
        final_decision=decision,
        rejection_reason=None if approved else "GR-01: 일일 한도 초과",
    )


def _make_orchestrator(
    decision: Decision,
    guardrail_approved: bool = True,
    session_factory=None,
) -> ExecutionOrchestrator:
    dm = AsyncMock()
    dm.decide.return_value = decision

    gr = AsyncMock()
    gr.check.return_value = _guardrail_result(guardrail_approved, decision)

    return ExecutionOrchestrator(
        decision_maker=dm,
        guardrail=gr,
        session_factory=session_factory,
        judgment_model=AiJudgment if session_factory else None,
    )


# ── fixture ───────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_factory():
    """SQLite in-memory + ai_judgments 테이블 생성."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    ai_table = Base.metadata.tables["ai_judgments"]
    async with engine.begin() as conn:
        await conn.run_sync(ai_table.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ── 테스트 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_v2_judgment_saved_with_agent_fields(db_factory):
    """
    Given: TRADING_MODE=v2 — AiDecision이 meta dict를 채운 Decision 반환
    When:  process() 호출
    Then:  ai_judgments 에 alice/samantha/rachel 필드가 저장됨
    """
    meta = {
        "alice_action": "entry_long",
        "alice_confidence": 0.80,
        "alice_reasoning": ["EMA 골든크로스", "ADX 강세"],
        "alice_risk_factors": ["뉴스 없음"],
        "samantha_verdict": "agree",
        "samantha_confidence_adj": 0.75,
        "samantha_reasoning": "추세 충분히 성숙",
        "samantha_missed_risks": [],
        "rachel_action": "execute",
        "rachel_confidence": 0.75,
        "rachel_reasoning": "앨리스 논증 우세",
        "rachel_failure_note": None,
    }
    dec = _decision(source="ai_v2", meta=meta)
    orch = _make_orchestrator(dec, session_factory=db_factory)

    result = await orch.process(_snapshot())

    assert result.action == "entry_long"

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment))).scalar_one()

    assert row.source == "ai_v2"
    assert row.pair == "USD_JPY"
    assert row.exchange == "gmo_fx"
    assert row.final_action == "entry_long"
    assert row.final_confidence == pytest.approx(0.75)
    assert row.alice_action == "entry_long"
    assert row.alice_confidence == pytest.approx(0.80)
    assert row.samantha_verdict == "agree"
    assert row.rachel_action == "execute"
    assert row.guardrail_approved is True
    assert row.guardrail_violations is None


@pytest.mark.asyncio
async def test_rule_based_v1_judgment_saved_null_agent_fields(db_factory):
    """
    Given: TRADING_MODE=v1 — meta 없는 Decision
    When:  process() 호출
    Then:  ai_judgments INSERT 됨, alice/samantha/rachel 컬럼 모두 NULL
    """
    dec = _decision(source="rule_based_v1", meta={})
    orch = _make_orchestrator(dec, session_factory=db_factory)

    await orch.process(_snapshot())

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment))).scalar_one()

    assert row.source == "rule_based_v1"
    assert row.alice_action is None
    assert row.samantha_verdict is None
    assert row.rachel_action is None


@pytest.mark.asyncio
async def test_no_session_factory_skips_save():
    """
    Given: session_factory=None (기본값)
    When:  process() 호출
    Then:  DB 저장 시도 없이 정상 ExecutionResult 반환
    """
    dec = _decision()
    orch = _make_orchestrator(dec, session_factory=None)

    result = await orch.process(_snapshot())

    assert result.action == "entry_long"
    # 예외 없이 완료됨 → session_factory None guard 동작 확인


@pytest.mark.asyncio
async def test_guardrail_blocked_saves_rejected_judgment(db_factory):
    """
    Given: 안전장치가 거부한 경우
    When:  process() 호출
    Then:  guardrail_approved=False, guardrail_violations 저장됨
    """
    dec = _decision(action="entry_long", source="ai_v2")
    orch = _make_orchestrator(dec, guardrail_approved=False, session_factory=db_factory)

    result = await orch.process(_snapshot())

    assert result.action == "blocked"

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment))).scalar_one()

    assert row.guardrail_approved is False
    assert row.guardrail_violations == ["GR-01: 일일 한도 초과"]


@pytest.mark.asyncio
async def test_hold_action_also_saves_judgment(db_factory):
    """
    Given: Decision.action = hold (안전장치 체크 생략 경로)
    When:  process() 호출
    Then:  ai_judgments 에 hold 기록이 저장됨
    """
    dec = _decision(action="hold", source="rule_based_v1")
    orch = _make_orchestrator(dec, session_factory=db_factory)

    result = await orch.process(_snapshot())

    assert result.action == "hold"

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment))).scalar_one()

    assert row.final_action == "hold"
    assert row.guardrail_approved is None   # hold는 안전장치 체크 안 함


@pytest.mark.asyncio
async def test_db_insert_failure_logs_warning_and_returns_normal(db_factory, caplog):
    """
    Given: DB INSERT 실패 (세션 오류 시뮬레이션)
    When:  process() 호출
    Then:  WARNING 로그 발생, ExecutionResult는 정상 반환 (판단 흐름 차단 없음)
    """
    dec = _decision(source="ai_v2")

    # 항상 예외를 발생시키는 가짜 팩토리
    class _FailingSession:
        async def __aenter__(self):
            raise RuntimeError("DB connection lost")

        async def __aexit__(self, *_):
            pass

    def _failing_factory():
        return _FailingSession()

    orch = _make_orchestrator(dec, session_factory=_failing_factory)
    # judgment_model을 직접 설정
    orch._judgment_model = AiJudgment

    with caplog.at_level(logging.WARNING, logger="core.execution.orchestrator"):
        result = await orch.process(_snapshot())

    assert result.action == "entry_long"
    assert any("ai_judgments 저장 실패" in r.message for r in caplog.records)


# ── 추가 엣지케이스 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_exit_action_saves_judgment(db_factory):
    """
    Given: Decision.action = exit (안전장치 체크 생략 경로)
    When:  process() 호출
    Then:  ai_judgments 에 exit 기록 저장, guardrail_approved=None
    """
    dec = _decision(action="exit", source="rule_based_v1")
    orch = _make_orchestrator(dec, session_factory=db_factory)

    result = await orch.process(_snapshot())

    assert result.action == "exit"

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment))).scalar_one()

    assert row.final_action == "exit"
    assert row.guardrail_approved is None


@pytest.mark.asyncio
async def test_tighten_stop_action_saves_judgment(db_factory):
    """
    Given: Decision.action = tighten_stop (안전장치 체크 생략 경로)
    When:  process() 호출
    Then:  ai_judgments 에 tighten_stop 기록 저장
    """
    dec = _decision(action="tighten_stop", source="rule_based_v1")
    orch = _make_orchestrator(dec, session_factory=db_factory)

    result = await orch.process(_snapshot())

    assert result.action == "tighten_stop"

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment))).scalar_one()

    assert row.final_action == "tighten_stop"
    assert row.guardrail_approved is None


@pytest.mark.asyncio
async def test_approved_entry_saves_guardrail_approved_true(db_factory):
    """
    Given: entry_long 안전장치 통과
    When:  process() 호출
    Then:  guardrail_approved=True 저장됨
    """
    dec = _decision(action="entry_long", source="ai_v2")
    orch = _make_orchestrator(dec, guardrail_approved=True, session_factory=db_factory)

    await orch.process(_snapshot())

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment))).scalar_one()

    assert row.guardrail_approved is True
    assert row.guardrail_violations is None
    assert row.stop_loss == pytest.approx(149.0)
    assert row.take_profit == pytest.approx(152.0)


# ── Stage 1: judgment_id 반환 검증 ───────────────────────────


@pytest.mark.asyncio
async def test_entry_long_returns_judgment_id(db_factory):
    """
    Given: entry_long 안전장치 통과
    When:  process() 호출
    Then:  ExecutionResult.judgment_id == 삽입된 행의 id (학습 루프 연결)
    """
    dec = _decision(action="entry_long", source="ai_v2")
    orch = _make_orchestrator(dec, guardrail_approved=True, session_factory=db_factory)

    result = await orch.process(_snapshot())

    assert result.judgment_id is not None
    assert isinstance(result.judgment_id, int)

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment))).scalar_one()

    assert row.id == result.judgment_id


@pytest.mark.asyncio
async def test_hold_returns_judgment_id(db_factory):
    """
    Given: hold 판단 (안전장치 스킵 경로)
    When:  process() 호출
    Then:  ExecutionResult.judgment_id == 삽입된 행의 id (hold도 기록됨)
    """
    dec = _decision(action="hold", source="rule_based_v1")
    orch = _make_orchestrator(dec, session_factory=db_factory)

    result = await orch.process(_snapshot())

    assert result.action == "hold"
    assert result.judgment_id is not None

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment))).scalar_one()

    assert row.id == result.judgment_id


@pytest.mark.asyncio
async def test_no_session_factory_returns_judgment_id_none():
    """
    Given: session_factory=None
    When:  process() 호출
    Then:  ExecutionResult.judgment_id is None (DB 없으면 None)
    """
    dec = _decision(action="entry_long", source="rule_based_v1")
    orch = _make_orchestrator(dec, session_factory=None)

    result = await orch.process(_snapshot())

    assert result.action == "entry_long"
    assert result.judgment_id is None


@pytest.mark.asyncio
async def test_blocked_returns_judgment_id(db_factory):
    """
    Given: 안전장치 거부
    When:  process() 호출
    Then:  ExecutionResult.judgment_id 존재 (거부된 판단도 기록됨)
    """
    dec = _decision(action="entry_long", source="ai_v2")
    orch = _make_orchestrator(dec, guardrail_approved=False, session_factory=db_factory)

    result = await orch.process(_snapshot())

    assert result.action == "blocked"
    assert result.judgment_id is not None

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment))).scalar_one()

    assert row.id == result.judgment_id
    assert row.guardrail_approved is False
