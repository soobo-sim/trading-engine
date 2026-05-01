"""
core/logging/telegram_handlers.py 단위 테스트.

검증 항목:
- _send_telegram: 토큰/chat_id 미설정, 성공, 실패, 길이 초과
- TelegramDigestHandler: INFO만 버퍼링, DEBUG/WARNING 무시, flush 포맷, 버퍼 초과
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
    TelegramTransactionHandler,
    _send_telegram,
    setup_telegram_logging,
    shutdown_telegram_logging,
    seed_telegram_regime_state,
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

    def test_info_only_buffered(self):
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=300)
        h.emit(self._make_record(logging.INFO, "info msg"))
        assert len(h._buffer) == 1

    def test_debug_ignored(self):
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=300)
        h.emit(self._make_record(logging.DEBUG, "debug msg"))
        assert len(h._buffer) == 0

    def test_warning_ignored(self):
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=300)
        h.emit(self._make_record(logging.WARNING, "warn msg"))
        assert len(h._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_sends_buffered_messages(self):
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=5)
        h.emit(self._make_record(logging.INFO, "msg1"))
        h.emit(self._make_record(logging.INFO, "msg2"))

        sent_texts = []
        with patch(
            "core.shared.logging.telegram_handlers._send_telegram",
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
        h.emit(self._make_record(logging.INFO, "msg"))

        with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock, return_value=True):
            await h._flush()

        assert len(h._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_over_100_items_batches(self):
        """100건 초과 시 나머지는 다음 배치 표시."""
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=5)
        for i in range(120):
            h.emit(self._make_record(logging.INFO, f"msg{i}"))

        sent_texts = []
        with patch(
            "core.shared.logging.telegram_handlers._send_telegram",
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
        h.emit(self._make_record(logging.INFO, "leftover"))

        sent_texts = []
        with patch(
            "core.shared.logging.telegram_handlers._send_telegram",
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
            "core.shared.logging.telegram_handlers._send_telegram",
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
            "core.shared.logging.telegram_handlers._send_telegram",
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
            "core.shared.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            h.emit(self._make_record(logging.CRITICAL, "치명적 오류"))
            await asyncio.sleep(0)

        assert "🔴" in sent_texts[0]

    @pytest.mark.asyncio
    async def test_debounce_skips_duplicate(self):
        """debounce_sec 이내 동일 logger+level+메시지는 스킵."""
        loop = asyncio.get_running_loop()
        h = TelegramAlertHandler("tok", "chat", exchange="BF", debounce_sec=100)
        h.set_loop(loop)

        sent_texts = []
        with patch(
            "core.shared.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            h.emit(self._make_record(logging.WARNING, "same message"))
            h.emit(self._make_record(logging.WARNING, "same message"))  # 디바운스로 스킵
            await asyncio.sleep(0)

        assert len(sent_texts) == 1  # 두 번째는 스킵

    @pytest.mark.asyncio
    async def test_different_message_same_logger_not_debounced(self):
        """동일 logger + level이라도 메시지가 다르면 디바운스 미적용."""
        loop = asyncio.get_running_loop()
        h = TelegramAlertHandler("tok", "chat", exchange="BF", debounce_sec=100)
        h.set_loop(loop)

        sent_texts = []
        with patch(
            "core.shared.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            h.emit(self._make_record(logging.WARNING, "GR-06 위반"))
            h.emit(self._make_record(logging.WARNING, "GR-02 위반"))  # 다른 메시지 → 전송
            await asyncio.sleep(0)

        assert len(sent_texts) == 2

    @pytest.mark.asyncio
    async def test_different_logger_not_debounced(self):
        """다른 logger는 별도 키 → 디바운스 미적용."""
        loop = asyncio.get_running_loop()
        h = TelegramAlertHandler("tok", "chat", exchange="BF", debounce_sec=100)
        h.set_loop(loop)

        sent_texts = []
        with patch(
            "core.shared.logging.telegram_handlers._send_telegram",
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
        """HEARTBEAT_CHAT_ID 설정 시 TransactionHandler 등록."""
        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_HEARTBEAT_CHAT_ID": "hb123",
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("bitflyer")

        try:
            assert len(_handlers) == 1
            assert isinstance(_handlers[0], TelegramTransactionHandler)
            assert _handlers[0]._domain == "judge"
        finally:
            await shutdown_telegram_logging()

    @pytest.mark.asyncio
    async def test_saveus_alert_handler_registered(self):
        """SAVEUS_CHAT_ID 설정 시 PunisherTransaction + AlertHandler 2개 등록."""
        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_SAVEUS_CHAT_ID": "saveus123",
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("bitflyer")

        try:
            assert len(_handlers) == 2
            types = {type(h) for h in _handlers}
            assert TelegramAlertHandler in types
            assert TelegramTransactionHandler in types
            # PunisherTransaction 확인
            tx = next(h for h in _handlers if isinstance(h, TelegramTransactionHandler))
            assert tx._domain == "punisher"
        finally:
            await shutdown_telegram_logging()

    @pytest.mark.asyncio
    async def test_all_channels(self):
        """HeartBeat + SaveUs 모두 설정 시 핸들러 3개(JudgeDigest + PunisherDigest + Alert) 등록."""
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
            types = [type(h) for h in _handlers]
            assert types.count(TelegramTransactionHandler) == 2  # judge + punisher
            assert TelegramAlertHandler in types
            # 도메인 분리 확인
            domains = [getattr(h, "_domain", None) for h in _handlers if isinstance(h, TelegramTransactionHandler)]
            assert "judge" in domains
            assert "punisher" in domains
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
            assert isinstance(h, TelegramTransactionHandler)
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

        # HEARTBEAT_CHAT_ID만 설정 → JudgeTransaction 핸들러 1개
        assert len(_handlers) == 1
        h = _handlers[0]
        assert isinstance(h, TelegramTransactionHandler)
        assert h._domain == "judge"

        # shutdown 시 핸들러 정리 확인
        with patch(
            "core.shared.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await shutdown_telegram_logging()

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
            assert len(_handlers) == 3  # 6이 되면 안 됨 (JudgeDigest + PunisherDigest + Alert)
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

        with patch("core.shared.logging.telegram_handlers._send_telegram", side_effect=fake_send):
            await h._flush()

        assert call_count[0] == 0

    @pytest.mark.asyncio
    async def test_info_flush_empty_buffer_no_send(self):
        """DigestHandler: INFO emit 없이 flush 시 미호출."""
        h = TelegramDigestHandler("tok", "chat", exchange="BF", interval_sec=300)

        call_count = [0]

        async def fake_send(*a, **k):
            call_count[0] += 1
            return True

        with patch("core.shared.logging.telegram_handlers._send_telegram", side_effect=fake_send):
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
            "core.shared.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: sent_texts.append(a[2]) or True,
        ):
            h.emit(record)
            await asyncio.sleep(0)

        assert len(sent_texts) == 1
        assert "ValueError" in sent_texts[0] or "test error" in sent_texts[0]


# ─── 기본값 검증 ─────────────────────────────────────────────

class TestDefaultValues:
    """HeartBeat DigestHandler 간격 기본값이 5분(300초)임을 명시적으로 검증."""

    def test_digest_handler_default_interval_is_300(self):
        """TelegramDigestHandler 기본 interval_sec = 300 (5분)."""
        h = TelegramDigestHandler("tok", "chat", exchange="BF")
        assert h._interval == 300

    def test_digest_handler_level_is_info(self):
        """TelegramDigestHandler 핸들러 레벨이 INFO임을 검증."""
        h = TelegramDigestHandler("tok", "chat", exchange="BF")
        assert h.level == logging.INFO

    def test_alert_handler_ignores_info(self):
        """TelegramAlertHandler 핸들러 레벨이 WARNING이므로 INFO는 프레임워크에서 차단됨."""
        h = TelegramAlertHandler("tok", "chat", exchange="BF", debounce_sec=0)
        # Python 로깅 프레임워크는 callHandlers()에서 handler.level < record.levelno를 체크
        # WARNING 레벨 핸들러에 INFO(20)는 전달되지 않음
        assert h.level == logging.WARNING

    @pytest.mark.asyncio
    async def test_log_info_interval_sec_env_ignored(self):
        """LOG_INFO_INTERVAL_SEC 환경변수가 있어도 setup에서 무시됨 (삭제된 기능)."""
        from core.logging.telegram_handlers import _handlers

        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_HEARTBEAT_CHAT_ID": "hb",
            "LOG_INFO_INTERVAL_SEC": "60",  # 구버전 환경변수, 현재 미사용
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("bitflyer")

        try:
            # JudgeTransaction 핸들러 1개만 등록
            assert len(_handlers) == 1
            assert isinstance(_handlers[0], TelegramTransactionHandler)
            assert _handlers[0]._domain == "judge"
        finally:
            await shutdown_telegram_logging()

    @pytest.mark.asyncio
    async def test_setup_digest_default_interval_is_300(self):
        """LOG_DIGEST_INTERVAL_SEC 미설정 시 DigestHandler._interval == 300."""
        from core.logging.telegram_handlers import _handlers

        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_HEARTBEAT_CHAT_ID": "hb",
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("bitflyer")

        try:
            tx_handlers = [h for h in _handlers if isinstance(h, TelegramTransactionHandler)]
            assert len(tx_handlers) == 1
            assert tx_handlers[0]._interval == 300
        finally:
            await shutdown_telegram_logging()


# ─── 도메인 라우팅 검증 ─────────────────────────────────────────────

class TestDomainRouting:
    """_get_domain() 및 도메인별 수집 필터 테스트."""

    def _make_record(self, name: str, level: int = logging.INFO) -> logging.LogRecord:
        return logging.LogRecord(
            name=name, level=level, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )

    def test_judge_prefixes_mapped_correctly(self):
        """JUDGE_PREFIXES 속한 logger가 'judge'로 분류됨."""
        from core.logging.telegram_handlers import _get_domain
        assert _get_domain("core.judge.decision.rule_based") == "judge"
        assert _get_domain("core.judge.safety.guardrails") == "judge"
        assert _get_domain("core.execution.orchestrator") == "judge"
        assert _get_domain("core.judge.monitoring.event_detector") == "judge"
        assert _get_domain("core.data.hub") == "judge"
        assert _get_domain("core.strategy.signals") == "judge"

    def test_punisher_prefixes_mapped_correctly(self):
        """PUNISHER_PREFIXES 속한 logger가 'punisher'로 분류됨."""
        from core.logging.telegram_handlers import _get_domain
        # NOTE: core.strategy.base_trend 제거 — core.judge.candle_loop/core.judge.signal로 이관
        assert _get_domain("core.strategy.plugins.gmo_coin_trend") == "punisher"
        assert _get_domain("core.execution.regime_gate") == "punisher"
        assert _get_domain("core.punisher.task.auto_reporter") == "punisher"
        assert _get_domain("adapters.gmo_coin.client") == "punisher"
        assert _get_domain("main") == "punisher"
        assert _get_domain("api.routes.strategies") == "punisher"

    def test_unknown_logger_fallback_to_shared(self):
        """JUDGE/PUNISHER 모두 매칭 안 되면 'shared' fallback."""
        from core.logging.telegram_handlers import _get_domain
        assert _get_domain("unknown.module") == "shared"
        assert _get_domain("uvicorn.access") == "shared"

    def test_judge_digest_filters_punisher_logs(self):
        """domain='judge' 핸들러는 punisher logger INFO를 무시."""
        h = TelegramDigestHandler("tok", "chat", domain="judge")
        record = self._make_record("core.punisher.strategy.base_trend")
        h.emit(record)
        assert len(h._buffer) == 0

    def test_judge_digest_collects_judge_logs(self):
        """domain='judge' 핸들러는 judge logger INFO를 수집."""
        h = TelegramDigestHandler("tok", "chat", domain="judge")
        record = self._make_record("core.judge.decision.rule_based")
        h.emit(record)
        assert len(h._buffer) == 1

    def test_punisher_digest_filters_judge_logs(self):
        """domain='punisher' 핸들러는 judge logger INFO를 무시."""
        h = TelegramDigestHandler("tok", "chat", domain="punisher")
        record = self._make_record("core.judge.decision.rule_based")
        h.emit(record)
        assert len(h._buffer) == 0

    def test_punisher_digest_collects_punisher_logs(self):
        """domain='punisher' 핸들러는 punisher logger INFO를 수집."""
        h = TelegramDigestHandler("tok", "chat", domain="punisher")
        record = self._make_record("core.punisher.task.auto_reporter")
        h.emit(record)
        assert len(h._buffer) == 1

    def test_punisher_digest_collects_shared_logs(self):
        """domain='punisher' 핸들러는 shared(fallback) logger도 수집."""
        h = TelegramDigestHandler("tok", "chat", domain="punisher")
        record = self._make_record("uvicorn.access")
        h.emit(record)
        assert len(h._buffer) == 1

    def test_domain_none_collects_all(self):
        """domain=None(legacy) 시 모든 INFO 수집."""
        h = TelegramDigestHandler("tok", "chat", domain=None)
        h.emit(self._make_record("core.judge.decision.rule_based"))
        h.emit(self._make_record("core.punisher.task.auto_reporter"))
        assert len(h._buffer) == 2


# ─── 정기 요약 도메인 귀속 검증 ─────────────────────────────────────

class TestPeriodicSummaryDomain:
    """5분 정기 요약이 판단 도메인(HeartBeat) 전용임을 검증."""

    @pytest.mark.asyncio
    async def test_judge_handler_starts_flush_loop_task(self):
        """domain='judge' 핸들러는 start() 시 _flush_loop 태스크를 생성한다."""
        h = TelegramTransactionHandler("tok", "chat", exchange="GMO", domain="judge")
        await h.start()
        try:
            assert h._task is not None
            assert not h._task.done()
        finally:
            if h._task:
                h._task.cancel()
                try:
                    await h._task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_punisher_handler_does_not_start_flush_loop(self):
        """domain='punisher' 핸들러는 start() 후 _task가 None — 정기 요약 루프 없음."""
        h = TelegramTransactionHandler("tok", "chat", exchange="GMO", domain="punisher")
        await h.start()
        assert h._task is None

    @pytest.mark.asyncio
    async def test_periodic_summary_sends_to_judge_channel(self):
        """_send_periodic_summary() 호출 시 judge 채널(bot_token/chat_id)로 전송."""
        h = TelegramTransactionHandler("tok", "hb_chat", exchange="GMO", domain="judge")

        sent_calls: list[tuple] = []
        with patch(
            "core.shared.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
            side_effect=lambda token, chat, text: sent_calls.append((token, chat, text)) or True,
        ):
            await h._send_periodic_summary()

        assert len(sent_calls) == 1
        _, chat_id, text = sent_calls[0]
        assert chat_id == "hb_chat"
        assert "🔮" in text
        assert "판단 사이클" in text

    @pytest.mark.asyncio
    async def test_setup_judge_handler_has_task_punisher_has_none(self):
        """setup_telegram_logging 후 judge 핸들러만 _task 가짐."""
        from core.logging.telegram_handlers import _handlers
        _handlers.clear()
        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_HEARTBEAT_CHAT_ID": "hb",
            "TELEGRAM_SAVEUS_CHAT_ID": "su",
        }
        with patch.dict("os.environ", env, clear=True):
            await setup_telegram_logging("GMO")

        try:
            tx_handlers = [h for h in _handlers if isinstance(h, TelegramTransactionHandler)]
            judge_h = next(h for h in tx_handlers if h._domain == "judge")
            punisher_h = next(h for h in tx_handlers if h._domain == "punisher")

            # judge: 정기 루프 태스크 있음
            assert judge_h._task is not None
            assert not judge_h._task.done()
            # punisher: 정기 루프 태스크 없음
            assert punisher_h._task is None
        finally:
            await shutdown_telegram_logging()


# ─── seed_telegram_regime_state ─────────────────────────────────────────────

class TestSeedTelegramRegimeState:
    """SRS-01~SRS-04: 재시작 후 RegimeGate DB 복원 상태 주입."""

    def _make_handler(self, domain: str) -> TelegramTransactionHandler:
        return TelegramTransactionHandler("tok", "chat", exchange="TEST", domain=domain)

    def test_srs01_seeds_both_handlers(self):
        """SRS-01: judge + punisher 핸들러 모두 regime_status/consecutive 업데이트."""
        _handlers.clear()
        h_judge = self._make_handler("judge")
        h_punisher = self._make_handler("punisher")
        _handlers.extend([h_judge, h_punisher])

        seed_telegram_regime_state("trending", 4)

        assert h_judge._state['regime_status'] == "trending"
        assert h_judge._state['regime_consecutive'] == 4
        assert h_punisher._state['regime_status'] == "trending"
        assert h_punisher._state['regime_consecutive'] == 4
        _handlers.clear()

    def test_srs02_none_regime_is_noop(self):
        """SRS-02: regime=None이면 기존 _state 변경 없음."""
        _handlers.clear()
        h = self._make_handler("judge")
        h._state['regime_status'] = "trending"
        h._state['regime_consecutive'] = 3
        _handlers.append(h)

        seed_telegram_regime_state(None, 0)

        assert h._state['regime_status'] == "trending"  # 변경 없음
        assert h._state['regime_consecutive'] == 3
        _handlers.clear()

    def test_srs03_empty_handlers_no_error(self):
        """SRS-03: 핸들러 없으면 예외 없이 종료."""
        _handlers.clear()
        seed_telegram_regime_state("trending", 4)  # 예외 없이 통과

    def test_srs04_periodic_summary_uses_seeded_values(self):
        """SRS-04: seed 후 _send_periodic_summary가 seeded 값 사용 확인."""
        _handlers.clear()
        h = self._make_handler("judge")
        _handlers.append(h)

        seed_telegram_regime_state("trending", 4)

        # 5분 요약 텍스트를 직접 생성하여 값 확인 (send는 mock)
        import asyncio
        import unittest.mock as mock

        async def _check():
            with mock.patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=mock.AsyncMock) as m:
                h._loop = asyncio.get_running_loop()
                await h._send_periodic_summary()
                text = m.call_args[0][2]
                assert "추세 진행" in text
                assert "4H×4" in text

        asyncio.run(_check())
        _handlers.clear()


# ─── 결론 텍스트: 포지션 보유 + 진입 조건 충족 여부 ────────────────────────────

class TestConclusionWithPosition:
    """CON-01~03: 포지션 보유 시 결론 텍스트 분기 확인."""

    def _make_handler_with_state(self, has_pos: bool, all_met: bool) -> "TelegramTransactionHandler":
        h = TelegramTransactionHandler("tok", "chat", exchange="GMO", domain="judge")
        # 추세 진행 중 (체제 조건 충족)
        h._state.update({
            'regime_status': 'trending',
            'regime_consecutive': 5,
            'has_position': has_pos,
            'signal': 'short_setup',
        })
        if all_met:
            # 숏 4조건 모두 충족
            h._state.update({
                'current_price': 11_900_000,
                'ema_price': 12_000_000,
                'ema_slope_pct': -0.10,
                'rsi': 49.0,
            })
        else:
            # 기울기 미충족 (❌ 포함)
            h._state.update({
                'current_price': 11_900_000,
                'ema_price': 12_000_000,
                'ema_slope_pct': 0.05,   # SHORT_SLOPE_TH = -0.05 → ❌
                'rsi': 49.0,
            })
        return h

    @pytest.mark.asyncio
    async def test_con01_all_met_with_position_shows_reserve(self):
        """CON-01: 진입 조건 3/3 충족 + 포지션 보유 → '추가 진입 유보' 표시."""
        h = self._make_handler_with_state(has_pos=True, all_met=True)
        with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
            h._loop = asyncio.get_event_loop()
            await h._send_periodic_summary()
            text = m.call_args[0][2]
            assert "추가 진입 유보" in text
            assert "진입 조건 모두 충족" in text

    @pytest.mark.asyncio
    async def test_con02_unmet_with_position_shows_monitoring(self):
        """CON-02: 진입 조건 미충족 + 포지션 보유 → '청산 조건 감시' 표시."""
        h = self._make_handler_with_state(has_pos=True, all_met=False)
        with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
            h._loop = asyncio.get_event_loop()
            await h._send_periodic_summary()
            text = m.call_args[0][2]
            assert "청산 조건 감시" in text
            assert "추가 진입 유보" not in text

    @pytest.mark.asyncio
    async def test_con03_no_position_all_met_shows_entry(self):
        """CON-03: 포지션 없음 + 숏 조건 충족 → '숏 진입 조건 충족' 표시."""
        h = self._make_handler_with_state(has_pos=False, all_met=True)
        with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
            h._loop = asyncio.get_event_loop()
            await h._send_periodic_summary()
            text = m.call_args[0][2]
            assert "숏 진입 조건 충족" in text


# ─── _send_stop_tighten ──────────────────────────────────────────────────────

class TestSendStopTighten:
    """ST-01~ST-02: 스탑 타이트닝 텔레그램 발송 조건."""

    def _make_handler(self) -> TelegramTransactionHandler:
        return TelegramTransactionHandler("tok", "chat", exchange="GMO", domain="punisher")

    @pytest.mark.asyncio
    async def test_st01_sends_when_stop_rises(self):
        """ST-01: prev < curr이면 '스탑 상향' 메시지 발송."""
        h = self._make_handler()
        h._state['stop_tighten_event'] = {'prev': 12_000_000.0, 'curr': 12_100_000.0}
        with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
            await h._send_stop_tighten()
            assert m.called
            text = m.call_args[0][2]
            assert "스탑 상향" in text
            assert "¥12,000,000" in text
            assert "¥12,100,000" in text

    @pytest.mark.asyncio
    async def test_st02_skips_when_stop_unchanged(self):
        """ST-02: prev == curr(diff=0)이면 발송 생략 — 오해 방지."""
        h = self._make_handler()
        h._state['stop_tighten_event'] = {'prev': 12_074_695.0, 'curr': 12_074_695.0}
        with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
            await h._send_stop_tighten()
            assert not m.called
            assert h._state['stop_tighten_event'] is None


# ─── gate_status 3단계 표현 ──────────────────────────────────────────────────

class TestGateStatusThreeLevel:
    """GS-01~GS-04: 체제+시그널 상태에 따른 gate_status 3단계 표현 확인."""

    def _make_handler(self, regime: str, consecutive: int, signal: str | None) -> "TelegramTransactionHandler":
        h = TelegramTransactionHandler("tok", "chat", exchange="GMO", domain="judge")
        h._state.update({
            'regime_status': regime,
            'regime_consecutive': consecutive,
            'signal': signal,
            'current_price': 12_000_000.0,
            'ema_price': 12_100_000.0,
            'ema_slope_pct': -0.01,
            'rsi': 50.0,
        })
        return h

    @pytest.mark.asyncio
    async def test_gs01_no_signal_shows_wait(self):
        """GS-01: trending ×6, signal=hold → '체제OK · 신호 대기' 표시."""
        h = self._make_handler('trending', 6, 'hold')
        with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
            await h._send_periodic_summary()
            text = m.call_args[0][2]
        assert "체제OK · 신호 대기" in text
        assert "진입 허용" not in text

    @pytest.mark.asyncio
    async def test_gs02_long_setup_shows_long_signal(self):
        """GS-02: trending ×3, signal=long_setup → '신호 발생 (롱)' 표시."""
        h = self._make_handler('trending', 3, 'long_setup')
        with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
            await h._send_periodic_summary()
            text = m.call_args[0][2]
        assert "신호 발생 (롱)" in text

    @pytest.mark.asyncio
    async def test_gs03_short_setup_shows_short_signal(self):
        """GS-03: trending ×4, signal=short_setup → '신호 발생 (숏)' 표시."""
        h = self._make_handler('trending', 4, 'short_setup')
        with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
            await h._send_periodic_summary()
            text = m.call_args[0][2]
        assert "신호 발생 (숏)" in text

    @pytest.mark.asyncio
    async def test_gs04_insufficient_consecutive_shows_blocked(self):
        """GS-04: trending ×2 (warm-up 미완료) → '진입 차단 중' 표시."""
        h = self._make_handler('trending', 2, 'long_setup')
        with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
            await h._send_periodic_summary()
            text = m.call_args[0][2]
        assert "진입 차단 중" in text
        assert "신호 발생" not in text


# ─── GateSummary JIT 모드 ───────────────────────────────────────────────────

class TestGateSummaryJITMode:
    """GS-JIT-01~03: TRADING_MODE=jit 시 게이트 줄을 JIT Advisory 상태로 표시."""

    def _make_handler(self, jit_decision=None, jit_reasoning=''):
        h = TelegramTransactionHandler("tok", "chat", exchange="GMO_COIN", domain="judge")
        h._state.update({
            'regime_status': 'trending',
            'regime_consecutive': 6,
            'signal': 'hold',
            'current_price': 12_000_000.0,
            'ema_price': 12_100_000.0,
            'ema_slope_pct': -0.01,
            'rsi': 50.0,
            'jit_decision': jit_decision,
            'jit_reasoning': jit_reasoning,
        })
        return h

    @pytest.mark.asyncio
    async def test_gs_jit01_no_prior_jit_shows_waiting(self):
        """GS-JIT-01: TRADING_MODE=jit, jit_decision=None → 진입 신호 대기 중."""
        h = self._make_handler(jit_decision=None)
        with patch.dict('os.environ', {'TRADING_MODE': 'jit'}):
            with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
                await h._send_periodic_summary()
                text = m.call_args[0][2]
        assert "JIT Advisory" in text
        assert "진입 신호 대기 중" in text
        assert "실행 게이트: 4H×" not in text

    @pytest.mark.asyncio
    async def test_gs_jit02_go_shows_approved(self):
        """GS-JIT-02: TRADING_MODE=jit, jit_decision='GO' → 최근 진입 승인."""
        h = self._make_handler(jit_decision='GO')
        with patch.dict('os.environ', {'TRADING_MODE': 'jit'}):
            with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
                await h._send_periodic_summary()
                text = m.call_args[0][2]
        assert "JIT Advisory" in text
        assert "최근 진입 승인" in text

    @pytest.mark.asyncio
    async def test_gs_jit03_no_go_shows_blocked(self):
        """GS-JIT-03: TRADING_MODE=jit, jit_decision='NO_GO' → 최근 진입 차단."""
        h = self._make_handler(jit_decision='NO_GO')
        with patch.dict('os.environ', {'TRADING_MODE': 'jit'}):
            with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
                await h._send_periodic_summary()
                text = m.call_args[0][2]
        assert "JIT Advisory" in text
        assert "최근 진입 차단" in text

    @pytest.mark.asyncio
    async def test_gs_jit04_v1_mode_keeps_regime_gate(self):
        """GS-JIT-04: TRADING_MODE=v1 (기본) → 기존 실행 게이트 표시 유지."""
        h = self._make_handler(jit_decision=None)
        with patch.dict('os.environ', {'TRADING_MODE': 'v1'}):
            with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
                await h._send_periodic_summary()
                text = m.call_args[0][2]
        assert "실행 게이트: 4H×" in text
        assert "JIT Advisory" not in text


# ─── JIT Advisory Gate 파싱 검증 ────────────────────────────────────────────

class TestJITAdvisoryParsing:
    """JIT-01~JIT-06: JIT Advisory Gate 로그 파싱 — GO/NO_GO/ADJUST/fail-soft/orchestrator."""

    def _make_handler(self) -> TelegramTransactionHandler:
        h = TelegramTransactionHandler("tok", "chat", exchange="GMO_COIN", domain="judge")
        return h

    def test_jit01_go_sets_state(self):
        """JIT-01: GO 로그 → jit_decision='GO', decision_action 갱신."""
        h = self._make_handler()
        h._parse_and_update(
            "[JIT][abc] btc_jpy GO — action=entry_long size=50% conf=0.80. 사유: RSI 양호"
        )
        assert h._state['jit_decision'] == 'GO'
        assert h._state['decision_action'] == 'entry_long'

    def test_jit02_no_go_sets_hold(self):
        """JIT-02: NO_GO 로그 → jit_decision='NO_GO', decision_action='hold'."""
        h = self._make_handler()
        h._parse_and_update(
            "[JIT][abc] btc_jpy NO_GO — action=entry_long → hold. 사유: RSI 과매도 85"
        )
        assert h._state['jit_decision'] == 'NO_GO'
        assert h._state['decision_action'] == 'hold'
        assert 'RSI' in h._state.get('jit_reasoning', '')

    def test_jit03_adjust_updates_size(self):
        """JIT-03: ADJUST 로그 → jit_decision='ADJUST', decision_size_pct 갱신."""
        h = self._make_handler()
        h._parse_and_update(
            "[JIT][abc] btc_jpy ADJUST — size 50%→30% action entry_long→entry_long 사유: 변동성 고려"
        )
        assert h._state['jit_decision'] == 'ADJUST'
        assert h._state['decision_size_pct'] == pytest.approx(0.30)
        assert h._state['decision_action'] == 'entry_long'
        assert '변동성' in h._state.get('jit_reasoning', '')

    def test_jit04_fail_soft_no_go(self):
        """JIT-04: fail-soft NO_GO → jit_decision='NO_GO', jit_reasoning 타임아웃 표시."""
        h = self._make_handler()
        h._parse_and_update(
            "[JIT][abc] btc_jpy fail-soft NO_GO — timeout 5s exceeded"
        )
        assert h._state['jit_decision'] == 'NO_GO'
        assert '타임아웃' in h._state.get('jit_reasoning', '')

    def test_jit05_orchestrator_new_format(self):
        """JIT-05: 오케스트레이터 신 포맷 '사이즈 50%, 확신도 0.72' 파싱."""
        h = self._make_handler()
        h._parse_and_update(
            "btc_jpy: 롱 진입 결정. 사이즈 50%, 확신도 0.72, SL ¥12000000. 안전장치 통과."
        )
        assert h._state['decision_size_pct'] == pytest.approx(0.50)
        assert h._state['decision_confidence'] == pytest.approx(0.72)

    def test_jit06_orchestrator_old_format(self):
        """JIT-06: 오케스트레이터 구 포맷 '확신도=0.65 사이즈=0.25' 파싱."""
        h = self._make_handler()
        h._parse_and_update(
            "btc_jpy: 진입 결정 확신도=0.65 사이즈=0.25 안전장치 통과"
        )
        assert h._state['decision_size_pct'] == pytest.approx(0.25)
        assert h._state['decision_confidence'] == pytest.approx(0.65)


# ─── BoxMgr 신호 필터 (매분 신호 변경 오발 방지) ────────────────────────────

class TestBoxMgrSignalFilter:
    """BX-01~BX-03: BoxMgr 로그의 signal은 judge 핸들러에서 파싱 무시해야 함.

    추세 전략(GmocMgr)과 박스 전략(BoxMgr)이 같은 페어를 동시 처리할 때
    두 전략의 signal이 교대로 파싱되면 매분 신호 변경 알림이 발송되는 버그.
    [BoxMgr] 로그는 signal 파싱에서 완전 제외해야 함.
    """

    def _handler(self) -> TelegramTransactionHandler:
        h = TelegramTransactionHandler("tok", "cid", exchange="GMO_COIN", domain="judge")
        return h

    def _trend_log(self, signal: str = "long_setup") -> str:
        return (
            f"[GmocMgr] btc_jpy: 추세 유지 중 "
            f"signal={signal} ema_slope_pct=0.0234 rsi=58.2 ema=12331251 price=12416643"
        )

    def _box_log(self, signal: str = "exit_warning") -> str:
        return (
            f"[BoxMgr] btc_jpy: 박스 이탈 감지 "
            f"signal={signal} ema_slope_pct=0.0234 rsi=58.2 ema=12331251 price=12416643"
        )

    def test_bx01_box_log_does_not_update_signal_state(self):
        """BX-01: [BoxMgr] signal=exit_warning 로그는 _state['signal']을 바꾸지 않음."""
        h = self._handler()
        h._state['signal'] = 'long_setup'

        h._parse_and_update(self._box_log('exit_warning'))

        assert h._state['signal'] == 'long_setup', (
            "BoxMgr 로그가 signal 상태를 변경하면 안 됨"
        )

    def test_bx02_trend_log_after_box_log_stays_stable(self):
        """BX-02: 추세 로그(long_setup) → 박스 로그(exit_warning) 교대 → 상태는 long_setup 유지."""
        h = self._handler()
        h._state['signal'] = None  # 초기화

        h._parse_and_update(self._trend_log('long_setup'))
        assert h._state['signal'] == 'long_setup'

        # 박스 로그 → 무시
        h._parse_and_update(self._box_log('exit_warning'))
        assert h._state['signal'] == 'long_setup', (
            "BoxMgr 신호가 추세 신호를 덮어쓰면 안 됨"
        )

    def test_bx03_no_signal_change_task_from_box_log(self):
        """BX-03: [BoxMgr] 로그는 _state['signal']이 변경되지 않으므로 signal_change 발송 불필요."""
        h = self._handler()
        initial_signal = h._state.get('signal')

        # BoxMgr 로그 처리 전후로 signal 상태가 바뀌지 않으면 task 생성도 없음
        h._parse_and_update(self._box_log('exit_warning'))

        assert h._state['signal'] == initial_signal


    def _rachel_box_advisory_log(self, signal: str = "exit_warning") -> str:
        """BUG-034: RachelAdvisory:box_mean_reversion 로그."""
        return (
            f"[RachelAdvisory:box_mean_reversion] btc_jpy: advisory 읽음 — "
            f"id=94 action=hold confidence=0.90 size_pct=None 잔여=4.7H "
            f"signal={signal} 포지션 없음 pyramid=0\n"
            f"  근거: 긴급 차단"
        )

    def _rachel_trend_advisory_log(self, signal: str = "long_setup") -> str:
        """BUG-034: RachelAdvisory:trend_following 로그."""
        return (
            f"[RachelAdvisory:trend_following] btc_jpy: advisory 읽음 — "
            f"id=94 action=hold confidence=0.90 size_pct=None 잔여=4.7H "
            f"signal={signal} 포지션 있음 pyramid=2\n"
            f"  근거: 긴급 차단"
        )

    def test_bx04_box_advisory_log_does_not_update_signal(self):
        """BX-04 (BUG-034): [RachelAdvisory:box_mean_reversion] 로그는 signal 갱신 안 함."""
        h = self._handler()
        h._state['signal'] = 'long_setup'

        h._parse_and_update(self._rachel_box_advisory_log('exit_warning'))

        assert h._state['signal'] == 'long_setup', \
            "box_mean_reversion advisory 로그가 signal을 변경하면 안 됨"

    def test_bx05_trend_advisory_log_updates_signal(self):
        """BX-05: [RachelAdvisory:trend_following] 로그는 signal 정상 갱신."""
        h = self._handler()
        h._state['signal'] = None

        h._parse_and_update(self._rachel_trend_advisory_log('long_setup'))

        # trend_following advisory 로그에 signal= 있으면 갱신돼야 함
        # (상세 포맷 아니라 폴백 정규식으로 처리)
        assert h._state['signal'] == 'long_setup', \
            "trend_following advisory 로그가 signal을 갱신해야 함"

    def test_bx06_alternating_advisory_logs_no_spam(self):
        """BX-06 (BUG-034): trend/box advisory 교대 5회 → signal 변경 횟수 0 (초기 제외)."""
        h = self._handler()
        h._state['signal'] = 'long_setup'
        signal_changes = []
        original_signal = h._state['signal']

        for _ in range(5):
            h._parse_and_update(self._rachel_trend_advisory_log('long_setup'))
            h._parse_and_update(self._rachel_box_advisory_log('exit_warning'))

        # long_setup 유지 — exit_warning으로 변경되지 않음
        assert h._state['signal'] == 'long_setup', \
            f"매분 교대 로그 후 signal이 변경됨: {h._state['signal']}"

    def test_bx07_ranging_regime_box_log_updates_signal(self):
        """BX-07: regime=ranging일 때 [BoxMgr] 로그가 signal을 갱신해야 함."""
        h = self._handler()
        h._state['regime_status'] = 'ranging'
        h._state['regime_consecutive'] = 4
        h._state['signal'] = None

        h._parse_and_update(self._box_log('long_setup'))

        assert h._state['signal'] == 'long_setup', \
            "ranging 체제에서 박스 로그가 signal을 갱신해야 함"

    def test_bx08_ranging_periodic_summary_shows_box_conditions(self):
        """BX-08: regime=ranging 5분 요약 → 박스 조건 표시."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        h = TelegramTransactionHandler("tok", "cid", exchange="GMO_COIN", domain="judge")
        h._state.update({
            'regime_status': 'ranging',
            'regime_consecutive': 4,
            'signal': 'long_setup',
            'current_price': 11_900_000.0,
            'box_lower': 11_800_000.0,
            'box_upper': 12_200_000.0,
            'box_detected': True,
            'rsi': 32.0,
        })

        async def _check():
            with patch("core.shared.logging.telegram_handlers._send_telegram", new_callable=AsyncMock) as m:
                h._loop = asyncio.get_running_loop()
                await h._send_periodic_summary()
                return m.call_args[0][2]

        text = asyncio.run(_check())
        assert "박스 감지" in text
        assert "실행 게이트" in text
        assert "4H×" in text
        assert "¥11,800,000" in text
        assert "¥12,200,000" in text


