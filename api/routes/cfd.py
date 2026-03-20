"""
CFD API — BitFlyer FX_BTC_JPY 포지션 조회.

GET /api/cfd/status           — 현재 CFD 포지션 상태 + keep_rate
GET /api/cfd/positions        — CFD 포지션 이력 (DB)
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState, get_db, get_state

router = APIRouter(prefix="/api/cfd", tags=["CFD"])


@router.get("/status", summary="CFD 실시간 상태")
async def get_cfd_status(
    product_code: str = Query("FX_BTC_JPY", description="CFD 상품 코드"),
    state: AppState = Depends(get_state),
):
    """인메모리 포지션 + BitFlyer 증거금/keep_rate 반환."""
    cfd = state.cfd_manager
    if cfd is None:
        raise HTTPException(400, detail={"error": "CFD 매니저 미등록 (BF 전용)"})

    position_obj = cfd.get_position(product_code)
    running = cfd.is_running(product_code)

    # 증거금 조회
    collateral = None
    if hasattr(state.adapter, "get_collateral"):
        try:
            collateral = await state.adapter.get_collateral()
        except Exception as e:
            collateral = {"error": str(e)}

    position_data = None
    if position_obj and position_obj.entry_price:
        side = (position_obj.extra or {}).get("side", "unknown")
        position_data = {
            "side": side,
            "entry_price": position_obj.entry_price,
            "entry_amount": position_obj.entry_amount,
            "stop_loss_price": position_obj.stop_loss_price,
        }

    return {
        "product_code": product_code,
        "is_running": running,
        "position": position_data,
        "collateral": {
            "collateral": collateral.collateral,
            "open_position_pnl": collateral.open_position_pnl,
            "require_collateral": collateral.require_collateral,
            "keep_rate": collateral.keep_rate,
        } if collateral and not isinstance(collateral, dict) else collateral,
        "task_health": cfd.get_task_health(),
    }


@router.get("/positions", summary="CFD 포지션 이력")
async def get_cfd_positions(
    product_code: str = Query("FX_BTC_JPY", description="CFD 상품 코드"),
    status: str | None = Query(None, description="open / closed"),
    limit: int = Query(20, ge=1, le=100),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """DB에서 CFD 포지션 이력 조회."""
    PosModel = state.models.cfd_position
    pair_col = getattr(PosModel, state.pair_column)

    stmt = select(PosModel).where(pair_col == product_code)
    if status:
        if status not in ("open", "closed"):
            raise HTTPException(400, detail={"error": "status must be 'open' or 'closed'"})
        stmt = stmt.where(PosModel.status == status)

    stmt = stmt.order_by(desc(PosModel.created_at)).limit(limit)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    return {
        "product_code": product_code,
        "count": len(rows),
        "positions": [_pos_to_dict(r, state.pair_column) for r in rows],
    }


def _pos_to_dict(row, pair_column: str) -> dict:
    return {
        "id": row.id,
        "strategy_id": row.strategy_id,
        "side": row.side,
        pair_column: getattr(row, pair_column),
        "entry_price": float(row.entry_price) if row.entry_price else None,
        "entry_size": float(row.entry_size) if row.entry_size else None,
        "entry_collateral_jpy": float(row.entry_collateral_jpy) if row.entry_collateral_jpy else None,
        "stop_loss_price": float(row.stop_loss_price) if row.stop_loss_price else None,
        "exit_price": float(row.exit_price) if row.exit_price else None,
        "exit_size": float(row.exit_size) if row.exit_size else None,
        "exit_reason": row.exit_reason,
        "realized_pnl_jpy": float(row.realized_pnl_jpy) if row.realized_pnl_jpy else None,
        "realized_pnl_pct": float(row.realized_pnl_pct) if row.realized_pnl_pct else None,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
    }
