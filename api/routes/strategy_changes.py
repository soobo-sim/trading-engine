"""
Strategy Changes API — 전략 변경 이력 저장/조회/업데이트.

POST   /api/strategy-changes          — 변경 기록 생성
GET    /api/strategy-changes          — 이력 조회 (pair/status 필터)
PATCH  /api/strategy-changes/{id}     — Kill 발동 / 사후 평가 기록
GET    /api/strategy-changes/{id}     — 단건 조회
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.models import SC_CHANGE_TYPES, SC_STATUSES, StrategyChange
from api.dependencies import get_db

router = APIRouter(prefix="/api/strategy-changes", tags=["StrategyChanges"])


# ──────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────

class StrategyChangeCreate(BaseModel):
    pair: str
    old_strategy_id: Optional[int] = None
    new_strategy_id: int
    change_type: str
    changed_params: Optional[dict] = None
    trigger: Optional[str] = None
    rationale: Optional[str] = None
    alice_opinion: Optional[str] = None
    samantha_opinion: Optional[str] = None
    rachel_verdict: Optional[str] = None
    kill_conditions: Optional[list] = None
    observation_period: Optional[str] = None
    status: str = "active"

    @field_validator("change_type")
    @classmethod
    def validate_change_type(cls, v: str) -> str:
        if v not in SC_CHANGE_TYPES:
            raise ValueError(f"change_type must be one of {SC_CHANGE_TYPES}")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in SC_STATUSES:
            raise ValueError(f"status must be one of {SC_STATUSES}")
        return v


class StrategyChangePatch(BaseModel):
    status: Optional[str] = None
    kill_triggered_at: Optional[str] = None
    outcome_summary: Optional[str] = None
    rachel_verdict: Optional[str] = None
    alice_opinion: Optional[str] = None
    samantha_opinion: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in SC_STATUSES:
            raise ValueError(f"status must be one of {SC_STATUSES}")
        return v


def _to_dict(r: StrategyChange) -> dict:
    return {
        "id": r.id,
        "pair": r.pair,
        "old_strategy_id": r.old_strategy_id,
        "new_strategy_id": r.new_strategy_id,
        "change_type": r.change_type,
        "changed_params": r.changed_params,
        "trigger": r.trigger,
        "rationale": r.rationale,
        "alice_opinion": r.alice_opinion,
        "samantha_opinion": r.samantha_opinion,
        "rachel_verdict": r.rachel_verdict,
        "kill_conditions": r.kill_conditions,
        "observation_period": r.observation_period,
        "status": r.status,
        "kill_triggered_at": r.kill_triggered_at.isoformat() if r.kill_triggered_at else None,
        "outcome_summary": r.outcome_summary,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# ──────────────────────────────────────────
# Routes
# ──────────────────────────────────────────

@router.post("", status_code=201)
async def create_strategy_change(
    body: StrategyChangeCreate,
    db: AsyncSession = Depends(get_db),
):
    """전략 변경 기록 생성."""
    rec = StrategyChange(**body.model_dump())
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return {"strategy_change": _to_dict(rec)}


@router.get("")
async def list_strategy_changes(
    pair: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    change_type: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """전략 변경 이력 조회."""
    if status and status not in SC_STATUSES:
        raise HTTPException(400, f"status must be one of {SC_STATUSES}")
    if change_type and change_type not in SC_CHANGE_TYPES:
        raise HTTPException(400, f"change_type must be one of {SC_CHANGE_TYPES}")

    conditions = []
    if pair:
        conditions.append(StrategyChange.pair == pair)
    if status:
        conditions.append(StrategyChange.status == status)
    if change_type:
        conditions.append(StrategyChange.change_type == change_type)

    stmt = select(StrategyChange).order_by(desc(StrategyChange.created_at)).limit(limit)
    if conditions:
        stmt = stmt.where(*conditions)
    rows = (await db.execute(stmt)).scalars().all()
    return {"strategy_changes": [_to_dict(r) for r in rows], "count": len(rows)}


@router.get("/{change_id}")
async def get_strategy_change(
    change_id: int,
    db: AsyncSession = Depends(get_db),
):
    """단건 조회."""
    row = (await db.execute(
        select(StrategyChange).where(StrategyChange.id == change_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"strategy_change id={change_id} 없음")
    return {"strategy_change": _to_dict(row)}


@router.patch("/{change_id}")
async def patch_strategy_change(
    change_id: int,
    body: StrategyChangePatch,
    db: AsyncSession = Depends(get_db),
):
    """Kill 발동 / 사후 평가 기록."""
    row = (await db.execute(
        select(StrategyChange).where(StrategyChange.id == change_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"strategy_change id={change_id} 없음")

    update_data = body.model_dump(exclude_none=True)
    if "kill_triggered_at" in update_data:
        try:
            update_data["kill_triggered_at"] = datetime.fromisoformat(update_data["kill_triggered_at"])
        except ValueError:
            raise HTTPException(400, f"kill_triggered_at 형식 오류")

    # status → killed 전환 시 kill_triggered_at 자동 설정
    if update_data.get("status") == "killed" and row.kill_triggered_at is None and "kill_triggered_at" not in update_data:
        update_data["kill_triggered_at"] = datetime.now(tz=__import__("datetime").timezone.utc)

    for k, v in update_data.items():
        setattr(row, k, v)

    await db.commit()
    await db.refresh(row)
    return {"strategy_change": _to_dict(row)}
