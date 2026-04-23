"""
owner_query_service.py — P8 OwnerQuery CRUD + 자동 카테고리 추론.

ID 형식: OQ-{YYYY}-{NNN}
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.owner_query_model import OwnerQuery

JST = timezone(timedelta(hours=9))
logger = logging.getLogger("core.judge.evolution.owner_query")

# ── 카테고리 자동 추론 ────────────────────────────────────────

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "no_trade": ["거래 없", "진입 없", "매매 없", "안 열린", "진입이 이루어지지 않"],
    "regime_stuck": ["체제 전환", "횡보장인데", "레짐 안 바뀌", "trending인데 이상", "ranging인데"],
    "regime_repeat": ["같은 체제", "연속으로", "반복", "이상하다", "너무 많이"],
    "signal_doubt": ["왜 진입 안", "조건 어디가", "시그널이 왜", "entry 안"],
    "parameter_doubt": ["파라미터", "값이 너무", "임계값", "기준이"],
    "performance": ["PnL", "손실", "수익", "이달", "이번 주"],
    "risk": ["포지션이 너무", "오래 열려", "위험", "리스크"],
}

VALID_CATEGORIES = set(CATEGORY_KEYWORDS) | {"general"}
VALID_PRIORITIES = {"high", "medium", "low"}
VALID_STATUSES = {"open", "closed"}


def infer_category(content: str) -> str:
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in content for kw in keywords):
            return cat
    return "general"


# ── 서비스 ───────────────────────────────────────────────────

class OwnerQueryService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        content: str,
        category: str | None = None,
        priority: str = "medium",
        source: str = "samantha",
    ) -> OwnerQuery:
        if not content or len(content) < 10:
            raise ValueError("content must be at least 10 characters")
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"priority must be one of {VALID_PRIORITIES}")

        resolved_category = category or infer_category(content)
        if resolved_category not in VALID_CATEGORIES:
            resolved_category = "general"

        new_id = await self._next_id()
        now = datetime.now(tz=JST)
        q = OwnerQuery(
            id=new_id,
            content=content,
            category=resolved_category,
            status="open",
            priority=priority,
            source=source,
            asked_at=now,
        )
        self.db.add(q)
        await self.db.commit()
        await self.db.refresh(q)
        logger.info("OwnerQuery 등록 %s [%s] %s", new_id, resolved_category, priority)
        return q

    async def get(self, query_id: str) -> OwnerQuery | None:
        return await self.db.get(OwnerQuery, query_id)

    async def list(
        self,
        status: str | None = "open",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[OwnerQuery]]:
        stmt = select(OwnerQuery)
        if status:
            stmt = stmt.where(OwnerQuery.status == status)
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total: int = (await self.db.execute(count_stmt)).scalar() or 0
        stmt = stmt.order_by(OwnerQuery.asked_at.desc()).limit(limit).offset(offset)
        rows = (await self.db.execute(stmt)).scalars().all()
        return total, list(rows)

    async def close(
        self,
        query_id: str,
        cycle_id: str,
        outcome_summary: str,
        hypothesis_id: str | None = None,
    ) -> OwnerQuery:
        q = await self.get(query_id)
        if q is None:
            raise ValueError(f"OwnerQuery not found: {query_id!r}")
        if q.status == "closed":
            raise ValueError(f"OwnerQuery {query_id} is already closed")
        if not outcome_summary or len(outcome_summary) < 20:
            raise ValueError("outcome_summary must be at least 20 characters")

        now = datetime.now(tz=JST)
        q.status = "closed"
        q.closed_at = now
        q.addressed_in_cycle = cycle_id
        q.addressed_in_hypothesis = hypothesis_id
        q.outcome_summary = outcome_summary
        await self.db.commit()
        await self.db.refresh(q)
        logger.info("OwnerQuery 완료 %s → cycle=%s", query_id, cycle_id)
        return q

    async def _next_id(self) -> str:
        year = datetime.now(tz=JST).year
        prefix = f"OQ-{year}-"
        stmt = select(func.max(OwnerQuery.id)).where(OwnerQuery.id.like(f"{prefix}%"))
        last: str | None = (await self.db.execute(stmt)).scalar()
        next_num = int(last.split("-")[-1]) + 1 if last else 1
        return f"{prefix}{next_num:03d}"
