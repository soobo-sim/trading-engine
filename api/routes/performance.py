"""
Performance API — 성과 메트릭 + 백테스트 vs 실전 괴리 비교.

Phase 1-B: 성과 메트릭 인프라
  GET /api/performance           — 종합 성과 메트릭 (수익률, 샤프, 드로다운 등)
  GET /api/performance/compare   — 백테스트 vs 실전 괴리 비교

Phase 1-C: 백테스트
  POST /api/backtest/run         — 백테스트 실행
  POST /api/backtest/grid        — 파라미터 그리드 서치
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState, get_db, get_state
from api.services import performance_service as svc

router = APIRouter(tags=["Performance"])


# ──────────────────────────────────────────────────────────────
# GET /api/performance
# ──────────────────────────────────────────────────────────────

@router.get("/api/performance", summary="종합 성과 메트릭")
async def get_performance(
    pair: str = Query(..., description="페어 (e.g. xrp_jpy)"),
    period: str = Query("30d", description="기간: 7d|30d|90d|180d|365d|all"),
    strategy_type: Optional[str] = Query(
        None, description="전략 필터: trend_following|box_mean_reversion (없으면 전체)"
    ),
    strategy_id: Optional[int] = Query(None, description="특정 strategy_id만 집계"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """실전 거래 성과 메트릭."""
    if period not in svc.PERIOD_DAYS:
        raise HTTPException(400, {"blocked_code": "INVALID_PERIOD", "valid": list(svc.PERIOD_DAYS.keys())})
    if strategy_type and strategy_type not in ("trend_following", "box_mean_reversion"):
        raise HTTPException(400, {"blocked_code": "INVALID_STRATEGY_TYPE"})
    pair = state.normalize_pair(pair)
    if strategy_id is not None:
        return await svc.get_performance_by_strategy_id(pair, period, strategy_id, state, db)
    return await svc.get_performance(pair, period, strategy_type, state, db)


@router.get("/api/performance/by-strategy", summary="전략별 성과 비교표")
async def get_performance_by_strategy(
    pair: str = Query(..., description="페어 (e.g. BTC_JPY)"),
    period: str = Query("30d", description="기간: 7d|30d|90d|180d|365d|all"),
    status: Optional[str] = Query(None, description="전략 status 필터: active|archived (없으면 전체)"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """전략별 성과 비교표. grade(A/B/C/insufficient) 포함."""
    if period not in svc.PERIOD_DAYS:
        raise HTTPException(400, {"blocked_code": "INVALID_PERIOD", "valid": list(svc.PERIOD_DAYS.keys())})
    if status and status not in ("active", "archived"):
        raise HTTPException(400, {"blocked_code": "INVALID_STATUS"})
    pair = state.normalize_pair(pair)
    return await svc.get_performance_by_strategy(pair, period, status, state, db)


# ──────────────────────────────────────────────────────────────
# POST /api/backtest/run
# ──────────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    pair: str = Field(..., description="페어 (e.g. xrp_jpy)")
    params: dict = Field(..., description="전략 파라미터")
    days: int = Field(90, ge=7, le=365, description="백테스트 기간 (일)")
    timeframe: str = Field("4h", description="캔들 타임프레임: 1h | 4h")
    initial_capital_jpy: float = Field(100_000.0, ge=1000, description="초기 자본 (JPY)")
    slippage_pct: float = Field(0.05, ge=0, le=1.0, description="슬리피지 (%)")
    fee_pct: float = Field(0.15, ge=0, le=1.0, description="수수료 편도 (%)")


@router.post("/api/backtest/run", summary="백테스트 실행")
async def run_backtest_api(
    body: BacktestRequest,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """캔들 리플레이 백테스트."""
    if body.timeframe not in ("1h", "4h"):
        raise HTTPException(400, {"blocked_code": "INVALID_TIMEFRAME"})

    result = await svc.run_backtest_api(
        body.pair, body.params, body.days, body.timeframe,
        body.initial_capital_jpy, body.slippage_pct, body.fee_pct,
        state, db,
    )
    if "error" in result:
        raise HTTPException(400, {
            "blocked_code": result["error"],
            "detail": f"캔들 {result['count']}개 — 최소 25개 필요",
        })
    return result


# ──────────────────────────────────────────────────────────────
# POST /api/backtest/grid
# ──────────────────────────────────────────────────────────────

MAX_GRID_COMBINATIONS = svc.MAX_GRID_COMBINATIONS


class GridSearchRequest(BaseModel):
    pair: str = Field(..., description="페어")
    base_params: dict = Field(..., description="기본 전략 파라미터")
    param_grid: dict = Field(..., description="그리드 서치 파라미터 (키: 파라미터명, 값: 후보 리스트)")
    days: int = Field(90, ge=7, le=365, description="백테스트 기간 (일)")
    timeframe: str = Field("4h", description="캔들 타임프레임")
    top_n: int = Field(10, ge=1, le=50, description="상위 N개 결과")
    initial_capital_jpy: float = Field(100_000.0, ge=1000)
    slippage_pct: float = Field(0.05, ge=0, le=1.0)
    fee_pct: float = Field(0.15, ge=0, le=1.0)


@router.post("/api/backtest/grid", summary="파라미터 그리드 서치")
async def grid_search_api(
    body: GridSearchRequest,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """파라미터 조합 자동 비교."""
    if body.timeframe not in ("1h", "4h"):
        raise HTTPException(400, {"blocked_code": "INVALID_TIMEFRAME"})

    total = 1
    for vals in body.param_grid.values():
        if not isinstance(vals, list):
            raise HTTPException(400, {"blocked_code": "INVALID_PARAM_GRID", "detail": "값은 리스트여야 합니다"})
        total *= len(vals)
    if total > MAX_GRID_COMBINATIONS:
        raise HTTPException(400, {
            "blocked_code": "TOO_MANY_COMBINATIONS",
            "detail": f"조합 {total}개 > 상한 {MAX_GRID_COMBINATIONS}개",
        })

    result = await svc.run_grid_search_api(
        body.pair, body.base_params, body.param_grid,
        body.days, body.timeframe, body.top_n,
        body.initial_capital_jpy, body.slippage_pct, body.fee_pct,
        state, db,
    )
    if "error" in result:
        raise HTTPException(400, {
            "blocked_code": result["error"],
            "detail": f"캔들 {result['count']}개",
        })
    return result


# ──────────────────────────────────────────────────────────────
# GET /api/performance/compare
# ──────────────────────────────────────────────────────────────

@router.get("/api/performance/compare", summary="백테스트 vs 실전 괴리 비교")
async def compare_performance(
    pair: str = Query(..., description="페어"),
    period: str = Query("90d", description="기간: 7d|30d|90d|180d|365d|all"),
    strategy_type: Optional[str] = Query(
        None, description="전략 필터: trend_following|box_mean_reversion"
    ),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """동일 기간 백테스트 vs 실전 성과 괴리 비교."""
    if period not in svc.PERIOD_DAYS:
        raise HTTPException(400, {"blocked_code": "INVALID_PERIOD"})

    result = await svc.compare_performance(pair, period, strategy_type, state, db)
    if "error" in result:
        raise HTTPException(404, {
            "blocked_code": result["error"],
            "detail": f"pair={result['pair']}에 해당하는 전략 없음",
        })
    return result
