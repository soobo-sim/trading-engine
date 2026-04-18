"""
Decision Layer — IDecisionMaker Protocol.

모든 판단 엔진(v1 규칙 기반, v2 AI 기반)이 이 Protocol을 구현한다.
Signal Layer의 SignalSnapshot → Decision DTO 변환이 책임.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.data.dto import Decision, SignalSnapshot


@runtime_checkable
class IDecisionMaker(Protocol):
    """판단 엔진 인터페이스.

    decide():
        SignalSnapshot을 받아 실행할 Decision을 반환한다.
        - v1: RuleBasedDecision (신호값 → action 1:1 매핑)
        - v2: AiDecision (LLM 판단 + ai_judgments 로그)
    """

    async def decide(self, snapshot: SignalSnapshot) -> Decision:
        """시그널 스냅샷 → 판단 결과."""
        ...
