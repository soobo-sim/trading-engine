"""
전략 분석 보고 텔레그램 알림.

POST /api/strategy-analysis/reports 성공 시 fire-and-forget으로 호출.
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 시 조용히 skip.

기존 send_telegram_message (core/task/auto_reporter.py) 재사용.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta

from core.task.auto_reporter import send_telegram_message

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

REPORT_TYPE_LABEL: dict[str, str] = {
    "daily": "매일",
    "weekly": "주간",
    "monthly": "월간",
}

DECISION_EMOJI: dict[str, str] = {
    "approved": "✅ 승인",
    "conditional": "✅ 조건부 승인",
    "rejected": "❌ 거부",
    "hold": "⏸️ 보류",
}

AGENT_ICON: dict[str, str] = {
    "alice": "👩‍💼 Alice",
    "samantha": "🛡️ Samantha",
    "rachel": "⚖️ Rachel",
}

AGENT_ORDER = ["alice", "samantha", "rachel"]


def format_analysis_report_message(
    *,
    report_type: str,
    currency_pair: str,
    reported_at: str | datetime,
    final_decision: str | None,
    strategy_active: bool,
    analyses: list[dict],
) -> str:
    """텔레그램 plain text 메시지 생성."""
    # 시각 포맷
    if isinstance(reported_at, str):
        try:
            dt = datetime.fromisoformat(reported_at.replace("Z", "+00:00"))
        except ValueError:
            dt = None
    else:
        dt = reported_at

    if dt:
        dt_jst = dt.astimezone(JST)
        time_str = dt_jst.strftime("%Y-%m-%d %H:%M")
    else:
        time_str = str(reported_at)

    type_label = REPORT_TYPE_LABEL.get(report_type, report_type)
    display_pair = currency_pair.replace("_", "/")

    lines: list[str] = [
        f"📊 GMO FX {type_label} 분석 보고 ({time_str})",
        "━━━━━━━━━━━━━━━━━━━",
        f"🪙 {display_pair}",
        "",
    ]

    # 에이전트 분석 (순서 고정)
    analysis_map = {a["agent_name"]: a for a in analyses}
    for name in AGENT_ORDER:
        a = analysis_map.get(name)
        icon = AGENT_ICON.get(name, f"🤖 {name}")
        if a and a.get("summary"):
            lines.append(f"{icon}: {a['summary']}")

    lines.append("")

    # 결정
    if final_decision:
        decision_str = DECISION_EMOJI.get(final_decision, f"📋 {final_decision}")
        lines.append(f"📋 결정: {decision_str}")

    # 전략 상태
    lines.append("🟢 전략 운영 중" if strategy_active else "⚪ 전략 미운영")

    return "\n".join(lines)


async def send_analysis_report_telegram(
    *,
    report_type: str,
    currency_pair: str,
    reported_at: str | datetime,
    final_decision: str | None,
    strategy_active: bool,
    analyses: list[dict],
) -> bool:
    """
    포맷 후 Telegram 전송.
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 시 skip → True 반환.
    전송 실패 시 False 반환 (호출자는 fire-and-forget이므로 무시해도 됨).
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        logger.debug("TELEGRAM_BOT_TOKEN/CHAT_ID 미설정 — 분석 보고 전송 skip")
        return True

    text = format_analysis_report_message(
        report_type=report_type,
        currency_pair=currency_pair,
        reported_at=reported_at,
        final_decision=final_decision,
        strategy_active=strategy_active,
        analyses=analyses,
    )

    try:
        ok = await send_telegram_message(bot_token=bot_token, chat_id=chat_id, text=text)
        if ok:
            logger.info(f"[AnalysisReport] Telegram 전송 완료: {currency_pair} {report_type}")
        else:
            logger.warning(f"[AnalysisReport] Telegram 전송 실패: {currency_pair} {report_type}")
        return ok
    except Exception as e:
        logger.error(f"[AnalysisReport] Telegram 전송 예외: {e}")
        return False
