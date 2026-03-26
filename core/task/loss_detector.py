"""
패배 포지션 감지 + 정신차리자 webhook (WAKE_UP_REVIEW_AUTO).

auto_reporter 15분 주기에서 호출.
- closed + realized_pnl < 0 + loss_webhook_sent = false → webhook
- realized_pnl = NULL → 스킵 + 경고 로그
- webhook 실패 시 플래그 false 유지 (다음 주기 재시도)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

RACHEL_WEBHOOK_URL = os.getenv(
    "RACHEL_WEBHOOK_URL", "http://localhost:18793/hooks/market-alert"
)
RACHEL_WEBHOOK_TOKEN = os.getenv("RACHEL_WEBHOOK_TOKEN", "")

# 최근 N시간 이내 closed 포지션만 감지 (너무 오래된 건 스킵)
LOOKBACK_HOURS = 48


async def detect_and_notify_losses(
    db: AsyncSession,
    trend_position_model,
    *,
    prefix: str = "bf",
    http_client: httpx.AsyncClient | None = None,
) -> int:
    """
    손실 포지션 감지 → webhook 전송 → 플래그 업데이트.
    Returns: webhook 전송 성공 건수.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    # 손실 + 미전송 포지션 조회
    stmt = select(trend_position_model).where(
        and_(
            trend_position_model.status == "closed",
            trend_position_model.loss_webhook_sent == False,  # noqa: E712
            trend_position_model.closed_at >= cutoff,
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
                "Loss detect: pos#%d realized_pnl_jpy=NULL — 스킵 (BUG-008)",
                pos.id,
            )
            continue

        # 이익 or 무승부 → 스킵 (손실만 대상)
        if float(pos.realized_pnl_jpy) >= 0:
            # 이익/무승부도 플래그 true로 (다시 체크 안 하도록)
            pos.loss_webhook_sent = True
            continue

        # 손실 포지션 → webhook 전송
        success = await _send_loss_webhook(pos, prefix=prefix, client=http_client)
        if success:
            pos.loss_webhook_sent = True
            sent_count += 1
            logger.info("Loss webhook sent: pos#%d pnl=¥%.0f", pos.id, float(pos.realized_pnl_jpy))
        else:
            # 실패 시 false 유지 → 다음 주기 재시도
            logger.warning("Loss webhook failed: pos#%d — 다음 주기 재시도", pos.id)

    await db.commit()
    return sent_count


async def _send_loss_webhook(
    pos,
    *,
    prefix: str = "bf",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Rachel webhook 전송. 성공 시 True."""
    if not RACHEL_WEBHOOK_TOKEN:
        logger.warning("RACHEL_WEBHOOK_TOKEN 미설정 — loss webhook 스킵")
        return False

    closed_at_str = pos.closed_at.isoformat() if pos.closed_at else None
    review_at = None
    if pos.closed_at:
        review_at = (pos.closed_at + timedelta(hours=24)).isoformat()

    pnl = float(pos.realized_pnl_jpy)
    pair = pos.pair
    entry_price = float(pos.entry_price) if pos.entry_price else None
    exit_price = float(pos.exit_price) if pos.exit_price else None

    message = (
        f"패배 감지: {pair} pos#{pos.id} ¥{pnl:,.0f}. "
        f"24h 후 정신차리자 파이프라인 발동."
    )

    payload = {
        "message": message,
        "name": "PositionLoss",
        "deliver": True,
        "channel": "telegram",
        "timeoutSeconds": 60,
        "metadata": {
            "type": "position_closed_loss",
            "prefix": prefix,
            "position_id": pos.id,
            "pair": pair,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "realized_pnl": pnl,
            "exit_reason": pos.exit_reason,
            "closed_at": closed_at_str,
            "review_at": review_at,
        },
    }

    owns_client = client is None
    try:
        if owns_client:
            client = httpx.AsyncClient(timeout=10)
        try:
            resp = await client.post(
                RACHEL_WEBHOOK_URL,
                headers={
                    "Authorization": f"Bearer {RACHEL_WEBHOOK_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code == 200:
                return True
            logger.error("Loss webhook HTTP %d: %s", resp.status_code, resp.text[:200])
            return False
        finally:
            if owns_client:
                await client.aclose()
    except Exception as e:
        logger.error("Loss webhook error: %s", e)
        return False
