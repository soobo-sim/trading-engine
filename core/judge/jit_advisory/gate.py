"""
JIT Advisory Gate — 진입 직전 LLM 자문 게이트.

IDecisionMaker Protocol 구현.
내부적으로 RuleBasedDecision이 먼저 판단하고,
진입 액션(entry_long/entry_short/add_position)에만 JIT 자문을 요청한다.

원칙:
  - 청산/tighten_stop/hold → JIT 호출 없이 즉시 통과
  - 진입 판단 → JIT 호출 → GO면 통과, NO_GO면 hold, ADJUST면 수정
  - JIT 실패(타임아웃/오류) → fail-soft NO_GO (진입 차단)
  - 모든 호출은 jit_advisories 테이블에 기록

설계서: trader-common/docs/proposals/active/JIT_ADVISORY_ARCHITECTURE.md §4.2
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from core.judge.decision.base import IDecisionMaker
from core.judge.decision.rule_based import RuleBasedDecision
from core.shared.data.dto import Decision, SignalSnapshot, modify_decision
from core.shared.logging.context import get_judge_cycle_id

from .audit import save_jit_audit
from .client import JITAdvisoryClient
from .context import build_jit_request
from .models import JITAdvisoryResponse

logger = logging.getLogger(__name__)

_ENTRY_ACTIONS = frozenset({"entry_long", "entry_short", "add_position"})

# NO_GO 직후 쿨다운 (초) — 같은 페어에서 연속 JIT 차단 시 대기
_NOGO_COOLDOWN_SEC = 60.0


class JITAdvisoryGate:
    """룰엔진 + JIT 자문 게이트.

    IDecisionMaker Protocol을 구현한다.
    ExecutionOrchestrator의 decision_maker 자리에 RuleBasedDecision 대신 주입.
    """

    def __init__(
        self,
        session_factory,
        jit_client: Optional[JITAdvisoryClient] = None,
    ) -> None:
        self._rule = RuleBasedDecision()
        self._client = jit_client or JITAdvisoryClient()
        self._session_factory = session_factory

    async def decide(self, snapshot: SignalSnapshot) -> Decision:
        """SignalSnapshot → Decision.

        1. 룰엔진으로 1차 판단
        2. 진입 액션이면 JIT 자문 요청
        3. JIT 결과에 따라 최종 Decision 확정
        """
        rule_decision = await self._rule.decide(snapshot)

        if rule_decision.action not in _ENTRY_ACTIONS:
            # 청산/tighten/hold — JIT 없이 즉시 통과
            return rule_decision

        # ── 진입 액션: JIT 자문 ──────────────────────────────
        _cid = get_judge_cycle_id() or "????"
        _pfx = f"[JIT][{_cid}]"
        _rsi_str = f"{snapshot.rsi:.1f}" if snapshot.rsi is not None else "N/A"
        _slope_str = f"{snapshot.ema_slope_pct:.4f}" if snapshot.ema_slope_pct is not None else "N/A"
        logger.info(
            f"{_pfx} {snapshot.pair} JIT 자문 요청 — "
            f"action={rule_decision.action} signal={snapshot.signal} "
            f"regime={snapshot.regime} price=¥{snapshot.current_price:,.0f} "
            f"rsi={_rsi_str} ema_slope={_slope_str} "
            f"conf={rule_decision.confidence:.2f} size={rule_decision.size_pct:.0%}"
        )

        jit_req = build_jit_request(snapshot, rule_decision)
        jit_resp: Optional[JITAdvisoryResponse] = None
        error_msg: Optional[str] = None
        final_decision = rule_decision

        try:
            jit_resp = await self._client.request(jit_req)
        except Exception as e:
            error_msg = f"JIT 클라이언트 오류: {e}"
            logger.error(f"[JIT] {error_msg}")

        if jit_resp is None:
            # 타임아웃/오류 → fail-soft NO_GO
            error_msg = error_msg or "JIT 타임아웃 또는 응답 파싱 실패"
            logger.warning(
                f"{_pfx} {snapshot.pair} fail-soft NO_GO — {error_msg}"
            )
            final_decision = modify_decision(
                rule_decision,
                action="hold",
                reasoning=f"[JIT fail-soft] {error_msg}",
            )

        elif jit_resp.decision == "NO_GO":
            logger.info(
                f"{_pfx} {snapshot.pair} NO_GO — "
                f"action={rule_decision.action} → hold. "
                f"사유: {jit_resp.reasoning[:120]}"
            )
            final_decision = modify_decision(
                rule_decision,
                action="hold",
                reasoning=f"[JIT NO_GO] {jit_resp.reasoning}",
                meta={**rule_decision.meta, "jit_decision": "NO_GO", "jit_reasoning": jit_resp.reasoning},
            )

        elif jit_resp.decision == "ADJUST":
            adjusted_size = jit_resp.adjusted_size_pct or rule_decision.size_pct
            adjusted_action = jit_resp.adjusted_action or rule_decision.action
            adjusted_sl = jit_resp.adjusted_stop_loss or rule_decision.stop_loss
            adjusted_tp = jit_resp.adjusted_take_profit or rule_decision.take_profit

            logger.info(
                f"{_pfx} {snapshot.pair} ADJUST — "
                f"size {rule_decision.size_pct:.0%}→{adjusted_size:.0%} "
                f"action {rule_decision.action}→{adjusted_action} "
                f"사유: {jit_resp.reasoning[:80]}"
            )
            final_decision = modify_decision(
                rule_decision,
                action=adjusted_action,
                size_pct=adjusted_size,
                stop_loss=adjusted_sl,
                take_profit=adjusted_tp,
                reasoning=f"[JIT ADJUST] {jit_resp.reasoning}",
                meta={
                    **rule_decision.meta,
                    "jit_decision": "ADJUST",
                    "jit_reasoning": jit_resp.reasoning,
                    "jit_original_size": rule_decision.size_pct,
                },
            )

        else:
            # GO — 룰엔진 결정 그대로, reasoning에 JIT 승인 메모 추가
            logger.info(
                f"{_pfx} {snapshot.pair} GO — "
                f"action={rule_decision.action} size={rule_decision.size_pct:.0%} "
                f"conf={jit_resp.confidence:.2f}. "
                f"사유: {jit_resp.reasoning[:80]}"
            )
            final_decision = modify_decision(
                rule_decision,
                reasoning=f"[JIT GO] {jit_resp.reasoning}",
                meta={
                    **rule_decision.meta,
                    "jit_decision": "GO",
                    "jit_confidence": jit_resp.confidence,
                    "jit_reasoning": jit_resp.reasoning,
                },
            )

        # ── 감사 로그 ─────────────────────────────────────────
        try:
            await self._log_audit(jit_req, jit_resp, final_decision, error_msg)
        except Exception as e:
            logger.warning(f"{_pfx} {snapshot.pair} 감사 로그 실패 (무시): {e}")

        return final_decision

    async def _log_audit(
        self,
        jit_req,
        jit_resp: Optional[JITAdvisoryResponse],
        final_decision: Decision,
        error_msg: Optional[str],
    ) -> None:
        """비동기 감사 로그 — DB 저장 실패해도 거래 방해 안 함."""
        try:
            async with self._session_factory() as session:
                await save_jit_audit(
                    session=session,
                    request=jit_req,
                    response=jit_resp,
                    final_action=final_decision.action,
                    final_size_pct=final_decision.size_pct,
                    error=error_msg,
                )
        except Exception as e:
            logger.warning(f"[JIT] 감사 로그 세션 오류 (무시): {e}")
