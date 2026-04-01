"""
Strategy Analysis Service — 분석 보고 + 에이전트 분석 + 반성 사이클 CRUD.

설계서: trader-common/solution-design/STRATEGY_ANALYSIS_SYSTEM.md §2~3
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.models import AgentAnalysis, AgentReflection, AnalysisReport

logger = logging.getLogger(__name__)

# report_type → 차트 기간 오프셋 (일)
_CHART_OFFSETS: dict[str, int] = {"daily": 7, "weekly": 14, "monthly": 30}

VALID_REPORT_TYPES = frozenset({"daily", "weekly", "monthly"})
VALID_AGENT_NAMES = frozenset({"alice", "samantha", "rachel"})
VALID_PERIOD_TYPES = frozenset({"short", "medium", "long"})
VALID_DECISIONS = frozenset({"approved", "rejected", "conditional", "hold"})


# ──────────────────────────────────────────────────────────────
# 변환 헬퍼
# ──────────────────────────────────────────────────────────────

def _report_to_dict(report: AnalysisReport, include_analyses: bool = False) -> dict:
    d: dict = {
        "id": report.id,
        "exchange": report.exchange,
        "currency_pair": report.currency_pair,
        "report_type": report.report_type,
        "reported_at": report.reported_at.isoformat() if report.reported_at else None,
        "chart_start": report.chart_start.isoformat() if report.chart_start else None,
        "chart_end": report.chart_end.isoformat() if report.chart_end else None,
        "strategy_active": report.strategy_active,
        "strategy_id": report.strategy_id,
        "final_decision": report.final_decision,
        "final_rationale": report.final_rationale,
        "next_review": report.next_review.isoformat() if report.next_review else None,
        "created_at": report.created_at.isoformat() if report.created_at else None,
    }
    if include_analyses:
        d["analyses"] = [_analysis_to_dict(a) for a in (report.analyses or [])]
    return d


def _analysis_to_dict(a: AgentAnalysis) -> dict:
    return {
        "id": a.id,
        "report_id": a.report_id,
        "agent_name": a.agent_name,
        "summary": a.summary,
        "structured_data": a.structured_data,
        "full_text": a.full_text,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _reflection_to_dict(r: AgentReflection) -> dict:
    return {
        "id": r.id,
        "reflection_date": r.reflection_date.isoformat() if isinstance(r.reflection_date, date) else str(r.reflection_date),
        "agent_name": r.agent_name,
        "period_type": r.period_type,
        "period_start": r.period_start.isoformat() if r.period_start else None,
        "period_end": r.period_end.isoformat() if r.period_end else None,
        "missed_data": r.missed_data,
        "data_improvement": r.data_improvement,
        "effective_decisions": r.effective_decisions,
        "action_items": r.action_items,
        "strategy_performance": r.strategy_performance,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _compute_chart_range(
    reported_at: datetime, report_type: str
) -> tuple[datetime, datetime]:
    """report_type에 따라 chart_start/chart_end 자동 계산."""
    offset_days = _CHART_OFFSETS[report_type]
    chart_end = reported_at
    chart_start = reported_at - timedelta(days=offset_days)
    return chart_start, chart_end


# ──────────────────────────────────────────────────────────────
# 보고 (AnalysisReport + AgentAnalysis)
# ──────────────────────────────────────────────────────────────

async def create_report(
    exchange: str,
    currency_pair: str,
    report_type: str,
    reported_at: datetime,
    strategy_active: bool,
    strategy_id: Optional[int],
    final_decision: Optional[str],
    final_rationale: Optional[str],
    next_review: Optional[datetime],
    analyses: list[dict],
    db: AsyncSession,
) -> dict:
    """보고 헤더 + 에이전트 분석 N건을 단일 트랜잭션으로 저장."""
    chart_start, chart_end = _compute_chart_range(reported_at, report_type)

    report = AnalysisReport(
        exchange=exchange,
        currency_pair=currency_pair,
        report_type=report_type,
        reported_at=reported_at,
        chart_start=chart_start,
        chart_end=chart_end,
        strategy_active=strategy_active,
        strategy_id=strategy_id,
        final_decision=final_decision,
        final_rationale=final_rationale,
        next_review=next_review,
    )
    db.add(report)
    await db.flush()  # report.id 확보

    for a in analyses:
        db.add(AgentAnalysis(
            report_id=report.id,
            agent_name=a["agent_name"],
            summary=a["summary"],
            structured_data=a["structured_data"],
            full_text=a.get("full_text"),
        ))

    await db.commit()
    await db.refresh(report)

    # analyses 로드
    result = await db.execute(
        select(AgentAnalysis).where(AgentAnalysis.report_id == report.id)
    )
    report.analyses = list(result.scalars().all())

    return _report_to_dict(report, include_analyses=True)


async def list_reports(
    exchange: Optional[str],
    currency_pair: Optional[str],
    report_type: Optional[str],
    limit: int,
    db: AsyncSession,
) -> list[dict]:
    """보고 목록 (reported_at DESC). agent_analysis summary 포함."""
    stmt = (
        select(AnalysisReport)
        .order_by(desc(AnalysisReport.reported_at))
        .limit(limit)
    )
    if exchange:
        stmt = stmt.where(AnalysisReport.exchange == exchange)
    if currency_pair:
        stmt = stmt.where(AnalysisReport.currency_pair == currency_pair)
    if report_type:
        stmt = stmt.where(AnalysisReport.report_type == report_type)

    result = await db.execute(stmt)
    reports = list(result.scalars().all())

    # 각 보고의 agent_analysis 일괄 로드
    if reports:
        report_ids = [r.id for r in reports]
        a_result = await db.execute(
            select(AgentAnalysis).where(AgentAnalysis.report_id.in_(report_ids))
        )
        analyses_by_report: dict[int, list] = {}
        for a in a_result.scalars().all():
            analyses_by_report.setdefault(a.report_id, []).append(a)
        for r in reports:
            r.analyses = analyses_by_report.get(r.id, [])

    return [_report_to_dict(r, include_analyses=True) for r in reports]


async def get_report(report_id: int, db: AsyncSession) -> Optional[dict]:
    """보고 상세 + agent_analysis 전문(full_text) 포함."""
    result = await db.execute(
        select(AnalysisReport).where(AnalysisReport.id == report_id)
    )
    report = result.scalar_one_or_none()
    if report is None:
        return None

    a_result = await db.execute(
        select(AgentAnalysis).where(AgentAnalysis.report_id == report_id)
    )
    report.analyses = list(a_result.scalars().all())
    return _report_to_dict(report, include_analyses=True)


async def get_latest_reports(exchange: str, db: AsyncSession) -> list[dict]:
    """거래소 내 통화별 최신 보고 1건씩. 목록 화면 첫 로드용."""
    # 윈도우 함수로 currency_pair별 최신 1건 선택
    subq = (
        select(
            AnalysisReport,
            func.row_number()
            .over(
                partition_by=AnalysisReport.currency_pair,
                order_by=desc(AnalysisReport.reported_at),
            )
            .label("rn"),
        )
        .where(AnalysisReport.exchange == exchange)
        .subquery()
    )

    result = await db.execute(
        select(AnalysisReport)
        .join(subq, AnalysisReport.id == subq.c.id)
        .where(subq.c.rn == 1)
        .order_by(AnalysisReport.currency_pair)
    )
    reports = list(result.scalars().all())

    if reports:
        report_ids = [r.id for r in reports]
        a_result = await db.execute(
            select(AgentAnalysis).where(AgentAnalysis.report_id.in_(report_ids))
        )
        analyses_by_report: dict[int, list] = {}
        for a in a_result.scalars().all():
            analyses_by_report.setdefault(a.report_id, []).append(a)
        for r in reports:
            r.analyses = analyses_by_report.get(r.id, [])

    return [_report_to_dict(r, include_analyses=True) for r in reports]


# ──────────────────────────────────────────────────────────────
# 반성 사이클 (AgentReflection)
# ──────────────────────────────────────────────────────────────

async def create_reflection(
    reflection_date: date,
    agent_name: str,
    period_type: str,
    period_start: Optional[date],
    period_end: Optional[date],
    missed_data: Optional[list],
    data_improvement: Optional[list],
    effective_decisions: Optional[list],
    action_items: Optional[list],
    strategy_performance: Optional[dict],
    db: AsyncSession,
) -> dict:
    """반성 사이클 저장."""
    reflection = AgentReflection(
        reflection_date=reflection_date,
        agent_name=agent_name,
        period_type=period_type,
        period_start=period_start,
        period_end=period_end,
        missed_data=missed_data,
        data_improvement=data_improvement,
        effective_decisions=effective_decisions,
        action_items=action_items,
        strategy_performance=strategy_performance,
    )
    db.add(reflection)
    await db.commit()
    await db.refresh(reflection)
    return _reflection_to_dict(reflection)


async def list_reflections(
    agent_name: Optional[str],
    period_type: Optional[str],
    limit: int,
    db: AsyncSession,
) -> list[dict]:
    """반성 목록 (reflection_date DESC)."""
    stmt = (
        select(AgentReflection)
        .order_by(desc(AgentReflection.reflection_date))
        .limit(limit)
    )
    if agent_name:
        stmt = stmt.where(AgentReflection.agent_name == agent_name)
    if period_type:
        stmt = stmt.where(AgentReflection.period_type == period_type)

    result = await db.execute(stmt)
    return [_reflection_to_dict(r) for r in result.scalars().all()]


async def get_reflection(reflection_id: int, db: AsyncSession) -> Optional[dict]:
    """반성 상세."""
    result = await db.execute(
        select(AgentReflection).where(AgentReflection.id == reflection_id)
    )
    r = result.scalar_one_or_none()
    return _reflection_to_dict(r) if r else None
