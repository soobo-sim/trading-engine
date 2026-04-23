"""
LessonsService — P2 외장 기억 저장소 CRUD.

ID 형식: L-{YYYY}-{NNN} (매년 001부터 증가)
delete()는 물리 삭제 없이 status=deprecated 소프트 삭제.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.lesson_model import Lesson
from api.schemas.evolution import LessonCreate, LessonUpdate

# KST/JST 오프셋 (+9h)
JST = timezone(timedelta(hours=9))

# pattern_type 통제 어휘
VALID_PATTERN_TYPES = frozenset({
    "entry_condition",
    "exit_condition",
    "regime_transition",
    "parameter_calibration",
    "macro_context",
    "risk_management",
    "data_quality",
    "workflow_process",
    "meta",
})

VALID_STATUSES = frozenset({"active", "deprecated", "superseded", "draft"})


class LessonsService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── 생성 ─────────────────────────────────────────────────

    async def create(self, payload: LessonCreate) -> Lesson:
        if payload.pattern_type not in VALID_PATTERN_TYPES:
            raise ValueError(f"Unknown pattern_type: {payload.pattern_type!r}")

        new_id = await self._next_id()
        lesson = Lesson(
            id=new_id,
            hypothesis_id=payload.hypothesis_id,
            pattern_type=payload.pattern_type,
            market_regime=payload.market_regime,
            pair=payload.pair,
            conditions=payload.conditions or {},
            observation=payload.observation,
            recommendation=payload.recommendation,
            outcome_stats=payload.outcome_stats,
            confidence=payload.confidence,
            source=payload.source,
            author=payload.author,
            status="active",
        )
        self.db.add(lesson)
        await self.db.commit()
        await self.db.refresh(lesson)
        return lesson

    # ── 조회 ─────────────────────────────────────────────────

    async def get(self, lesson_id: str) -> Lesson | None:
        return await self.db.get(Lesson, lesson_id)

    async def list(
        self,
        *,
        status: str | None = None,
        pattern_type: str | None = None,
        market_regime: str | None = None,
        pair: str | None = None,
        min_confidence: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Lesson]]:
        stmt = select(Lesson)

        if status:
            stmt = stmt.where(Lesson.status == status)
        if pattern_type:
            stmt = stmt.where(Lesson.pattern_type == pattern_type)
        if market_regime:
            # "any" 레코드도 항상 포함
            stmt = stmt.where(Lesson.market_regime.in_([market_regime, "any"]))
        if pair:
            stmt = stmt.where(Lesson.pair.in_([pair, "any", None]))
        if min_confidence is not None:
            stmt = stmt.where(Lesson.confidence >= min_confidence)

        # 카운트
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total: int = (await self.db.execute(count_stmt)).scalar() or 0

        # 페이지
        stmt = (
            stmt
            .order_by(Lesson.confidence.desc(), Lesson.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return total, list(rows)

    async def stats(self) -> dict[str, Any]:
        """status별 / pattern_type별 카운트."""
        # status 분포
        status_rows = (
            await self.db.execute(
                select(Lesson.status, func.count(Lesson.id)).group_by(Lesson.status)
            )
        ).all()
        by_status = {row[0]: row[1] for row in status_rows}

        # pattern_type 분포
        pt_rows = (
            await self.db.execute(
                select(Lesson.pattern_type, func.count(Lesson.id))
                .group_by(Lesson.pattern_type)
            )
        ).all()
        by_pattern_type = {row[0]: row[1] for row in pt_rows}

        total = sum(by_status.values())
        return {
            "total": total,
            "by_status": by_status,
            "by_pattern_type": by_pattern_type,
        }

    # ── 수정 ─────────────────────────────────────────────────

    async def update(self, lesson_id: str, payload: LessonUpdate) -> Lesson | None:
        lesson = await self.get(lesson_id)
        if lesson is None:
            return None

        updates = payload.model_dump(exclude_unset=True)

        # superseded_by 타깃 검증
        if "superseded_by" in updates and updates["superseded_by"] is not None:
            target_id = updates["superseded_by"]
            if target_id == lesson_id:
                raise ValueError("superseded_by cannot point to itself")
            target = await self.get(target_id)
            if target is None:
                raise ValueError(f"superseded_by target not found: {target_id!r}")

        for k, v in updates.items():
            setattr(lesson, k, v)

        # status=superseded 이면 superseded_by 필수
        if lesson.status == "superseded" and not lesson.superseded_by:
            raise ValueError("status=superseded requires superseded_by")

        lesson.updated_at = datetime.now(tz=JST)
        await self.db.commit()
        await self.db.refresh(lesson)
        return lesson

    # ── 삭제 (soft) ──────────────────────────────────────────

    async def delete(self, lesson_id: str) -> bool:
        """물리 삭제 금지. status=deprecated 로 soft delete."""
        lesson = await self.get(lesson_id)
        if lesson is None:
            return False
        lesson.status = "deprecated"
        lesson.updated_at = datetime.now(tz=JST)
        await self.db.commit()
        return True

    # ── ID 발급 ──────────────────────────────────────────────

    async def _next_id(self) -> str:
        year = datetime.now(tz=JST).year
        prefix = f"L-{year}-"
        stmt = select(func.max(Lesson.id)).where(Lesson.id.like(f"{prefix}%"))
        last: str | None = (await self.db.execute(stmt)).scalar()
        next_num = int(last.split("-")[-1]) + 1 if last else 1
        return f"{prefix}{next_num:03d}"
