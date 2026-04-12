"""
Decision Layer — AiDecision v2 (IDecisionMaker 구현체).

앨리스 → 사만다 → 레이첼 3단 판단 체인으로 SignalSnapshot → Decision DTO 변환.
각 단계 실패 시 폴백 체인으로 무중단 보장:
  Alice 실패  → RuleBasedDecision v1 폴백
  Samantha 실패 → 앨리스 결과 보수적 변환 (confidence×0.7, size×0.5)
  Rachel 실패  → _auto_verdict(alice, samantha)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from core.data.dto import Decision, SignalSnapshot
from core.decision.ai_types import (
    ALICE_RESPONSE_SCHEMA,
    RACHEL_RESPONSE_SCHEMA,
    SAMANTHA_RESPONSE_SCHEMA,
    AliceProposal,
    RachelVerdict,
    SamanthaAudit,
    serialize_snapshot,
)
from core.decision.llm_client import (
    ALICE_SYSTEM_PROMPT,
    RACHEL_SYSTEM_PROMPT,
    SAMANTHA_SYSTEM_PROMPT,
    ILlmClient,
    LlmCallError,
)

logger = logging.getLogger(__name__)

_SOURCE_AI = "ai_v2"
_SOURCE_FALLBACK = "ai_v2_fallback_v1"

# 하드캡 — GR-03와 동일
_MAX_SIZE_PCT = 0.80


# ──────────────────────────────────────────────────────────────
# 확신도 → 포지션 크기 변환
# ──────────────────────────────────────────────────────────────

def confidence_to_size(confidence: float) -> float:
    """확신도(0.0~1.0) → 포지션 크기 비율(0.0~0.80).

    변환표 (02_JUDGMENT_ENGINE.md §2-3):
      < 0.3         → 0.0  (진입 안함)
      0.3  ~ 0.5   → 0.10 ~ 0.15  (보간)
      0.5  ~ 0.7   → 0.20 ~ 0.35  (보간)
      0.7  ~ 0.85  → 0.40 ~ 0.60  (보간)
      0.85 ~ 1.0   → 0.60 ~ 0.80  (보간)

    최대 0.80 (80% 하드캡).
    """
    c = max(0.0, min(1.0, confidence))

    if c < 0.3:
        return 0.0
    elif c < 0.5:
        # 0.3→0.10, 0.5→0.15 선형 보간
        t = (c - 0.3) / 0.2
        return 0.10 + t * 0.05
    elif c < 0.7:
        # 0.5→0.20, 0.7→0.35 선형 보간
        t = (c - 0.5) / 0.2
        return 0.20 + t * 0.15
    elif c < 0.85:
        # 0.7→0.40, 0.85→0.60 선형 보간
        t = (c - 0.7) / 0.15
        return 0.40 + t * 0.20
    else:
        # 0.85→0.60, 1.0→0.80 선형 보간
        t = (c - 0.85) / 0.15
        return min(0.60 + t * 0.20, _MAX_SIZE_PCT)


# ──────────────────────────────────────────────────────────────
# AiDecision
# ──────────────────────────────────────────────────────────────

class AiDecision:
    """v2 AI 판단 엔진. IDecisionMaker Protocol 준수.

    Args:
        llm_client: ILlmClient 구현체.
        alice_model: 앨리스용 모델. None이면 llm_client 기본값.
        samantha_model: 사만다용 모델.
        rachel_model: 레이첼용 모델.
    """

    def __init__(
        self,
        llm_client: ILlmClient,
        alice_model: str | None = None,
        samantha_model: str | None = None,
        rachel_model: str | None = None,
    ) -> None:
        self._llm = llm_client
        self._alice_model = alice_model
        self._samantha_model = samantha_model
        self._rachel_model = rachel_model

    async def decide(self, snapshot: SignalSnapshot) -> Decision:
        """SignalSnapshot → Decision.

        Alice → Samantha → Rachel 순차 호출. 각 단계 실패 시 폴백.
        """
        # 공통 컨텍스트 (한 번만 직렬화)
        context = serialize_snapshot(snapshot)

        # ── Alice ──────────────────────────────────────────────
        try:
            alice = await self._call_alice(context)
        except (LlmCallError, Exception) as e:
            logger.warning(
                f"[AiDecision] {snapshot.pair}: Alice 실패 → v1 폴백. 원인: {e}"
            )
            # 지연 import — 순환 참조 방지
            from core.decision.rule_based import RuleBasedDecision
            fallback = RuleBasedDecision()
            decision = await fallback.decide(snapshot)
            # source를 폴백으로 표시
            from core.data.dto import modify_decision
            return modify_decision(decision, source=_SOURCE_FALLBACK)

        # ── Samantha ───────────────────────────────────────────
        try:
            samantha = await self._call_samantha(context, alice)
        except (LlmCallError, Exception) as e:
            logger.warning(
                f"[AiDecision] {snapshot.pair}: Samantha 실패 → 보수적 변환. 원인: {e}"
            )
            samantha = self._conservative_audit(alice)

        # ── Rachel ─────────────────────────────────────────────
        try:
            rachel = await self._call_rachel(context, alice, samantha)
        except (LlmCallError, Exception) as e:
            logger.warning(
                f"[AiDecision] {snapshot.pair}: Rachel 실패 → 자동 판정. 원인: {e}"
            )
            rachel = self._auto_verdict(alice, samantha)

        decision = self._verdict_to_decision(rachel, alice, samantha, snapshot)
        logger.info(
            f"[AiDecision] {snapshot.pair}: "
            f"Alice={alice.action}(확신 {alice.confidence:.0%}) → "
            f"Samantha={samantha.verdict}(조정 {samantha.confidence_adjustment:.0%}) → "
            f"Rachel={rachel.final_action}(확신 {rachel.final_confidence:.0%}, "
            f"사이즈 {rachel.final_size_pct:.0%}) | "
            f"근거: {rachel.reasoning[:80]}"
        )
        return decision

    # ── LLM 호출 ──────────────────────────────────────────────

    async def _call_alice(self, context: str) -> AliceProposal:
        raw = await self._llm.chat(
            system_prompt=ALICE_SYSTEM_PROMPT,
            user_prompt=context,
            response_schema=ALICE_RESPONSE_SCHEMA,
            model=self._alice_model,
        )
        return AliceProposal(
            action=raw["action"],
            confidence=float(raw["confidence"]),
            stop_loss=raw.get("stop_loss"),
            take_profit=raw.get("take_profit"),
            situation_summary=raw["situation_summary"],
            reasoning=tuple(raw.get("reasoning", [])),
            risk_factors=tuple(raw.get("risk_factors", [])),
            pessimistic_scenario=raw["pessimistic_scenario"],
        )

    async def _call_samantha(
        self, context: str, proposal: AliceProposal
    ) -> SamanthaAudit:
        alice_json = json.dumps({
            "action": proposal.action,
            "confidence": proposal.confidence,
            "situation_summary": proposal.situation_summary,
            "reasoning": list(proposal.reasoning),
            "risk_factors": list(proposal.risk_factors),
            "pessimistic_scenario": proposal.pessimistic_scenario,
            "stop_loss": proposal.stop_loss,
            "take_profit": proposal.take_profit,
        }, ensure_ascii=False)

        user_prompt = (
            f"{context}\n\n"
            f"## 앨리스 제안서\n\n```json\n{alice_json}\n```"
        )
        raw = await self._llm.chat(
            system_prompt=SAMANTHA_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_schema=SAMANTHA_RESPONSE_SCHEMA,
            model=self._samantha_model,
        )
        return SamanthaAudit(
            verdict=raw["verdict"],
            confidence_adjustment=float(raw["confidence_adjustment"]),
            max_size_pct=raw.get("max_size_pct"),
            worst_case_jpy=float(raw["worst_case_jpy"]),
            reasoning=raw["reasoning"],
            missed_risks=tuple(raw.get("missed_risks", [])),
        )

    async def _call_rachel(
        self,
        context: str,
        proposal: AliceProposal,
        audit: SamanthaAudit,
    ) -> RachelVerdict:
        alice_json = json.dumps({
            "action": proposal.action,
            "confidence": proposal.confidence,
            "reasoning": list(proposal.reasoning),
        }, ensure_ascii=False)
        samantha_json = json.dumps({
            "verdict": audit.verdict,
            "confidence_adjustment": audit.confidence_adjustment,
            "max_size_pct": audit.max_size_pct,
            "reasoning": audit.reasoning,
            "missed_risks": list(audit.missed_risks),
        }, ensure_ascii=False)

        user_prompt = (
            f"{context}\n\n"
            f"## 앨리스 제안\n\n```json\n{alice_json}\n```\n\n"
            f"## 사만다 감사 보고\n\n```json\n{samantha_json}\n```"
        )
        raw = await self._llm.chat(
            system_prompt=RACHEL_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_schema=RACHEL_RESPONSE_SCHEMA,
            model=self._rachel_model,
        )
        return RachelVerdict(
            final_action=raw["final_action"],
            final_confidence=float(raw["final_confidence"]),
            final_size_pct=float(raw["final_size_pct"]),
            stop_loss=raw.get("stop_loss"),
            take_profit=raw.get("take_profit"),
            alice_grade=raw["alice_grade"],
            samantha_grade=raw["samantha_grade"],
            adopted_side=raw["adopted_side"],
            reasoning=raw["reasoning"],
            failure_probability=raw["failure_probability"],
        )

    # ── 폴백 헬퍼 ─────────────────────────────────────────────

    @staticmethod
    def _conservative_audit(proposal: AliceProposal) -> SamanthaAudit:
        """Samantha 실패 시 — 앨리스 제안의 보수적 변환."""
        adjusted = max(0.0, proposal.confidence * 0.7)
        return SamanthaAudit(
            verdict="conditional",
            confidence_adjustment=adjusted,
            max_size_pct=None,       # 사이징은 Rachel이 결정
            worst_case_jpy=0.0,      # 알 수 없음
            reasoning="[Samantha 응답 없음 — 보수적 변환 적용]",
            missed_risks=(),
        )

    @staticmethod
    def _auto_verdict(
        proposal: AliceProposal,
        audit: SamanthaAudit,
    ) -> RachelVerdict:
        """Rachel 실패 시 — 사만다 결론 기반 자동 판정.

        규칙:
          agree      → execute (alice 채택, size -10%)
          oppose     → hold
          conditional → modified_execute (사만다 조건 적용)
        """
        if audit.verdict == "agree":
            confidence = audit.confidence_adjustment
            size = min(
                confidence_to_size(confidence) * 0.9,  # -10%
                _MAX_SIZE_PCT,
            )
            return RachelVerdict(
                final_action="execute",
                final_confidence=confidence,
                final_size_pct=size,
                stop_loss=proposal.stop_loss,
                take_profit=proposal.take_profit,
                alice_grade="data",
                samantha_grade="data",
                adopted_side="alice",
                reasoning="[Rachel 응답 없음 — 사만다 동의 기반 자동 판정]",
                failure_probability="Rachel 판정 없이 자동 실행됨",
            )
        elif audit.verdict == "oppose":
            return RachelVerdict(
                final_action="hold",
                final_confidence=audit.confidence_adjustment,
                final_size_pct=0.0,
                stop_loss=None,
                take_profit=None,
                alice_grade="pattern",
                samantha_grade="data",
                adopted_side="samantha",
                reasoning="[Rachel 응답 없음 — 사만다 반대 기반 보류]",
                failure_probability="기회 손실 가능성 있음",
            )
        else:  # conditional
            confidence = audit.confidence_adjustment
            max_size = audit.max_size_pct
            size = min(
                confidence_to_size(confidence),
                max_size if max_size is not None else _MAX_SIZE_PCT,
            )
            return RachelVerdict(
                final_action="modified_execute",
                final_confidence=confidence,
                final_size_pct=size,
                stop_loss=proposal.stop_loss,
                take_profit=proposal.take_profit,
                alice_grade="pattern",
                samantha_grade="data",
                adopted_side="compromise",
                reasoning="[Rachel 응답 없음 — 사만다 조건부 자동 판정]",
                failure_probability="Rachel 판정 없이 조건부 실행됨",
            )

    @staticmethod
    def _verdict_to_decision(
        verdict: RachelVerdict,
        alice: AliceProposal,
        samantha: SamanthaAudit,
        snapshot: SignalSnapshot,
    ) -> Decision:
        """RachelVerdict → Decision DTO.

        final_action 매핑:
          "execute"          → alice.action (entry_long/entry_short)
          "hold"             → "hold"
          "modified_execute" → alice.action + 수정 params
        """
        now = datetime.now(timezone.utc)

        if verdict.final_action in ("execute", "modified_execute"):
            action = alice.action
        else:
            action = "hold"

        # 확신도가 0.3 미만이면 hold 강제
        if verdict.final_confidence < 0.3 and action != "hold":
            action = "hold"

        size_cap = confidence_to_size(verdict.final_confidence)
        size = min(verdict.final_size_pct, size_cap, _MAX_SIZE_PCT)

        return Decision(
            action=action,
            pair=snapshot.pair,
            exchange=snapshot.exchange,
            confidence=verdict.final_confidence,
            size_pct=size,
            stop_loss=verdict.stop_loss or alice.stop_loss,
            take_profit=verdict.take_profit or alice.take_profit,
            reasoning=(
                f"[Rachel] {verdict.reasoning}\n"
                f"[Alice] {alice.situation_summary}\n"
                f"[위험] {verdict.failure_probability}"
            ),
            risk_factors=alice.risk_factors,
            source=_SOURCE_AI,
            trigger="regular_4h",
            raw_signal=snapshot.signal,
            timestamp=now,
            meta={
                "alice_action": alice.action,
                "alice_confidence": alice.confidence,
                "alice_reasoning": list(alice.reasoning),
                "alice_risk_factors": list(alice.risk_factors),
                "samantha_verdict": samantha.verdict,
                "samantha_confidence_adj": samantha.confidence_adjustment,
                "samantha_reasoning": samantha.reasoning,
                "samantha_missed_risks": list(samantha.missed_risks),
                "rachel_action": verdict.final_action,
                "rachel_confidence": verdict.final_confidence,
                "rachel_reasoning": verdict.reasoning,
                "rachel_failure_note": None,
            },
        )
