"""
Strategy Scores & Switch Recommendations API.

GET  /api/strategy-scores                     — active 전략 최신 Score 목록
GET  /api/strategy-scores/{strategy_id}       — 특정 전략 최신 Score
GET  /api/strategy-snapshots/latest           — 전 전략(active+proposed) 최신 스냅샷
GET  /api/strategy-snapshots/{strategy_id}    — 특정 전략 스냅샷 이력
GET  /api/strategy-snapshots/compare          — 복수 전략 Score 비교 (ids 쿼리)
GET  /api/switch-recommendations              — 추천 이력 목록
GET  /api/switch-recommendations/{rec_id}     — 추천 상세
POST /api/switch-recommendations/{rec_id}/approve — 추천 승인
POST /api/switch-recommendations/{rec_id}/reject  — 추천 거부

설계서: trader-common/solution-design/DYNAMIC_STRATEGY_SWITCHING.md §P-1 Step 4/4
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState, get_db, get_state
from adapters.database.models import WakeUpReview

logger = logging.getLogger(__name__)

router = APIRouter(tags=["StrategyScores"])


# ──────────────────────────────────────────────────────────────
# Pydantic 스키마
# ──────────────────────────────────────────────────────────────

class ApproveRequest(BaseModel):
    decided_by: str = Field(default="soobo", description="승인자 (e.g. 'soobo', 'rachel')")


class RejectRequest(BaseModel):
    decided_by: str = Field(default="soobo", description="거부자")
    reject_reason: str = Field(..., min_length=5, description="거부 사유")


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _snapshot_to_dict(row) -> dict:
    return {
        "id": row.id,
        "strategy_id": row.strategy_id,
        "pair": row.pair,
        "trading_style": row.trading_style,
        "trigger_type": row.trigger_type,
        "snapshot_time": row.snapshot_time.isoformat() if row.snapshot_time else None,
        "score": float(row.score) if row.score is not None else None,
        "readiness": float(row.readiness) if row.readiness is not None else None,
        "edge": float(row.edge) if row.edge is not None else None,
        "regime_fit": float(row.regime_fit) if row.regime_fit is not None else None,
        "regime": row.regime,
        "confidence": row.confidence,
        "has_position": row.has_position,
        "current_price": float(row.current_price) if row.current_price is not None else None,
        "detail": row.detail,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _rec_to_dict(row) -> dict:
    return {
        "id": row.id,
        "trigger_type": row.trigger_type,
        "triggered_at": row.triggered_at.isoformat() if row.triggered_at else None,
        "current_strategy_id": row.current_strategy_id,
        "current_score": float(row.current_score) if row.current_score is not None else None,
        "recommended_strategy_id": row.recommended_strategy_id,
        "recommended_score": float(row.recommended_score) if row.recommended_score is not None else None,
        "score_ratio": float(row.score_ratio) if row.score_ratio is not None else None,
        "confidence": row.confidence,
        "reason": row.reason,
        "decision": row.decision,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "decided_by": row.decided_by,
        "reject_reason": row.reject_reason,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ──────────────────────────────────────────────────────────────
# GET /api/strategy-scores     — active 전략 최신 Score
# ──────────────────────────────────────────────────────────────

@router.get("/api/strategy-scores", summary="active 전략 최신 Score 목록")
async def list_strategy_scores(
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """active 전략별 마지막 스냅샷 1건씩 반환."""
    model = state.models.strategy_snapshot
    if model is None:
        return {"scores": []}

    # active 전략 목록
    stmt_s = select(state.models.strategy).where(state.models.strategy.status == "active")
    result_s = await db.execute(stmt_s)
    active_strategies = result_s.scalars().all()

    scores = []
    for strategy in active_strategies:
        stmt = (
            select(model)
            .where(model.strategy_id == strategy.id)
            .order_by(desc(model.snapshot_time))
            .limit(1)
        )
        result = await db.execute(stmt)
        row = result.scalars().first()
        if row:
            scores.append(_snapshot_to_dict(row))

    return {"scores": scores}


# ──────────────────────────────────────────────────────────────
# GET /api/strategy-scores/{strategy_id}  — 특정 전략 최신 Score
# ──────────────────────────────────────────────────────────────

@router.get("/api/strategy-scores/{strategy_id}", summary="특정 전략 최신 Score")
async def get_strategy_score(
    strategy_id: int,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    model = state.models.strategy_snapshot
    if model is None:
        raise HTTPException(status_code=404, detail="스냅샷 기록 없음")

    stmt = (
        select(model)
        .where(model.strategy_id == strategy_id)
        .order_by(desc(model.snapshot_time))
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"strategy_id={strategy_id} 스냅샷 없음")
    return _snapshot_to_dict(row)


# ──────────────────────────────────────────────────────────────
# GET /api/strategy-snapshots/latest  — 전 전략 최신 스냅샷
# NOTE: 반드시 /{strategy_id} 보다 먼저 등록
# ──────────────────────────────────────────────────────────────

@router.get("/api/strategy-snapshots/latest", summary="전 전략 최신 스냅샷")
async def list_snapshots_latest(
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """active + proposed 전 전략별 최신 스냅샷 1건씩."""
    model = state.models.strategy_snapshot
    if model is None:
        return {"snapshots": []}

    # 활성/proposed 전략 목록
    from sqlalchemy import or_
    stmt_s = select(state.models.strategy).where(
        or_(state.models.strategy.status == "active", state.models.strategy.status == "proposed")
    )
    result_s = await db.execute(stmt_s)
    strategies = result_s.scalars().all()

    snapshots = []
    for strategy in strategies:
        stmt = (
            select(model)
            .where(model.strategy_id == strategy.id)
            .order_by(desc(model.snapshot_time))
            .limit(1)
        )
        result = await db.execute(stmt)
        row = result.scalars().first()
        if row:
            d = _snapshot_to_dict(row)
            d["strategy_status"] = strategy.status
            snapshots.append(d)

    return {"snapshots": snapshots}


# ──────────────────────────────────────────────────────────────
# GET /api/strategy-snapshots/compare  — 복수 전략 Score 비교
# NOTE: 반드시 /{strategy_id} 보다 먼저 등록
# ──────────────────────────────────────────────────────────────

@router.get("/api/strategy-snapshots/compare", summary="복수 전략 Score 비교")
async def compare_strategy_snapshots(
    ids: str = Query(..., description="전략 ID 콤마 구분 (e.g. '1,2,10')"),
    days: int = Query(7, ge=1, le=90, description="조회 기간 (일)"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """복수 전략의 최근 N일 스냅샷 평균 Score 비교."""
    from datetime import timedelta
    from sqlalchemy import func

    try:
        strategy_ids = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids는 정수 콤마 구분이어야 합니다")

    if not strategy_ids:
        raise HTTPException(status_code=400, detail="ids가 비어있습니다")

    model = state.models.strategy_snapshot
    if model is None:
        return {"compare": []}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result_list = []

    for sid in strategy_ids:
        stmt = (
            select(
                func.avg(model.score).label("avg_score"),
                func.max(model.score).label("max_score"),
                func.min(model.score).label("min_score"),
                func.count(model.id).label("count"),
            )
            .where(model.strategy_id == sid)
            .where(model.snapshot_time >= cutoff)
        )
        res = await db.execute(stmt)
        r = res.first()
        result_list.append({
            "strategy_id": sid,
            "days": days,
            "avg_score": float(r.avg_score) if r.avg_score is not None else None,
            "max_score": float(r.max_score) if r.max_score is not None else None,
            "min_score": float(r.min_score) if r.min_score is not None else None,
            "snapshot_count": r.count,
        })

    return {"compare": result_list}


# ──────────────────────────────────────────────────────────────
# GET /api/strategy-snapshots/{strategy_id}  — 특정 전략 스냅샷 이력
# ──────────────────────────────────────────────────────────────

@router.get("/api/strategy-snapshots/{strategy_id}", summary="특정 전략 스냅샷 이력")
async def get_strategy_snapshots(
    strategy_id: int,
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(100, ge=1, le=500),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    from datetime import timedelta

    model = state.models.strategy_snapshot
    if model is None:
        return {"snapshots": [], "strategy_id": strategy_id}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(model)
        .where(model.strategy_id == strategy_id)
        .where(model.snapshot_time >= cutoff)
        .order_by(desc(model.snapshot_time))
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return {
        "strategy_id": strategy_id,
        "days": days,
        "snapshots": [_snapshot_to_dict(r) for r in rows],
    }


# ──────────────────────────────────────────────────────────────
# GET /api/switch-recommendations  — 추천 이력 목록
# ──────────────────────────────────────────────────────────────

@router.get("/api/switch-recommendations", summary="스위칭 추천 이력")
async def list_switch_recommendations(
    status: Optional[str] = Query(None, description="필터 ('pending'|'approved'|'rejected')"),
    limit: int = Query(20, ge=1, le=100),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    model = state.models.switch_recommendation
    if model is None:
        return {"recommendations": []}

    stmt = select(model).order_by(desc(model.created_at)).limit(limit)
    if status:
        stmt = stmt.where(model.decision == status)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return {"recommendations": [_rec_to_dict(r) for r in rows]}


# ──────────────────────────────────────────────────────────────
# GET /api/switch-recommendations/{rec_id}  — 추천 상세
# NOTE: approve/reject 라우트보다 뒤에 등록해도 /approve|/reject가 먼저 매칭됨
# ──────────────────────────────────────────────────────────────

@router.get("/api/switch-recommendations/{rec_id}", summary="스위칭 추천 상세")
async def get_switch_recommendation(
    rec_id: int,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    model = state.models.switch_recommendation
    if model is None:
        raise HTTPException(status_code=404, detail="스위칭 추천 없음")

    stmt = select(model).where(model.id == rec_id)
    result = await db.execute(stmt)
    row = result.scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"rec_id={rec_id} 없음")
    return _rec_to_dict(row)


# ──────────────────────────────────────────────────────────────
# POST /api/switch-recommendations/{rec_id}/approve
# ──────────────────────────────────────────────────────────────

@router.post("/api/switch-recommendations/{rec_id}/approve", summary="스위칭 추천 승인")
async def approve_switch_recommendation(
    rec_id: int,
    body: ApproveRequest,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """추천 승인 → decision=approved. recommended 전략 PaperExecutor 해제."""
    model = state.models.switch_recommendation
    if model is None:
        raise HTTPException(status_code=404, detail="스위칭 추천 없음")

    stmt = select(model).where(model.id == rec_id)
    result = await db.execute(stmt)
    row = result.scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"rec_id={rec_id} 없음")
    if row.decision != "pending":
        raise HTTPException(status_code=400, detail=f"이미 처리된 추천 (decision={row.decision})")

    # PaperExecutor 해제
    await _unregister_recommended_paper(state, db, row.recommended_strategy_id)

    row.decision = "approved"
    row.decided_at = datetime.now(timezone.utc)
    row.decided_by = body.decided_by
    await db.commit()
    await db.refresh(row)

    logger.info(
        f"[StrategyScores] 추천 승인: rec_id={rec_id}, "
        f"recommended={row.recommended_strategy_id}, decided_by={body.decided_by}"
    )

    # 미완료 액션아이템 경고 (informational — 실패해도 승인 처리에 영향 없음)
    open_actions_warning = None
    if row.current_strategy_id is not None:
        try:
            wur_stmt = select(WakeUpReview).where(
                WakeUpReview.strategy_id == row.current_strategy_id,
                WakeUpReview.action_items.isnot(None),
            )
            wur_rows = (await db.execute(wur_stmt)).scalars().all()
            open_items = []
            for wur in wur_rows:
                if not wur.action_items:
                    continue
                for item in wur.action_items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("status") in ("done", "skipped"):
                        continue
                    deadline = item.get("deadline", "")
                    label = (
                        f"{item.get('id', '?')}: {item.get('action', '')} (기한: {deadline})"
                        if deadline
                        else f"{item.get('id', '?')}: {item.get('action', '')}"
                    )
                    open_items.append(label)
            if open_items:
                open_actions_warning = {
                    "open_action_count": len(open_items),
                    "items": open_items,
                }
                logger.info(
                    f"[StrategyScores] 미완료 액션아이템 {len(open_items)}건 (strategy_id={row.current_strategy_id})"
                )
        except Exception as exc:
            logger.warning(f"[StrategyScores] 미완료 액션 체크 실패 (무시): {exc}")

    resp = _rec_to_dict(row)
    if open_actions_warning:
        resp["warning"] = open_actions_warning
    return resp


# ──────────────────────────────────────────────────────────────
# POST /api/switch-recommendations/{rec_id}/reject
# ──────────────────────────────────────────────────────────────

@router.post("/api/switch-recommendations/{rec_id}/reject", summary="스위칭 추천 거부")
async def reject_switch_recommendation(
    rec_id: int,
    body: RejectRequest,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """추천 거부 → decision=rejected."""
    model = state.models.switch_recommendation
    if model is None:
        raise HTTPException(status_code=404, detail="스위칭 추천 없음")

    stmt = select(model).where(model.id == rec_id)
    result = await db.execute(stmt)
    row = result.scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"rec_id={rec_id} 없음")
    if row.decision != "pending":
        raise HTTPException(status_code=400, detail=f"이미 처리된 추천 (decision={row.decision})")

    row.decision = "rejected"
    row.decided_at = datetime.now(timezone.utc)
    row.decided_by = body.decided_by
    row.reject_reason = body.reject_reason
    await db.commit()
    await db.refresh(row)

    logger.info(
        f"[StrategyScores] 추천 거부: rec_id={rec_id}, "
        f"reason={body.reject_reason!r}, decided_by={body.decided_by}"
    )
    return _rec_to_dict(row)


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼 — PaperExecutor 해제
# ──────────────────────────────────────────────────────────────

async def _unregister_recommended_paper(
    state: AppState, db: AsyncSession, strategy_id: Optional[int]
) -> None:
    """recommended_strategy_id의 unregister_paper_pair를 대응 매니저에 호출."""
    if strategy_id is None:
        return
    try:
        stmt = select(state.models.strategy).where(state.models.strategy.id == strategy_id)
        result = await db.execute(stmt)
        strategy = result.scalars().first()
        if not strategy:
            return

        params = strategy.parameters or {}
        pair = params.get("pair") or params.get("product_code")
        style = params.get("trading_style")
        if not pair or not style:
            return

        pair = state.normalize_pair(pair)
        if style == "box_mean_reversion":
            state.box_manager.unregister_paper_pair(pair)
        elif style == "trend_following":
            state.trend_manager.unregister_paper_pair(pair)
        elif style == "cfd_trend_following" and state.cfd_manager:
            state.cfd_manager.unregister_paper_pair(pair)

        logger.info(
            f"[StrategyScores] PaperExecutor 해제: strategy_id={strategy_id} pair={pair} style={style}"
        )
    except Exception as e:
        logger.warning(f"[StrategyScores] unregister_paper_pair 실패 (무시): {e}")
