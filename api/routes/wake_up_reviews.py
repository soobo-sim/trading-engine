"""
Wake-Up Reviews API — 정신차리자 파이프라인 리뷰 저장/조회.

POST   /api/wake-up-reviews                               — 리뽰 생성
GET    /api/wake-up-reviews                               — 목록 조회 (필터)
GET    /api/wake-up-reviews/lessons                       — 교훈 집계
GET    /api/wake-up-reviews/open-actions                  — 미완료 액션아이템 (전체)
GET    /api/wake-up-reviews/patterns                      — 반복 패턴 집계 (root_cause_codes)
GET    /api/wake-up-reviews/{review_id}/action-items      — 단건 액션아이템
PATCH  /api/wake-up-reviews/{review_id}/action-items/{item_id} — 액션아이템 상태 업데이트
GET    /api/wake-up-reviews/{position_id}                 — 단건 조회 (position FK)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import desc, func as sqlfunc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.models import (
    CAUSE_CODES,
    OVERFIT_RISKS,
    RACHEL_VERDICTS,
    REVIEW_STATUSES,
    ROOT_CAUSE_CODES,
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
    exchange: Optional[str] = None          # bf / gmo (BUG-025)
    position_type: Optional[str] = None     # trend / box
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

    # ── Section I: 최적 파라미터 역산 ──────────────────────────────────────────
    optimal_params: Optional[dict] = None
    optimal_pnl: Optional[float] = None
    optimal_pnl_pct: Optional[float] = None
    actual_vs_optimal_diff_pct: Optional[float] = None
    optimal_long_term_ev: Optional[float] = None
    optimal_long_term_wr: Optional[float] = None
    optimal_long_term_sharpe: Optional[float] = None
    optimal_long_term_trades: Optional[int] = None
    optimal_overfit_risk: Optional[str] = None
    optimal_entry_timing: Optional[str] = None
    optimal_exit_timing: Optional[str] = None
    optimal_key_diff: Optional[str] = None

    # ── Section J: 근본 원인 ────────────────────────────────────────────────────
    root_cause_codes: Optional[List[str]] = None
    root_cause_detail: Optional[str] = None
    decision_date: Optional[str] = None   # ISO date string (YYYY-MM-DD)
    decision_by: Optional[str] = None
    info_gap_had: Optional[str] = None
    info_gap_new: Optional[str] = None

    # ── Section K: 액션 아이템 ──────────────────────────────────────────────────
    action_items: Optional[List[dict]] = None
    prevention_checklist: Optional[List[dict]] = None
    review_quality_score: Optional[float] = None

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

    @field_validator("optimal_overfit_risk")
    @classmethod
    def validate_optimal_overfit_risk(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in OVERFIT_RISKS:
            raise ValueError(f"optimal_overfit_risk must be one of {OVERFIT_RISKS}")
        return v

    @field_validator("root_cause_codes")
    @classmethod
    def validate_root_cause_codes(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is not None:
            invalid = [c for c in v if c not in ROOT_CAUSE_CODES]
            if invalid:
                raise ValueError(f"root_cause_codes에 유효하지 않은 값: {invalid}. 허용: {ROOT_CAUSE_CODES}")
        return v


def _review_to_dict(r: WakeUpReview) -> dict:
    return {
        "id": r.id,
        "position_id": r.position_id,
        "strategy_id": r.strategy_id,
        "exchange": r.exchange,
        "position_type": r.position_type,
        "pipeline_status": r.pipeline_status,
        "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
        "pipeline_started_at": r.pipeline_started_at.isoformat() if r.pipeline_started_at else None,
        "pipeline_completed_at": r.pipeline_completed_at.isoformat() if r.pipeline_completed_at else None,
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
        # Section I
        "optimal_params": r.optimal_params,
        "optimal_pnl": float(r.optimal_pnl) if r.optimal_pnl is not None else None,
        "optimal_pnl_pct": float(r.optimal_pnl_pct) if r.optimal_pnl_pct is not None else None,
        "actual_vs_optimal_diff_pct": float(r.actual_vs_optimal_diff_pct) if r.actual_vs_optimal_diff_pct is not None else None,
        "optimal_long_term_ev": float(r.optimal_long_term_ev) if r.optimal_long_term_ev is not None else None,
        "optimal_long_term_wr": float(r.optimal_long_term_wr) if r.optimal_long_term_wr is not None else None,
        "optimal_long_term_sharpe": float(r.optimal_long_term_sharpe) if r.optimal_long_term_sharpe is not None else None,
        "optimal_long_term_trades": r.optimal_long_term_trades,
        "optimal_overfit_risk": r.optimal_overfit_risk,
        "optimal_entry_timing": r.optimal_entry_timing,
        "optimal_exit_timing": r.optimal_exit_timing,
        "optimal_key_diff": r.optimal_key_diff,
        # Section J
        "root_cause_codes": list(r.root_cause_codes) if r.root_cause_codes is not None else None,
        "root_cause_detail": r.root_cause_detail,
        "decision_date": r.decision_date.isoformat() if r.decision_date is not None else None,
        "decision_by": r.decision_by,
        "info_gap_had": r.info_gap_had,
        "info_gap_new": r.info_gap_new,
        # Section K
        "action_items": r.action_items,
        "prevention_checklist": r.prevention_checklist,
        "review_quality_score": float(r.review_quality_score) if r.review_quality_score is not None else None,
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
    min_repeat: Optional[int] = Query(None, ge=1, description="동일 cause_code N회 이상 반복된 원인만"),
    db: AsyncSession = Depends(get_db),
):
    """교훈 집계 API (Phase 1: top_causes + recent_lessons).
    min_repeat: top_causes를 N회 이상 반복된 원인만 필터링.
    """
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

    # top_causes (min_repeat 필터 포함)
    cause_stmt = (
        select(WakeUpReview.cause_code, sqlfunc.count().label("cnt"))
        .select_from(WakeUpReview)
        .group_by(WakeUpReview.cause_code)
        .order_by(desc("cnt"))
    )
    if conditions:
        cause_stmt = cause_stmt.where(*conditions)
    if min_repeat is not None:
        cause_stmt = cause_stmt.having(sqlfunc.count() >= min_repeat)
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


# ──────────────────────────────────────────────────────────────
# 신규 고정 경로 (/{position_id} 앞에 등록 필수)
# ──────────────────────────────────────────────────────────────

@router.get("/open-actions")
async def get_open_actions(
    assignee: Optional[str] = Query(None, description="담당자 필터 (e.g. 'maria')"),
    db: AsyncSession = Depends(get_db),
):
    """미완료 액션아이템 전체 조회 (cross-review). action_items 배열에서 status != done/skipped 추출."""
    stmt = select(WakeUpReview).where(WakeUpReview.action_items.isnot(None))
    rows = (await db.execute(stmt)).scalars().all()

    open_actions = []
    for r in rows:
        if not r.action_items:
            continue
        for item in r.action_items:
            if not isinstance(item, dict):
                continue
            if item.get("status") in ("done", "skipped"):
                continue
            if assignee and item.get("assignee") != assignee:
                continue
            open_actions.append({
                "review_id": r.id,
                "pair": r.pair,
                "strategy_id": r.strategy_id,
                "cause_code": r.cause_code,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                **{k: item.get(k) for k in ("id", "category", "action", "assignee", "deadline", "measure", "status", "result")},
            })

    return {"open_actions": open_actions, "count": len(open_actions)}


@router.get("/patterns")
async def get_patterns(
    db: AsyncSession = Depends(get_db),
):
    """root_cause_codes 기반 반복 패턴 집계. unnest로 각 코드 카운트."""
    # SQLAlchemy unnest: text() fallback 사용 (PostgreSQL 전용)
    query = text(
        """
        SELECT code, COUNT(*) AS cnt
        FROM wake_up_reviews, unnest(root_cause_codes) AS code
        GROUP BY code
        ORDER BY cnt DESC
        """
    )
    result = await db.execute(query)
    rows = result.all()

    total_reviews_stmt = select(sqlfunc.count()).select_from(WakeUpReview).where(
        WakeUpReview.root_cause_codes.isnot(None)
    )
    total = (await db.execute(total_reviews_stmt)).scalar_one()

    patterns = [
        {
            "code": row.code,
            "count": row.cnt,
            "pct": round(row.cnt / total * 100, 1) if total else 0,
        }
        for row in rows
    ]
    return {"patterns": patterns, "total_reviews_with_codes": total}


@router.get("/by-id/{review_id}")
async def get_review_by_id(
    review_id: int,
    db: AsyncSession = Depends(get_db),
):
    """PK(id) 기준 리뷰 단건 조회. BFF 복합키 라우팅용."""
    stmt = select(WakeUpReview).where(WakeUpReview.id == review_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"review_id={review_id} 없음")
    return {"review": _review_to_dict(row)}


@router.get("/{review_id}/action-items")
async def get_action_items(
    review_id: int,
    db: AsyncSession = Depends(get_db),
):
    """WakeUpReview PK(id) 기준 액션아이템 목록 조회."""
    stmt = select(WakeUpReview).where(WakeUpReview.id == review_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"review_id={review_id} 없음")
    return {
        "review_id": review_id,
        "action_items": row.action_items or [],
        "prevention_checklist": row.prevention_checklist or [],
    }


class ActionItemPatch(BaseModel):
    status: str  # "open" | "done" | "skipped"
    result: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("open", "done", "skipped"):
            raise ValueError("status must be one of: open, done, skipped")
        return v


@router.patch("/{review_id}/action-items/{item_id}", status_code=200)
async def patch_action_item(
    review_id: int,
    item_id: str,
    body: ActionItemPatch,
    db: AsyncSession = Depends(get_db),
):
    """액션아이템 상태 업데이트 (read-modify-write). item_id는 action_items[].id 값."""
    stmt = select(WakeUpReview).where(WakeUpReview.id == review_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"review_id={review_id} 없음")

    items = list(row.action_items or [])
    target_idx = next((i for i, it in enumerate(items) if isinstance(it, dict) and it.get("id") == item_id), None)
    if target_idx is None:
        raise HTTPException(404, f"item_id={item_id!r} 없음 (review_id={review_id})")

    items[target_idx] = {
        **items[target_idx],
        "status": body.status,
        "result": body.result,
        "completed_at": datetime.now(timezone.utc).isoformat() if body.status == "done" else items[target_idx].get("completed_at"),
    }

    # JSONB 컬럼은 mutation 감지 안 됨 → 새 객체로 교체
    from sqlalchemy.orm.attributes import flag_modified
    row.action_items = items
    flag_modified(row, "action_items")
    await db.commit()
    await db.refresh(row)

    return {"review_id": review_id, "updated_item": items[target_idx]}


# ──────────────────────────────────────────────────────────────
# PATCH /{review_id}/checklist/{item_id}
# ──────────────────────────────────────────────────────────────

class ChecklistItemPatch(BaseModel):
    checked: bool


@router.patch("/{review_id}/checklist/{item_id}", status_code=200)
async def patch_checklist_item(
    review_id: int,
    item_id: str,
    body: ChecklistItemPatch,
    db: AsyncSession = Depends(get_db),
):
    """재발 방지 체크리스트 항목 체크/언체크 (JSONB read-modify-write)."""
    stmt = select(WakeUpReview).where(WakeUpReview.id == review_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"review_id={review_id} 없음")

    items = list(row.prevention_checklist or [])
    target_idx = next(
        (i for i, it in enumerate(items) if isinstance(it, dict) and it.get("id") == item_id),
        None,
    )
    if target_idx is None:
        raise HTTPException(404, f"item_id={item_id!r} 없음 (review_id={review_id})")

    items[target_idx] = {**items[target_idx], "checked": body.checked}

    from sqlalchemy.orm.attributes import flag_modified
    row.prevention_checklist = items
    flag_modified(row, "prevention_checklist")
    await db.commit()
    await db.refresh(row)

    return {"review_id": review_id, "updated_item": items[target_idx]}


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
