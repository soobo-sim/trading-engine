"""
Paper Trades API — 가상 매매 기록 조회.

GET /api/paper-trades                    — 거래 이력 (strategy_id 필터)
GET /api/paper-trades/summary            — 성과 요약 (거래수, WR, PnL)
GET /api/paper-trades/overview           — proposed 전략 전체 + paper 집계 (카드용)

설계서: trader-common/solution-design/DASHBOARD_PAPER_TRADING.md §3.1
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.models import PaperTrade
from api.dependencies import AppState, get_db, get_state
from core.pair import normalize_pair

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paper-trades", tags=["PaperTrades"])


# ──────────────────────────────────────────────────────────────
# GET /api/paper-trades  — 거래 이력
# ──────────────────────────────────────────────────────────────

@router.get("", summary="Paper 거래 이력")
async def list_paper_trades(
    strategy_id: Optional[int] = Query(None, description="전략 ID 필터"),
    pair: Optional[str] = Query(None, description="페어 필터 (e.g. USD_JPY)"),
    closed_only: bool = Query(False, description="청산 완료 거래만"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Paper 거래 이력. strategy_id/pair 필터 선택 가능."""
    stmt = select(PaperTrade).order_by(PaperTrade.entry_time.desc())
    if strategy_id is not None:
        stmt = stmt.where(PaperTrade.strategy_id == strategy_id)
    if pair:
        from core.pair import normalize_pair
        stmt = stmt.where(PaperTrade.pair == normalize_pair(pair))
    if closed_only:
        stmt = stmt.where(PaperTrade.exit_time.is_not(None))
    stmt = stmt.limit(limit).offset(offset)

    result = await db.execute(stmt)
    rows = result.scalars().all()
    return {
        "trades": [_trade_to_dict(r) for r in rows],
        "total": len(rows),
    }


# ──────────────────────────────────────────────────────────────
# GET /api/paper-trades/summary  — 성과 요약
# ──────────────────────────────────────────────────────────────

