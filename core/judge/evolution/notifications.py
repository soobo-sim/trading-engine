"""
core.judge.evolution.notifications — 진화 채널 알림 헬퍼.

TELEGRAM_EVOLUTION_CHAT_ID 환경변수를 읽어 발송. 없으면 로그만.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("core.judge.evolution.notifications")


async def notify_evolution(message: str) -> bool:
    """진화 채널로 메시지 발송. 성공 시 True, 실패/미설정 시 False."""
    chat_id = os.getenv("TELEGRAM_EVOLUTION_CHAT_ID", "")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not chat_id or not bot_token:
        logger.info("[Evolution Notify — channel not configured] %s", message[:80])
        return False
    try:
        from core.shared.logging.telegram_handlers import _send_telegram
        return await _send_telegram(bot_token, chat_id, message)
    except Exception as exc:
        logger.warning("[Evolution Notify failed] %s: %s", type(exc).__name__, exc)
        return False
