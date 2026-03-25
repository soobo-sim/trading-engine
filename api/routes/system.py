"""
System Health API — 거래소-무관 헬스 체크 라우트.

GET /api/system/health
GET /health  (Docker HEALTHCHECK용 레거시 경로)
"""
from dataclasses import asdict

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.dependencies import AppState, get_state

router = APIRouter(tags=["System"])


@router.get("/api/system/health")
async def system_health(state: AppState = Depends(get_state)):
    """비즈니스 수준 헬스 체크 (안전장치 포함)."""
    report = await state.health_checker.check()

    return JSONResponse(
        status_code=200 if report.healthy else 503,
        content=asdict(report),
    )


@router.get("/health")
async def health_simple():
    """Docker HEALTHCHECK용 간단 헬스 엔드포인트."""
    return {"status": "ok"}