@router.get("/summary", summary="Paper 전략 성과 요약")
async def get_paper_summary(
    strategy_id: int = Query(..., description="전략 ID"),
    db: AsyncSession = Depends(get_db),
):
    """청산 완료 거래 기준 성과 요약 (WR, PnL 평균/합계, Sharpe 미지원)."""
    # 청산된 거래만 집계
    stmt = (
        select(PaperTrade)
        .where(PaperTrade.strategy_id == strategy_id)
        .where(PaperTrade.exit_time.is_not(None))
        .order_by(PaperTrade.exit_time.desc())
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()

    if not rows:
        return _empty_summary(strategy_id)

    total = len(rows)
    wins = sum(1 for r in rows if (r.paper_pnl_pct or 0) > 0)
    pnl_pct_list = [float(r.paper_pnl_pct or 0) for r in rows]
    pnl_jpy_list = [float(r.paper_pnl_jpy or 0) for r in rows]

    win_rate = wins / total * 100 if total else 0.0
    avg_pnl_pct = sum(pnl_pct_list) / total if total else 0.0
    total_pnl_pct = sum(pnl_pct_list)
    total_pnl_jpy = sum(pnl_jpy_list)
    max_dd_pct = _calc_max_drawdown(pnl_pct_list)

    # 진행 중인 거래 수
    open_stmt = (
        select(func.count())
        .select_from(PaperTrade)
        .where(PaperTrade.strategy_id == strategy_id)
        .where(PaperTrade.exit_time.is_(None))
    )
    open_result = await db.execute(open_stmt)
    open_trades = open_result.scalar() or 0

    return {
        "strategy_id": strategy_id,
        "total_trades": total,
        "open_trades": open_trades,
        "win_rate": round(win_rate, 1),
        "avg_pnl_pct": round(avg_pnl_pct, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "total_pnl_jpy": round(total_pnl_jpy, 0),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "insufficient_data": total < 20,  # 통계적 유의성 경고 기준
    }


# ──────────────────────────────────────────────────────────────
# GET /api/paper-trades/overview  — proposed 전략 카드용
# ──────────────────────────────────────────────────────────────

@router.get("/overview", summary="proposed 전략 전체 + paper 집계")
async def get_paper_overview(
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """
    proposed 상태 전략 목록 + 각 전략의 paper_trades 집계.
    분석실 카드(PairCard)에서 proposed 배지 + paper 성과 표시용.
    """
    Model = state.models.strategy
    stmt = (
        select(Model)
        .where(Model.status == "proposed")
        .order_by(Model.created_at.desc())
    )
    result = await db.execute(stmt)
    strategies = result.scalars().all()

    items = []
    for s in strategies:
        params = s.parameters or {}
        pair = normalize_pair(params.get("pair") or params.get("product_code") or "")

        # paper_trades 집계 (청산 완료)
        trade_stmt = (
            select(PaperTrade)
            .where(PaperTrade.strategy_id == s.id)
            .where(PaperTrade.exit_time.is_not(None))
        )
        trade_result = await db.execute(trade_stmt)
        trades = trade_result.scalars().all()

        # 진행 중인 거래
        open_stmt = (
            select(func.count())
            .select_from(PaperTrade)
            .where(PaperTrade.strategy_id == s.id)
            .where(PaperTrade.exit_time.is_(None))
        )
        open_result = await db.execute(open_stmt)
        open_count = open_result.scalar() or 0

        paper_summary = _build_summary_from_trades(s.id, trades, open_count)

        items.append(
            {
                "strategy_id": s.id,
                "strategy_name": s.name,
                "technique_code": s.technique_code,
                "pair": pair,
                "status": s.status,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "paper_summary": paper_summary,
            }
        )

    return {"strategies": items, "total": len(items)}


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _trade_to_dict(row: PaperTrade) -> dict:
    return {
        "id": row.id,
        "strategy_id": row.strategy_id,
        "pair": row.pair,
        "direction": row.direction,
        "entry_price": float(row.entry_price) if row.entry_price is not None else None,
        "entry_time": row.entry_time.isoformat() if row.entry_time else None,
        "exit_price": float(row.exit_price) if row.exit_price is not None else None,
        "exit_time": row.exit_time.isoformat() if row.exit_time else None,
        "exit_reason": row.exit_reason,
        "paper_pnl_pct": float(row.paper_pnl_pct) if row.paper_pnl_pct is not None else None,
        "paper_pnl_jpy": float(row.paper_pnl_jpy) if row.paper_pnl_jpy is not None else None,
    }


def _empty_summary(strategy_id: int) -> dict:
    return {
        "strategy_id": strategy_id,
        "total_trades": 0,
        "open_trades": 0,
        "win_rate": 0.0,
        "avg_pnl_pct": 0.0,
        "total_pnl_pct": 0.0,
        "total_pnl_jpy": 0.0,
        "max_drawdown_pct": 0.0,
        "insufficient_data": True,
    }


def _build_summary_from_trades(strategy_id: int, trades: list, open_count: int) -> dict:
    total = len(trades)
    if not total:
        return {**_empty_summary(strategy_id), "open_trades": open_count}
    wins = sum(1 for r in trades if (r.paper_pnl_pct or 0) > 0)
    pnl_pct_list = [float(r.paper_pnl_pct or 0) for r in trades]
    pnl_jpy_list = [float(r.paper_pnl_jpy or 0) for r in trades]
    return {
        "strategy_id": strategy_id,
        "total_trades": total,
        "open_trades": open_count,
        "win_rate": round(wins / total * 100, 1),
        "avg_pnl_pct": round(sum(pnl_pct_list) / total, 2),
        "total_pnl_pct": round(sum(pnl_pct_list), 2),
        "total_pnl_jpy": round(sum(pnl_jpy_list), 0),
        "max_drawdown_pct": round(_calc_max_drawdown(pnl_pct_list), 2),
        "insufficient_data": total < 20,
    }


def _calc_max_drawdown(pnl_pct_list: list[float]) -> float:
    """누적 수익률 기준 최대 낙폭(%) 계산."""
    if not pnl_pct_list:
        return 0.0
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for pnl in pnl_pct_list:
        cum += pnl
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd
