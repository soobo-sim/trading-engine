"""
Execution Layer — ExecutionOrchestrator.

Decision → Guardrail 파이프라인을 단일 호출로 처리한다.
실제 주문 실행(open_position / close_position)은 BaseTrendManager가 담당.
오케스트레이터는 "어떤 액션을 할지"를 결정하고 결과를 반환한다.

v1 파이프라인:
  1. IDecisionMaker.decide(snapshot) → Decision
  2. IGuardrail.check(decision, snapshot) → GuardrailResult
  3. ExecutionResult 반환 (approved? blocked?)

v2 확장 시:
  - IDecisionMaker를 AiDecision으로 교체
  - 승인 게이트 (Telegram 확인) 추가 — 이 클래스 안에 삽입
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.data.dto import Decision, ExecutionResult, GuardrailResult, SignalSnapshot
from core.decision.base import IDecisionMaker
from core.safety.guardrails import IGuardrail

logger = logging.getLogger(__name__)


class ExecutionOrchestrator:
    """Decision → Guardrail 파이프라인.

    Args:
        decision_maker:   IDecisionMaker 구현체 (RuleBasedDecision 또는 AiDecision).
        guardrail:        IGuardrail 구현체 (AiGuardrails).
        session_factory:  AsyncSession 팩토리 (Optional). 제공 시 ai_judgments 저장.
        judgment_model:   AiJudgment ORM 클래스 (Optional). session_factory와 함께 사용.
        approval_gate:    IApprovalGate 구현체 (Optional).
                          제공 시 진입 신호 매마다 수보오빠 승인을 요청한다.
                          None이면 승인 단계 스킵 (기존 v1 동작).
    """

    def __init__(
        self,
        decision_maker: IDecisionMaker,
        guardrail: IGuardrail,
        session_factory: Any | None = None,
        judgment_model: type | None = None,
        approval_gate: Any | None = None,
    ) -> None:
        self._decision_maker = decision_maker
        self._guardrail = guardrail
        self._session_factory = session_factory
        self._judgment_model = judgment_model
        self._approval_gate = approval_gate

    async def process(self, snapshot: SignalSnapshot) -> ExecutionResult:
        """Signal Snapshot → ExecutionResult.

        BaseTrendManager._candle_monitor()에서 호출된다.
        결과를 받아 실제 주문 실행(open/close)은 매니저가 한다.
        """
        # Step 1: 판단
        try:
            decision: Decision = await self._decision_maker.decide(snapshot)
        except Exception as e:
            logger.error(f"[Orchestrator] {snapshot.pair} 판단 실패 — {e}")
            return ExecutionResult(
                action="hold",
                executed=False,
                reason=f"판단 오류: {e}",
            )

        # 즉시 통과: hold / 청산 계열은 안전장치 체크 불필요
        if decision.action in {"hold", "exit", "tighten_stop"}:
            judgment_id = await self._save_judgment(snapshot, decision, guardrail_result=None)
            if decision.action == "hold":
                logger.debug(
                    f"[Orchestrator] {snapshot.pair}: 판단=hold "
                    f"\u2192 안전장치 생략 (비진입). 근거: {decision.reasoning[:60]}"
                )
            else:
                logger.info(
                    f"[Orchestrator] {snapshot.pair}: 판단={decision.action} "
                    f"\u2192 안전장치 생략 (비진입). 근거: {decision.reasoning[:60]}"
                )
            return ExecutionResult(
                action=decision.action,
                executed=False,   # 실제 실행은 매니저가 함
                decision=decision,
                reason=decision.reasoning,
                judgment_id=judgment_id,
            )

        # Step 2: 안전장치
        try:
            result: GuardrailResult = await self._guardrail.check(decision, snapshot)
        except Exception as e:
            logger.error(f"[Orchestrator] {snapshot.pair} 안전장치 오류 — {e}")
            return ExecutionResult(
                action="hold",
                executed=False,
                reason=f"안전장치 오류: {e}",
            )

        if not result.approved:
            judgment_id = await self._save_judgment(snapshot, decision, guardrail_result=result)
            logger.info(
                f"[Orchestrator] {snapshot.pair}: 판단={decision.action} "
                f"→ 안전장치 거부 → 진입 차단. 위반: {result.rejection_reason}"
            )
            return ExecutionResult(
                action="blocked",
                executed=False,
                decision=result.final_decision,
                reason=result.rejection_reason,
                judgment_id=judgment_id,
            )

        # Step 3: 승인 게이트 (진입 액션만)
        if (
            self._approval_gate is not None
            and result.final_decision.action in {"entry_long", "entry_short"}
        ):
            try:
                approved = await self._approval_gate.request_approval(
                    result.final_decision
                )
            except Exception as e:
                logger.error(f"[Orchestrator] {snapshot.pair} 승인 게이트 오류 — {e}")
                approved = False

            if not approved:
                judgment_id = await self._save_judgment(snapshot, decision, guardrail_result=result)
                logger.info(
                    f"[Orchestrator] {snapshot.pair}: 판단={decision.action} "
                    f"→ 안전장치 통과 → 수보오빠 승인 거부/타임아웃"
                )
                return ExecutionResult(
                    action="rejected_by_user",
                    executed=False,
                    decision=result.final_decision,
                    reason="수보오빠 승인 거부/타임아웃",
                    judgment_id=judgment_id,
                )

        # 최종 승인: 매니저에서 실행할 것
        judgment_id = await self._save_judgment(snapshot, decision, guardrail_result=result)
        logger.info(
            f"[Orchestrator] {snapshot.pair}: 판단={result.final_decision.action} "
            f"→ 안전장치 통과 → 실행 대기 (매니저에게 위임)"
        )
        return ExecutionResult(
            action=result.final_decision.action,
            executed=False,  # 실제 실행은 매니저가 함
            decision=result.final_decision,
            judgment_id=judgment_id,
        )

    async def _save_judgment(
        self,
        snapshot: SignalSnapshot,
        decision: Decision,
        guardrail_result: GuardrailResult | None,
    ) -> int | None:
        """ai_judgments 테이블에 판단 결과 INSERT 후 id 반환.

        실패해도 WARNING만 — 거래 흐름 블록하지 않는다.
        학습 루프 연결을 위해 삽입된 행의 id를 반환한다 (실패 시 None).
        """
        if self._session_factory is None or self._judgment_model is None:
            return None

        try:
            approved: bool | None = None
            violations: list | None = None
            if guardrail_result is not None:
                approved = guardrail_result.approved
                violations = list(guardrail_result.violations) if not guardrail_result.approved else None

            # AiDecision이 meta dict를 채운 경우 agent 필드 추출
            meta: dict = getattr(decision, "meta", {}) or {}

            record = self._judgment_model(
                trigger_type="regular_4h",
                timestamp=datetime.now(timezone.utc),
                pair=snapshot.pair,
                exchange=getattr(snapshot, "exchange", "unknown"),
                alice_action=meta.get("alice_action"),
                alice_confidence=meta.get("alice_confidence"),
                alice_reasoning=meta.get("alice_reasoning"),
                alice_risk_factors=meta.get("alice_risk_factors"),
                samantha_verdict=meta.get("samantha_verdict"),
                samantha_confidence_adj=meta.get("samantha_confidence_adj"),
                samantha_reasoning=meta.get("samantha_reasoning"),
                samantha_missed_risks=meta.get("samantha_missed_risks"),
                rachel_action=meta.get("rachel_action"),
                rachel_confidence=meta.get("rachel_confidence"),
                rachel_reasoning=meta.get("rachel_reasoning"),
                rachel_failure_note=meta.get("rachel_failure_note"),
                final_action=decision.action,
                final_confidence=decision.confidence,
                final_size_pct=decision.size_pct,
                stop_loss=decision.stop_loss,
                take_profit=decision.take_profit,
                source=decision.source,
                guardrail_approved=approved,
                guardrail_violations=violations,
            )
            async with self._session_factory() as session:
                session.add(record)
                await session.commit()
                await session.refresh(record)
                return record.id
        except Exception as e:
            logger.warning(f"[Orchestrator] ai_judgments 저장 실패 — {e}")
            return None
