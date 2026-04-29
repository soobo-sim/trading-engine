"""
Decision Layer — 규칙 기반 판단 엔진 (v1).

_candle_monitor()의 if/elif 분기 로직을 IDecisionMaker로 분리한 것.
코드 동작은 완전히 동일하며, 테스트와 교체(v2 AI)가 쉬운 구조로 만든다.

signal → action 매핑:
┌──────────────────┬──────────────────┬──────────────────────────────────┐
│ signal           │ position 상태    │ action                           │
├──────────────────┼──────────────────┼──────────────────────────────────┤
│ long_setup       │ 없음             │ entry_long                       │
│ short_setup      │ 없음             │ entry_short                      │
│ long_caution     │ 있음(롱)         │ exit (trigger=long_caution)      │
│ short_caution    │ 있음(숏)         │ exit (trigger=short_caution)     │
│ (exit_signal)    │ 있음 full_exit   │ exit (trigger=full_exit)         │
│ (exit_signal)    │ 있음 tighten_stop│ tighten_stop                     │
│ 그 외            │ -                │ hold                             │
└──────────────────┴──────────────────┴──────────────────────────────────┘
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from core.data.dto import Decision, SignalSnapshot

logger = logging.getLogger("core.judge.decision.rule_based")  # 구 경로 유지

_SOURCE = "rule_based_v1"


class RuleBasedDecision:
    """v1 규칙 기반 판단 엔진. IDecisionMaker Protocol 준수."""

    async def decide(self, snapshot: SignalSnapshot) -> Decision:
        """SignalSnapshot → Decision.

        BaseTrendManager._candle_monitor()의 진입·청산·스탑 타이트닝 분기를
        동일하게 재현한다. EMA 기울기 이력과 다이버전스는 호출 전에 처리되므로
        여기서는 신호값(signal / exit_signal.action)만 참조한다.
        """
        signal = snapshot.signal
        exit_signal = snapshot.exit_signal or {}
        exit_action = exit_signal.get("action", "hold")
        exit_reason = exit_signal.get("reason", "")
        pos = snapshot.position
        params = snapshot.params
        now = datetime.now(timezone.utc)

        # ── 포지션 없음: 진입 판단 ────────────────
        if pos is None:
            if signal == "long_setup":
                decision = self._entry_decision(
                    snapshot, "entry_long", "롱 진입 조건 충족", now
                )
            elif signal == "short_setup":
                decision = self._entry_decision(
                    snapshot, "entry_short", "숏 진입 조건 충족", now
                )
            else:
                decision = self._hold(snapshot, f"signal={signal} — 진입 조건 없음", now)

        # ── 포지션 있음: 청산 우선순위 ────────────
        # 1) long_caution / short_caution (EMA 이탈 하드 청산)
        elif signal in ("long_caution", "short_caution"):
            decision = self._exit_decision(
                snapshot, signal,
                f"{signal} @ {snapshot.current_price} — EMA 이탈", now,
            )

        # 2) full_exit (exit_signal 기반 전량 청산)
        elif exit_action == "full_exit":
            trigger = self._resolve_full_exit_trigger(exit_signal)
            decision = self._exit_decision(snapshot, trigger, exit_reason, now)

        # 3) tighten_stop (스탑 타이트닝, 아직 적용 안 됐을 때만)
        elif exit_action == "tighten_stop" and not pos.stop_tightened:
            decision = Decision(
                action="tighten_stop",
                pair=snapshot.pair,
                exchange=snapshot.exchange,
                confidence=0.8,
                size_pct=0.0,            # 스탑 조정 — 수량 변동 없음
                stop_loss=snapshot.stop_loss_price,
                take_profit=None,
                reasoning=exit_reason or "tighten_stop 시그널",
                risk_factors=(),
                source=_SOURCE,
                trigger="tighten_stop",
                raw_signal=signal,
                timestamp=now,
            )

        # 4) hold (트레일링 스탑은 Execution Layer에서 처리)
        else:
            decision = self._hold(
                snapshot,
                f"signal={signal} exit={exit_action} — 포지션 유지",
                now,
            )

        # 서사 로그: hold=DEBUG, 그 외 상태 변이=INFO
        if decision.action == "hold":
            logger.debug(
                f"[RuleBasedDecision] {snapshot.pair}: signal={signal} pos={'있음' if pos else '없음'} "
                f"→ hold. {decision.reasoning[:60]}"
            )
        else:
            logger.info(
                f"[RuleBasedDecision] {snapshot.pair}: signal={signal} pos={'있음' if pos else '없음'} "
                f"→ {decision.action}. {decision.reasoning[:60]}"
            )
        return decision

    # ── 헬퍼 ─────────────────────────────────────

    def _entry_decision(
        self,
        snapshot: SignalSnapshot,
        action: str,
        reasoning: str,
        now: datetime,
        confidence_override: Optional[float] = None,
    ) -> Decision:
        params = snapshot.params
        size_pct = float(params.get("position_size_pct", 1.0))
        rsi_val = snapshot.rsi
        risk_factors: list[str] = []
        if rsi_val is not None and rsi_val > 60:
            risk_factors.append(f"RSI={rsi_val:.1f} — 약간 과열")
        confidence = confidence_override if confidence_override is not None else 0.7
        return Decision(
            action=action,
            pair=snapshot.pair,
            exchange=snapshot.exchange,
            confidence=confidence,
            size_pct=size_pct,
            stop_loss=snapshot.stop_loss_price,
            take_profit=None,
            reasoning=reasoning,
            risk_factors=tuple(risk_factors),
            source=_SOURCE,
            trigger="regular_4h",
            raw_signal=snapshot.signal,
            timestamp=now,
        )

    def _exit_decision(
        self,
        snapshot: SignalSnapshot,
        trigger: str,
        reasoning: str,
        now: datetime,
    ) -> Decision:
        return Decision(
            action="exit",
            pair=snapshot.pair,
            exchange=snapshot.exchange,
            confidence=1.0,
            size_pct=1.0,   # 전량 청산
            stop_loss=None,
            take_profit=None,
            reasoning=reasoning,
            risk_factors=(),
            source=_SOURCE,
            trigger=trigger,
            raw_signal=snapshot.signal,
            timestamp=now,
        )

    def _hold(
        self,
        snapshot: SignalSnapshot,
        reasoning: str,
        now: datetime,
    ) -> Decision:
        return Decision(
            action="hold",
            pair=snapshot.pair,
            exchange=snapshot.exchange,
            confidence=1.0,
            size_pct=0.0,
            stop_loss=None,
            take_profit=None,
            reasoning=reasoning,
            risk_factors=(),
            source=_SOURCE,
            trigger="hold",
            raw_signal=snapshot.signal,
            timestamp=now,
        )

    @staticmethod
    def _resolve_full_exit_trigger(exit_signal: dict) -> str:
        """exit_signal triggers → trigger 코드."""
        triggers = exit_signal.get("triggers", {})
        if triggers.get("ema_slope_negative"):
            return "full_exit_ema_slope"
        if triggers.get("rsi_breakdown"):
            return "full_exit_rsi_breakdown"
        return "full_exit"
