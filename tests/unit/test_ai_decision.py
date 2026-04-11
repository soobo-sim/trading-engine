"""
core/decision/ai_decision.py 단위 테스트 — AiDecision + confidence_to_size.

MockLlmClient로 실제 LLM 호출 없이 행동 검증.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from core.data.dto import SignalSnapshot
from core.decision.ai_decision import AiDecision, confidence_to_size
from core.decision.llm_client import ILlmClient, LlmCallError


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _ts() -> datetime:
    return datetime(2026, 4, 11, 8, 0, 0, tzinfo=timezone.utc)


def _snapshot(signal: str = "entry_ok") -> SignalSnapshot:
    return SignalSnapshot(
        pair="USD_JPY",
        exchange="gmo",
        timestamp=_ts(),
        signal=signal,
        current_price=150.25,
        exit_signal={"action": "hold"},
        rsi=42.0,
        stop_loss_price=149.50,
        params={"position_size_pct": 1.0},
    )


def _alice_response(
    action: str = "entry_long",
    confidence: float = 0.72,
    stop_loss: float | None = 149.50,
) -> dict:
    return {
        "action": action,
        "confidence": confidence,
        "stop_loss": stop_loss,
        "take_profit": 151.20,
        "situation_summary": "박스 하단 근접",
        "reasoning": ["RSI 42 — 과매도 근접", "DXY 하락 중"],
        "risk_factors": ["CPI 발표 21:30"],
        "pessimistic_scenario": "DXY 반등 시 즉시 손절",
    }


def _samantha_response(
    verdict: str = "agree",
    confidence_adjustment: float = 0.72,
    max_size_pct: float | None = None,
) -> dict:
    return {
        "verdict": verdict,
        "confidence_adjustment": confidence_adjustment,
        "max_size_pct": max_size_pct,
        "worst_case_jpy": 9000.0,
        "reasoning": "약점 없음. 동의",
        "missed_risks": [],
    }


def _rachel_response(
    final_action: str = "execute",
    final_confidence: float = 0.70,
    final_size_pct: float = 0.40,
    stop_loss: float | None = 149.50,
) -> dict:
    return {
        "final_action": final_action,
        "final_confidence": final_confidence,
        "final_size_pct": final_size_pct,
        "stop_loss": stop_loss,
        "take_profit": 151.20,
        "alice_grade": "data",
        "samantha_grade": "pattern",
        "adopted_side": "alice",
        "reasoning": "앨리스 데이터 근거 우위",
        "failure_probability": "DXY 반등 시 손절 위험",
    }


class MockLlmClient:
    """순서대로 응답을 반환하는 mock LLM 클라이언트."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self._index = 0

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict,
        model: str | None = None,
    ) -> dict:
        if self._index >= len(self._responses):
            raise LlmCallError("응답 없음 (mock 소진)")
        resp = self._responses[self._index]
        self._index += 1
        if isinstance(resp, Exception):
            raise resp
        return resp


# ──────────────────────────────────────────────────────────────
# confidence_to_size
# ──────────────────────────────────────────────────────────────


class TestConfidenceToSize:
    def test_below_threshold(self):
        """확신도 < 0.3 → 0.0"""
        assert confidence_to_size(0.25) == 0.0
        assert confidence_to_size(0.0) == 0.0
        assert confidence_to_size(0.29) == 0.0

    def test_threshold_boundary(self):
        """확신도 = 0.3 → 0.10 (구간 시작)"""
        assert confidence_to_size(0.3) == pytest.approx(0.10, abs=1e-10)

    def test_0_40_range(self):
        """확신도 0.40 → 0.3~0.5 구간 중간"""
        result = confidence_to_size(0.40)
        assert 0.10 <= result <= 0.15

    def test_standard_0_70(self):
        """확신도 0.70 → 0.40 (구간 경계)"""
        result = confidence_to_size(0.70)
        assert result == pytest.approx(0.40, abs=1e-10)

    def test_high_0_85(self):
        """확신도 0.85 → 0.60 (구간 경계)"""
        result = confidence_to_size(0.85)
        assert result == pytest.approx(0.60, abs=1e-10)

    def test_max_cap(self):
        """확신도 1.0 → 0.80 하드캡"""
        assert confidence_to_size(1.0) == pytest.approx(0.80, abs=1e-10)
        assert confidence_to_size(0.95) <= 0.80

    def test_monotone_increasing(self):
        """단조 증가 확인."""
        values = [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 1.0]
        sizes = [confidence_to_size(v) for v in values]
        for a, b in zip(sizes, sizes[1:]):
            assert a <= b


