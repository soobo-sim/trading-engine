"""
HypothesesService — P4 가설 생애주기 CRUD + 상태 머신.

ID 형식: H-{YYYY}-{NNN}
proposed → backtested → paper → canary → adopted (또는 rejected/rolled_back/archived)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.hypothesis_model import Hypothesis
from api.schemas.evolution import HypothesisCreate, HypothesisTransition
from api.services.lessons_service import LessonsService
from api.schemas.evolution import LessonCreate
from core.judge.evolution.lifecycle import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    check_promotion_to_adopted,
    check_promotion_to_canary,
    check_promotion_to_paper,
    validate_transition,
)
from core.shared.tunable_catalog import TunableCatalog

JST = timezone(timedelta(hours=9))
logger = logging.getLogger("core.judge.evolution.hypotheses")


async def _notify_evolution(message: str) -> None:
    """진화 텔레그램 채널 알림 (TELEGRAM_EVOLUTION_CHAT_ID). 없으면 로그만."""
    chat_id = os.getenv("TELEGRAM_EVOLUTION_CHAT_ID")
    if not chat_id:
        logger.info("[Evolution Notify — no channel] %s", message)
        return
    try:
        from core.punisher.notifications.switch_telegram import send_switch_recommendation_telegram  # noqa
        # 진화 채널 전용: 구현 간이화 — 실제 채널로 발송은 P5에서 완성
        logger.info("[Evolution → %s] %s", chat_id[:6], message)
    except Exception:
        logger.warning("[Evolution Notify failed] %s", message)


class HypothesesService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── 생성 ─────────────────────────────────────────────────

    async def create(self, payload: HypothesisCreate) -> Hypothesis:
        # (1) Tunable 변경 검증
        for ch in payload.changes:
            spec = TunableCatalog.get(ch.tunable_key)
            if spec is None:
                raise ValueError(f"Unknown tunable key: {ch.tunable_key!r}")
            ok, err = TunableCatalog.validate_change(ch.tunable_key, ch.proposed_value)
            if not ok:
                raise ValueError(f"changes invalid for {ch.tunable_key!r}: {err}")

        # (2) escalation 트랙 자동 판정
        is_escalation = any(
            TunableCatalog.get(ch.tunable_key).autonomy == "escalation"  # type: ignore[union-attr]
            for ch in payload.changes
        )
        track = "escalation" if is_escalation else "standard"
        expires_at: datetime | None = (
            datetime.now(tz=JST) + timedelta(days=7) if is_escalation else None
        )

        # (3) ID 발급 + 저장
        new_id = await self._next_id()
        now = datetime.now(tz=JST)
        h = Hypothesis(
            id=new_id,
            title=payload.title,
            description=payload.description,
            track=track,
            status="proposed",
            changes=[ch.model_dump() for ch in payload.changes],
            proposer=payload.proposer,
            source_lessons=payload.source_lessons,
            baseline_metrics=payload.baseline_metrics,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        self.db.add(h)
        await self.db.commit()
        await self.db.refresh(h)

        await _notify_evolution(f"가설 등록 {h.id} [{track}]: {h.title}")
        return h

    # ── 조회 ─────────────────────────────────────────────────

    async def get(self, hypothesis_id: str) -> Hypothesis | None:
        return await self.db.get(Hypothesis, hypothesis_id)

    async def list(
        self,
        *,
        status: str | None = None,
        track: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Hypothesis]]:
        stmt = select(Hypothesis)
        if status:
            stmt = stmt.where(Hypothesis.status == status)
        if track:
            stmt = stmt.where(Hypothesis.track == track)

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total: int = (await self.db.execute(count_stmt)).scalar() or 0

        stmt = stmt.order_by(Hypothesis.created_at.desc()).limit(limit).offset(offset)
        rows = (await self.db.execute(stmt)).scalars().all()
        return total, list(rows)

    async def stats(self) -> dict[str, Any]:
        status_rows = (
            await self.db.execute(
                select(Hypothesis.status, func.count(Hypothesis.id)).group_by(Hypothesis.status)
            )
        ).all()
        track_rows = (
            await self.db.execute(
                select(Hypothesis.track, func.count(Hypothesis.id)).group_by(Hypothesis.track)
            )
        ).all()
        by_status = {r[0]: r[1] for r in status_rows}
        by_track = {r[0]: r[1] for r in track_rows}
        return {"total": sum(by_status.values()), "by_status": by_status, "by_track": by_track}

    # ── 상태 전이 ─────────────────────────────────────────────

    async def transition(
        self,
        hypothesis_id: str,
        new_status: str,
        *,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> Hypothesis:
        h = await self.get(hypothesis_id)
        if h is None:
            raise ValueError(f"Hypothesis not found: {hypothesis_id!r}")

        # (1) 허용 전이 검증
        validate_transition(h.status, new_status)

        # (2) 단계별 데이터 검증 + 부작용
        if new_status == "backtested":
            if not payload or "backtest_result" not in payload:
                raise ValueError("backtest_result required for backtested transition")
            h.backtest_result = payload["backtest_result"]

        elif new_status == "paper":
            if h.track == "standard":
                check_promotion_to_paper(h)
            if payload and "paper_result" in payload:
                h.paper_result = payload["paper_result"]

        elif new_status == "canary":
            if h.track == "standard":
                check_promotion_to_canary(h)
            if h.track == "escalation" and not actor.startswith("sub"):
                raise PermissionError("Escalation canary requires actor starting with 'sub'")
            h.approver = actor
            h.approved_at = datetime.now(tz=JST)
            if payload and "canary_result" in payload:
                h.canary_result = payload["canary_result"]

            # P6: canary 진입 시 시작 잔고 기록 + CanaryMonitor 캐시 등록
            try:
                from core.judge.evolution.guardrails import _fetch_current_balance_jpy
                from core.judge.evolution.canary_monitor import get_canary_monitor
                start_bal = await _fetch_current_balance_jpy(self.db)
                h.canary_result = {**(h.canary_result or {}), "start_balance_jpy": float(start_bal)}
                get_canary_monitor()._canary_start_balances[h.id] = float(start_bal)
            except Exception as exc:
                logger.debug("canary start_balance 기록 실패(무시): %s", exc)

        elif new_status == "adopted":
            check_promotion_to_adopted(h)
            await self._apply_changes_to_production(h)
            lesson_id = await self._create_resulting_lesson(h)
            h.resulting_lesson_id = lesson_id

        elif new_status == "rejected":
            if not payload or "reason" not in payload:
                raise ValueError("reason required for rejected transition")
            h.rejection_reason = payload["reason"]

        elif new_status == "rolled_back":
            if not payload or "reason" not in payload:
                raise ValueError("reason required for rolled_back transition")
            h.rollback_reason = payload["reason"]
            await self._revert_changes(h)

        # (3) 상태 갱신
        h.status = new_status
        h.updated_at = datetime.now(tz=JST)
        await self.db.commit()
        await self.db.refresh(h)
        await _notify_evolution(f"가설 {h.id} [{h.track}] → {new_status} (actor={actor})")
        return h

    # ── Escalation 만료 처리 ──────────────────────────────────

    async def expire_overdue(self) -> list[str]:
        """expires_at 지난 escalation 가설을 자동 rejected. cron 호출용."""
        now = datetime.now(tz=JST)
        stmt = select(Hypothesis).where(
            Hypothesis.track == "escalation",
            Hypothesis.status.in_(["proposed", "backtested"]),
            Hypothesis.expires_at < now,
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        expired_ids: list[str] = []
        for h in rows:
            await self.transition(
                h.id, "rejected",
                actor="system",
                payload={"reason": "expires_at 도과 — 사용자 미승인"},
            )
            expired_ids.append(h.id)
        return expired_ids

    # ── 내부 헬퍼 ─────────────────────────────────────────────

    async def _apply_changes_to_production(self, h: Hypothesis) -> None:
        """changes를 실제 DB(gmoc_strategies.parameters JSONB)에 반영."""
        for ch in (h.changes or []):
            spec = TunableCatalog.get(ch.get("tunable_key", ""))
            if spec is None or spec.db_table is None:
                continue
            if spec.db_table == "gmoc_strategies" and spec.db_path:
                path_parts = spec.db_path.split(".")
                json_path = "{" + ",".join(path_parts[1:]) + "}"
                new_val = json.dumps(ch["proposed_value"])
                try:
                    await self.db.execute(
                        text(
                            "UPDATE gmoc_strategies "
                            "SET parameters = jsonb_set(parameters, :path, :value::jsonb) "
                            "WHERE active = true"
                        ),
                        {"path": json_path, "value": new_val},
                    )
                except Exception as exc:
                    logger.warning("apply_changes failed for %s: %s", ch.get("tunable_key"), exc)
        await self.db.commit()

    async def _revert_changes(self, h: Hypothesis) -> None:
        """rollback — changes[i].current_value로 복원."""
        for ch in (h.changes or []):
            spec = TunableCatalog.get(ch.get("tunable_key", ""))
            if spec is None or spec.db_table is None:
                continue
            if spec.db_table == "gmoc_strategies" and spec.db_path:
                path_parts = spec.db_path.split(".")
                json_path = "{" + ",".join(path_parts[1:]) + "}"
                orig_val = json.dumps(ch.get("current_value"))
                try:
                    await self.db.execute(
                        text(
                            "UPDATE gmoc_strategies "
                            "SET parameters = jsonb_set(parameters, :path, :value::jsonb) "
                            "WHERE active = true"
                        ),
                        {"path": json_path, "value": orig_val},
                    )
                except Exception as exc:
                    logger.warning("revert_changes failed for %s: %s", ch.get("tunable_key"), exc)
        await self.db.commit()

    async def _create_resulting_lesson(self, h: Hypothesis) -> str:
        """adopted 시 Lesson 자동 등록."""
        cr: dict = h.canary_result or {}
        payload = LessonCreate(
            pattern_type="parameter_calibration",
            market_regime=cr.get("regime", "any"),
            pair=cr.get("pair", "any"),
            conditions={"hypothesis_changes": h.changes or []},
            observation=(
                f"가설 {h.id} '{h.title}' canary 검증 통과 — "
                f"sharpe={cr.get('sharpe', '?')}, wr={cr.get('win_rate', '?')}"
            ),
            recommendation=h.description,
            outcome_stats=cr if cr else None,
            confidence=0.7,
            source="hypothesis",
            author=h.proposer,
            hypothesis_id=h.id,
        )
        lessons_service = LessonsService(self.db)
        lesson = await lessons_service.create(payload)
        return lesson.id

    async def _next_id(self) -> str:
        year = datetime.now(tz=JST).year
        prefix = f"H-{year}-"
        stmt = select(func.max(Hypothesis.id)).where(Hypothesis.id.like(f"{prefix}%"))
        last: str | None = (await self.db.execute(stmt)).scalar()
        next_num = int(last.split("-")[-1]) + 1 if last else 1
        return f"{prefix}{next_num:03d}"
