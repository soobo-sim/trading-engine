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
import os
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

GMO_MAX_POSITION_SIZE_PCT = float(os.environ.get("GMO_MAX_POSITION_SIZE_PCT", "50.0"))
GMO_MAX_LEVERAGE = float(os.environ.get("GMO_MAX_LEVERAGE", "5.0"))


def _validate_gmo_safety(params: dict, state: AppState) -> None:
    """GMO FX(prefix=gmo)에만 적용되는 안전장치 검증.

    GMO Coin(prefix=gmoc)은 암호화폐 레버리지로 FX와 특성이 달라 별도 적용하지 않음.
    - position_size_pct: BTC 100% 허용 (FX 50% 제한은 USD/JPY 기준)
    - regime 임계값: BTC 기본값 사용 가능 (FX 필수 아님)
    """
    if state.prefix != "gmo":
        return  # BF(bf), GMO Coin(gmoc)은 이 검증 불필요

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

    # regime 임계값 필수 검증 (FX는 BTC 기본값과 크게 다름)
    _REGIME_REQUIRED_STYLES = {"trend_following", "cfd_trend_following"}
    _REGIME_KEYS = [
        "bb_width_trending_min",
        "range_pct_trending_min",
        "bb_width_ranging_max",
        "range_pct_ranging_max",
    ]
    style = params.get("trading_style", "")
    if style in _REGIME_REQUIRED_STYLES:
        missing = [k for k in _REGIME_KEYS if k not in params]
        if missing:
            raise HTTPException(
                400,
                f"GMO FX {style} 전략에 regime 임계값 필수 "
                f"(BTC 기본값은 FX에 부적합). 누락: {missing}",
            )



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
    """proposed → active. 동일 pair + 동일 trading_style 기존 전략은 archive.

    서로 다른 trading_style(trend_following / box_mean_reversion)은
    같은 pair에 동시에 active 상태로 공존 가능 — 듀얼 매니저 운용.
    """
    Model = state.models.strategy
    row = await db.get(Model, strategy_id)
    if not row:
        raise HTTPException(404, "전략 없음")
    if row.status != "proposed":
        raise HTTPException(400, f"proposed 상태만 활성화 가능 (현재: {row.status})")
    _validate_gmo_safety(row.parameters or {}, state)

    # 동일 pair + 동일 trading_style 기존 active 아카이브
    # (trading_style이 다르면 공존 허용 — 듀얼 매니저)
    pair = (row.parameters or {}).get("pair")
    style = (row.parameters or {}).get("trading_style")
    if pair:
        stmt = (
            select(Model)
            .where(Model.status == "active")
            .where(Model.id != strategy_id)
        )
        result = await db.execute(stmt)
        for existing in result.scalars().all():
            existing_pair = (existing.parameters or {}).get("pair")
            existing_style = (existing.parameters or {}).get("trading_style")
            if existing_pair == pair and existing_style == style:
                existing.status = "archived"
                existing.archived_at = datetime.now(timezone.utc)

    row.status = "active"
    row.activated_at = datetime.now(timezone.utc)
    await db.commit()

    # ── Hot Activation: 런타임 즉시 기동 ──
    try:
        registry = state.strategy_registry
        params = row.parameters or {}
        hot_pair = params.get("pair") or params.get("product_code")
        hot_style = params.get("trading_style")
        if registry and hot_pair and hot_style:
            hot_pair = state.normalize_pair(hot_pair)
            # 동일 스타일 매니저만 중단 (다른 스타일은 계속 실행 — 듀얼 매니저 운용)
            manager = registry.get(hot_style)
            if manager and manager.is_running(hot_pair):
                await manager.stop(hot_pair)
            # 새 전략 실전 모드 기동
            start_params = {**params, "strategy_id": row.id}
            if not await registry.start_strategy(hot_style, hot_pair, start_params):
                logger.warning(
                    f"[HotActivation] 전략 {row.id} 런타임 기동 실패 "
                    f"(style={hot_style}, pair={hot_pair})"
                )
            else:
                logger.info(
                    f"[HotActivation] 전략 {row.id} 즉시 기동 완료 "
                    f"(style={hot_style}, pair={hot_pair})"
                )
    except Exception as e:
        # DB는 이미 committed — 런타임 실패는 다음 재시작 시 복구
        logger.warning(f"[HotActivation] 런타임 기동 중 에러 (DB 상태 유지): {e}")

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

    # ── Hot Deactivation: 런타임 즉시 중단 ──
    try:
        registry = state.strategy_registry
        params = row.parameters or {}
        hot_pair = params.get("pair") or params.get("product_code")
        hot_style = params.get("trading_style")
        if registry and hot_pair and hot_style:
            hot_pair = state.normalize_pair(hot_pair)
            manager = registry.get(hot_style)
            if manager and manager.is_running(hot_pair):
                await manager.stop(hot_pair)
                logger.info(
                    f"[HotDeactivation] 전략 {row.id} 런타임 중단 완료 "
                    f"(style={hot_style}, pair={hot_pair})"
                )
    except Exception as e:
        logger.warning(f"[HotDeactivation] 런타임 중단 중 에러 (DB 상태 유지): {e}")

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
