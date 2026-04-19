"""
Advisory API — 레이첼 전략 자문 저장/조회.

POST /api/advisories           — 레이첼이 자문 저장 (엔진이 읽을 대상)
GET  /api/advisories/{pair}/latest — 최신 미만료 자문 조회 (디버깅용)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.models import RachelAdvisory
from api.dependencies import AppState, get_db, get_state
from core.judge.decision.advisory_bypass import advisory_bypass
from core.pair import normalize_pair

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/advisories", tags=["Advisories"])

_VALID_ACTIONS = frozenset({"entry_long", "entry_short", "hold", "exit", "adjust_risk", "add_position"})
_VALID_REGIMES = frozenset({"trending", "ranging", "uncertain"})
_VALID_STYLES = frozenset({"trend_following", "box_mean_reversion"})
_VALID_HOLD_OVERRIDES = frozenset({"none", "signal_entry_ok"})


# ── Schemas ───────────────────────────────────────────────────

class AdvisoryCreateRequest(BaseModel):
    pair: str = Field(..., description="페어 (예: BTC_JPY, USD_JPY)")
    action: str = Field(..., description="entry_long|entry_short|hold|exit|adjust_risk")
    confidence: float = Field(..., ge=0.0, le=1.0, description="판정 확신도 0.0~1.0")
    size_pct: float | None = Field(None, ge=0.0, le=0.80, description="포지션 사이즈 비율 (최대 0.80)")
    stop_loss: float | None = None
    take_profit: float | None = None
    regime: str | None = Field(None, description="trending|ranging|uncertain")
    reasoning: str = Field(..., min_length=20, description="판정 근거 요약 (20자 이상)")
    risk_notes: str | None = None
    alice_summary: str | None = None
    samantha_summary: str | None = None
    trading_style: str = Field("trend_following", description="trend_following|box_mean_reversion")
    hold_override_policy: str = Field(
        "none",
        description="hold 시 엔진 자율 진입 허용 정책. none=절대 hold | signal_entry_ok=entry_ok/entry_sell 시그널 시 진입 허용 (기술적 hold 전용)",
    )
    ttl_hours: float = Field(5.0, gt=0.0, le=48.0, description="만료까지 시간 (기본 5H, 최대 48H)")
    adjustments: dict | None = Field(
        None,
        description="adjust_risk 전용. 키: stop_loss_pct, take_profit_ratio, trailing_atr_multiplier, force_exit",
    )
    macro_context: dict | None = Field(
        None,
        description="AI 판단 추적·학습용 매크로 컨텍스트. 구조: {raw: {fng, news_avg, vix, dxy}, interpretation: str, impact_direction: str, impact_notes: str}",
    )


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
    macro_context: dict | None
    risk_notes: str | None
    alice_summary: str | None
    samantha_summary: str | None
    adjustments: dict | None
    trading_style: str
    hold_override_policy: str
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
        macro_context=advisory.macro_context,
        reasoning=advisory.reasoning,
        risk_notes=advisory.risk_notes,
        alice_summary=advisory.alice_summary,
        samantha_summary=advisory.samantha_summary,
        adjustments=advisory.adjustments,
        trading_style=advisory.trading_style,
        hold_override_policy=advisory.hold_override_policy,
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
    pair는 소문자로 정규화하여 저장 (normalize_pair 적용).
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
    if body.action == "adjust_risk" and not body.adjustments:
        raise HTTPException(
            400,
            {"blocked_code": "ADJUSTMENTS_REQUIRED", "detail": "action=adjust_risk 이면 adjustments 필수."},
        )
    if body.trading_style not in _VALID_STYLES:
        raise HTTPException(
            400,
            {"blocked_code": "INVALID_STYLE", "detail": f"trading_style은 {sorted(_VALID_STYLES)} 중 하나여야 합니다."},
        )
    if body.hold_override_policy not in _VALID_HOLD_OVERRIDES:
        raise HTTPException(
            400,
            {"blocked_code": "INVALID_HOLD_OVERRIDE", "detail": f"hold_override_policy는 {sorted(_VALID_HOLD_OVERRIDES)} 중 하나여야 합니다."},
        )

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=body.ttl_hours)
    exchange = os.environ.get("EXCHANGE", "bitflyer").lower()

    # adjust_risk 이외 액션은 adjustments 무시
    adjustments = body.adjustments if body.action == "adjust_risk" else None
    # hold 이외 액션은 hold_override_policy 무의미 → none 고정
    hold_override_policy = body.hold_override_policy if body.action == "hold" else "none"

    pair = normalize_pair(body.pair)
    advisory = RachelAdvisory(
        pair=pair,
        exchange=exchange,
        action=body.action,
        confidence=body.confidence,
        size_pct=body.size_pct,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        macro_context=body.macro_context,
        regime=body.regime,
        reasoning=body.reasoning,
        risk_notes=body.risk_notes,
        alice_summary=body.alice_summary,
        samantha_summary=body.samantha_summary,
        adjustments=adjustments,
        trading_style=body.trading_style,
        hold_override_policy=hold_override_policy,
        expires_at=expires_at,
    )
    db.add(advisory)
    await db.commit()
    await db.refresh(advisory)

    logger.info(
        f"[Advisory] 저장 완료: pair={advisory.pair} action={advisory.action} "
        f"confidence={advisory.confidence:.2f} size_pct={advisory.size_pct} "
        f"regime={advisory.regime} expires={expires_at.astimezone(JST).isoformat()}\n"
        f"  근거: {advisory.reasoning}\n"
        f"  리스크: {advisory.risk_notes or '없음'}"
    )
    return _to_response(advisory)


# ── Bypass 엔드포인트 ─────────────────────────────────────────
# ⚠️ /bypass 고정 경로는 반드시 /{pair}/latest 가변 경로보다 앞에 등록할 것

class AdvisoryBypassSetRequest(BaseModel):
    start: datetime = Field(..., description="bypass 시작 시각 (ISO 8601, 타임존 포함 권장)")
    end: datetime = Field(..., description="bypass 종료 시각 (ISO 8601, 타임존 포함 권장)")


class AdvisoryBypassResponse(BaseModel):
    active: bool
    start: datetime | None
    end: datetime | None
    message: str


@router.get("/bypass", response_model=AdvisoryBypassResponse)
async def get_advisory_bypass():
    """현재 advisory bypass 상태 조회."""
    window = advisory_bypass.get_window()
    active = advisory_bypass.is_active()
    return AdvisoryBypassResponse(
        active=active,
        start=window.start if window else None,
        end=window.end if window else None,
        message="bypass 활성" if active else ("bypass 설정됨 (비활성 시간대)" if window else "bypass 없음"),
    )


@router.post("/bypass", response_model=AdvisoryBypassResponse, status_code=200)
async def set_advisory_bypass(body: AdvisoryBypassSetRequest):
    """Advisory 체크 일시 bypass 창 설정.

    bypass 기간 동안 rachel_advisory는 WARNING 없이 조용히 v1 룰 기반 폴백을 사용한다.
    rate limit 등으로 레이첼이 advisory를 갱신할 수 없을 때 사용.

    예시:
        POST /api/advisories/bypass
        { "start": "2026-04-19T20:00:00+09:00", "end": "2026-04-20T09:00:00+09:00" }
    """
    try:
        advisory_bypass.set(body.start, body.end)
    except ValueError as e:
        raise HTTPException(400, {"blocked_code": "INVALID_BYPASS_RANGE", "detail": str(e)})

    window = advisory_bypass.get_window()
    return AdvisoryBypassResponse(
        active=advisory_bypass.is_active(),
        start=window.start,
        end=window.end,
        message=f"bypass 설정 완료 — {body.start.isoformat()} ~ {body.end.isoformat()}",
    )


@router.delete("/bypass", response_model=AdvisoryBypassResponse, status_code=200)
async def clear_advisory_bypass():
    """Advisory bypass 해제 — 즉시 advisory 체크 재개."""
    advisory_bypass.clear()
    return AdvisoryBypassResponse(
        active=False,
        start=None,
        end=None,
        message="bypass 해제 완료",
    )


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
    pair = normalize_pair(pair)
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