# ─── WS-Cross / entry_timeframe 5분 요약 표시 테스트 ────────────────────────

class TestEntryModeAndArmedSummary:
    """EP-01~EP-08: entry_mode / entry_timeframe / armed 상태 5분 요약 표시."""

    def _make_handler(self) -> TelegramTransactionHandler:
        h = TelegramTransactionHandler("tok", "cid", exchange="GMO_COIN", domain="judge")
        h._state.update({
            'regime_status': 'trending',
            'regime_consecutive': 4,
            'signal': 'short_setup',
            'current_price': 11_900_000.0,
            'ema_price': 12_000_000.0,
            'ema_slope_pct': -0.08,
            'rsi': 50.0,
        })
        return h

    def _run_summary(self, h: TelegramTransactionHandler) -> str:
        import asyncio
        from unittest.mock import AsyncMock, patch

        async def _inner():
            with patch(
                "core.shared.logging.telegram_handlers._send_telegram",
                new_callable=AsyncMock,
            ) as m:
                h._loop = asyncio.get_running_loop()
                await h._send_periodic_summary()
                return m.call_args[0][2]

        return asyncio.run(_inner())

    def test_ep01_default_mode_no_mode_line(self):
        """EP-01: 기본 모드(market + 4H)는 '진입 모드' 줄 표시 안 함."""
        h = self._make_handler()
        text = self._run_summary(h)
        assert "진입 모드" not in text

    def test_ep02_ws_cross_mode_shows_mode_line(self):
        """EP-02: entry_mode=ws_cross → '진입 모드: ⚡ WS 돌파' 표시."""
        h = self._make_handler()
        h._state['entry_mode'] = 'ws_cross'
        text = self._run_summary(h)
        assert "진입 모드" in text
        assert "WS 돌파" in text

    def test_ep03_entry_timeframe_1h_shows_mode_line(self):
        """EP-03: entry_timeframe=1h (market 모드) → '진입 모드: 📊 1H slope/RSI' 표시."""
        h = self._make_handler()
        h._state['entry_timeframe'] = '1h'
        text = self._run_summary(h)
        assert "진입 모드" in text
        assert "1H slope/RSI" in text

    def test_ep04_ws_cross_with_1h_shows_combined(self):
        """EP-04: entry_mode=ws_cross + entry_timeframe=1h → WS 돌파 + 1H 같이 표시."""
        h = self._make_handler()
        h._state['entry_mode'] = 'ws_cross'
        h._state['entry_timeframe'] = '1h'
        text = self._run_summary(h)
        assert "WS 돌파" in text
        assert "1H slope/RSI" in text

    def test_ep05_slope_label_shows_1h_suffix(self):
        """EP-05: entry_timeframe=1h → ② EMA 기울기 라벨에 '(1H)' 표시."""
        h = self._make_handler()
        h._state['entry_timeframe'] = '1h'
        text = self._run_summary(h)
        assert "(1H)" in text

    def test_ep06_default_no_1h_suffix(self):
        """EP-06: entry_timeframe None → ② EMA 기울기 라벨에 '(1H)' 없음."""
        h = self._make_handler()
        text = self._run_summary(h)
        assert "(1H)" not in text

    def test_ep07_armed_short_shows_armed_line(self):
        """EP-07: ws_cross + armed_direction=short → EMA/현재가/거리 표시."""
        import time as _time
        h = self._make_handler()
        h._state['entry_mode'] = 'ws_cross'
        h._state['armed_direction'] = 'short'
        h._state['armed_ema'] = 12_000_000.0
        h._state['armed_expire_at'] = _time.time() + 3600 * 3.5  # 3h 30m
        h._state['current_price'] = 12_142_312.0   # current > ema → 더 내려가야
        text = self._run_summary(h)
        assert "숏 armed" in text
        assert "¥12,000,000" in text
        assert "만료까지" in text
        assert "현재" in text
        assert "더 내려가야 진입" in text

    def test_ep08_ws_cross_no_armed_shows_waiting(self):
        """EP-08: ws_cross + armed 없음 → '⏳ WS 대기: armed 조건 미충족' 표시."""
        h = self._make_handler()
        h._state['entry_mode'] = 'ws_cross'
        text = self._run_summary(h)
        assert "WS 대기" in text
        assert "armed 조건 미충족" in text


