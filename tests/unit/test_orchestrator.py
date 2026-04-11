"""
core/execution/orchestrator.py 단위 테스트 — ExecutionOrchestrator.process().

IDecisionMaker / IGuardrail 을 간단한 익명 구현체로 mock 한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from core.data.dto import (
    Decision,
    ExecutionResult,
    GuardrailResult,
    SignalSnapshot,
    modify_decision,
)
from core.execution.orchestrator import ExecutionOrchestrator


# ── 공통 헬퍼 ──────────────────────────────────────────────────


def _snapshot(signal: str = "entry_ok") -> SignalSnapshot:
    return SignalSnapshot(
        pair="BTC_JPY",
        exchange="bitflyer",
        timestamp=datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
        signal=signal,
        current_price=5_000_000.0,
        exit_signal={"action": "hold"},
    )


def _decision(action: str = "entry_long") -> Decision:
    return Decision(
        action=action,
        pair="BTC_JPY",
        exchange="bitflyer",
        confidence=0.7,
        size_pct=1.0,
        stop_loss=4_900_000.0,
        take_profit=None,
        reasoning="테스트",
        risk_factors=(),
        source="rule_based_v1",
        trigger="regular_4h",
        raw_signal="entry_ok",
        timestamp=datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


def _guardrail_result(approved: bool, decision: Decision | None = None) -> GuardrailResult:
    d = decision or _decision()
    return GuardrailResult(
        approved=approved,
        violations=() if approved else ("GR-01: 일일 한도 초과",),
        final_decision=d,
        rejection_reason=None if approved else "GR-01: 일일 한도 초과",
    )


def _make_orchestrator(
    decision: Decision | None = None,
    decision_raises: Exception | None = None,
    guardrail_approved: bool = True,
    guardrail_raises: Exception | None = None,
) -> ExecutionOrchestrator:
    dm = AsyncMock()
    if decision_raises:
        dm.decide.side_effect = decision_raises
    else:
        dm.decide.return_value = decision or _decision()

    gr = AsyncMock()
    if guardrail_raises:
        gr.check.side_effect = guardrail_raises
    else:
        gr.check.return_value = _guardrail_result(
            guardrail_approved, decision
        )

    return ExecutionOrchestrator(decision_maker=dm, guardrail=gr)


# ── decision_maker 예외 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_decision_maker_exception_returns_hold():
    """
    Given: decision_maker.decide() 예외 발생
    When:  process() 호출
    Then:  action=hold, 안전장치 체크 없음
    """
    orch = _make_orchestrator(decision_raises=RuntimeError("DB timeout"))
    result = await orch.process(_snapshot())

    assert result.action == "hold"
    assert result.executed is False
    assert result.reason is not None
    assert "판단 오류" in result.reason


# ── hold / exit / tighten_stop — 안전장치 생략 ───────────────


@pytest.mark.asyncio
async def test_hold_decision_skips_guardrail():
    """
    Given: decision_maker → hold
    When:  process()
    Then:  action=hold, guardrail.check() 미호출
    """
    orch = _make_orchestrator(decision=_decision("hold"))
    result = await orch.process(_snapshot())

    assert result.action == "hold"
    assert result.executed is False
    orch._guardrail.check.assert_not_awaited()


@pytest.mark.asyncio
async def test_exit_decision_skips_guardrail():
    """
    Given: decision_maker → exit
    When:  process()
    Then:  action=exit, guardrail 미호출
    """
    orch = _make_orchestrator(decision=_decision("exit"))
    result = await orch.process(_snapshot())

    assert result.action == "exit"
    assert result.executed is False
    orch._guardrail.check.assert_not_awaited()


@pytest.mark.asyncio
async def test_tighten_stop_skips_guardrail():
    """
    Given: decision_maker → tighten_stop
    When:  process()
    Then:  action=tighten_stop, guardrail 미호출
    """
    orch = _make_orchestrator(decision=_decision("tighten_stop"))
    result = await orch.process(_snapshot())

    assert result.action == "tighten_stop"
    orch._guardrail.check.assert_not_awaited()


# ── entry_long — 안전장치 통과·차단 ─────────────────────────


@pytest.mark.asyncio
async def test_entry_long_approved_by_guardrail():
    """
    Given: decision_maker → entry_long, guardrail 승인
    When:  process()
    Then:  action=entry_long, executed=False (매니저가 실행)
    """
    orch = _make_orchestrator(guardrail_approved=True)
    result = await orch.process(_snapshot())

    assert result.action == "entry_long"
    assert result.executed is False
    assert result.decision is not None


@pytest.mark.asyncio
async def test_entry_long_blocked_by_guardrail():
    """
    Given: decision_maker → entry_long, guardrail 차단
    When:  process()
    Then:  action=blocked
    """
    orch = _make_orchestrator(guardrail_approved=False)
    result = await orch.process(_snapshot())

    assert result.action == "blocked"
    assert result.executed is False
    assert result.reason is not None


# ── 안전장치 예외 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guardrail_exception_returns_hold():
    """
    Given: guardrail.check() 예외 발생
    When:  process()
    Then:  action=hold ("안전장치 오류")
    """
    orch = _make_orchestrator(guardrail_raises=RuntimeError("DB error"))
    result = await orch.process(_snapshot())

    assert result.action == "hold"
    assert result.executed is False
    assert result.reason is not None
    assert "안전장치 오류" in result.reason


# ── 결정 decision 필드 전달 ───────────────────────────────────


@pytest.mark.asyncio
async def test_approved_result_carries_decision():
    """승인된 결과에 final_decision이 담겨 있어야 한다."""
    custom_decision = _decision("entry_long")
    orch = _make_orchestrator(decision=custom_decision, guardrail_approved=True)
    result = await orch.process(_snapshot())

    assert result.decision is not None
    assert result.decision.action == "entry_long"


@pytest.mark.asyncio
async def test_hold_result_carries_decision():
    """hold 결과에도 decision이 포함된다."""
    hold_dec = _decision("hold")
    orch = _make_orchestrator(decision=hold_dec)
    result = await orch.process(_snapshot())

    assert result.decision is not None
    assert result.decision.action == "hold"


# ── approval_gate — 승인/거부/액션별 스킵 ────────────────────


@pytest.mark.asyncio
async def test_approval_gate_approve_returns_entry_long():
    """
    Given: guardrail 통과, approval_gate 승인
    When:  process()
    Then:  action=entry_long (rejected_by_user 아님)
    """
    approval = AsyncMock()
    approval.request_approval.return_value = True

    orch = _make_orchestrator(guardrail_approved=True)
    orch._approval_gate = approval

    result = await orch.process(_snapshot())

    assert result.action == "entry_long"
    approval.request_approval.assert_awaited_once()


@pytest.mark.asyncio
async def test_approval_gate_reject_returns_rejected_by_user():
    """
    Given: guardrail 통과, approval_gate 거부
    When:  process()
    Then:  action=rejected_by_user
    """
    approval = AsyncMock()
    approval.request_approval.return_value = False

    orch = _make_orchestrator(guardrail_approved=True)
    orch._approval_gate = approval

    result = await orch.process(_snapshot())

    assert result.action == "rejected_by_user"
    assert result.executed is False
    assert result.reason is not None


@pytest.mark.asyncio
async def test_approval_gate_not_called_for_exit():
    """
    Given: approval_gate 있음, action=exit
    When:  process()
    Then:  approval_gate.request_approval 미호출 — exit는 승인 불필요
    """
    approval = AsyncMock()
    approval.request_approval.return_value = True

    orch = _make_orchestrator(decision=_decision("exit"))
    orch._approval_gate = approval

    result = await orch.process(_snapshot())

    assert result.action == "exit"
    approval.request_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_gate_not_called_for_hold():
    """
    Given: approval_gate 있음, action=hold
    When:  process()
    Then:  approval_gate.request_approval 미호출
    """
    approval = AsyncMock()
    approval.request_approval.return_value = True

    orch = _make_orchestrator(decision=_decision("hold"))
    orch._approval_gate = approval

    await orch.process(_snapshot())

    approval.request_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_gate_none_skips_approval_step():
    """
    Given: approval_gate=None (기본값)
    When:  entry_long, guardrail 통과
    Then:  action=entry_long (승인 단계 없이 통과)
    """
    orch = _make_orchestrator(guardrail_approved=True)
    assert orch._approval_gate is None

    result = await orch.process(_snapshot())
    assert result.action == "entry_long"


@pytest.mark.asyncio
async def test_approval_gate_exception_returns_rejected():
    """
    Given: approval_gate.request_approval() 예외 발생
    When:  process()
    Then:  action=rejected_by_user (안전 방향)
    """
    approval = AsyncMock()
    approval.request_approval.side_effect = RuntimeError("Telegram 오류")

    orch = _make_orchestrator(guardrail_approved=True)
    orch._approval_gate = approval

    result = await orch.process(_snapshot())

    assert result.action == "rejected_by_user"
    assert result.executed is False


@pytest.mark.asyncio
async def test_approval_gate_called_for_entry_short():
    """
    Given: guardrail 통과, action=entry_short, approval_gate 승인
    When:  process()
    Then:  approval_gate.request_approval 호출됨 — entry_short도 승인 대상
    """
    approval = AsyncMock()
    approval.request_approval.return_value = True

    orch = _make_orchestrator(decision=_decision("entry_short"), guardrail_approved=True)
    orch._approval_gate = approval

    result = await orch.process(_snapshot())

    assert result.action == "entry_short"
    approval.request_approval.assert_awaited_once()
