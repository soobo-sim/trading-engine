"""
Wake-Up Reviews API — 정신차리자 파이프라인 리뷰 저장/조회.

POST   /api/wake-up-reviews               — 리뷰 생성
GET    /api/wake-up-reviews               — 목록 조회 (필터)
GET    /api/wake-up-reviews/lessons       — 교훈 집계 (Phase 1)
GET    /api/wake-up-reviews/{position_id} — 단건 조회
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import desc, func as sqlfunc, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.models import (
    CAUSE_CODES,
    RACHEL_VERDICTS,
    REVIEW_STATUSES,
    WakeUpReview,
)
from api.dependencies import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wake-up-reviews", tags=["WakeUpReviews"])


# ──────────────────────────────────────────
# Pydantic schemas
# ──────────────────────────────────────────

class WakeUpReviewCreate(BaseModel):
    position_id: Optional[int] = None
    strategy_id: Optional[int] = None
    pair: str
    entry_price: float
    exit_price: float
    realized_pnl: float
    cause_code: str
    review_status: str = "draft"

    # optional
    cause_detail: Optional[str] = None
    sub_cause: Optional[str] = None
    holding_duration_min: Optional[int] = None
    entry_regime: Optional[str] = None
    actual_regime: Optional[str] = None
    simulation_hold_pnl: Optional[float] = None
    simulation_best_exit_pnl: Optional[float] = None
    simulation_verdict: Optional[str] = None
    capital_at_entry: Optional[float] = None
    position_size_pct: Optional[float] = None
    alice_analysis: Optional[str] = None
    samantha_audit: Optional[str] = None
    rachel_verdict: Optional[str] = None
    rachel_rationale: Optional[str] = None
    lessons_learned: Optional[str] = None
    param_changes: Optional[dict] = None
    optimistic_ev: Optional[float] = None
    pessimistic_ev: Optional[float] = None
    pessimistic_max_loss: Optional[float] = None
    grid_search_result: Optional[dict] = None
    overfit_risk: Optional[str] = None
    kill_condition_met: bool = False
    kill_condition_text: Optional[str] = None
    safety_check_ok: Optional[bool] = None
    stop_loss_price: Optional[float] = None
    actual_stop_hit_price: Optional[float] = None

    @field_validator("cause_code")
    @classmethod
    def validate_cause_code(cls, v: str) -> str:
        if v not in CAUSE_CODES:
            raise ValueError(f"cause_code must be one of {CAUSE_CODES}")
        return v

    @field_validator("review_status")
    @classmethod
    def validate_review_status(cls, v: str) -> str:
        if v not in REVIEW_STATUSES:
            raise ValueError(f"review_status must be one of {REVIEW_STATUSES}")
        return v

    @field_validator("rachel_verdict")
    @classmethod
    def validate_rachel_verdict(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in RACHEL_VERDICTS:
            raise ValueError(f"rachel_verdict must be one of {RACHEL_VERDICTS}")
        return v


def _review_to_dict(r: WakeUpReview) -> dict:
    return {
        "id": r.id,
        "position_id": r.position_id,
        "strategy_id": r.strategy_id,
        "pair": r.pair,
        "entry_price": float(r.entry_price),
        "exit_price": float(r.exit_price),
        "realized_pnl": float(r.realized_pnl),
        "cause_code": r.cause_code,
        "review_status": r.review_status,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "cause_detail": r.cause_detail,
        "sub_cause": r.sub_cause,
        "holding_duration_min": r.holding_duration_min,
        "entry_regime": r.entry_regime,
        "actual_regime": r.actual_regime,
        "simulation_hold_pnl": float(r.simulation_hold_pnl) if r.simulation_hold_pnl is not None else None,
        "simulation_best_exit_pnl": float(r.simulation_best_exit_pnl) if r.simulation_best_exit_pnl is not None else None,
        "simulation_verdict": r.simulation_verdict,
        "capital_at_entry": float(r.capital_at_entry) if r.capital_at_entry is not None else None,
        "position_size_pct": float(r.position_size_pct) if r.position_size_pct is not None else None,
        "alice_analysis": r.alice_analysis,
        "samantha_audit": r.samantha_audit,
        "rachel_verdict": r.rachel_verdict,
        "rachel_rationale": r.rachel_rationale,
        "lessons_learned": r.lessons_learned,
        "param_changes": r.param_changes,
        "optimistic_ev": float(r.optimistic_ev) if r.optimistic_ev is not None else None,
        "pessimistic_ev": float(r.pessimistic_ev) if r.pessimistic_ev is not None else None,
        "pessimistic_max_loss": float(r.pessimistic_max_loss) if r.pessimistic_max_loss is not None else None,
        "overfit_risk": r.overfit_risk,
        "kill_condition_met": r.kill_condition_met,
        "kill_condition_text": r.kill_condition_text,
        "safety_check_ok": r.safety_check_ok,
        "stop_loss_price": float(r.stop_loss_price) if r.stop_loss_price is not None else None,
        "actual_stop_hit_price": float(r.actual_stop_hit_price) if r.actual_stop_hit_price is not None else None,
        "rejection_count": r.rejection_count,
    }


# ──────────────────────────────────────────
# Routes — 고정 경로 먼저
# ──────────────────────────────────────────

@router.get("/lessons")
async def get_lessons(
    pair: Optional[str] = Query(None),
    strategy_id: Optional[int] = Query(None),
    limit: int = Query(10, ge=1, le=100),
    kill_met: Optional[bool] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """교훈 집계 API (Phase 1: top_causes + recent_lessons)."""
    conditions = []
    if pair:
        conditions.append(WakeUpReview.pair == pair)
    if strategy_id:
        conditions.append(WakeUpReview.strategy_id == strategy_id)
    if kill_met is not None:
        conditions.append(WakeUpReview.kill_condition_met == kill_met)

    # total_reviews
    count_stmt = select(sqlfunc.count()).select_from(WakeUpReview)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = (await db.execute(count_stmt)).scalar_one()

    # avg / worst loss
    agg_stmt = select(
        sqlfunc.avg(WakeUpReview.realized_pnl),
        sqlfunc.min(WakeUpReview.realized_pnl),
    ).select_from(WakeUpReview)
    if conditions:
        agg_stmt = agg_stmt.where(*conditions)
    agg = (await db.execute(agg_stmt)).one()
    avg_loss = round(float(agg[0]), 0) if agg[0] is not None else None
    worst_loss = round(float(agg[1]), 0) if agg[1] is not None else None

    # top_causes
    cause_stmt = (
        select(WakeUpReview.cause_code, sqlfunc.count().label("cnt"))
        .select_from(WakeUpReview)
        .group_by(WakeUpReview.cause_code)
        .order_by(desc("cnt"))
    )
    if conditions:
        cause_stmt = cause_stmt.where(*conditions)
    cause_rows = (await db.execute(cause_stmt)).all()
    top_causes = [
        {
            "code": row.cause_code,
            "count": row.cnt,
            "pct": round(row.cnt / total * 100, 1) if total else 0,
        }
        for row in cause_rows
    ]

    # recent_lessons
    lesson_stmt = (
        select(WakeUpReview)
        .where(WakeUpReview.lessons_learned.isnot(None))
        .order_by(desc(WakeUpReview.created_at))
        .limit(limit)
    )
    if conditions:
        lesson_stmt = lesson_stmt.where(*conditions)
    lesson_rows = (await db.execute(lesson_stmt)).scalars().all()
    recent_lessons = [
        {
            "id": r.id,
            "pair": r.pair,
            "cause_code": r.cause_code,
            "realized_pnl": float(r.realized_pnl),
            "lessons_learned": r.lessons_learned,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in lesson_rows
    ]

    return {
        "pair": pair,
        "strategy_id": strategy_id,
        "summary": {
            "total_reviews": total,
            "top_causes": top_causes,
            "avg_loss": avg_loss,
            "worst_loss": worst_loss,
        },
        "recent_lessons": recent_lessons,
    }


@router.post("", status_code=201)
async def create_review(
    body: WakeUpReviewCreate,
    db: AsyncSession = Depends(get_db),
):
    """리뷰 생성. rachel_verdict 저장 시 alice_analysis + samantha_approved 필수."""
    if body.rachel_verdict is not None:
        if not body.alice_analysis:
            raise HTTPException(422, "rachel_verdict 저장 시 alice_analysis 필수")
        if body.review_status != "samantha_approved":
            raise HTTPException(422, "rachel_verdict 저장 시 review_status=samantha_approved 필수")

    rec = WakeUpReview(**body.model_dump())
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return {"review": _review_to_dict(rec)}


@router.get("")
async def list_reviews(
    pair: Optional[str] = Query(None),
    cause_code: Optional[str] = Query(None),
    strategy_id: Optional[int] = Query(None),
    review_status: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """리뷰 목록 조회."""
    conditions = []
    if pair:
        conditions.append(WakeUpReview.pair == pair)
    if cause_code:
        if cause_code not in CAUSE_CODES:
            raise HTTPException(400, f"cause_code must be one of {CAUSE_CODES}")
        conditions.append(WakeUpReview.cause_code == cause_code)
    if strategy_id:
        conditions.append(WakeUpReview.strategy_id == strategy_id)
    if review_status:
        if review_status not in REVIEW_STATUSES:
            raise HTTPException(400, f"review_status must be one of {REVIEW_STATUSES}")
        conditions.append(WakeUpReview.review_status == review_status)
    if date_from:
        try:
            conditions.append(WakeUpReview.created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            raise HTTPException(400, f"date_from 형식 오류: {date_from}")
    if date_to:
        try:
            conditions.append(WakeUpReview.created_at <= datetime.fromisoformat(date_to))
        except ValueError:
            raise HTTPException(400, f"date_to 형식 오류: {date_to}")

    stmt = select(WakeUpReview).order_by(desc(WakeUpReview.created_at)).limit(limit)
    if conditions:
        stmt = stmt.where(*conditions)
    rows = (await db.execute(stmt)).scalars().all()
    return {"reviews": [_review_to_dict(r) for r in rows], "count": len(rows)}


@router.get("/{position_id}")
async def get_review(
    position_id: int,
    db: AsyncSession = Depends(get_db),
):
    """포지션 ID로 리뷰 단건 조회."""
    stmt = (
        select(WakeUpReview)
        .where(WakeUpReview.position_id == position_id)
        .order_by(desc(WakeUpReview.created_at))
        .limit(1)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"position {position_id}에 대한 리뷰 없음")
    return {"review": _review_to_dict(row)}
