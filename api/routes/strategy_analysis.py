"""
Strategy Analysis API — 분석 보고 + 에이전트 분석 + 반성 사이클 CRUD.

POST   /api/strategy-analysis/reports           — 보고 생성 (analyses 동시 저장)
GET    /api/strategy-analysis/reports/latest    — 통화별 최신 보고 1건씩
GET    /api/strategy-analysis/reports           — 보고 목록
GET    /api/strategy-analysis/reports/{id}      — 보고 상세

POST   /api/strategy-analysis/reflections       — 반성 저장
GET    /api/strategy-analysis/reflections       — 반성 목록
GET    /api/strategy-analysis/reflections/{id}  — 반성 상세

설계서: trader-common/solution-design/STRATEGY_ANALYSIS_SYSTEM.md §2~3
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db
from api.services import strategy_analysis_service as svc
from core.notifications.analysis_telegram import send_analysis_report_telegram

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy-analysis", tags=["StrategyAnalysis"])

# ──────────────────────────────────────────────────────────────
# Pydantic 스키마
# ──────────────────────────────────────────────────────────────

class AgentAnalysisCreate(BaseModel):
    agent_name: str = Field(..., description="'alice' | 'samantha' | 'rachel'")
    summary: str = Field(..., min_length=5)
    structured_data: dict
    full_text: Optional[str] = None

    @field_validator("agent_name")
    @classmethod
    def validate_agent_name(cls, v: str) -> str:
        if v not in svc.VALID_AGENT_NAMES:
            raise ValueError(f"agent_name must be one of {sorted(svc.VALID_AGENT_NAMES)}")
        return v


class ReportCreate(BaseModel):
    exchange: str = Field(..., description="거래소 식별자 (e.g. 'gmofx')")
    currency_pair: str = Field(..., description="통화 페어 (e.g. 'USD_JPY')")
    report_type: str = Field(..., description="'daily' | 'weekly' | 'monthly'")
    reported_at: datetime = Field(..., description="보고 시각 (ISO8601)")
    strategy_active: bool = False
    strategy_id: Optional[int] = None
    final_decision: Optional[str] = Field(
        None, description="'approved' | 'rejected' | 'conditional' | 'hold'"
    )
    final_rationale: Optional[str] = None
    next_review: Optional[datetime] = None
    analyses: list[AgentAnalysisCreate] = []

    @field_validator("report_type")
    @classmethod
    def validate_report_type(cls, v: str) -> str:
        if v not in svc.VALID_REPORT_TYPES:
            raise ValueError(f"report_type must be one of {sorted(svc.VALID_REPORT_TYPES)}")
        return v

    @field_validator("final_decision")
    @classmethod
    def validate_final_decision(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in svc.VALID_DECISIONS:
            raise ValueError(f"final_decision must be one of {sorted(svc.VALID_DECISIONS)}")
        return v


class ReflectionCreate(BaseModel):
    reflection_date: date
    agent_name: str = Field(..., description="'alice' | 'samantha' | 'rachel'")
    period_type: str = Field(..., description="'short' | 'medium' | 'long'")
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    missed_data: Optional[list[dict]] = None
    data_improvement: Optional[list[dict]] = None
    effective_decisions: Optional[list[dict]] = None
    action_items: Optional[list[dict]] = None
    strategy_performance: Optional[dict] = None

    @field_validator("agent_name")
    @classmethod
    def validate_agent_name(cls, v: str) -> str:
        if v not in svc.VALID_AGENT_NAMES:
            raise ValueError(f"agent_name must be one of {sorted(svc.VALID_AGENT_NAMES)}")
        return v

    @field_validator("period_type")
    @classmethod
    def validate_period_type(cls, v: str) -> str:
        if v not in svc.VALID_PERIOD_TYPES:
            raise ValueError(f"period_type must be one of {sorted(svc.VALID_PERIOD_TYPES)}")
        return v


# ──────────────────────────────────────────────────────────────
# 보고 엔드포인트 — 고정 경로(/latest)를 가변 경로(/{id}) 앞에 등록
# ──────────────────────────────────────────────────────────────

@router.post("/reports", status_code=201, summary="분석 보고 생성")
async def create_report(
    body: ReportCreate,
    db: AsyncSession = Depends(get_db),
):
    """보고 헤더 + 에이전트 분석 N건을 단일 트랜잭션으로 저장."""
    try:
        result = await svc.create_report(
            exchange=body.exchange,
            currency_pair=body.currency_pair,
            report_type=body.report_type,
            reported_at=body.reported_at,
            strategy_active=body.strategy_active,
            strategy_id=body.strategy_id,
            final_decision=body.final_decision,
            final_rationale=body.final_rationale,
            next_review=body.next_review,
            analyses=[a.model_dump() for a in body.analyses],
            db=db,
        )
        # fire-and-forget: 전송 실패해도 201 반환
        asyncio.create_task(
            send_analysis_report_telegram(
                report_type=body.report_type,
                currency_pair=body.currency_pair,
                reported_at=body.reported_at,
                final_decision=body.final_decision,
                strategy_active=body.strategy_active,
                analyses=[{"agent_name": a.agent_name, "summary": a.summary} for a in body.analyses],
            )
        )
        return result
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            {
                "blocked_code": "DUPLICATE_REPORT",
                "detail": "동일 exchange/currency_pair/report_type/reported_at 보고가 이미 존재합니다.",
            },
        )


@router.get("/reports/latest", summary="통화별 최신 보고 1건씩 (목록 화면용)")
async def get_latest_reports(
    exchange: str = Query(..., description="거래소 식별자 (e.g. 'gmofx')"),
    db: AsyncSession = Depends(get_db),
):
    """거래소 내 currency_pair별 가장 최신 보고 1건씩 반환."""
    return await svc.get_latest_reports(exchange=exchange, db=db)


@router.get("/reports", summary="분석 보고 목록")
async def list_reports(
    exchange: Optional[str] = Query(None, description="거래소 필터"),
    currency_pair: Optional[str] = Query(None, description="통화 페어 필터"),
    report_type: Optional[str] = Query(None, description="보고 유형 필터: daily|weekly|monthly"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    if report_type and report_type not in svc.VALID_REPORT_TYPES:
        raise HTTPException(
            400,
            {"blocked_code": "INVALID_REPORT_TYPE", "valid": sorted(svc.VALID_REPORT_TYPES)},
        )
    return await svc.list_reports(
        exchange=exchange,
        currency_pair=currency_pair,
        report_type=report_type,
        limit=limit,
        db=db,
    )


@router.get("/reports/{report_id}", summary="분석 보고 상세")
async def get_report(
    report_id: int,
    db: AsyncSession = Depends(get_db),
):
    if report_id <= 0:
        raise HTTPException(400, {"blocked_code": "INVALID_REPORT_ID"})
    data = await svc.get_report(report_id=report_id, db=db)
    if data is None:
        raise HTTPException(404, {"blocked_code": "REPORT_NOT_FOUND"})
    return data


# ──────────────────────────────────────────────────────────────
# 반성 엔드포인트
# ──────────────────────────────────────────────────────────────

@router.post("/reflections", status_code=201, summary="반성 사이클 저장")
async def create_reflection(
    body: ReflectionCreate,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await svc.create_reflection(
            reflection_date=body.reflection_date,
            agent_name=body.agent_name,
            period_type=body.period_type,
            period_start=body.period_start,
            period_end=body.period_end,
            missed_data=body.missed_data,
            data_improvement=body.data_improvement,
            effective_decisions=body.effective_decisions,
            action_items=body.action_items,
            strategy_performance=body.strategy_performance,
            db=db,
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            {
                "blocked_code": "DUPLICATE_REFLECTION",
                "detail": "동일 reflection_date/agent_name/period_type 반성이 이미 존재합니다.",
            },
        )


@router.get("/reflections", summary="반성 목록")
async def list_reflections(
    agent_name: Optional[str] = Query(None, description="에이전트 필터: alice|samantha|rachel"),
    period_type: Optional[str] = Query(None, description="기간 유형 필터: short|medium|long"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    if agent_name and agent_name not in svc.VALID_AGENT_NAMES:
        raise HTTPException(
            400,
            {"blocked_code": "INVALID_AGENT_NAME", "valid": sorted(svc.VALID_AGENT_NAMES)},
        )
    if period_type and period_type not in svc.VALID_PERIOD_TYPES:
        raise HTTPException(
            400,
            {"blocked_code": "INVALID_PERIOD_TYPE", "valid": sorted(svc.VALID_PERIOD_TYPES)},
        )
    return await svc.list_reflections(
        agent_name=agent_name,
        period_type=period_type,
        limit=limit,
        db=db,
    )


@router.get("/reflections/{reflection_id}", summary="반성 상세")
async def get_reflection(
    reflection_id: int,
    db: AsyncSession = Depends(get_db),
):
    if reflection_id <= 0:
        raise HTTPException(400, {"blocked_code": "INVALID_REFLECTION_ID"})
    data = await svc.get_reflection(reflection_id=reflection_id, db=db)
    if data is None:
        raise HTTPException(404, {"blocked_code": "REFLECTION_NOT_FOUND"})
    return data
