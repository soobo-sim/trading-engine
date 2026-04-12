"""
Telegram 로그 핸들러 — 로그 레벨별 채널 분리 전송.

- TelegramDigestHandler : INFO만 → HeartBeat 채널 (5분 배치)
- TelegramAlertHandler  : WARNING+ → Save Us 그룹 (즉시, 5초 디바운스)

사용:
    setup_telegram_logging() 을 lifespan 내에서 호출.
    shutdown_telegram_logging() 을 shutdown 시 호출.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_MSG_MAX = 4096
JST = timezone(timedelta(hours=9))


# ── 유틸 ─────────────────────────────────────────────

async def _send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    """Telegram Bot API 전송. 실패 시 False (예외 삼킴)."""
    if not bot_token or not chat_id:
        return False
    url = TELEGRAM_API.format(token=bot_token)
    # 메시지 길이 제한
    if len(text) > TELEGRAM_MSG_MAX:
        text = text[:TELEGRAM_MSG_MAX - 20] + "\n… (truncated)"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": text})
            return resp.status_code == 200
    except Exception:
        # 전송 실패를 로깅하면 무한 루프 가능 → 조용히 삼킴
        return False


def _format_time(ts: float) -> str:
    """epoch → HH:MM:SS JST."""
    return datetime.fromtimestamp(ts, tz=JST).strftime("%H:%M:%S")


# ── INFO 다이제스트 (HeartBeat) ──────────────────────

class TelegramDigestHandler(logging.Handler):
    """INFO 레벨만 버퍼링 → 5분 배치로 HeartBeat 채널 전송."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        exchange: str = "??",
        interval_sec: int = 300,
    ):
        super().__init__(level=logging.INFO)
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._exchange = exchange.upper()
        self._interval = interval_sec
        self._buffer: list[tuple[float, str]] = []  # (created, message)
        self._task: asyncio.Task | None = None

    def emit(self, record: logging.LogRecord) -> None:
        # INFO만 수집 (DEBUG 제외, WARNING 이상 제외)
        if record.levelno != logging.INFO:
            return
        self._buffer.append((record.created, record.getMessage()))

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._flush_loop(), name="log_digest")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # 남은 버퍼 최종 전송
        if self._buffer:
            await self._flush()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            if self._buffer:
                await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            return
        items = self._buffer[:100]
        self._buffer = self._buffer[100:]
        lines = [f"{_format_time(ts)} {msg}" for ts, msg in items]
        text = (
            f"📋 [{self._exchange}] 활동 로그 ({self._interval // 60}분, {len(items)}건)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(lines)
        )
        if len(self._buffer) > 0:
            text += f"\n… 외 {len(self._buffer)}건 다음 배치"
        await _send_telegram(self._bot_token, self._chat_id, text)


# ── WARNING+ 즉시 알림 (Save Us) ───────────────────

class TelegramAlertHandler(logging.Handler):
    """WARNING 이상 → Save Us 그룹 즉시 전송 (5초 디바운스)."""

    LEVEL_EMOJI = {
        logging.WARNING: "⚠️",
        logging.ERROR: "🚨",
        logging.CRITICAL: "🔴",
    }

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        exchange: str = "??",
        debounce_sec: float = 5.0,
    ):
        super().__init__(level=logging.WARNING)
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._exchange = exchange.upper()
        self._debounce = debounce_sec
        self._last_sent: dict[str, float] = {}  # key → last send time
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        # 디바운스: 동일 logger+level 조합 5초 이내 중복 스킵
        key = f"{record.name}:{record.levelno}"
        now = time.time()
        if now - self._last_sent.get(key, 0) < self._debounce:
            return
        self._last_sent[key] = now

        emoji = self.LEVEL_EMOJI.get(record.levelno, "⚠️")
        text = (
            f"{emoji} [{record.levelname}] [{self._exchange}]\n"
            f"{record.getMessage()}"
        )
        if record.exc_info and record.exc_info[0] is not None:
            text += f"\n{self.format(record)}"

        if self._loop and self._loop.is_running():
            self._loop.create_task(
                _send_telegram(self._bot_token, self._chat_id, text)
            )


# ── 세팅 헬퍼 ───────────────────────────────────────

_handlers: list[TelegramDigestHandler | TelegramAlertHandler] = []


async def setup_telegram_logging(exchange: str) -> None:
    """Telegram 핸들러를 루트 로거에 등록 + 비동기 태스크 시작.

    환경변수:
        TELEGRAM_BOT_TOKEN         — 공유 봇 토큰 (필수)
        TELEGRAM_HEARTBEAT_CHAT_ID — HeartBeat 채널 (INFO 다이제스트, 5분 배치)
        TELEGRAM_SAVEUS_CHAT_ID    — Save Us 그룹 (WARNING+ 즉시)
        LOG_DIGEST_INTERVAL_SEC    — HeartBeat 다이제스트 주기 (기본 300)
    """
    import os

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return

    root = logging.getLogger()
    loop = asyncio.get_running_loop()

    # 이미 등록된 핸들러 타입 집합 (중복 등록 방어)
    existing_types = {type(h) for h in _handlers}

    # HeartBeat 채널 (INFO 다이제스트)
    heartbeat_chat = os.environ.get("TELEGRAM_HEARTBEAT_CHAT_ID", "")
    if heartbeat_chat and TelegramDigestHandler not in existing_types:
        digest_interval = int(os.environ.get("LOG_DIGEST_INTERVAL_SEC", "300"))
        h = TelegramDigestHandler(
            bot_token, heartbeat_chat,
            exchange=exchange, interval_sec=digest_interval,
        )
        root.addHandler(h)
        await h.start()
        _handlers.append(h)

    # Save Us 그룹 (WARNING+ 즉시)
    saveus_chat = os.environ.get("TELEGRAM_SAVEUS_CHAT_ID", "")
    if saveus_chat:
        if TelegramAlertHandler not in existing_types:
            h_alert = TelegramAlertHandler(
                bot_token, saveus_chat, exchange=exchange,
            )
            h_alert.set_loop(loop)
            root.addHandler(h_alert)
            _handlers.append(h_alert)


async def shutdown_telegram_logging() -> None:
    """비동기 태스크 정리 + 잔여 버퍼 전송."""
    for h in _handlers:
        if hasattr(h, "stop"):
            await h.stop()
    _handlers.clear()
