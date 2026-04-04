"""
전략 스위칭 추천 텔레그램 알림.

SwitchRecommender의 on_recommendation 콜백으로 주입된다.
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 시 조용히 skip.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from core.task.auto_reporter import send_telegram_message

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

TRIGGER_LABEL: dict[str, str] = {
    "T1_position_close": "포지션 청산 후",
    "T2_candle_close": "4H 봉 확정 후",
}

CONFIDENCE_EMOJI: dict[str, str] = {
    "high": "🟢",
    "medium": "🟡",
    "low": "🔴",
    "none": "⚫",
}


def format_switch_message(rec) -> str:
    """텔레그램 plain text 메시지 생성."""
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    trigger_label = TRIGGER_LABEL.get(rec.trigger_type, rec.trigger_type)
    confidence = rec.confidence or "none"
    conf_emoji = CONFIDENCE_EMOJI.get(confidence, "⚫")

    current_score = float(rec.current_score) if rec.current_score is not None else 0.0
    recommended_score = float(rec.recommended_score) if rec.recommended_score is not None else 0.0
    score_ratio = float(rec.score_ratio) if rec.score_ratio is not None else 0.0

    lines: list[str] = [
        f"🔄 전략 스위칭 추천 ({now_jst} JST)",
        "━━━━━━━━━━━━━━━━━━━",
        f"⏱️ 트리거: {trigger_label}",
        "",
        f"📉 현재 전략 (id={rec.current_strategy_id})",
        f"   Score: {current_score:.3f}",
        "",
        f"📈 추천 전략 (id={rec.recommended_strategy_id})",
        f"   Score: {recommended_score:.3f}",
        f"   비율: {score_ratio:.2f}배 {conf_emoji}",
        "",
    ]

    if rec.reason:
        lines.append(f"💬 {rec.reason}")
        lines.append("")

    lines.extend([
        f"🗂️ 추천 ID: {rec.id}",
        "📌 승인: POST /api/switch-recommendations/{id}/approve",
        "❌ 거부: POST /api/switch-recommendations/{id}/reject",
    ])

    return "\n".join(lines)


async def send_switch_recommendation_telegram(rec) -> None:
    """추천 생성 즉시 Telegram 알림 전송. 실패해도 예외 미전파."""
    try:
        message = format_switch_message(rec)
        await send_telegram_message(message)
        logger.info(f"[SwitchTelegram] 추천 알림 전송 완료 (rec_id={rec.id})")
    except Exception as e:
        logger.warning(f"[SwitchTelegram] 알림 전송 실패 (무시): {e}")