class TestArmedStateParsing:
    """AP-01~AP-05: armed 상태 로그 파싱."""

    def _make_handler(self) -> TelegramTransactionHandler:
        return TelegramTransactionHandler("tok", "cid", exchange="GMO_COIN", domain="judge")

    def _emit(self, h: TelegramTransactionHandler, msg: str) -> None:
        import logging
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0, msg=msg, args=(), exc_info=None,
        )
        h.emit(record)

    def test_ap01_short_armed_sets_state(self):
        """AP-01: 'short armed @ EMA ¥12,000,000' 로그 → armed_direction/ema 설정."""
        import time as _time
        h = self._make_handler()
        self._emit(h, "[TrendMgr] btc_jpy: short armed @ EMA ¥12,000,000 (slope=-0.0610%)")
        assert h._state['armed_direction'] == 'short'
        assert h._state['armed_ema'] == 12_000_000.0
        assert h._state['armed_expire_at'] > _time.time()

    def test_ap02_long_armed_sets_state(self):
        """AP-02: 'long armed @ EMA ¥12,000,000' 로그 → armed_direction=long."""
        h = self._make_handler()
        self._emit(h, "[TrendMgr] btc_jpy: long armed @ EMA ¥12,000,000 (slope=0.0312%)")
        assert h._state['armed_direction'] == 'long'
        assert h._state['armed_ema'] == 12_000_000.0

    def test_ap03_armed_disarm_clears_state(self):
        """AP-03: 'armed 해제 (조건 소멸)' 로그 → armed 클리어."""
        h = self._make_handler()
        h._state['armed_direction'] = 'short'
        h._state['armed_ema'] = 12_000_000.0
        self._emit(h, "[TrendMgr] btc_jpy: armed 해제 (조건 소멸)")
        assert h._state['armed_direction'] is None
        assert h._state['armed_ema'] is None

    def test_ap04_arm_expire_clears_state(self):
        """AP-04: 'arm 만료 → 해제' 로그 → armed 클리어."""
        h = self._make_handler()
        h._state['armed_direction'] = 'long'
        h._state['armed_ema'] = 11_900_000.0
        self._emit(h, "[TrendMgr] btc_jpy: arm 만료 → 해제")
        assert h._state['armed_direction'] is None
        assert h._state['armed_ema'] is None

    def test_ap05_ws_trigger_clears_state(self):
        """AP-05: 'WS EMA 돌파 감지 ... → 진입 트리거' 로그 → armed 클리어."""
        h = self._make_handler()
        h._state['armed_direction'] = 'short'
        h._state['armed_ema'] = 12_000_000.0
        self._emit(
            h,
            "[TrendMgr] btc_jpy: WS EMA 돌파 감지 direction=short price=¥11990000 "
            "ema=¥12000000 → 진입 트리거",
        )
        assert h._state['armed_direction'] is None
        assert h._state['armed_ema'] is None


