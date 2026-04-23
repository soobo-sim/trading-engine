"""
Lessons Recall 서비스 — P3 Self-Evolution Loop.

structured 매칭 + confidence 가중치로 관련 교훈을 반환한다.
LLM 기반 의미 검색은 P3 범위 외 (P5+ 확장 가능).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from adapters.database.lesson_model import Lesson

JST = timezone(timedelta(hours=9))

# 환경변수로 임계값 조정 가능
_RECALL_MIN_SCORE = float(os.getenv("LESSON_RECALL_MIN_SCORE", "0.3"))


# ── 입력 컨텍스트 ────────────────────────────────────────────


@dataclass
class RecallContext:
    pair: str                                   # btc_jpy
    market_regime: str                          # trending / ranging / unclear
    has_position: bool = False
    position_side: str | None = None            # long / short / None
    bb_width_pct: float | None = None
    atr_pct: float | None = None
    last_4h_change_pct: float | None = None
    macro_context: dict[str, Any] | None = None  # fng, news_count, events 등
    workflow: str = "4h_advisory"
    top_k: int = 3                              # advisory.lessons_recall_top_k


# ── 조건 매칭 ────────────────────────────────────────────────


def _match_conditions(cond: dict[str, Any], ctx: RecallContext) -> float:
    """lesson.conditions JSONB 키별 매칭 비율 (0.0 ~ 1.0) 반환."""
    matches = 0
    total = 0

    if "bb_width_min" in cond and ctx.bb_width_pct is not None:
        total += 1
        if ctx.bb_width_pct >= cond["bb_width_min"]:
            matches += 1

    if "bb_width_max" in cond and ctx.bb_width_pct is not None:
        total += 1
        if ctx.bb_width_pct <= cond["bb_width_max"]:
            matches += 1

    if "atr_pct_min" in cond and ctx.atr_pct is not None:
        total += 1
        if ctx.atr_pct >= cond["atr_pct_min"]:
            matches += 1

    if "fng_max" in cond and ctx.macro_context and "fng" in ctx.macro_context:
        total += 1
        fng_val = ctx.macro_context["fng"]
        if isinstance(fng_val, (int, float)) and fng_val <= cond["fng_max"]:
            matches += 1

    if "side" in cond and ctx.position_side:
        total += 1
        if cond["side"] == ctx.position_side:
            matches += 1

    # 조건이 없는 lesson은 "보편 적용" → 0.5
    if total == 0:
        return 0.5
    return matches / total


# ── 개별 lesson 점수 계산 ────────────────────────────────────


def score(lesson: Lesson, ctx: RecallContext) -> float:
    """lesson이 ctx에 얼마나 적합한지 0.0 ~ 1.0 점수를 반환한다."""
    s = 0.0

    # (1) 페어 일치 +0.20 (any/None 면 +0.10)
    if lesson.pair in (ctx.pair, "any", None):
        s += 0.20 if lesson.pair == ctx.pair else 0.10
    else:
        return 0.0  # 다른 페어는 즉시 탈락

    # (2) 체제 일치 +0.30 (any/None 면 +0.15)
    if lesson.market_regime in (ctx.market_regime, "any", None):
        s += 0.30 if lesson.market_regime == ctx.market_regime else 0.15
    else:
        return 0.0  # 다른 체제는 탈락

    # (3) 워크플로우 매치 +0.10
    wf_match = (lesson.conditions or {}).get("workflow") if lesson.conditions else None
    if wf_match is None or wf_match == ctx.workflow:
        s += 0.10

    # (4) 조건 일치 — JSONB 키별 매칭 (최대 +0.20)
    cond_score = _match_conditions(lesson.conditions or {}, ctx)
    s += cond_score * 0.20

    # (5) 신뢰도 가중 — confidence=1.0 → 100%, 0.0 → 50%
    s *= 0.5 + 0.5 * lesson.confidence

    # (6) 최근성 보너스/패널티
    age_days = (datetime.now(JST) - lesson.updated_at.replace(tzinfo=JST)).days
    if age_days < 30:
        s *= 1.05
    elif age_days > 90:
        s *= 0.95

    return min(s, 1.0)


# ── 관련 교훈 소환 (메인 함수) ───────────────────────────────


async def recall_lessons(
    db: AsyncSession,
    ctx: RecallContext,
    *,
    min_score: float | None = None,
) -> list[tuple[Lesson, float]]:
    """ctx에 매칭되는 active Lesson을 score 내림차순 top_k 반환.

    반환: [(Lesson, score), ...]
    """
    effective_min_score = min_score if min_score is not None else _RECALL_MIN_SCORE

    # DB 단 후보 좁히기 (페어 + 체제 pre-filter)
    stmt = select(Lesson).where(
        Lesson.status == "active",
        or_(
            Lesson.pair == ctx.pair,
            Lesson.pair == "any",
            Lesson.pair.is_(None),
        ),
        or_(
            Lesson.market_regime == ctx.market_regime,
            Lesson.market_regime == "any",
            Lesson.market_regime.is_(None),
        ),
    )
    candidates = (await db.execute(stmt)).scalars().all()

    # Python 단 세밀 점수 계산
    scored = [(l, score(l, ctx)) for l in candidates]
    scored = [(l, s) for l, s in scored if s > effective_min_score]
    scored.sort(key=lambda x: x[1], reverse=True)
    selected = scored[: ctx.top_k]

    # reference_count / last_referenced_at 갱신 (비동기)
    if selected:
        ids = [l.id for l, _ in selected]
        await db.execute(
            update(Lesson)
            .where(Lesson.id.in_(ids))
            .values(
                reference_count=Lesson.reference_count + 1,
                last_referenced_at=func.now(),
            )
        )
        await db.commit()

    return selected


# ── 요약 헬퍼 ────────────────────────────────────────────────


def summarize(text: str, max_len: int = 100) -> str:
    """observation 텍스트를 max_len 자로 요약 (advisory 인용용)."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"
