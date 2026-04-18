"""
Wake-Up 파이프라인 트리거 (BUG-025).

auto_reporter 15분 주기에서 호출.
scheduled_at이 경과한 pending_pipeline 리뷰를 감지 →
레이첼 main 세션으로 webhook 발사 → pipeline_status = triggered.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.models import WakeUpReview

logger = logging.getLogger(__name__)

RACHEL_WEBHOOK_URL = os.getenv(
    "RACHEL_WEBHOOK_URL", "http://localhost:18793/hooks/market-alert"
)
RACHEL_WEBHOOK_TOKEN = os.getenv("RACHEL_WEBHOOK_TOKEN", "")


async def trigger_pending_reviews(
    db: AsyncSession,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> int:
    """
    scheduled_at 경과한 pending_pipeline 리뷰 → 레이첼 main 세션 webhook 발사.
    Returns: 발사 성공 건수.
    """
    now = datetime.now(timezone.utc)

    stmt = select(WakeUpReview).where(
        and_(
            WakeUpReview.pipeline_status == "pending_pipeline",
            WakeUpReview.scheduled_at <= now,
        )
    )
    result = await db.execute(stmt)
    reviews = result.scalars().all()

    if not reviews:
        return 0

    triggered = 0
    for review in reviews:
        success = await _send_pipeline_webhook(review, http_client=http_client)
        if success:
            review.pipeline_status = "triggered"
            review.pipeline_started_at = now
            triggered += 1
            logger.info(
                "Wake-up pipeline triggered: review#%d %s pos#%s",
                review.id, review.pair, review.position_id,
            )
        else:
            # 실패: 상태 유지 → 다음 주기 재시도
            logger.warning(
                "Wake-up trigger failed: review#%d — 다음 주기 재시도", review.id
            )

    await db.commit()
    return triggered


async def _send_pipeline_webhook(
    review: WakeUpReview,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> bool:
    """레이첼 main 세션으로 pipeline 발동 webhook 전송. 성공 시 True."""
    if not RACHEL_WEBHOOK_TOKEN:
        logger.warning("RACHEL_WEBHOOK_TOKEN 미설정 — pipeline trigger 스킵")
        return False

    pnl = float(review.realized_pnl) if review.realized_pnl is not None else 0.0
    message = (
        f"정신차리자 파이프라인 발동. "
        f"position_id={review.position_id}, pair={review.pair}, "
        f"realized_pnl=¥{pnl:,.0f}, "
        f"review_id={review.id}"
    )

    payload = {
        "message": message,
        "name": "PositionLoss",
        "deliver": True,
        "channel": "telegram",
        "timeoutSeconds": 60,
        "metadata": {
            "type": "wake_up_pipeline_trigger",
            "review_id": review.id,
            "position_id": review.position_id,
            "position_type": review.position_type,
            "exchange": review.exchange,
            "pair": review.pair,
            "realized_pnl": pnl,
        },
    }

    owns_client = http_client is None
    try:
        if owns_client:
            http_client = httpx.AsyncClient(timeout=10)
        try:
            resp = await http_client.post(
                RACHEL_WEBHOOK_URL,
                headers={
                    "Authorization": f"Bearer {RACHEL_WEBHOOK_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code == 200:
                return True
            logger.error(
                "Pipeline webhook HTTP %d: %s", resp.status_code, resp.text[:200]
            )
            return False
        finally:
            if owns_client:
                await http_client.aclose()
    except Exception as e:
        logger.error("Pipeline webhook error: %s", e)
        return False
