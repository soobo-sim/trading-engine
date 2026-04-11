"""
Advisory API — 레이첼 전략 자문 저장/조회.

POST /api/advisories           — 레이첼이 자문 저장 (엔진이 읽을 대상)
GET  /api/advisories/{pair}/latest — 최신 미만료 자문 조회 (디버깅용)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.models import RachelAdvisory
from api.dependencies import AppState, get_db, get_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/advisories", tags=["Advisories"])

_VALID_ACTIONS = frozenset({"entry_long", "entry_short", "hold", "exit"})
_VALID_REGIMES = frozenset({"trending", "ranging", "uncertain"})


# ── Schemas ───────────────────────────────────────────────────

class AdvisoryCreateRequest(BaseModel):
    pair: str = Field(..., description="페어 (예: BTC_JPY, USD_JPY)")
    action: str = Field(..., description="entry_long|entry_short|hold|exit")
    confidence: float = Field(..., ge=0.0, le=1.0, description="판정 확신도 0.0~1.0")
    size_pct: float | None = Field(None, ge=0.0, le=0.80, description="포지션 사이즈 비율 (최대 0.80)")
    stop_loss: float | None = None
    take_profit: float | None = None
    regime: str | None = Field(None, description="trending|ranging|uncertain")
    reasoning: str = Field(..., min_length=20, description="판정 근거 요약 (20자 이상)")
    risk_notes: str | None = None
    alice_summary: str | None = None
    samantha_summary: str | None = None
    ttl_hours: float = Field(5.0, gt=0.0, le=48.0, description="만료까지 시간 (기본 5H, 최대 48H)")


class AdvisoryResponse(BaseModel):
    id: int
    pair: str
    exchange: str
    action: str
    confidence: float
    size_pct: float | None
    stop_loss: float | None
    take_profit: float | None
    regime: str | None
    reasoning: str
    risk_notes: str | None
    alice_summary: str | None
    samantha_summary: str | None
    created_at: datetime
    expires_at: datetime
    is_expired: bool

    model_config = {"from_attributes": True}


def _to_response(advisory: RachelAdvisory) -> AdvisoryResponse:
    now = datetime.now(timezone.utc)
    expires_at = advisory.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return AdvisoryResponse(
        id=advisory.id,
        pair=advisory.pair,
        exchange=advisory.exchange,
        action=advisory.action,
        confidence=advisory.confidence,
        size_pct=advisory.size_pct,
        stop_loss=advisory.stop_loss,
        take_profit=advisory.take_profit,
        regime=advisory.regime,
        reasoning=advisory.reasoning,
        risk_notes=advisory.risk_notes,
        alice_summary=advisory.alice_summary,
        samantha_summary=advisory.samantha_summary,
        created_at=advisory.created_at,
        expires_at=expires_at,
        is_expired=now >= expires_at,
    )


# ── 엔드포인트 ────────────────────────────────────────────────

@router.post("", response_model=AdvisoryResponse, status_code=201)
async def create_advisory(
    body: AdvisoryCreateRequest,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """레이첼이 정기 분석 완료 후 자문을 저장한다.

    엔진 candle_monitor()가 TRADING_MODE=rachel 일 때 이 레코드를 읽어 판단에 활용한다.
    pair는 요청 값 그대로 저장 (대소문자 보존).
    exchange는 서버의 EXCHANGE 환경변수에서 자동 결정.
    """
    if body.action not in _VALID_ACTIONS:
        raise HTTPException(
            400,
            {"blocked_code": "INVALID_ACTION", "detail": f"action은 {sorted(_VALID_ACTIONS)} 중 하나여야 합니다."},
        )
    if body.regime is not None and body.regime not in _VALID_REGIMES:
        raise HTTPException(
            400,
            {"blocked_code": "INVALID_REGIME", "detail": f"regime은 {sorted(_VALID_REGIMES)} 중 하나여야 합니다."},
        )

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=body.ttl_hours)
    exchange = os.environ.get("EXCHANGE", "bitflyer").lower()

    advisory = RachelAdvisory(
        pair=body.pair,
        exchange=exchange,
        action=body.action,
        confidence=body.confidence,
        size_pct=body.size_pct,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        regime=body.regime,
        reasoning=body.reasoning,
        risk_notes=body.risk_notes,
        alice_summary=body.alice_summary,
        samantha_summary=body.samantha_summary,
        expires_at=expires_at,
    )
    db.add(advisory)
    await db.commit()
    await db.refresh(advisory)

    logger.info(
        f"[Advisory] 저장 완료: pair={advisory.pair} action={advisory.action} "
        f"confidence={advisory.confidence:.2f} expires={expires_at.isoformat()}"
    )
    return _to_response(advisory)


@router.get("/{pair}/latest", response_model=AdvisoryResponse)
async def get_latest_advisory(
    pair: str,
    include_expired: bool = Query(False, description="만료된 자문도 포함"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """디버깅용 — 해당 pair의 최신 자문 (미만료) 조회.

    include_expired=true 이면 만료된 자문도 반환 (기동 확인용).
    advisory 없으면 404.
    """
    exchange = os.environ.get("EXCHANGE", "bitflyer").lower()
    now = datetime.now(timezone.utc)

    stmt = (
        select(RachelAdvisory)
        .where(
            RachelAdvisory.pair == pair,
            RachelAdvisory.exchange == exchange,
        )
    )
    if not include_expired:
        stmt = stmt.where(RachelAdvisory.expires_at > now)

    stmt = stmt.order_by(desc(RachelAdvisory.created_at)).limit(1)
    result = await db.execute(stmt)
    advisory = result.scalars().first()

    if advisory is None:
        raise HTTPException(
            404,
            {"error": f"pair={pair}의 {'미만료 ' if not include_expired else ''}advisory 없음"},
        )
    return _to_response(advisory)
