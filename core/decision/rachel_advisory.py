"""
Decision Layer — RachelAdvisoryDecision (IDecisionMaker 구현).

레이첼(OpenClaw) 에이전트가 POST /api/advisories 로 저장한 자문(RachelAdvisory)을
DB에서 읽어 실시간 시그널과 결합하여 Decision DTO를 반환한다.

핵심 원칙:
  - 진입: advisory action + 실시간 signal 이 둘 다 합의할 때만 실행
  - 청산: advisory 또는 실시간 signal 중 하나라도 청산 요구하면 실행(보수적)
  - 만료: advisory.expires_at < now() → RuleBasedDecision v1 폴백
  - 없음: advisory 없음 → v1 폴백 + WARNING 로그

설계서: trader-common/docs/specs/ai-native/02_JUDGMENT_ENGINE.md
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select

from core.data.dto import Decision, SignalSnapshot

logger = logging.getLogger(__name__)

_SOURCE_RACHEL = "rachel_advisory"
_SOURCE_FALLBACK = "rachel_fallback_v1"

# 만료 근접 임계값 (초) — 만료까지 이 시간 이하이면 진입 억제
_EXPIRY_GUARD_SEC = 3600  # 1시간


class RachelAdvisoryDecision:
    """IDecisionMaker 구현 — 레이첼 advisory 기반 판단.

    Args:
        session_factory:  AsyncSession 팩토리.
        advisory_model:   RachelAdvisory ORM 클래스.
        fallback:         IDecisionMaker (RuleBasedDecision). advisory 없거나 만료 시 사용.
    """

    def __init__(
        self,
        session_factory: Any,
        advisory_model: Any,
        fallback: Any,
    ) -> None:
        self._session_factory = session_factory
        self._advisory_model = advisory_model
        self._fallback = fallback

    async def decide(self, snapshot: SignalSnapshot) -> Decision:
        """SignalSnapshot → Decision.

        1. DB에서 해당 pair/exchange의 최신 미만료 advisory 조회
        2. advisory 없거나 만료됨 → v1 폴백
        3. advisory 있음 → _merge_advisory_with_signal()
        """
        advisory = await self._fetch_advisory(snapshot.pair, snapshot.exchange)

        if advisory is None:
            logger.warning(
                f"[RachelAdvisory] {snapshot.pair}: advisory 없음 → v1 폴백"
            )
            decision = await self._fallback.decide(snapshot)
            return _replace_source(decision, _SOURCE_FALLBACK)

        now = datetime.now(timezone.utc)
        expires_at = advisory.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if now >= expires_at:
            logger.warning(
                f"[RachelAdvisory] {snapshot.pair}: advisory 만료됨 "
                f"(expires={expires_at.isoformat()}) → v1 폴백"
            )
            decision = await self._fallback.decide(snapshot)
            return _replace_source(decision, _SOURCE_FALLBACK)

        return self._merge_advisory_with_signal(advisory, snapshot, now, expires_at)

    # ── 내부 헬퍼 ──────────────────────────────────────────────

    async def _fetch_advisory(self, pair: str, exchange: str):
        """DB에서 최신 미만료 advisory 조회."""
        now = datetime.now(timezone.utc)
        model = self._advisory_model
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(model)
                    .where(
                        model.pair == pair,
                        model.exchange == exchange,
                        model.expires_at > now,
                    )
                    .order_by(desc(model.created_at))
                    .limit(1)
                )
                result = await session.execute(stmt)
                return result.scalars().first()
        except Exception as e:
            logger.error(f"[RachelAdvisory] advisory DB 조회 실패 — {e}")
            return None

    def _merge_advisory_with_signal(
        self,
        advisory: Any,
        snapshot: SignalSnapshot,
        now: datetime,
        expires_at: datetime,
    ) -> Decision:
        """advisory + 실시간 시그널 결합 → Decision.

        결합 규칙:
          진입: advisory action + signal 둘 다 합의 필요
          청산/스탑: 어느 쪽이든 요구하면 실행 (보수적)
          advisory hold: 항상 hold (레이첼 보류 존중)
          advisory exit: 포지션 있으면 즉시 exit

        만료 근접 (< 1H) 시 진입 억제.
        """
        signal = snapshot.signal
        exit_signal = snapshot.exit_signal or {}
        exit_action = exit_signal.get("action", "hold")
        has_position = snapshot.position is not None
        now_ts = now

        advisory_action = advisory.action  # "entry_long"|"entry_short"|"hold"|"exit"
        confidence = advisory.confidence
        size_pct = advisory.size_pct
        stop_loss = advisory.stop_loss
        take_profit = advisory.take_profit

        # ── 청산 시그널 항상 존중 (advisory와 무관) ────────────
        if has_position and exit_action in ("exit_warning", "full_exit"):
            return self._decision(
                action="exit",
                snapshot=snapshot,
                confidence=1.0,
                size_pct=0.0,
                stop_loss=None,
                take_profit=None,
                reasoning=f"긴급 시그널 exit→{exit_action} (advisory 무관)",
            )

        if has_position and exit_action == "tighten_stop":
            return self._decision(
                action="tighten_stop",
                snapshot=snapshot,
                confidence=1.0,
                size_pct=0.0,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reasoning="tighten_stop 시그널 (advisory 무관)",
            )

        # ── advisory exit → 즉시 청산 ───────────────────────────
        if advisory_action == "exit" and has_position:
            return self._decision(
                action="exit",
                snapshot=snapshot,
                confidence=confidence,
                size_pct=0.0,
                stop_loss=None,
                take_profit=None,
                reasoning=f"레이첼 advisory exit 지시: {advisory.reasoning}",
            )

        # ── advisory hold → 보류 ────────────────────────────────
        if advisory_action == "hold":
            return self._decision(
                action="hold",
                snapshot=snapshot,
                confidence=confidence,
                size_pct=0.0,
                stop_loss=None,
                take_profit=None,
                reasoning=f"레이첼 advisory hold: {advisory.reasoning}",
            )

        # ── 만료 근접 시 진입 억제 ──────────────────────────────
        remaining_sec = (expires_at - now_ts).total_seconds()
        if advisory_action in ("entry_long", "entry_short") and not has_position:
            if remaining_sec < _EXPIRY_GUARD_SEC:
                return self._decision(
                    action="hold",
                    snapshot=snapshot,
                    confidence=confidence,
                    size_pct=0.0,
                    stop_loss=None,
                    take_profit=None,
                    reasoning=(
                        f"advisory 만료 임박 ({remaining_sec/3600:.1f}H 남음) → 진입 억제. "
                        f"advisory={advisory_action}"
                    ),
                )

        # ── 진입: advisory + signal 합의 필요 ───────────────────
        if advisory_action == "entry_long" and not has_position:
            if signal == "entry_ok":
                return self._decision(
                    action="entry_long",
                    snapshot=snapshot,
                    confidence=confidence,
                    size_pct=size_pct,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reasoning=f"레이첼 entry_long × signal entry_ok: {advisory.reasoning}",
                )
            if signal == "entry_preview":
                # 프리뷰 시그널: confidence × 0.85, size_pct × 0.7 (미확인 진입 리스크 반영)
                preview_confidence = round(confidence * 0.85, 4)
                preview_size = round((size_pct or 0.0) * 0.7, 4)
                return self._decision(
                    action="entry_long",
                    snapshot=snapshot,
                    confidence=preview_confidence,
                    size_pct=preview_size,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reasoning=(
                        f"레이첼 entry_long × entry_preview (confidence={preview_confidence:.2f}, "
                        f"size={preview_size:.0%}): {advisory.reasoning}"
                    ),
                )
            return self._decision(
                action="hold",
                snapshot=snapshot,
                confidence=confidence,
                size_pct=0.0,
                stop_loss=None,
                take_profit=None,
                reasoning=(
                    f"advisory=entry_long이나 signal={signal} → 타이밍 미충족. "
                    f"advisory 근거: {advisory.reasoning}"
                ),
            )

        if advisory_action == "entry_short" and not has_position:
            if signal == "entry_sell":
                return self._decision(
                    action="entry_short",
                    snapshot=snapshot,
                    confidence=confidence,
                    size_pct=size_pct,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reasoning=f"레이첼 entry_short × signal entry_sell: {advisory.reasoning}",
                )
            return self._decision(
                action="hold",
                snapshot=snapshot,
                confidence=confidence,
                size_pct=0.0,
                stop_loss=None,
                take_profit=None,
                reasoning=(
                    f"advisory=entry_short이나 signal={signal} → 타이밍 미충족. "
                    f"advisory 근거: {advisory.reasoning}"
                ),
            )

        # ── 기본: hold ───────────────────────────────────────────
        return self._decision(
            action="hold",
            snapshot=snapshot,
            confidence=confidence,
            size_pct=0.0,
            stop_loss=None,
            take_profit=None,
            reasoning=f"advisory={advisory_action}, signal={signal} → hold",
        )

    @staticmethod
    def _decision(
        action: str,
        snapshot: SignalSnapshot,
        confidence: float,
        size_pct: float | None,
        stop_loss: float | None,
        take_profit: float | None,
        reasoning: str,
    ) -> Decision:
        return Decision(
            action=action,
            pair=snapshot.pair,
            exchange=snapshot.exchange,
            confidence=confidence,
            size_pct=size_pct,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reasoning=reasoning,
            risk_factors=(),
            source=_SOURCE_RACHEL,
            trigger="regular_4h",
            raw_signal=snapshot.signal,
            timestamp=datetime.now(timezone.utc),
        )


def _replace_source(decision: Decision, new_source: str) -> Decision:
    """Decision.source를 교체하여 새 Decision 반환."""
    from core.data.dto import modify_decision
    return modify_decision(decision, source=new_source)
