"""
core/logging/telegram_handlers.py 단위 테스트.

검증 항목:
- _send_telegram: 토큰/chat_id 미설정, 성공, 실패, 길이 초과
- TelegramDigestHandler: DEBUG만 버퍼링, INFO 이상 무시, flush 포맷, 버퍼 초과
- TelegramInfoHandler: INFO만 버퍼링, WARNING 이상 무시, flush 포맷
- TelegramAlertHandler: WARNING+ 즉시 전송, 디바운스, 루프 없으면 스킵
- setup_telegram_logging: 토큰 없으면 스킵, 채널별 핸들러 등록, 환경변수 커스텀 주기
- shutdown_telegram_logging: 잔여 버퍼 전송, 태스크 취소
"""
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.logging.telegram_handlers import (
    TelegramAlertHandler,
    TelegramDigestHandler,
    TelegramInfoHandler,
    _send_telegram,
    setup_telegram_logging,
    shutdown_telegram_logging,
    _handlers,
)


# ─── _send_telegram ──────────────────────────────────────────

class TestSendTelegram:

    @pytest.mark.asyncio
    async def test_empty_token_returns_false(self):
        result = await _send_telegram("", "12345", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_chat_id_returns_false(self):
        result = await _send_telegram("token", "", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_success_returns_true(self):
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("core.logging.telegram_handlers.httpx.AsyncClient", return_value=mock_client):
            result = await _send_telegram("token", "123", "hello")

        assert result is True

    @pytest.mark.asyncio
    async def test_failure_returns_false(self):
        mock_resp = MagicMock(status_code=400)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("core.logging.telegram_handlers.httpx.AsyncClient", return_value=mock_client):
            result = await _send_telegram("token", "123", "hello")

        assert result is False

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        """예외 발생 시 False (삼킴)."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("network error"))

        with patch("core.logging.telegram_handlers.httpx.AsyncClient", return_value=mock_client):
            result = await _send_telegram("token", "123", "hello")

        assert result is False

    @pytest.mark.asyncio
    async def test_long_message_truncated(self):
        """4096자 초과 시 자동 truncate."""
        long_text = "x" * 5000
        sent_text = None

        async def fake_post(url, **kwargs):
            nonlocal sent_text
            sent_text = kwargs["json"]["text"]
            return MagicMock(status_code=200)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=fake_post)

        with patch("core.logging.telegram_handlers.httpx.AsyncClient", return_value=mock_client):
            await _send_telegram("token", "123", long_text)

        assert sent_text is not None
        assert len(sent_text) <= 4096
        assert "truncated" in sent_text


# ─── TelegramDigestHandler ───────────────────────────────────

class TestTelegramDigestHandler:

    def _make_record(self, level: int, msg: str) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test.logger", level=level, pathname="", lineno=0,
            msg=msg, args=(), exc_info=None,
        )
        return record

    def test_debug_only_buffered(self):
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=300)
        h.emit(self._make_record(logging.DEBUG, "debug msg"))
        assert len(h._buffer) == 1

    def test_info_ignored(self):
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=300)
        h.emit(self._make_record(logging.INFO, "info msg"))
        assert len(h._buffer) == 0

    def test_warning_ignored(self):
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=300)
        h.emit(self._make_record(logging.WARNING, "warn msg"))
        assert len(h._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_sends_buffered_messages(self):
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=5)
        h.emit(self._make_record(logging.DEBUG, "msg1"))
        h.emit(self._make_record(logging.DEBUG, "msg2"))

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            await h._flush()

        assert len(sent_texts) == 1
        assert "BF" in sent_texts[0]
        assert "msg1" in sent_texts[0]
        assert "msg2" in sent_texts[0]

    @pytest.mark.asyncio
    async def test_flush_clears_buffer(self):
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=5)
        h.emit(self._make_record(logging.DEBUG, "msg"))

        with patch("core.logging.telegram_handlers._send_telegram", new_callable=AsyncMock, return_value=True):
            await h._flush()

        assert len(h._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_over_100_items_batches(self):
        """100건 초과 시 나머지는 다음 배치 표시."""
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=5)
        for i in range(120):
            h.emit(self._make_record(logging.DEBUG, f"msg{i}"))

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            await h._flush()

        assert "외 20건" in sent_texts[0]
        assert len(h._buffer) == 20  # 나머지 보존

    @pytest.mark.asyncio
    async def test_stop_flushes_remaining(self):
        """stop() 시 잔여 버퍼 전송."""
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=300)
        h.emit(self._make_record(logging.DEBUG, "leftover"))

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            await h.stop()

        assert len(sent_texts) == 1
        assert "leftover" in sent_texts[0]

    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=300)
        await h.start()
        assert h._task is not None
        assert not h._task.done()
        h._task.cancel()
        try:
            await h._task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        """이미 실행 중이면 두 번째 start는 새 태스크 만들지 않음."""
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=300)
        await h.start()
        task1 = h._task
        await h.start()
        assert h._task is task1
        task1.cancel()
        try:
            await task1
        except asyncio.CancelledError:
            pass


# ─── TelegramInfoHandler ─────────────────────────────────────

class TestTelegramInfoHandler:

    def _make_record(self, level: int, msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="test.logger", level=level, pathname="", lineno=0,
            msg=msg, args=(), exc_info=None,
        )

    def test_info_only_buffered(self):
        h = TelegramInfoHandler("tok", "chat", exchange="GMO", interval_sec=120)
        h.emit(self._make_record(logging.INFO, "info msg"))
        assert len(h._buffer) == 1

    def test_debug_ignored(self):
        h = TelegramInfoHandler("tok", "chat", exchange="GMO", interval_sec=120)
        h.emit(self._make_record(logging.DEBUG, "debug msg"))
        assert len(h._buffer) == 0

    def test_warning_ignored(self):
        h = TelegramInfoHandler("tok", "chat", exchange="GMO", interval_sec=120)
        h.emit(self._make_record(logging.WARNING, "warn msg"))
        assert len(h._buffer) == 0

    def test_error_ignored(self):
        h = TelegramInfoHandler("tok", "chat", exchange="GMO", interval_sec=120)
        h.emit(self._make_record(logging.ERROR, "error msg"))
        assert len(h._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_format_contains_exchange(self):
        h = TelegramInfoHandler("tok", "chat", exchange="GMO", interval_sec=120)
        h.emit(self._make_record(logging.INFO, "진입 완료"))

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            await h._flush()

        assert "GMO" in sent_texts[0]
        assert "진입 완료" in sent_texts[0]
        assert "📌" in sent_texts[0]

    @pytest.mark.asyncio
    async def test_flush_over_50_items_batches(self):
        h = TelegramInfoHandler("tok", "chat", exchange="GMO", interval_sec=120)
        for i in range(60):
            h.emit(self._make_record(logging.INFO, f"evt{i}"))

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            await h._flush()

        assert "외 10건" in sent_texts[0]

    @pytest.mark.asyncio
    async def test_stop_flushes_remaining(self):
        h = TelegramInfoHandler("tok", "chat", exchange="GMO", interval_sec=300)
        h.emit(self._make_record(logging.INFO, "state_change"))

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            await h.stop()

        assert len(sent_texts) == 1
        assert "state_change" in sent_texts[0]


# ─── TelegramAlertHandler ────────────────────────────────────

class TestTelegramAlertHandler:

    def _make_record(self, level: int, msg: str, name: str = "test.logger") -> logging.LogRecord:
        return logging.LogRecord(
            name=name, level=level, pathname="", lineno=0,
            msg=msg, args=(), exc_info=None,
        )

    @pytest.mark.asyncio
    async def test_warning_sends_immediately(self):
        loop = asyncio.get_running_loop()
        h = TelegramAlertHandler("tok", "chat", exchange="BF", debounce_sec=0)
        h.set_loop(loop)

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            h.emit(self._make_record(logging.WARNING, "잔고 불일치"))
            await asyncio.sleep(0)  # task 실행 기회 부여

        assert len(sent_texts) == 1
        assert "⚠️" in sent_texts[0]
        assert "BF" in sent_texts[0]
        assert "잔고 불일치" in sent_texts[0]

    @pytest.mark.asyncio
    async def test_error_uses_fire_emoji(self):
        loop = asyncio.get_running_loop()
        h = TelegramAlertHandler("tok", "chat", exchange="BF", debounce_sec=0)
        h.set_loop(loop)

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            h.emit(self._make_record(logging.ERROR, "주문 실패"))
            await asyncio.sleep(0)

        assert "🚨" in sent_texts[0]

    @pytest.mark.asyncio
    async def test_critical_uses_red_circle(self):
        loop = asyncio.get_running_loop()
        h = TelegramAlertHandler("tok", "chat", exchange="BF", debounce_sec=0)
        h.set_loop(loop)

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            h.emit(self._make_record(logging.CRITICAL, "치명적 오류"))
            await asyncio.sleep(0)

        assert "🔴" in sent_texts[0]

    @pytest.mark.asyncio
    async def test_debounce_skips_duplicate(self):
        """5초 이내 동일 logger+level은 스킵."""
        loop = asyncio.get_running_loop()
        h = TelegramAlertHandler("tok", "chat", exchange="BF", debounce_sec=100)
        h.set_loop(loop)

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            h.emit(self._make_record(logging.WARNING, "first"))
            h.emit(self._make_record(logging.WARNING, "second"))  # 디바운스로 스킵
            await asyncio.sleep(0)

        assert len(sent_texts) == 1  # 두 번째는 스킵

    @pytest.mark.asyncio
    async def test_different_logger_not_debounced(self):
        """다른 logger는 별도 키 → 디바운스 미적용."""
        loop = asyncio.get_running_loop()
        h = TelegramAlertHandler("tok", "chat", exchange="BF", debounce_sec=100)
        h.set_loop(loop)

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            h.emit(self._make_record(logging.WARNING, "a", name="logger.a"))
            h.emit(self._make_record(logging.WARNING, "b", name="logger.b"))
            await asyncio.sleep(0)

        assert len(sent_texts) == 2

    def test_no_loop_does_not_crash(self):
        """이벤트 루프 없어도 emit이 크래시 없이 스킵."""
        h = TelegramAlertHandler("tok", "chat", exchange="BF", debounce_sec=0)
        # set_loop 미호출 → _loop=None
        record = self._make_record(logging.WARNING, "warn")
        h.emit(record)  # 예외 없어야 함


# ─── setup_telegram_logging / shutdown_telegram_logging ──────

class TestSetupTelegramLogging:

    @pytest.mark.asyncio
    async def test_no_token_skips_all(self):
        """BOT_TOKEN 없으면 핸들러 등록 없음."""
        _handlers.clear()
        with patch.dict("os.environ", {}, clear=True):
            await setup_telegram_logging("bitflyer")
        assert len(_handlers) == 0

    @pytest.mark.asyncio
    async def test_heartbeat_channel_registered(self):
        """HEARTBEAT_CHAT_ID 설정 시 DigestHandler 등록."""
        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_HEARTBEAT_CHAT_ID": "hb123",
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("bitflyer")

        try:
            assert len(_handlers) == 1
            assert isinstance(_handlers[0], TelegramDigestHandler)
        finally:
            await shutdown_telegram_logging()

    @pytest.mark.asyncio
    async def test_saveus_info_handler_registered(self):
        """SAVEUS_CHAT_ID 설정 시 InfoHandler + AlertHandler 2개 등록."""
        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_SAVEUS_CHAT_ID": "su123",
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("bitflyer")

        try:
            assert len(_handlers) == 2
            types = {type(h) for h in _handlers}
            assert TelegramInfoHandler in types
            assert TelegramAlertHandler in types
        finally:
            await shutdown_telegram_logging()

    @pytest.mark.asyncio
    async def test_saveus_alert_handler_registered(self):
        """SAVEUS_CHAT_ID 설정 시 AlertHandler 포함 확인."""
        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_SAVEUS_CHAT_ID": "saveus123",
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("bitflyer")

        try:
            types = {type(h) for h in _handlers}
            assert TelegramAlertHandler in types
        finally:
            await shutdown_telegram_logging()

    @pytest.mark.asyncio
    async def test_all_channels(self):
        """HeartBeat + SaveUs 모두 설정 시 핸들러 3개(Digest+Info+Alert) 등록."""
        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_HEARTBEAT_CHAT_ID": "hb",
            "TELEGRAM_SAVEUS_CHAT_ID": "su",
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("gmofx")

        try:
            assert len(_handlers) == 3
            types = {type(h) for h in _handlers}
            assert TelegramDigestHandler in types
            assert TelegramInfoHandler in types
            assert TelegramAlertHandler in types
        finally:
            await shutdown_telegram_logging()

    @pytest.mark.asyncio
    async def test_custom_interval_env(self):
        """LOG_DIGEST_INTERVAL_SEC 환경변수 반영."""
        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_HEARTBEAT_CHAT_ID": "hb",
            "LOG_DIGEST_INTERVAL_SEC": "60",
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("bitflyer")

        try:
            h = _handlers[0]
            assert isinstance(h, TelegramDigestHandler)
            assert h._interval == 60
        finally:
            await shutdown_telegram_logging()

    @pytest.mark.asyncio
    async def test_shutdown_flushes_buffers(self):
        """shutdown 시 잔여 버퍼 전송."""
        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_HEARTBEAT_CHAT_ID": "hb",
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("bitflyer")

        h = _handlers[0]
        record = logging.LogRecord(
            name="t", level=logging.DEBUG, pathname="", lineno=0,
            msg="残 메시지", args=(), exc_info=None,
        )
        h.emit(record)

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            await shutdown_telegram_logging()

        assert len(sent_texts) == 1
        assert "残 메시지" in sent_texts[0]
        assert len(_handlers) == 0

    @pytest.mark.asyncio
    async def test_setup_twice_no_duplicate_handlers(self):
        """setup_telegram_logging 두 번 호출해도 핸들러 중복 등록 안 됨."""
        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_HEARTBEAT_CHAT_ID": "hb",
            "TELEGRAM_SAVEUS_CHAT_ID": "su",
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("bitflyer")
            await setup_telegram_logging("bitflyer")  # 두 번째 호출

        try:
            assert len(_handlers) == 3  # 6이 되면 안 됨
        finally:
            await shutdown_telegram_logging()

    @pytest.mark.asyncio
    async def test_info_custom_interval_env(self):
        """LOG_INFO_INTERVAL_SEC 환경변수 → InfoHandler 주기 반영."""
        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_SAVEUS_CHAT_ID": "su",
            "LOG_INFO_INTERVAL_SEC": "60",
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("bitflyer")

        try:
            h = _handlers[0]
            assert isinstance(h, TelegramInfoHandler)
            assert h._interval == 60
        finally:
            await shutdown_telegram_logging()


# ─── 엣지케이스 보강 ─────────────────────────────────────────

class TestEdgeCases:

    def _make_record(self, level: int, msg: str, name: str = "test") -> logging.LogRecord:
        return logging.LogRecord(
            name=name, level=level, pathname="", lineno=0,
            msg=msg, args=(), exc_info=None,
        )

    @pytest.mark.asyncio
    async def test_digest_flush_empty_buffer_no_send(self):
        """DigestHandler: 빈 버퍼 _flush() 호출 시 _send_telegram 미호출."""
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=300)
        # 버퍼 비어있음 (아무것도 emit 안 함)

        call_count = [0]

        async def fake_send(*a, **k):
            call_count[0] += 1
            return True

        with patch("core.logging.telegram_handlers._send_telegram", side_effect=fake_send):
            await h._flush()

        assert call_count[0] == 0

    @pytest.mark.asyncio
    async def test_info_flush_empty_buffer_no_send(self):
        """InfoHandler: 빈 버퍼 _flush() 호출 시 _send_telegram 미호출."""
        h = TelegramInfoHandler("tok", "chat", exchange="GMO", interval_sec=120)

        call_count = [0]

        async def fake_send(*a, **k):
            call_count[0] += 1
            return True

        with patch("core.logging.telegram_handlers._send_telegram", side_effect=fake_send):
            await h._flush()

        assert call_count[0] == 0

    @pytest.mark.asyncio
    async def test_alert_exc_info_includes_traceback(self):
        """AlertHandler: exc_info 포함 레코드 → 메시지에 traceback 포함."""
        loop = asyncio.get_running_loop()
        h = TelegramAlertHandler("tok", "chat", exchange="BF", debounce_sec=0)
        h.set_loop(loop)

        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0,
            msg="주문 실패", args=(), exc_info=exc_info,
        )

        sent_texts = []
        with patch(
            "core.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            h.emit(record)
            await asyncio.sleep(0)

        assert len(sent_texts) == 1
        assert "ValueError" in sent_texts[0] or "test error" in sent_texts[0]
