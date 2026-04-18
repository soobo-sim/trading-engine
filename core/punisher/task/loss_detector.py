"""
패배 포지션 감지 + 정신차리자 DB 직접 기록 (BUG-025 수정).

auto_reporter 15분 주기에서 호출.
- closed + realized_pnl < 0 + loss_webhook_sent = false → wake_up_reviews 생성 + Telegram 알림
- realized_pnl = NULL → 스킵 + 경고 로그 (BUG-008)
- trend 포지션 + box 포지션 양쪽 감지
- DB 기록 실패 시 플래그 false 유지 (다음 주기 재시도)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.models import WakeUpReview

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("AUTO_REPORT_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("AUTO_REPORT_CHAT_ID", "")

# 최근 N시간 이내 closed 포지션만 감지 (너무 오래된 건 스킵)
LOOKBACK_HOURS = 48
# 정신차리자 파이프라인 발동까지 대기 시간 (시간)
PIPELINE_DELAY_HOURS = 24


async def detect_and_notify_losses(
    db: AsyncSession,
    trend_position_model,
    *,
    box_position_model=None,
    prefix: str = "bf",
    http_client: httpx.AsyncClient | None = None,
) -> int:
    """
    손실 포지션 감지 → wake_up_reviews DB 기록 + Telegram 알림.
    Returns: 처리 성공 건수.
    """
    sent_count = 0
    sent_count += await _detect_from_model(
        db, trend_position_model, "trend", prefix, http_client
    )
    if box_position_model is not None:
        sent_count += await _detect_from_model(
            db, box_position_model, "box", prefix, http_client
        )
    return sent_count


async def _detect_from_model(
    db: AsyncSession,
    position_model,
    position_type: str,
    prefix: str,
    http_client: httpx.AsyncClient | None,
) -> int:
    """단일 포지션 테이블에서 손실 감지 + DB 기록."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    stmt = select(position_model).where(
        and_(
            position_model.status == "closed",
            position_model.loss_webhook_sent == False,  # noqa: E712
            position_model.closed_at >= cutoff,
        )
    )
    result = await db.execute(stmt)
    positions = result.scalars().all()

    if not positions:
        return 0

    sent_count = 0
    for pos in positions:
        # realized_pnl NULL → 스킵 + 경고 (BUG-008)
        if pos.realized_pnl_jpy is None:
            logger.warning(
                "Loss detect: %s#%d realized_pnl_jpy=NULL — 스킵 (BUG-008)",
                position_type, pos.id,
            )
            continue

        # 이익 or 무승부 → 스킵, 플래그 true로 (다시 체크 안 하도록)
        if float(pos.realized_pnl_jpy) >= 0:
            pos.loss_webhook_sent = True
            continue

        # 손실 포지션 → DB 기록 + Telegram
        success = await _record_loss(
            db, pos,
            position_type=position_type,
            prefix=prefix,
            http_client=http_client,
        )
        if success:
            pos.loss_webhook_sent = True
            sent_count += 1
            logger.info(
                "Loss recorded: %s#%d pnl=¥%.0f",
                position_type, pos.id, float(pos.realized_pnl_jpy),
            )
        else:
            # 실패 시 false 유지 → 다음 주기 재시도
            logger.warning(
                "Loss record failed: %s#%d — 다음 주기 재시도",
                position_type, pos.id,
            )

    await db.commit()
    return sent_count


async def _record_loss(
    db: AsyncSession,
    pos,
    *,
    position_type: str,
    prefix: str,
    http_client: httpx.AsyncClient | None,
) -> bool:
    """
    wake_up_reviews 행 생성 + Telegram 알림.
    성공 시 True.
    """
    pnl = float(pos.realized_pnl_jpy)
    pair = getattr(pos, "pair", None) or getattr(pos, "currency_pair", "UNKNOWN")
    entry_price = float(pos.entry_price) if pos.entry_price else 0.0
    exit_price = float(pos.exit_price) if pos.exit_price else 0.0
    strategy_id = getattr(pos, "strategy_id", None)
    closed_at = pos.closed_at or datetime.now(timezone.utc)
    scheduled_at = closed_at + timedelta(hours=PIPELINE_DELAY_HOURS)

    try:
        review = WakeUpReview(
            position_id=pos.id,
            strategy_id=strategy_id,
            exchange=prefix,
            position_type=position_type,
            pair=pair,
            entry_price=entry_price,
            exit_price=exit_price,
            realized_pnl=pnl,
            cause_code="ENTRY_TIMING",       # 초기값. 파이프라인이 분석 후 갱신
            review_status="pending_pipeline",
            pipeline_status="pending_pipeline",
            scheduled_at=scheduled_at,
        )
        db.add(review)
        await db.flush()  # id 채번
        review_id = review.id
        logger.info(
            "WakeUpReview 생성: id=%d %s#%d pair=%s pnl=¥%.0f scheduled=%s",
            review_id, position_type, pos.id, pair, pnl, scheduled_at.isoformat(),
        )
    except Exception as e:
        logger.error("WakeUpReview 생성 실패: %s#%d — %s", position_type, pos.id, e)
        return False

    # Telegram 직접 알림 (OpenClaw 무의존)
    await _send_telegram_alert(
        pair=pair,
        pos_id=pos.id,
        pnl=pnl,
        review_id=review_id,
        scheduled_at=scheduled_at,
        http_client=http_client,
    )
    return True


async def _send_telegram_alert(
    *,
    pair: str,
    pos_id: int,
    pnl: float,
    review_id: int,
    scheduled_at: datetime,
    http_client: httpx.AsyncClient | None,
) -> None:
    """Telegram 직접 알림. 실패해도 DB 기록은 유지 (non-blocking)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram 토큰 미설정 — loss alert 스킵")
        return

    scheduled_jst = scheduled_at.astimezone(timezone(timedelta(hours=9)))
    text = (
        f"📉 패배 감지: {pair} pos#{pos_id} ¥{pnl:,.0f}\n"
        f"24시간 후 정신차리자 파이프라인 발동 예정.\n"
        f"review_id: {review_id}\n"
        f"발동 예정: {scheduled_jst.strftime('%m/%d %H:%M')} JST"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}

    owns_client = http_client is None
    try:
        if owns_client:
            http_client = httpx.AsyncClient(timeout=10)
        try:
            resp = await http_client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    "Loss Telegram 전송 실패: HTTP %d — %s",
                    resp.status_code, resp.text[:100],
                )
        finally:
            if owns_client:
                await http_client.aclose()
    except Exception as e:
        logger.warning("Loss Telegram 전송 예외: %s", e)
