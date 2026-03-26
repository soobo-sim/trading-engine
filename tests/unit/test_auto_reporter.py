"""AutoReporter 단위 테스트."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.task.auto_reporter import (
    AutoReporter,
    create_auto_reporter,
    send_telegram_message,
)


class TestSendTelegramMessage:
    """Telegram API 전송 테스트."""

    @pytest.mark.asyncio
    async def test_send_success(self):
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp

        result = await send_telegram_message("token123", "12345", "test msg", client=mock_client)

        assert result is True
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_4xx_no_retry(self):
        """4xx (429 제외)는 재시도하지 않음."""
        mock_resp = MagicMock(status_code=400, text="Bad Request")
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp

        result = await send_telegram_message("token123", "12345", "test msg", client=mock_client)

        assert result is False
        assert mock_client.post.call_count == 1  # 재시도 없음

    @pytest.mark.asyncio
    async def test_send_retry_on_5xx(self):
        """5xx는 재시도."""
        mock_fail = MagicMock(status_code=500, text="Internal Server Error")
        mock_ok = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.post.side_effect = [mock_fail, mock_ok]

        with patch("core.task.auto_reporter.asyncio.sleep", new_callable=AsyncMock):
            result = await send_telegram_message(
                "token123", "12345", "test msg",
                client=mock_client, backoff_base=0.01,
            )

        assert result is True
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_send_retry_exhausted(self):
        """재시도 소진 시 False."""
        mock_fail = MagicMock(status_code=500, text="error")
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_fail

        with patch("core.task.auto_reporter.asyncio.sleep", new_callable=AsyncMock):
            result = await send_telegram_message(
                "token123", "12345", "test msg",
                client=mock_client, max_retries=1, backoff_base=0.01,
            )

        assert result is False
        assert mock_client.post.call_count == 2  # 1 + 1 retry

    @pytest.mark.asyncio
    async def test_send_exception_retry(self):
        """네트워크 예외도 재시도."""
        mock_ok = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.post.side_effect = [Exception("network"), mock_ok]

        with patch("core.task.auto_reporter.asyncio.sleep", new_callable=AsyncMock):
            result = await send_telegram_message(
                "token123", "12345", "test msg",
                client=mock_client, backoff_base=0.01,
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_send_without_client_creates_own(self):
        """client 미전달 시 자체 생성."""
        mock_resp = MagicMock(status_code=200)
        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_resp

        with patch("core.task.auto_reporter.httpx.AsyncClient", return_value=mock_client_instance):
            result = await send_telegram_message("token123", "12345", "test msg")

        assert result is True
        mock_client_instance.aclose.assert_called_once()


class TestCreateAutoReporter:
    """팩토리 함수 테스트."""

    def test_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            result = create_auto_reporter(MagicMock(), MagicMock())
        assert result is None

    def test_disabled_explicit(self):
        with patch.dict("os.environ", {"AUTO_REPORT_ENABLED": "false"}):
            result = create_auto_reporter(MagicMock(), MagicMock())
        assert result is None

    def test_enabled_missing_token(self):
        env = {"AUTO_REPORT_ENABLED": "true", "AUTO_REPORT_CHAT_ID": "123"}
        with patch.dict("os.environ", env, clear=True):
            result = create_auto_reporter(MagicMock(), MagicMock())
        assert result is None

    def test_enabled_missing_chat_id(self):
        env = {"AUTO_REPORT_ENABLED": "true", "AUTO_REPORT_BOT_TOKEN": "tok"}
        with patch.dict("os.environ", env, clear=True):
            result = create_auto_reporter(MagicMock(), MagicMock())
        assert result is None

    def test_enabled_with_all_params(self):
        env = {
            "AUTO_REPORT_ENABLED": "true",
            "AUTO_REPORT_BOT_TOKEN": "tok",
            "AUTO_REPORT_CHAT_ID": "123",
            "AUTO_REPORT_INTERVAL_MIN": "5",
        }
        with patch.dict("os.environ", env, clear=True):
            result = create_auto_reporter(MagicMock(), MagicMock())
        assert result is not None
        assert isinstance(result, AutoReporter)
        assert result._interval_sec == 300


class TestAutoReporter:
    """AutoReporter 동작 테스트."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        reporter = AutoReporter(
            session_factory=MagicMock(),
            state=MagicMock(),
            bot_token="tok",
            chat_id="123",
            interval_min=1,
        )
        await reporter.start()
        assert reporter._task is not None
        assert not reporter._task.done()

        await reporter.stop()
        assert reporter._task.done()

    @pytest.mark.asyncio
    async def test_run_once_sends_telegram(self):
        """_run_once가 활성 전략의 보고를 생성하고 Telegram 전송하는지 확인."""
        from adapters.database.models import create_strategy_model
        StrategyModel = create_strategy_model("bf")

        # Mock DB — select()에 실제 모델을 넘기되, execute 결과만 mock
        mock_strategy = MagicMock()
        mock_strategy.parameters = {
            "pair": "BTC_JPY",
            "trading_style": "trend_following",
        }
        mock_strategy.name = "test"
        mock_strategy.id = 1

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_strategy]

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        mock_session_factory = MagicMock(return_value=mock_db)

        # Mock state — models.strategy는 실제 ORM 모델
        mock_state = MagicMock()
        mock_state.models.strategy = StrategyModel
        mock_state.prefix = "bf"
        mock_state.pair_column = "product_code"

        mock_safety = MagicMock()
        mock_safety.status = "all_ok"
        mock_safety.checks = []
        mock_state.health_checker.check_safety_only = AsyncMock(return_value=mock_safety)

        reporter = AutoReporter(
            session_factory=mock_session_factory,
            state=mock_state,
            bot_token="tok",
            chat_id="123",
        )

        fake_report = {
            "success": True,
            "report": {"telegram_text": "테스트 보고"},
        }

        with patch.object(reporter, "_generate_report", new_callable=AsyncMock, return_value=fake_report):
            with patch("core.task.auto_reporter.send_telegram_message", new_callable=AsyncMock, return_value=True) as mock_send:
                await reporter._run_once()

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        assert "테스트 보고" in call_args[0][2]

    @pytest.mark.asyncio
    async def test_loop_error_does_not_crash(self):
        """_loop에서 _run_once 예외 발생해도 크래시하지 않는지 확인."""
        from adapters.database.models import create_strategy_model
        StrategyModel = create_strategy_model("bf")

        mock_state = MagicMock()
        mock_state.models.strategy = StrategyModel

        reporter = AutoReporter(
            session_factory=MagicMock(),
            state=mock_state,
            bot_token="tok",
            chat_id="123",
            interval_min=1,
        )

        call_count = 0
        original_run_once = reporter._run_once

        async def failing_run_once():
            nonlocal call_count
            call_count += 1
            raise Exception("DB error")

        reporter._run_once = failing_run_once

        # _loop: sleep(interval) → _run_once → sleep(interval) → ...
        # Patch sleep to skip waits, cancel after first run
        with patch("core.task.auto_reporter.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            async def cancel_after_first(*args):
                if mock_sleep.call_count >= 2:
                    raise asyncio.CancelledError()

            mock_sleep.side_effect = cancel_after_first

            with pytest.raises(asyncio.CancelledError):
                await reporter._loop()

        assert call_count >= 1  # 예외 발생 후에도 루프가 계속됨


class TestFormatSafetySummary:
    """format_safety_summary n/a 제외 테스트."""

    def _make_report(self, checks, status="all_ok"):
        from core.monitoring.health import SafetyReport, SafetyCheck
        sc = [SafetyCheck(id=f"SF-{i+1:02d}", name=c[0], status=c[1], severity="critical", detail="")
              for i, c in enumerate(checks)]
        return SafetyReport(status=status, checks=sc, last_checked="2026-03-25T00:00:00Z")

    def test_all_ok_no_na(self):
        from core.monitoring.health import format_safety_summary
        r = self._make_report([("WS", "ok"), ("태스크", "ok")], "all_ok")
        s = format_safety_summary(r)
        assert "✅" in s
        assert "(2/2)" in s

    def test_all_ok_with_na(self):
        from core.monitoring.health import format_safety_summary
        r = self._make_report([("WS", "ok"), ("태스크", "ok"), ("사만사", "n/a")], "all_ok")
        s = format_safety_summary(r)
        assert "✅" in s
        assert "(2/2)" in s  # n/a excluded from denominator

    def test_warning_with_na(self):
        from core.monitoring.health import format_safety_summary
        r = self._make_report([("WS", "ok"), ("잔고", "warning"), ("사만사", "n/a")], "degraded")
        s = format_safety_summary(r)
        assert "🟡" in s
        assert "잔고" in s
        assert "(1/2)" in s  # 1 ok out of 2 active

    def test_critical(self):
        from core.monitoring.health import format_safety_summary
        r = self._make_report([("WS", "critical"), ("태스크", "ok")], "critical")
        s = format_safety_summary(r)
        assert "🔴" in s
        assert "WS" in s
        assert "(1/2)" in s