class TestSeedStrategyParamsExtended:
    """SP-01~SP-03: seed_telegram_strategy_params 확장 파라미터 주입."""

    def test_sp01_entry_mode_seeded(self):
        """SP-01: entry_mode 주입 → _state['entry_mode'] 갱신."""
        from core.shared.logging.telegram_handlers import _handlers, seed_telegram_strategy_params

        _handlers.clear()
        h = TelegramTransactionHandler("tok", "cid", exchange="GMO_COIN", domain="judge")
        _handlers.append(h)

        seed_telegram_strategy_params({"entry_mode": "ws_cross"})
        assert h._state['entry_mode'] == "ws_cross"
        _handlers.clear()

    def test_sp02_entry_timeframe_seeded(self):
        """SP-02: entry_timeframe 주입 → _state['entry_timeframe'] 갱신."""
        from core.shared.logging.telegram_handlers import _handlers, seed_telegram_strategy_params

        _handlers.clear()
        h = TelegramTransactionHandler("tok", "cid", exchange="GMO_COIN", domain="judge")
        _handlers.append(h)

        seed_telegram_strategy_params({"entry_timeframe": "1h"})
        assert h._state['entry_timeframe'] == "1h"
        _handlers.clear()

    def test_sp03_armed_expire_sec_seeded(self):
        """SP-03: armed_expire_sec 주입 → _state['armed_expire_sec'] 갱신."""
        from core.shared.logging.telegram_handlers import _handlers, seed_telegram_strategy_params

        _handlers.clear()
        h = TelegramTransactionHandler("tok", "cid", exchange="GMO_COIN", domain="judge")
        _handlers.append(h)

        seed_telegram_strategy_params({"armed_expire_sec": 7200})
        assert h._state['armed_expire_sec'] == 7200.0
        _handlers.clear()