# ──────────────────────────────────────────────────────────────
# AiDecision.decide — 정상 시나리오
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agree_execute():
    """
    Given: Alice→entry_long(0.72), Sam→agree, Rachel→execute(0.70)
    When:  decide(snapshot)
    Then:  Decision(action=entry_long, confidence=0.70, size≈0.40)
    """
    client = MockLlmClient([
        _alice_response("entry_long", 0.72),
        _samantha_response("agree", 0.72),
        _rachel_response("execute", 0.70, 0.40),
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    assert decision.action == "entry_long"
    assert decision.confidence == pytest.approx(0.70)
    assert decision.size_pct == pytest.approx(0.40)
    assert decision.source == "ai_v2"


@pytest.mark.asyncio
async def test_conditional_modified_execute():
    """
    Given: Alice→entry_long(0.72), Sam→conditional(max=0.30), Rachel→modified_execute(0.65, 0.30)
    When:  decide(snapshot)
    Then:  Decision(action=entry_long, size=0.30)
    """
    client = MockLlmClient([
        _alice_response("entry_long", 0.72),
        _samantha_response("conditional", 0.65, 0.30),
        _rachel_response("modified_execute", 0.65, 0.30),
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    assert decision.action == "entry_long"
    assert decision.size_pct == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_oppose_hold():
    """
    Given: Alice→entry, Sam→oppose, Rachel→hold
    When:  decide(snapshot)
    Then:  Decision(action=hold)
    """
    client = MockLlmClient([
        _alice_response("entry_long", 0.72),
        _samantha_response("oppose", 0.40),
        _rachel_response("hold", 0.40, 0.0, None),
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    assert decision.action == "hold"
    assert decision.size_pct == 0.0


@pytest.mark.asyncio
async def test_alice_hold():
    """
    Given: Alice→hold(0.85)
    When:  decide(snapshot)
    Then:  Decision(action=hold)
    """
    client = MockLlmClient([
        _alice_response("hold", 0.85, stop_loss=None),
        _samantha_response("agree", 0.85),
        {**_rachel_response("hold", 0.85, 0.0, None), "stop_loss": None},
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    assert decision.action == "hold"


# ──────────────────────────────────────────────────────────────
# 폴백 시나리오
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alice_failure_falls_back_to_v1():
    """
    Given: Alice LLM 실패
    When:  decide(snapshot)
    Then:  v1 폴백, source="ai_v2_fallback_v1"
    """
    client = MockLlmClient([
        LlmCallError("API timeout"),  # Alice 실패
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    assert decision.source == "ai_v2_fallback_v1"
    # v1은 entry_ok 시그널이면 entry_long 반환
    assert decision.action in ("entry_long", "hold")


@pytest.mark.asyncio
async def test_samantha_failure_conservative_conversion():
    """
    Given: Alice 성공, Samantha 실패
    When:  decide(snapshot)
    Then:  confidence×0.7 적용. action은 Alice 기반
    """
    alice_confidence = 0.72
    client = MockLlmClient([
        _alice_response("entry_long", alice_confidence),
        LlmCallError("Samantha timeout"),  # Samantha 실패
        _rachel_response("execute", alice_confidence * 0.7, 0.10),
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    assert decision.action == "entry_long"
    # Rachel이 0.504 confidence를 받아 결정한 결과
    assert decision.confidence == pytest.approx(alice_confidence * 0.7, abs=0.05)


@pytest.mark.asyncio
async def test_rachel_failure_auto_verdict_agree():
    """
    Given: Alice+Sam 성공(Sam=agree), Rachel 실패
    When:  decide(snapshot)
    Then:  _auto_verdict: Alice 채택, size-10%
    """
    client = MockLlmClient([
        _alice_response("entry_long", 0.72),
        _samantha_response("agree", 0.72),
        LlmCallError("Rachel timeout"),  # Rachel 실패
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    assert decision.action == "entry_long"
    assert decision.source == "ai_v2"
    # confidence 0.72 → size ≈ 0.416, -10% → 0.374
    expected_size = min(confidence_to_size(0.72) * 0.9, 0.80)
    assert decision.size_pct == pytest.approx(expected_size, abs=0.01)


@pytest.mark.asyncio
async def test_rachel_failure_auto_verdict_oppose():
    """
    Given: Sam=oppose, Rachel 실패
    When:  decide(snapshot)
    Then:  _auto_verdict: hold
    """
    client = MockLlmClient([
        _alice_response("entry_long", 0.72),
        _samantha_response("oppose", 0.35),
        LlmCallError("Rachel timeout"),
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    assert decision.action == "hold"


@pytest.mark.asyncio
async def test_rachel_failure_auto_verdict_conditional():
    """
    Given: Sam=conditional(max_size=0.25), Rachel 실패
    When:  decide(snapshot)
    Then:  _auto_verdict: entry_long, size≤0.25
    """
    client = MockLlmClient([
        _alice_response("entry_long", 0.72),
        _samantha_response("conditional", 0.60, 0.25),
        LlmCallError("Rachel timeout"),
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    assert decision.action == "entry_long"
    assert decision.size_pct <= 0.25


# ──────────────────────────────────────────────────────────────
# confidence < 0.3 강제 hold
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_low_confidence_forced_hold():
    """
    Given: Rachel 최종 확신도 0.25 (< 0.3)
    When:  decide(snapshot)
    Then:  action 강제 hold
    """
    client = MockLlmClient([
        _alice_response("entry_long", 0.25),
        _samantha_response("agree", 0.25),
        _rachel_response("execute", 0.25, 0.0),
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    assert decision.action == "hold"


# ──────────────────────────────────────────────────────────────
# 추가 엣지케이스
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_entry_short_maps_correctly():
    """
    Given: Alice→entry_short, Rachel→execute
    When:  decide(snapshot)
    Then:  Decision.action == 'entry_short' (alice.action 그대로 사용됨)
    """
    client = MockLlmClient([
        _alice_response("entry_short", 0.75),
        _samantha_response("agree", 0.75),
        _rachel_response("execute", 0.70, 0.40),
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    assert decision.action == "entry_short"
    assert decision.source == "ai_v2"


@pytest.mark.asyncio
async def test_meta_dict_populated_with_all_agent_fields():
    """
    Given: 정상 3단 체인 완료
    When:  decide(snapshot)
    Then:  meta dict에 alice/samantha/rachel 필드 모두 존재
    """
    client = MockLlmClient([
        _alice_response("entry_long", 0.72),
        _samantha_response("agree", 0.70),
        _rachel_response("execute", 0.68, 0.38),
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    meta = decision.meta
    # alice
    assert meta["alice_action"] == "entry_long"
    assert meta["alice_confidence"] == pytest.approx(0.72)
    assert isinstance(meta["alice_reasoning"], list)
    assert isinstance(meta["alice_risk_factors"], list)
    # samantha
    assert meta["samantha_verdict"] == "agree"
    assert meta["samantha_confidence_adj"] == pytest.approx(0.70)
    assert isinstance(meta["samantha_missed_risks"], list)
    # rachel
    assert meta["rachel_action"] == "execute"
    assert meta["rachel_confidence"] == pytest.approx(0.68)


@pytest.mark.asyncio
async def test_size_cap_enforced_when_rachel_overestimates():
    """
    Given: Rachel final_size_pct=0.95 (한도 초과), confidence=0.75
    When:  decide(snapshot)
    Then:  size_pct <= min(confidence_to_size(0.75), 0.80)
    """
    client = MockLlmClient([
        _alice_response("entry_long", 0.75),
        _samantha_response("agree", 0.75),
        _rachel_response("execute", 0.75, 0.95),  # 0.95 초과 요청
    ])
    agent = AiDecision(client)
    decision = await agent.decide(_snapshot())

    cap = confidence_to_size(0.75)
    assert decision.size_pct <= cap
    assert decision.size_pct <= 0.80


class TestConfidenceToSizeBoundaries:
    """경계값 및 예외 입력 추가 검증."""

    def test_exactly_0_5_is_second_range_start(self):
        """0.5는 두 번째 구간(0.5~0.7)에 속함 → 0.20 (단계 도약).
        첫 구간(c<0.5)은 0.15에 접근하지만 0.5 자체는 두 번째 구간 시작."""
        assert confidence_to_size(0.5) == pytest.approx(0.20, abs=1e-10)

    def test_negative_input_clamped(self):
        """음수 입력 → 0.0 (clamp)."""
        assert confidence_to_size(-0.5) == 0.0

    def test_over_1_clamped_to_max(self):
        """1.0 초과 → 0.80 (clamp)."""
        assert confidence_to_size(1.5) == pytest.approx(0.80, abs=1e-10)

    def test_exactly_0_3_boundary(self):
        """0.3 경계 = 0.10."""
        assert confidence_to_size(0.3) == pytest.approx(0.10, abs=1e-10)

    def test_exactly_0_85_boundary(self):
        """0.85 경계 = 0.60."""
        assert confidence_to_size(0.85) == pytest.approx(0.60, abs=1e-10)
