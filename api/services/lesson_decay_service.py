"""
lesson_decay_service.py — P7 Lesson 신뢰도 감쇠 + Hypothesis archived 정리.

`POST /api/lessons/decay-stale` 엔드포인트가 이 서비스를 호출.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

JST = timezone(timedelta(hours=9))
logger = logging.getLogger("core.judge.evolution.lesson_decay")

# ── 감쇠 규칙 ────────────────────────────────────────────────

DECAY_RULES = [
    (timedelta(days=180), 0.7),  # 180일+ → ×0.7
    (timedelta(days=90), 0.9),   # 90일~179일 → ×0.9
]
DEPRECATE_THRESHOLD = 0.3


# ── 보고 스키마 ──────────────────────────────────────────────

class DecayReport(BaseModel):
    run_at: datetime
    decayed: list[tuple[str, float, float]]  # (lesson_id, old_confidence, new_confidence)
    deprecated: list[str]
    archived_hypotheses: list[str]
    total_active_after: int


def format_decay_report(report: DecayReport) -> str:
    return (
        f"📚 Lesson 감쇠 보고 ({report.run_at.strftime('%Y-%m-%d')})\n"
        f"감쇠: {len(report.decayed)}건\n"
        f"deprecated: {len(report.deprecated)}건\n"
        f"archived 가설: {len(report.archived_hypotheses)}건\n"
        f"active lessons 잔여: {report.total_active_after}건"
    )


# ── 서비스 ───────────────────────────────────────────────────

class LessonDecayService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def run(self) -> DecayReport:
        from adapters.database.lesson_model import Lesson
        from adapters.database.hypothesis_model import Hypothesis

        now = datetime.now(tz=JST)
        decayed: list[tuple[str, float, float]] = []
        deprecated: list[str] = []
        archived: list[str] = []

        # (1) active lessons 감쇠
        stmt = select(Lesson).where(Lesson.status == "active")
        active = (await self.db.execute(stmt)).scalars().all()

        for lesson in active:
            ref_at = getattr(lesson, "last_referenced_at", None) or lesson.created_at
            if ref_at.tzinfo is None:
                ref_at = ref_at.replace(tzinfo=JST)
            age = now - ref_at

            # 이미 이 사이클에 감쇠됐으면 skip (25일 이내)
            last_decay = getattr(lesson, "last_decay_at", None)
            if last_decay is not None:
                if last_decay.tzinfo is None:
                    last_decay = last_decay.replace(tzinfo=JST)
                if (now - last_decay).days < 25:
                    continue

            new_conf = float(lesson.confidence)
            for threshold, mult in DECAY_RULES:
                if age >= threshold:
                    new_conf = round(new_conf * mult, 3)
                    break  # 가장 큰 감쇠 한 번만

            if new_conf < float(lesson.confidence):
                old = float(lesson.confidence)
                lesson.confidence = new_conf
                if hasattr(lesson, "last_decay_at"):
                    lesson.last_decay_at = now
                decayed.append((lesson.id, old, new_conf))

            if float(lesson.confidence) < DEPRECATE_THRESHOLD:
                lesson.status = "deprecated"
                deprecated.append(lesson.id)

        # (2) rolled_back hypotheses → archived (30일 경과)
        stmt2 = select(Hypothesis).where(
            Hypothesis.status == "rolled_back",
        )
        rb = (await self.db.execute(stmt2)).scalars().all()
        for h in rb:
            upd_at = h.updated_at
            if upd_at is None:
                continue
            if upd_at.tzinfo is None:
                upd_at = upd_at.replace(tzinfo=JST)
            if (now - upd_at).days >= 30:
                # 직접 갱신 (서비스 순환 방지)
                from core.judge.evolution.lifecycle import validate_transition
                try:
                    validate_transition(h.status, "archived")
                    h.status = "archived"
                    h.updated_at = now
                    archived.append(h.id)
                except Exception:
                    pass

        await self.db.commit()

        active_after = len(active) - len(deprecated)
        report = DecayReport(
            run_at=now,
            decayed=decayed,
            deprecated=deprecated,
            archived_hypotheses=archived,
            total_active_after=active_after,
        )

        # 진화 채널 알림
        try:
            from core.judge.evolution.notifications import notify_evolution
            msg = format_decay_report(report)
            await notify_evolution(msg)
        except Exception as exc:
            logger.debug("notify_evolution 실패(무시): %s", exc)

        return report
