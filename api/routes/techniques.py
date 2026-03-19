"""
Techniques API — strategy_techniques 마스터 테이블 CRUD.

GET   /api/techniques         — 기법 목록
GET   /api/techniques/{code}  — 기법 상세
PATCH /api/techniques/{code}/notes — 경험 노트 업데이트
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState, get_db, get_state

router = APIRouter(prefix="/api/techniques", tags=["Techniques"])


class TechniqueNotesUpdate(BaseModel):
    experience_notes: str = Field(..., min_length=5)


@router.get("")
async def list_techniques(
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """전체 기법 목록."""
    Model = state.models.technique
    stmt = select(Model).order_by(Model.code)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_technique_to_dict(r) for r in rows]


@router.get("/{code}")
async def get_technique(
    code: str,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """단일 기법 상세."""
    Model = state.models.technique
    stmt = select(Model).where(Model.code == code)
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(404, f"기법 없음: {code}")
    return _technique_to_dict(row)


@router.patch("/{code}/notes")
async def update_technique_notes(
    code: str,
    body: TechniqueNotesUpdate,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """경험 노트 업데이트 (Rachel 전용)."""
    Model = state.models.technique
    stmt = select(Model).where(Model.code == code)
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(404, f"기법 없음: {code}")
    row.experience_notes = body.experience_notes
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return _technique_to_dict(row)


def _technique_to_dict(row) -> dict:
    return {
        "code": row.code,
        "name": row.name,
        "description": row.description,
        "risk_level": row.risk_level,
        "observed_wins": row.observed_wins,
        "observed_losses": row.observed_losses,
        "avg_pnl_pct": float(row.avg_pnl_pct) if row.avg_pnl_pct else None,
        "experience_notes": row.experience_notes,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    }
