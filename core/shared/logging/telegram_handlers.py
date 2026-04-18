"""
Telegram 로그 핸들러 — 도메인별 채널 분리 전송.

- TelegramDomainHandler : logger prefix 기반으로 판단/실행 도메인 채널에 INFO+ 전송
  - 판단 도메인 (TELEGRAM_HEARTBEAT_CHAT_ID): 시그널 변경, 판단 결과, advisory, Guardrail
  - 실행 도메인 (TELEGRAM_SAVEUS_CHAT_ID): 주문 실행, 포지션, SL, 잔고, 어댑터
  - WARNING+: 양쪽 채널에 모두 전송 (이중 안전)
- TelegramAlertHandler  : WARNING+ → 실행 도메인 그룹 즉시 (5초 디바운스, 레거시 호환)

JUDGE_PREFIXES / PUNISHER_PREFIXES 로 라우팅 규칙 관리.

사용:
    setup_telegram_logging() 을 lifespan 내에서 호출.
    shutdown_telegram_logging() 을 shutdown 시 호출.

Canonical location: core/shared/logging/telegram_handlers.py
Backward-compat shim at: core/logging/telegram_handlers.py
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

# ── 도메인 라우팅 규칙 ──────────────────────────────
# logger.name이 아래 prefix로 시작하면 판단 도메인 채널 전송
JUDGE_PREFIXES: frozenset[str] = frozenset({
    # canonical 경로 (core.judge.*) — Phase E 이후 정식 경로
    "core.judge",
    # 레거시 경로 (shim 유지 기간 동안 하위호환)
    "core.data",
    "core.decision",
    "core.safety",
    "core.analysis",
    "core.strategy.signals",
    "core.strategy.box_signals",
    "core.strategy.scoring",
    "core.execution.orchestrator",
    "core.execution.approval",
})

# logger.name이 아래 prefix로 시작하면 실행 도메인 채널 전송
PUNISHER_PREFIXES: frozenset[str] = frozenset({
    # canonical 경로 (core.punisher.*) — Phase E 이후 정식 경로
    "core.punisher",
    # 레거시 경로 (shim 유지 기간 동안 하위호환)
    "core.strategy.base_trend",
    "core.strategy.plugins",
    "core.strategy.registry",
    "core.strategy.snapshot_collector",
    "core.strategy.switch_recommender",
    "core.execution.regime_gate",
    "core.execution.executor",
    "core.task",
    "core.learning",
    "core.notifications",
    "adapters",
    "api",
    "main",
})


def _get_domain(logger_name: str) -> str:
    """logger name → 'judge' | 'punisher' | 'shared'."""
    for prefix in JUDGE_PREFIXES:
        if logger_name == prefix or logger_name.startswith(prefix + "."):
            return "judge"
    for prefix in PUNISHER_PREFIXES:
        if logger_name == prefix or logger_name.startswith(prefix + "."):
            return "punisher"
    return "shared"  # 미분류 → 실행 도메인으로 fallback


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
    """INFO 레벨만 버퍼링 → 배치로 도메인 채널 전송.

    domain 파라미터:
        None  — 모든 INFO 수집 (레거시 동작)
        'judge'    — JUDGE_PREFIXES에 속하는 logger만 수집
        'punisher' — PUNISHER_PREFIXES 또는 미분류 logger만 수집
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        exchange: str = "??",
        interval_sec: int = 300,
        domain: str | None = None,
    ):
        super().__init__(level=logging.INFO)
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._exchange = exchange.upper()
        self._interval = interval_sec
        self._domain = domain  # 'judge' | 'punisher' | None
        self._buffer: list[tuple[float, str]] = []  # (created, message)
        self._task: asyncio.Task | None = None

    def emit(self, record: logging.LogRecord) -> None:
        # INFO만 수집 (DEBUG 제외, WARNING 이상 제외)
        if record.levelno != logging.INFO:
            return
        # 도메인 필터
        if self._domain is not None:
            record_domain = _get_domain(record.name)
            if self._domain == "judge" and record_domain != "judge":
                return
            if self._domain == "punisher" and record_domain == "judge":
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


# ── WARNING+ 즉시 알림 (실행 도메인 채널) ───────────────────

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
        TELEGRAM_HEARTBEAT_CHAT_ID — 판단 도메인 채널 (Judge INFO+, WARNING+ 이중 전송)
        TELEGRAM_SAVEUS_CHAT_ID    — 실행 도메인 채널 (Punisher INFO+, WARNING+ 즉시)
        LOG_DIGEST_INTERVAL_SEC    — 다이제스트 주기 (기본 300초)

    라우팅:
        - logger prefix → JUDGE_PREFIXES  : 판단 도메인 채널
        - logger prefix → PUNISHER_PREFIXES : 실행 도메인 채널
        - WARNING+ : 양쪽 채널에 모두 전송 (이중 안전)
        - 미분류(shared/api) : 실행 도메인 채널로 fallback
    """
    import os

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return

    root = logging.getLogger()
    loop = asyncio.get_running_loop()

    existing_types = {type(h) for h in _handlers}

    judge_chat = os.environ.get("TELEGRAM_HEARTBEAT_CHAT_ID", "")
    punisher_chat = os.environ.get("TELEGRAM_SAVEUS_CHAT_ID", "")
    digest_interval = int(os.environ.get("LOG_DIGEST_INTERVAL_SEC", "300"))

    # 판단 도메인 핸들러 (Judge 채널, JUDGE logger만 수집)
    if judge_chat:
        judge_registered = any(
            isinstance(h, TelegramDigestHandler) and getattr(h, "_domain", None) == "judge"
            for h in _handlers
        )
        if not judge_registered:
            h_judge = TelegramDigestHandler(
                bot_token, judge_chat,
                exchange=exchange, interval_sec=digest_interval, domain="judge",
            )
            root.addHandler(h_judge)
            await h_judge.start()
            _handlers.append(h_judge)

    # 실행 도메인 핸들러 (Punisher 채널, Punisher/shared logger 수집)
    if punisher_chat:
        punisher_registered = any(
            isinstance(h, TelegramDigestHandler) and getattr(h, "_domain", None) == "punisher"
            for h in _handlers
        )
        if not punisher_registered:
            h_punisher = TelegramDigestHandler(
                bot_token, punisher_chat,
                exchange=exchange, interval_sec=digest_interval, domain="punisher",
            )
            root.addHandler(h_punisher)
            await h_punisher.start()
            _handlers.append(h_punisher)

    # WARNING+ 즉시 알림 — 실행 도메인 채널 (기존 TelegramAlertHandler 재사용)
    if punisher_chat and TelegramAlertHandler not in existing_types:
        h_alert = TelegramAlertHandler(
            bot_token, punisher_chat, exchange=exchange,
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
