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
from core.judge.decision.advisory_bypass import advisory_bypass
from core.pair import normalize_pair

logger = logging.getLogger("core.judge.decision.rachel_advisory")  # 구 경로 유지

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

        # ── bypass 창 활성 시 → 조용히 v1 폴백 ──────────────
        if advisory_bypass.is_active():
            window = advisory_bypass.get_window()
            logger.info(
                f"[RachelAdvisory] {snapshot.pair}: advisory bypass 활성 "
                f"(~{window.end.isoformat()}) → v1 폴백 (silent)"
            )
            decision = await self._fallback.decide(snapshot)
            return _replace_source(decision, _SOURCE_FALLBACK)

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

        decision = self._merge_advisory_with_signal(advisory, snapshot, now, expires_at)
        # advisory_id를 meta에 삽입 — add_position 쿨다운 체크용 (BUG-032)
        decision.meta["advisory_id"] = advisory.id
        return decision

    # ── 내부 헬퍼 ──────────────────────────────────────────────

    async def _fetch_advisory(self, pair: str, exchange: str):
        """DB에서 최신 미만료 advisory 조회.

        Note: trading_style 조건 없음 — Rachel은 체제를 이미 고려한 시장 판단을
        1건 생성한다. 전략 선택은 RegimeGate가 담당하므로 여기서 trading_style
        로 필터링하지 않는다.
        """
        pair = normalize_pair(pair)
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
          add_position: 포지션 보유 + P&L>0 + pyramid<3 + 청산 시그널 없음

        만료 근접 (< 1H) 시 진입 억제.
        """
        signal = snapshot.signal
        exit_signal = snapshot.exit_signal or {}
        exit_action = exit_signal.get("action", "hold")
        has_position = snapshot.position is not None
        now_ts = now

        # ── advisory 읽기 로그 ────────────────────────────────────
        remaining_h = (expires_at - now_ts).total_seconds() / 3600
        pos_label = "포지션 있음" if has_position else "포지션 없음"
        pyramid_count_log = snapshot.position.extra.get("pyramid_count", 0) if snapshot.position else 0
        style = snapshot.params.get("trading_style", "?")
        _alice_str = (getattr(advisory, 'alice_summary', None) or '').strip()[:100]
        _samantha_str = (getattr(advisory, 'samantha_summary', None) or '').strip()[:100]
        _risk_str = (getattr(advisory, 'risk_notes', None) or '').strip()[:100]
        _extra_lines = ""
        if _alice_str:
            _extra_lines += f"\n  앨리스: {_alice_str}"
        if _samantha_str:
            _extra_lines += f"\n  사만다: {_samantha_str}"
        if _risk_str:
            _extra_lines += f"\n  리스크: {_risk_str}"
        logger.info(
            f"[RachelAdvisory:{style}] {snapshot.pair}: advisory 읽음 — "
            f"id={advisory.id} action={advisory.action} confidence={advisory.confidence:.2f} "
            f"size_pct={advisory.size_pct} 잔여={remaining_h:.1f}H "
            f"signal={snapshot.signal} {pos_label} pyramid={pyramid_count_log}\n"
            f"  근거: {advisory.reasoning[:200]}"
            f"{_extra_lines}"
        )

        advisory_action = advisory.action  # "entry_long"|"entry_short"|"hold"|"exit"
        confidence = advisory.confidence
        size_pct = advisory.size_pct
        stop_loss = advisory.stop_loss
        take_profit = advisory.take_profit

        # ── 청산 시그널 항상 존중 (advisory와 무관) ────────────
        if has_position and exit_action in ("long_caution", "short_caution", "full_exit"):
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

        # ── advisory hold → override 정책 체크 후 보류 ─────────
        if advisory_action == "hold":
            override_policy = getattr(advisory, "hold_override_policy", "none") or "none"

            if override_policy == "signal_long_setup" and not has_position:
                # 진입 시그널이 있으면 Rachel의 위임으로 자율 진입
                entry_action = None
                if signal == "long_setup":
                    entry_action = "entry_long"
                elif signal == "short_setup":
                    entry_action = "entry_short"

                if entry_action is not None:
                    # 만료 근접(< 1H) 시 override여도 진입 억제
                    remaining_sec = (expires_at - now_ts).total_seconds()
                    if remaining_sec < _EXPIRY_GUARD_SEC:
                        return self._decision(
                            action="hold",
                            snapshot=snapshot,
                            confidence=confidence,
                            size_pct=0.0,
                            stop_loss=None,
                            take_profit=None,
                            reasoning=(
                                f"hold override 가능하나 만료 임박 "
                                f"({remaining_sec/3600:.1f}H) → 진입 억제"
                            ),
                        )

                    # confidence 30% 할인 (Rachel 원래 판단 = hold, 불확실성 반영)
                    override_confidence = round(confidence * 0.7, 4)
                    logger.info(
                        f"[RachelAdvisory:{style}] {snapshot.pair}: hold override 발동 — "
                        f"policy={override_policy} signal={signal} action={entry_action} "
                        f"confidence={confidence:.2f}→{override_confidence:.2f}"
                    )
                    return self._decision(
                        action=entry_action,
                        snapshot=snapshot,
                        confidence=override_confidence,
                        size_pct=advisory.size_pct,
                        stop_loss=advisory.stop_loss,
                        take_profit=advisory.take_profit,
                        reasoning=(
                            f"hold override: Rachel hold이나 signal={signal} → "
                            f"자율 진입 (policy={override_policy}). "
                            f"원래 hold 사유: {advisory.reasoning}"
                        ),
                    )

            # override 조건 미충족 or policy="none" → 기존 hold 유지
            return self._decision(
                action="hold",
                snapshot=snapshot,
                confidence=confidence,
                size_pct=0.0,
                stop_loss=None,
                take_profit=None,
                reasoning=f"레이첼 advisory hold: {advisory.reasoning}",
            )

        # ── entry + 포지션 보유 → add_position 자동 전환 ──────────
        # Rachel(LLM)이 포지션 보유 상태에서 entry_long/entry_short를 잘못 출력한 경우
        # 방향이 일치하면 add_position으로 자동 전환 (기존 4중 안전장치 재활용)
        if advisory_action in ("entry_long", "entry_short") and has_position:
            pos_side = snapshot.position.extra.get("side", "buy")
            same_direction = (
                (advisory_action == "entry_long" and pos_side in ("buy", "long"))
                or (advisory_action == "entry_short" and pos_side in ("sell", "short"))
            )
            if same_direction:
                logger.info(
                    f"[RachelAdvisory:{style}] {snapshot.pair}: "
                    f"{advisory_action} + 포지션 보유(side={pos_side}) "
                    f"→ add_position 자동 전환 (피라미딩 안전장치 적용)"
                )
                advisory_action = "add_position"
                # fall-through → add_position + has_position 분기에서 4중 안전장치 실행
            else:
                logger.warning(
                    f"[RachelAdvisory:{style}] {snapshot.pair}: "
                    f"{advisory_action} + 포지션 보유(side={pos_side}) 방향 불일치 → hold"
                )
                return self._decision(
                    action="hold",
                    snapshot=snapshot,
                    confidence=confidence,
                    size_pct=0.0,
                    stop_loss=None,
                    take_profit=None,
                    reasoning=(
                        f"advisory={advisory_action}이나 기존 포지션(side={pos_side})과 방향 불일치 → hold. "
                        f"포지션 방향 전환은 기존 청산 후 재진입 필요"
                    ),
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
            if signal == "long_setup":
                return self._decision(
                    action="entry_long",
                    snapshot=snapshot,
                    confidence=confidence,
                    size_pct=size_pct,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reasoning=f"레이첼 entry_long × signal long_setup: {advisory.reasoning}",
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
            if signal == "short_setup":
                return self._decision(
                    action="entry_short",
                    snapshot=snapshot,
                    confidence=confidence,
                    size_pct=size_pct,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    reasoning=f"레이첼 entry_short × signal short_setup: {advisory.reasoning}",
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

        # ── add_position: 포지션 보유 중 추가 매수 (피라미딩) ─────
        _MAX_PYRAMID = 3
        if advisory_action == "add_position" and has_position:
            pyramid_count = snapshot.position.extra.get("pyramid_count", 0)

            # 조건 0: 총 사이즈 상한 사전 차단 (GR-06 사전 방지)
            # 이미 상한에 도달한 경우 판단 자체를 스킵 (WARNING 로그 없이 조용히 hold)
            existing_total_pct = snapshot.position.extra.get("total_size_pct", 0.0)
            add_pct = size_pct or 0.0
            max_total_pct = float(snapshot.params.get("position_size_pct", 50.0)) / 100.0
            new_total_pct = existing_total_pct + add_pct
            if new_total_pct > max_total_pct:
                logger.info(
                    f"[RachelAdvisory] {snapshot.pair}: add_position 스킵 — "
                    f"총 사이즈 상한 초과 ({existing_total_pct:.0%}+{add_pct:.0%}"
                    f"={new_total_pct:.0%} > {max_total_pct:.0%})"
                )
                return self._decision(
                    action="hold", snapshot=snapshot, confidence=confidence,
                    size_pct=0.0, stop_loss=None, take_profit=None,
                    reasoning=(
                        f"총 사이즈 상한 초과 — 추가 매수 스킵 "
                        f"({existing_total_pct:.0%}+{add_pct:.0%}={new_total_pct:.0%} > {max_total_pct:.0%})"
                    ),
                )

            # 조건 1: 피라미딩 횟수 제한
            if pyramid_count >= _MAX_PYRAMID:
                logger.info(
                    f"[RachelAdvisory] {snapshot.pair}: add_position 차단 — "
                    f"피라미딩 상한 도달 ({pyramid_count}/{_MAX_PYRAMID})"
                )
                return self._decision(
                    action="hold", snapshot=snapshot, confidence=confidence,
                    size_pct=0.0, stop_loss=None, take_profit=None,
                    reasoning=f"피라미딩 상한 도달 ({pyramid_count}/{_MAX_PYRAMID})",
                )

            # 조건 2: 현재 수익 구간에서만 (물타기 방지)
            entry_price = snapshot.position.entry_price
            current_price = snapshot.current_price
            side = snapshot.position.extra.get("side", "buy")
            if side in ("buy", "long"):
                pnl_jpy = (current_price - entry_price) * snapshot.position.entry_amount
                is_profitable = current_price > entry_price
            else:
                pnl_jpy = (entry_price - current_price) * snapshot.position.entry_amount
                is_profitable = current_price < entry_price

            if not is_profitable:
                logger.info(
                    f"[RachelAdvisory] {snapshot.pair}: add_position 차단 — "
                    f"손실 구간 (P&L ¥{pnl_jpy:+,.0f}). 물타기 방지"
                )
                return self._decision(
                    action="hold", snapshot=snapshot, confidence=confidence,
                    size_pct=0.0, stop_loss=None, take_profit=None,
                    reasoning=f"손실 구간 — 물타기 방지 (P&L ¥{pnl_jpy:+,.0f})",
                )

            # 조건 3: 청산 시그널 활성 시 차단
            if signal in ("long_caution", "short_caution") or exit_action in ("full_exit", "tighten_stop"):
                logger.info(
                    f"[RachelAdvisory] {snapshot.pair}: add_position 차단 — "
                    f"청산 시그널 활성 (signal={signal}, exit={exit_action})"
                )
                return self._decision(
                    action="hold", snapshot=snapshot, confidence=confidence,
                    size_pct=0.0, stop_loss=None, take_profit=None,
                    reasoning=f"청산 시그널 활성 — 추가 매수 차단 (signal={signal})",
                )

            # 조건 4: 만료 근접 시 차단
            remaining_sec = (expires_at - now_ts).total_seconds()
            if remaining_sec < _EXPIRY_GUARD_SEC:
                logger.info(
                    f"[RachelAdvisory] {snapshot.pair}: add_position 차단 — "
                    f"advisory 만료 임박 ({remaining_sec/3600:.1f}H)"
                )
                return self._decision(
                    action="hold", snapshot=snapshot, confidence=confidence,
                    size_pct=0.0, stop_loss=None, take_profit=None,
                    reasoning=f"advisory 만료 임박 ({remaining_sec/3600:.1f}H) → 추가 매수 보류",
                )

            # 모든 조건 통과 → add_position 승인
            logger.info(
                f"[RachelAdvisory] {snapshot.pair}: add_position 승인 — "
                f"피라미딩 #{pyramid_count+1}/{_MAX_PYRAMID} "
                f"P&L ¥{pnl_jpy:+,.0f} confidence={confidence:.2f} "
                f"size_pct={size_pct}\n"
                f"  근거: {advisory.reasoning[:100]}"
            )
            return self._decision(
                action="add_position", snapshot=snapshot,
                confidence=confidence, size_pct=size_pct,
                stop_loss=stop_loss, take_profit=take_profit,
                reasoning=f"레이첼 add_position #{pyramid_count+1}: {advisory.reasoning}",
            )

        if advisory_action == "add_position" and not has_position:
            logger.info(
                f"[RachelAdvisory] {snapshot.pair}: add_position이나 포지션 없음 → hold"
            )
            return self._decision(
                action="hold", snapshot=snapshot, confidence=confidence,
                size_pct=0.0, stop_loss=None, take_profit=None,
                reasoning="add_position이나 포지션 없음 → hold",
            )

        # ── adjust_risk: 리스크 파라미터 동적 재조정 ─────────────
        if advisory_action == "adjust_risk":
            if not has_position:
                # 포지션 없으면 조정 불필요
                return self._decision(
                    action="hold",
                    snapshot=snapshot,
                    confidence=confidence,
                    size_pct=0.0,
                    stop_loss=None,
                    take_profit=None,
                    reasoning=(
                        f"advisory=adjust_risk이나 포지션 없음 → hold. "
                        f"advisory 근거: {advisory.reasoning}"
                    ),
                )
            adjustments = advisory.adjustments or {}
            from core.data.dto import modify_decision as _mod
            decision = self._decision(
                action="adjust_risk",
                snapshot=snapshot,
                confidence=confidence,
                size_pct=0.0,
                stop_loss=advisory.stop_loss,
                take_profit=advisory.take_profit,
                reasoning=f"레이첼 adjust_risk: {advisory.reasoning}",
            )
            # meta에 조정 파라미터 첨부 (base_trend가 읽어 _params에 적용)
            return _mod(decision, meta={"adjustments": adjustments})

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