# ─── trending_score ④번 조건 표시 ─────────────────────────────────────────

class TestTrendingScoreCondition:
    """TS-01~TS-05: trending_score 파싱 + 조건 블록 ④번 줄 표시."""

    def _make_handler(self):
        from core.shared.logging.telegram_handlers import TelegramTransactionHandler
        return TelegramTransactionHandler("tok", "cid", exchange="GMOC", domain="judge")

    def _run_summary(self, h) -> str:
        import asyncio
        from unittest.mock import AsyncMock, patch

        async def _inner():
            with patch(
                "core.shared.logging.telegram_handlers._send_telegram",
                new_callable=AsyncMock,
            ) as m:
                h._loop = asyncio.get_running_loop()
                await h._send_periodic_summary()
                return m.call_args[0][2]

        return asyncio.run(_inner())

    def _base_short_state(self, h):
        """숏 대기 기본 상태 (trending + ①②③ 충족)."""
        h._state['regime_status'] = 'trending'
        h._state['regime_consecutive'] = 5
        h._state['current_price'] = 12_100_000.0
        h._state['ema_price'] = 12_200_000.0      # current < ema → 숏
        h._state['ema_slope_pct'] = -0.10
        h._state['rsi'] = 45.0

    def test_ts01_parse_trending_score_from_log(self):
        """TS-01: 로그에서 trending_score=2 파싱 → _state 갱신."""
        h = self._make_handler()
        from core.shared.logging.telegram_handlers import _handlers
        _handlers.clear()
        _handlers.append(h)
        h._parse_and_update(
            "[Judge-Layer][c1][TrendMgr] btc_jpy: 대기 "
            "signal=long_caution ema_slope_pct=-0.0500 rsi=43.0 ema=12200000 "
            "price=12100000 trending_score=2"
        )
        assert h._state['trending_score'] == 2
        _handlers.clear()

    def test_ts02_parse_trending_score_na(self):
        """TS-02: trending_score=N/A → _state 변경 없음 (None 유지)."""
        h = self._make_handler()
        from core.shared.logging.telegram_handlers import _handlers
        _handlers.clear()
        _handlers.append(h)
        h._parse_and_update(
            "[Judge-Layer][c1][TrendMgr] btc_jpy: 대기 "
            "signal=long_caution ema_slope_pct=-0.0500 rsi=43.0 ema=12200000 "
            "price=12100000 trending_score=N/A"
        )
        assert h._state['trending_score'] is None
        _handlers.clear()

    def test_ts03_score_met_shows_check(self):
        """TS-03: trending_score=2 → ④ ✅ 표시."""
        h = self._make_handler()
        self._base_short_state(h)
        h._state['trending_score'] = 2
        text = self._run_summary(h)
        assert "④ 추세 강도" in text
        assert "score=2" in text
        assert "✅ ④ 추세 강도" in text

    def test_ts04_score_zero_shows_cross(self):
        """TS-04: trending_score=0 → ④ ❌ 표시 + 결론에 '추세강도' 언급."""
        h = self._make_handler()
        self._base_short_state(h)
        h._state['trending_score'] = 0
        text = self._run_summary(h)
        assert "④ 추세 강도" in text
        assert "score=0" in text
        assert "❌ ④ 추세 강도" in text
        assert "추세강도" in text

    def test_ts05_no_score_shows_unknown(self):
        """TS-05: trending_score=None → ④ ❓ 데이터 없음 표시."""
        h = self._make_handler()
        self._base_short_state(h)
        h._state['trending_score'] = None
        text = self._run_summary(h)
        assert "④ 추세 강도" in text
        assert "데이터 없음" in text
