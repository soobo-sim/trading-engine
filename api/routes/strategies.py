"""
Strategy API — 전략 CRUD + 생명주기 관리.

GET    /api/strategies           — 전략 목록
GET    /api/strategies/active    — 활성 전략
GET    /api/strategies/{id}      — 전략 상세
POST   /api/strategies           — 전략 생성
PUT    /api/strategies/{id}/activate — 활성화
PUT    /api/strategies/{id}/archive  — 아카이브
PUT    /api/strategies/{id}/reject   — 거부
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState, get_db, get_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategies", tags=["Strategies"])


# ── Schemas ───────────────────────────────────────────────────

class StrategyCreate(BaseModel):
    name: str
    description: str
    parameters: dict
    rationale: str = Field(..., min_length=20)
    technique_code: str | None = None


class StrategyReject(BaseModel):
    rejection_reason: str = Field(..., min_length=10)


# ── GMO FX 안전장치 ──────────────────────────────────────────

GMO_MAX_POSITION_SIZE_PCT = 5.0   # 레버리지 환경 최대 포지션 비율
GMO_MAX_LEVERAGE = 5.0            # 최대 레버리지


def _validate_gmo_safety(params: dict, state: AppState) -> None:
    """GMO FX(pair_column=pair)에만 적용되는 안전장치 검증."""
    if state.pair_column != "pair":
        return  # BF는 제한 없음

    pos_pct = params.get("position_size_pct")
    if pos_pct is not None:
        try:
            if float(pos_pct) > GMO_MAX_POSITION_SIZE_PCT:
                raise HTTPException(
                    400,
                    f"GMO FX position_size_pct 최대 {GMO_MAX_POSITION_SIZE_PCT}% 초과 "
                    f"(입력: {pos_pct}%). 레버리지 환경 안전장치.",
                )
        except (TypeError, ValueError):
            pass

    leverage = params.get("leverage")
    if leverage is not None:
        try:
            if float(leverage) > GMO_MAX_LEVERAGE:
                raise HTTPException(
                    400,
                    f"GMO FX leverage 최대 {GMO_MAX_LEVERAGE}배 초과 "
                    f"(입력: {leverage}배). 레버리지 환경 안전장치.",
                )
        except (TypeError, ValueError):
            pass



@router.get("")
async def list_strategies(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """전략 목록 (status 필터 선택)."""
    Model = state.models.strategy
    stmt = select(Model).order_by(Model.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Model.status == status)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return {
        "strategies": [_strategy_to_dict(r) for r in rows],
        "total": len(rows),
    }


@router.get("/active")
async def get_active_strategies(
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """활성 전략 목록."""
    Model = state.models.strategy
    stmt = select(Model).where(Model.status == "active")
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_strategy_to_dict(r) for r in rows]


# ── 상세 ─────────────────────────────────────────────────────

@router.get("/{strategy_id}")
async def get_strategy(
    strategy_id: int,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """단일 전략 상세."""
    Model = state.models.strategy
    row = await db.get(Model, strategy_id)
    if not row:
        raise HTTPException(404, "전략 없음")
    return _strategy_to_dict(row)


# ── 생성 ─────────────────────────────────────────────────────

@router.post("")
async def create_strategy(
    body: StrategyCreate,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """전략 생성 (status=proposed)."""
    _validate_gmo_safety(body.parameters or {}, state)
    Model = state.models.strategy
    row = Model(
        name=body.name,
        description=body.description,
        parameters=body.parameters,
        rationale=body.rationale,
        technique_code=body.technique_code,
        status="proposed",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _strategy_to_dict(row)


# ── 생명주기 ─────────────────────────────────────────────────

@router.put("/{strategy_id}/activate")
async def activate_strategy(
    strategy_id: int,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """proposed → active. 동일 pair 기존 전략은 archive."""
    Model = state.models.strategy
    row = await db.get(Model, strategy_id)
    if not row:
        raise HTTPException(404, "전략 없음")
    if row.status != "proposed":
        raise HTTPException(400, f"proposed 상태만 활성화 가능 (현재: {row.status})")
    _validate_gmo_safety(row.parameters or {}, state)

    # 동일 pair 기존 active 아카이브
    pair = (row.parameters or {}).get("pair")
    if pair:
        stmt = (
            select(Model)
            .where(Model.status == "active")
            .where(Model.id != strategy_id)
        )
        result = await db.execute(stmt)
        for existing in result.scalars().all():
            existing_pair = (existing.parameters or {}).get("pair")
            if existing_pair == pair:
                existing.status = "archived"
                existing.archived_at = datetime.now(timezone.utc)

    row.status = "active"
    row.activated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return _strategy_to_dict(row)


@router.put("/{strategy_id}/archive")
async def archive_strategy(
    strategy_id: int,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """active|proposed → archived. 성과 카드 자동 생성."""
    Model = state.models.strategy
    row = await db.get(Model, strategy_id)
    if not row:
        raise HTTPException(404, "전략 없음")
    if row.status not in ("active", "proposed"):
        raise HTTPException(400, f"아카이브 불가 (현재: {row.status})")

    row.status = "archived"
    row.archived_at = datetime.now(timezone.utc)

    # 성과 카드 자동 생성 (1-B)
    pair = (row.parameters or {}).get("pair") or (row.parameters or {}).get("product_code")
    if pair and row.activated_at:
        try:
            from api.services.performance_service import compute_performance_summary
            summary = await compute_performance_summary(
                db=db, state=state,
                strategy_id=row.id, pair=pair,
                activated_at=row.activated_at, archived_at=row.archived_at,
            )
            row.performance_summary = summary
        except Exception as e:
            logger.warning(f"성과 카드 생성 실패 (strategy_id={strategy_id}): {e}")

    await db.commit()
    await db.refresh(row)
    return _strategy_to_dict(row)


@router.put("/{strategy_id}/reject")
async def reject_strategy(
    strategy_id: int,
    body: StrategyReject,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """proposed → rejected."""
    Model = state.models.strategy
    row = await db.get(Model, strategy_id)
    if not row:
        raise HTTPException(404, "전략 없음")
    if row.status != "proposed":
        raise HTTPException(400, f"proposed 상태만 거부 가능 (현재: {row.status})")

    row.status = "rejected"
    row.rejection_reason = body.rejection_reason
    await db.commit()
    await db.refresh(row)
    return _strategy_to_dict(row)


# ── 헬퍼 ─────────────────────────────────────────────────────

def _strategy_to_dict(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "status": row.status,
        "description": row.description,
        "parameters": row.parameters,
        "rationale": row.rationale,
        "technique_code": row.technique_code,
        "rejection_reason": row.rejection_reason,
        "performance_summary": row.performance_summary,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "activated_at": row.activated_at.isoformat() if row.activated_at else None,
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
    }
