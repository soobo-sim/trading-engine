"""
Evolution API 라우터 — P1~P8 진화 도메인 엔드포인트.

GET  /api/tunables          — Tunable 카탈로그 전체 조회 (P1)
GET  /api/lessons/stats     — Lesson 통계 (P2)
POST /api/lessons           — Lesson 생성 (P2)
GET  /api/lessons           — Lesson 목록 (P2)
GET  /api/lessons/{id}      — Lesson 단일 조회 (P2)
PATCH /api/lessons/{id}     — Lesson 부분 업데이트 (P2)
DELETE /api/lessons/{id}    — Lesson soft delete (P2)
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db
from api.schemas.evolution import (
    LessonCreate,
    LessonListResponse,
    LessonResponse,
    LessonStatsResponse,
    LessonUpdate,
    RecallRequest,
    RecallResponse,
    RecalledLesson,
    TunableListResponse,
    TunableResponse,
)
from api.services.lessons_recall import RecallContext, recall_lessons, summarize
from api.services.lessons_service import LessonsService
from core.shared.tunable_catalog import TunableCatalog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Evolution"])


# ── P1: Tunable 카탈로그 ──────────────────────────────────────


@router.get("/tunables", response_model=TunableListResponse, summary="Tunable 카탈로그 조회")
async def list_tunables(
    layer: str | None = Query(None, description="레이어 필터 (A/B/C/D/E)"),
    autonomy: str | None = Query(None, description="자율성 필터 (auto/escalation)"),
) -> TunableListResponse:
    """에이전트가 변경할 수 있는 모든 진화 대상 요소를 반환한다."""
    specs = TunableCatalog.list_all()

    if layer is not None:
        layer_upper = layer.upper()
        if layer_upper not in ("A", "B", "C", "D", "E"):
            raise HTTPException(400, f"Invalid layer: {layer!r}. A/B/C/D/E 중 하나.")
        specs = [s for s in specs if s.layer == layer_upper]

    if autonomy is not None:
        if autonomy not in ("auto", "escalation"):
            raise HTTPException(400, f"Invalid autonomy: {autonomy!r}. auto/escalation 중 하나.")
        specs = [s for s in specs if s.autonomy == autonomy]

    tunables = [
        TunableResponse(
            key=s.key,
            layer=s.layer,
            value_type=s.value_type,
            default=s.default,
            current_value=None,
            min=s.min,
            max=s.max,
            allowed_values=list(s.allowed_values) if s.allowed_values else None,
            owner=s.owner,
            risk_level=s.risk_level,
            autonomy=s.autonomy,
            description=s.description,
            affects=list(s.affects),
            db_table=s.db_table,
            db_path=s.db_path,
        )
        for s in specs
    ]

    return TunableListResponse(
        total=len(tunables),
        tunables=tunables,
        by_layer_count=TunableCatalog.count_by_layer(),
    )


# ── P2: Lessons ─────────────────────────────────────────────
# 주의: /api/lessons/stats (고정) 가 /api/lessons/{id} (가변) 보다 먼저 등록돼야 함.


@router.get(
    "/lessons/stats",
    response_model=LessonStatsResponse,
    summary="Lesson 통계",
)
async def get_lesson_stats(
    db: AsyncSession = Depends(get_db),
) -> LessonStatsResponse:
    """전체 Lesson의 status별 / pattern_type별 카운트를 반환한다."""
    svc = LessonsService(db)
    data = await svc.stats()
    return LessonStatsResponse(**data)


@router.post(
    "/lessons",
    response_model=LessonResponse,
    status_code=201,
    summary="Lesson 생성",
)
async def create_lesson(
    payload: LessonCreate,
    db: AsyncSession = Depends(get_db),
) -> LessonResponse:
    """새 교훈을 저장한다. ID는 서버에서 자동 발급 (L-YYYY-NNN)."""
    try:
        svc = LessonsService(db)
        lesson = await svc.create(payload)
        return LessonResponse.model_validate(lesson)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get(
    "/lessons",
    response_model=LessonListResponse,
    summary="Lesson 목록",
)
async def list_lessons(
    status: str | None = Query(None),
    pattern_type: str | None = Query(None),
    market_regime: str | None = Query(None),
    pair: str | None = Query(None),
    min_confidence: float | None = Query(None, ge=0.0, le=1.0),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> LessonListResponse:
    svc = LessonsService(db)
    total, lessons = await svc.list(
        status=status,
        pattern_type=pattern_type,
        market_regime=market_regime,
        pair=pair,
        min_confidence=min_confidence,
        limit=limit,
        offset=offset,
    )
    return LessonListResponse(
        total=total,
        lessons=[LessonResponse.model_validate(l) for l in lessons],
    )


@router.get(
    "/lessons/{lesson_id}",
    response_model=LessonResponse,
    summary="Lesson 단일 조회",
)
async def get_lesson(
    lesson_id: str,
    db: AsyncSession = Depends(get_db),
) -> LessonResponse:
    svc = LessonsService(db)
    lesson = await svc.get(lesson_id)
    if lesson is None:
        raise HTTPException(404, f"Lesson not found: {lesson_id!r}")
    return LessonResponse.model_validate(lesson)


@router.patch(
    "/lessons/{lesson_id}",
    response_model=LessonResponse,
    summary="Lesson 부분 업데이트",
)
async def update_lesson(
    lesson_id: str,
    payload: LessonUpdate,
    db: AsyncSession = Depends(get_db),
) -> LessonResponse:
    try:
        svc = LessonsService(db)
        lesson = await svc.update(lesson_id, payload)
        if lesson is None:
            raise HTTPException(404, f"Lesson not found: {lesson_id!r}")
        return LessonResponse.model_validate(lesson)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete(
    "/lessons/{lesson_id}",
    status_code=204,
    summary="Lesson soft delete (status=deprecated)",
)
async def delete_lesson(
    lesson_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    svc = LessonsService(db)
    found = await svc.delete(lesson_id)
    if not found:
        raise HTTPException(404, f"Lesson not found: {lesson_id!r}")


# ── P3: Lessons Recall ──────────────────────────────────────
# 주의: /api/lessons/recall (고정) 는 /api/lessons/{id} (가변) 보다 먼저 등록.
# FastAPI는 등록 순서대로 매칭하므로 stats와 recall을 {id} 앞에 선언해야 한다.
# (현재 stats + recall 이 먼저 선언되어 있으므로 정상)


@router.post(
    "/lessons/recall",
    response_model=RecallResponse,
    summary="관련 교훈 소환 (Recall)",
)
async def recall(
    payload: RecallRequest,
    db: AsyncSession = Depends(get_db),
) -> RecallResponse:
    """컨텍스트에 매칭되는 active Lesson을 score 순으로 최대 top_k 반환.
    reference_count 와 last_referenced_at 이 자동 갱신된다.
    """
    ctx = RecallContext(
        pair=payload.pair,
        market_regime=payload.market_regime,
        has_position=payload.has_position,
        position_side=payload.position_side,
        bb_width_pct=payload.bb_width_pct,
        atr_pct=payload.atr_pct,
        last_4h_change_pct=payload.last_4h_change_pct,
        macro_context=payload.macro_context,
        workflow=payload.workflow,
        top_k=payload.top_k,
    )
    matches = await recall_lessons(db, ctx)

    recalled = [
        RecalledLesson(
            id=lesson.id,
            pattern_type=lesson.pattern_type,
            observation=lesson.observation,
            recommendation=lesson.recommendation,
            confidence=lesson.confidence,
            match_score=round(s, 3),
            summary=summarize(lesson.observation, max_len=100),
        )
        for lesson, s in matches
    ]

    return RecallResponse(
        context=payload,
        matched_count=len(recalled),
        lessons=recalled,
    )


# ── P4: Hypotheses ──────────────────────────────────────────
# 주의: /api/hypotheses/stats, /api/hypotheses/expire-overdue 가
# /api/hypotheses/{id} 보다 먼저 등록돼야 함.

from api.schemas.evolution import (  # noqa: E402 — 의존성 순환 방지용 지연 임포트
    HypothesisCreate,
    HypothesisListResponse,
    HypothesisResponse,
    HypothesisStatsResponse,
    HypothesisTransition,
)
from api.services.hypotheses_service import HypothesesService  # noqa: E402


@router.get(
    "/hypotheses/stats",
    response_model=HypothesisStatsResponse,
    summary="가설 통계 (상태별 / 트랙별 카운트)",
)
async def get_hypothesis_stats(
    db: AsyncSession = Depends(get_db),
) -> HypothesisStatsResponse:
    svc = HypothesesService(db)
    data = await svc.stats()
    return HypothesisStatsResponse(**data)


@router.post(
    "/hypotheses/expire-overdue",
    summary="Escalation 만료 처리 (cron 호출용)",
)
async def expire_overdue_hypotheses(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """expires_at 지난 escalation 가설을 자동 rejected 처리한다."""
    svc = HypothesesService(db)
    expired = await svc.expire_overdue()
    return {"expired_count": len(expired), "expired_ids": expired}


@router.post(
    "/hypotheses",
    response_model=HypothesisResponse,
    status_code=201,
    summary="가설 등록 (proposed)",
)
async def create_hypothesis(
    payload: HypothesisCreate,
    db: AsyncSession = Depends(get_db),
) -> HypothesisResponse:
    """새 가설을 등록한다. escalation 트랙은 자동 판정 + expires_at=7일."""
    try:
        svc = HypothesesService(db)
        h = await svc.create(payload)
        return HypothesisResponse.model_validate(h)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get(
    "/hypotheses",
    response_model=HypothesisListResponse,
    summary="가설 목록",
)
async def list_hypotheses(
    status: str | None = Query(None),
    track: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> HypothesisListResponse:
    svc = HypothesesService(db)
    total, rows = await svc.list(status=status, track=track, limit=limit, offset=offset)
    return HypothesisListResponse(
        total=total,
        hypotheses=[HypothesisResponse.model_validate(h) for h in rows],
    )


@router.get(
    "/hypotheses/{hypothesis_id}",
    response_model=HypothesisResponse,
    summary="가설 단일 조회",
)
async def get_hypothesis(
    hypothesis_id: str,
    db: AsyncSession = Depends(get_db),
) -> HypothesisResponse:
    svc = HypothesesService(db)
    h = await svc.get(hypothesis_id)
    if h is None:
        raise HTTPException(404, f"Hypothesis not found: {hypothesis_id!r}")
    return HypothesisResponse.model_validate(h)


@router.post(
    "/hypotheses/{hypothesis_id}/transition",
    response_model=HypothesisResponse,
    summary="가설 상태 전이",
)
async def transition_hypothesis(
    hypothesis_id: str,
    payload: HypothesisTransition,
    db: AsyncSession = Depends(get_db),
) -> HypothesisResponse:
    """가설의 상태를 전이한다. 허용 전이 매트릭스를 벗어나면 422."""
    try:
        svc = HypothesesService(db)
        h = await svc.transition(
            hypothesis_id,
            payload.new_status,
            actor=payload.actor,
            payload=payload.payload,
        )
        return HypothesisResponse.model_validate(h)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(422, str(exc)) from exc


# ── P5: CycleReport ──────────────────────────────────────────

from api.schemas.evolution import (  # noqa: E402
    CycleReportInput,
    CycleReportResponse,
)
from api.services.cycle_report_service import (  # noqa: E402
    CycleReportService,
    format_evolution_report,
)
from core.judge.evolution.notifications import notify_evolution  # noqa: E402


@router.post(
    "/cycle-reports/generate",
    response_model=CycleReportResponse,
    status_code=201,
    summary="진화 사이클 보고서 생성 + 텔레그램 발송",
)
async def generate_cycle_report(
    payload: CycleReportInput,
    db: AsyncSession = Depends(get_db),
) -> CycleReportResponse:
    """Rachel이 생성한 6단계 보고서를 검증 후 저장 + 진화 채널로 발송."""
    try:
        svc = CycleReportService(db)
        report = await svc.build_and_validate(payload)
    except ValueError as exc:
        raise HTTPException(422, {"detail": "causality broken", "error": str(exc)}) from exc

    await svc.persist(report)
    msg = format_evolution_report(report)
    sent = await notify_evolution(msg)
    report.telegram_sent = sent
    return report


@router.get(
    "/cycle-reports",
    summary="사이클 보고서 목록",
)
async def list_cycle_reports(
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    svc = CycleReportService(db)
    items = await svc.list(limit=limit)
    return {"total": len(items), "reports": items}


# ── P6: Canary 상태 조회 ─────────────────────────────────────

from api.schemas.evolution import CanaryStatusResponse  # noqa: E402


@router.get(
    "/canary/active",
    response_model=list[CanaryStatusResponse],
    summary="활성 Canary 가설 목록 + 가드레일 상태",
)
async def list_active_canaries(
    db: AsyncSession = Depends(get_db),
) -> list[CanaryStatusResponse]:
    from adapters.database.hypothesis_model import Hypothesis
    from sqlalchemy import select as _sa_select
    from core.judge.evolution.guardrails import (
        check_guardrails,
        _fetch_current_balance_jpy,
    )
    from core.judge.evolution.canary_monitor import get_canary_monitor
    from datetime import datetime, timezone, timedelta

    JST = timezone(timedelta(hours=9))
    now = datetime.now(tz=JST)

    stmt = _sa_select(Hypothesis).where(Hypothesis.status == "canary")
    canaries = (await db.execute(stmt)).scalars().all()

    monitor = get_canary_monitor()
    current_balance = await _fetch_current_balance_jpy(db)

    out: list[CanaryStatusResponse] = []
    for h in canaries:
        start_bal = monitor._canary_start_balances.get(h.id) or (
            float(h.canary_result.get("start_balance_jpy", 0)) if h.canary_result else 0
        )
        canary_start_at = h.approved_at or h.created_at
        elapsed_hours = (now - (canary_start_at.replace(tzinfo=JST) if canary_start_at.tzinfo is None else canary_start_at)).total_seconds() / 3600
        pnl_pct = (current_balance - start_bal) / start_bal * 100 if start_bal > 0 else 0.0

        violation = await check_guardrails(
            db, h,
            current_balance_jpy=current_balance,
            canary_start_balance_jpy=start_bal,
            canary_start_at=canary_start_at,
        )
        out.append(CanaryStatusResponse(
            hypothesis_id=h.id,
            title=h.title,
            started_at=h.approved_at,
            elapsed_hours=round(elapsed_hours, 1),
            start_balance_jpy=start_bal,
            current_balance_jpy=current_balance,
            pnl_pct=round(pnl_pct, 2),
            current_violation=violation.to_dict() if violation else None,
        ))
    return out


# ── P7: Monthly Meta ─────────────────────────────────────────

@router.post(
    "/lessons/decay-stale",
    status_code=200,
    summary="Lesson 신뢰도 감쇠 실행 (월 1회 monthly-meta)",
)
async def decay_stale_lessons(db: AsyncSession = Depends(get_db)) -> dict:
    from api.services.lesson_decay_service import LessonDecayService
    svc = LessonDecayService(db)
    report = await svc.run()
    return report.model_dump(mode="json")


@router.get(
    "/tunables/audit",
    summary="미사용 Tunable 감사 (monthly-meta 보조)",
)
async def audit_tunables(
    since_days: int = Query(90, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select as _sa_select
    from adapters.database.hypothesis_model import Hypothesis

    JST = timezone(timedelta(hours=9))
    cutoff = datetime.now(tz=JST) - timedelta(days=since_days)

    stmt = _sa_select(Hypothesis).where(Hypothesis.created_at >= cutoff)
    try:
        rows = (await db.execute(stmt)).scalars().all()
    except Exception:
        rows = []

    used_keys: set[str] = set()
    for h in rows:
        for ch in (h.changes or []):
            if isinstance(ch, dict):
                used_keys.add(ch.get("tunable_key", ""))

    all_specs = TunableCatalog.list_all()
    unused = [s for s in all_specs if s.key not in used_keys]

    return {
        "period_days": since_days,
        "total_tunables": len(all_specs),
        "used_count": len(used_keys),
        "unused_count": len(unused),
        "unused_keys": [{"key": s.key, "layer": s.layer, "autonomy": s.autonomy} for s in unused],
    }


@router.get(
    "/evolution/health",
    summary="진화 건강도 지표 (P7)",
)
async def evolution_health(
    since_days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select as _sa_select, func as _sa_func, text as _sa_text
    from adapters.database.hypothesis_model import Hypothesis

    JST = timezone(timedelta(hours=9))
    cutoff = datetime.now(tz=JST) - timedelta(days=since_days)

    # cycle_reports
    try:
        rows = (await db.execute(
            _sa_text(
                "SELECT payload FROM ai_judgments "
                "WHERE kind = 'cycle_report' AND judged_at >= :cutoff"
            ),
            {"cutoff": cutoff},
        )).all()
        import json as _json
        cycles = [_json.loads(r[0]) for r in rows]
    except Exception:
        cycles = []

    no_signal = sum(1 for c in cycles if c.get("mode") == "no_signal")
    failed = sum(1 for c in cycles if c.get("mode") == "failed")
    full = len(cycles) - no_signal - failed

    # hypotheses
    try:
        hyps = (await db.execute(
            _sa_select(Hypothesis).where(Hypothesis.created_at >= cutoff)
        )).scalars().all()
    except Exception:
        hyps = []

    by_status: dict[str, int] = {}
    for h in hyps:
        by_status[h.status] = by_status.get(h.status, 0) + 1

    # lessons active count
    try:
        from adapters.database.lesson_model import Lesson
        active_count = (await db.execute(
            _sa_select(_sa_func.count(Lesson.id)).where(Lesson.status == "active")
        )).scalar() or 0
    except Exception:
        active_count = 0

    adoption_rate = round(by_status.get("adopted", 0) / len(hyps), 2) if hyps else 0.0
    no_signal_ratio = round(no_signal / len(cycles), 2) if cycles else 0.0

    return {
        "period_days": since_days,
        "cycles_total": len(cycles),
        "cycles_full": full,
        "cycles_no_signal": no_signal,
        "cycles_failed": failed,
        "no_signal_ratio": no_signal_ratio,
        "hypotheses_proposed": by_status.get("proposed", 0),
        "hypotheses_adopted": by_status.get("adopted", 0),
        "hypotheses_rolled_back": by_status.get("rolled_back", 0),
        "hypotheses_rejected": by_status.get("rejected", 0),
        "adoption_rate": adoption_rate,
        "active_lessons": active_count,
    }


# ── P8: Owner Queries ─────────────────────────────────────────

from api.schemas.evolution import (  # noqa: E402
    OwnerQueryCreate,
    OwnerQueryClose,
    OwnerQueryResponse,
)


@router.post(
    "/owner-queries",
    response_model=OwnerQueryResponse,
    status_code=201,
    summary="Owner Query 등록 (수보오빠 의문 영속화)",
)
async def create_owner_query(
    payload: OwnerQueryCreate,
    db: AsyncSession = Depends(get_db),
) -> OwnerQueryResponse:
    from api.services.owner_query_service import OwnerQueryService
    svc = OwnerQueryService(db)
    try:
        q = await svc.create(
            content=payload.content,
            category=payload.category if payload.category != "general" else None,
            priority=payload.priority,
            source=payload.source,
        )
    except ValueError as exc:
        raise HTTPException(400, {"detail": str(exc)}) from exc
    return OwnerQueryResponse.model_validate(q)


@router.get(
    "/owner-queries",
    response_model=dict,
    summary="Owner Query 목록 조회",
)
async def list_owner_queries(
    status: str = Query("open"),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from api.services.owner_query_service import OwnerQueryService
    svc = OwnerQueryService(db)
    total, rows = await svc.list(status=status, limit=limit)
    return {
        "total": total,
        "queries": [OwnerQueryResponse.model_validate(r).model_dump(mode="json") for r in rows],
    }


@router.patch(
    "/owner-queries/{query_id}/close",
    response_model=OwnerQueryResponse,
    summary="Owner Query 완료 처리",
)
async def close_owner_query(
    query_id: str,
    payload: OwnerQueryClose,
    db: AsyncSession = Depends(get_db),
) -> OwnerQueryResponse:
    from api.services.owner_query_service import OwnerQueryService
    svc = OwnerQueryService(db)
    try:
        q = await svc.close(
            query_id=query_id,
            cycle_id=payload.cycle_id,
            outcome_summary=payload.outcome_summary,
            hypothesis_id=payload.hypothesis_id,
        )
    except ValueError as exc:
        raise HTTPException(400, {"detail": str(exc)}) from exc
    return OwnerQueryResponse.model_validate(q)
