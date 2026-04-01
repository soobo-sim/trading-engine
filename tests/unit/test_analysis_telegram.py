"""
analysis_telegram.py 단위 테스트.
포매팅 로직 + Telegram 전송 동작 검증.
"""
import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone, timedelta

from core.notifications.analysis_telegram import (
    format_analysis_report_message,
    send_analysis_report_telegram,
    AGENT_ORDER,
)

JST = timezone(timedelta(hours=9))

# ─── 공통 픽스처 ──────────────────────────────────────────────

ANALYSES_FULL = [
    {"agent_name": "alice", "summary": "상승 추세 명확, EMA↑ RSI 52"},
    {"agent_name": "samantha", "summary": "리스크 중간, ATR 안정"},
    {"agent_name": "rachel", "summary": "진입 승인 (포지션 70%)"},
]


# ─── format_analysis_report_message ──────────────────────────

class TestFormatAnalysisReportMessage:
    def _fmt(self, **kwargs):
        defaults = dict(
            report_type="daily",
            currency_pair="USD_JPY",
            reported_at="2026-04-01T09:00:00+09:00",
            final_decision="approved",
            strategy_active=True,
            analyses=ANALYSES_FULL,
        )
        defaults.update(kwargs)
        return format_analysis_report_message(**defaults)

    def test_contains_pair(self):
        msg = self._fmt()
        assert "USD/JPY" in msg

    def test_daily_label(self):
        msg = self._fmt(report_type="daily")
        assert "매일" in msg

    def test_weekly_label(self):
        msg = self._fmt(report_type="weekly")
        assert "주간" in msg

    def test_monthly_label(self):
        msg = self._fmt(report_type="monthly")
        assert "월간" in msg

    def test_agent_summaries_included(self):
        msg = self._fmt()
        assert "상승 추세 명확" in msg
        assert "리스크 중간" in msg
        assert "진입 승인" in msg

    def test_agent_order_fixed(self):
        """alice → samantha → rachel 순서."""
        msg = self._fmt()
        idx_alice = msg.index("Alice")
        idx_sam = msg.index("Samantha")
        idx_rachel = msg.index("Rachel")
        assert idx_alice < idx_sam < idx_rachel

    def test_approved_decision_emoji(self):
        msg = self._fmt(final_decision="approved")
        assert "✅" in msg

    def test_rejected_decision_emoji(self):
        msg = self._fmt(final_decision="rejected")
        assert "❌" in msg

    def test_hold_decision_emoji(self):
        msg = self._fmt(final_decision="hold")
        assert "⏸️" in msg

    def test_no_decision(self):
        msg = self._fmt(final_decision=None)
        assert "📋 결정" not in msg

    def test_strategy_active_badge(self):
        msg = self._fmt(strategy_active=True)
        assert "🟢" in msg

    def test_strategy_inactive_badge(self):
        msg = self._fmt(strategy_active=False)
        assert "⚪" in msg

    def test_partial_analyses_no_error(self):
        """에이전트가 1명만 있어도 에러 없이 포맷."""
        msg = self._fmt(analyses=[{"agent_name": "alice", "summary": "테스트"}])
        assert "Alice" in msg
        # samantha/rachel는 라인 미삽입 (KeyError 없음)

    def test_empty_analyses_no_error(self):
        msg = self._fmt(analyses=[])
        assert "USD/JPY" in msg  # 최소한 페어는 표시

    def test_datetime_object_accepted(self):
        """reported_at으로 datetime 객체 전달."""
        dt = datetime(2026, 4, 1, 9, 0, tzinfo=JST)
        msg = format_analysis_report_message(
            report_type="daily",
            currency_pair="USD_JPY",
            reported_at=dt,
            final_decision=None,
            strategy_active=False,
            analyses=[],
        )
        assert "2026-04-01" in msg

    def test_currency_pair_underscore_converted(self):
        """USD_JPY → USD/JPY."""
        msg = self._fmt(currency_pair="GBP_JPY")
        assert "GBP/JPY" in msg
        assert "GBP_JPY" not in msg


# ─── send_analysis_report_telegram ───────────────────────────

class TestSendAnalysisReportTelegram:
    async def _send(self, env_patch: dict, **kwargs):
        defaults = dict(
            report_type="daily",
            currency_pair="USD_JPY",
            reported_at="2026-04-01T09:00:00+09:00",
            final_decision="approved",
            strategy_active=True,
            analyses=ANALYSES_FULL,
        )
        defaults.update(kwargs)
        with patch.dict("os.environ", env_patch, clear=False):
            return await send_analysis_report_telegram(**defaults)

    @pytest.mark.asyncio
    async def test_skip_when_no_token(self):
        """BOT_TOKEN 미설정 시 skip → True."""
        result = await self._send({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
        assert result is True

    @pytest.mark.asyncio
    async def test_skip_when_no_chat_id(self):
        """CHAT_ID 미설정 시 skip → True."""
        result = await self._send({"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": ""})
        assert result is True

    @pytest.mark.asyncio
    async def test_calls_send_telegram_message_when_configured(self):
        """토큰+채팅ID 설정 시 send_telegram_message 호출."""
        mock_send = AsyncMock(return_value=True)
        with patch("core.notifications.analysis_telegram.send_telegram_message", mock_send):
            result = await self._send(
                {"TELEGRAM_BOT_TOKEN": "tok123", "TELEGRAM_CHAT_ID": "chat456"}
            )
        assert result is True
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args.kwargs
        assert call_kwargs["bot_token"] == "tok123"
        assert call_kwargs["chat_id"] == "chat456"
        assert "USD/JPY" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_returns_false_on_send_failure(self):
        """send_telegram_message가 False 반환 시 False."""
        mock_send = AsyncMock(return_value=False)
        with patch("core.notifications.analysis_telegram.send_telegram_message", mock_send):
            result = await self._send(
                {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "chat"}
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        """send_telegram_message 예외 시 False (호출자 crash 방지)."""
        mock_send = AsyncMock(side_effect=Exception("네트워크 오류"))
        with patch("core.notifications.analysis_telegram.send_telegram_message", mock_send):
            result = await self._send(
                {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "chat"}
            )
        assert result is False


# ─── POST /reports → fire-and-forget 동작 검증 ───────────────

class TestCreateReportFireAndForget:
    """라우트 레벨: 성공 시 전송 함수 create_task, 실패(409) 시 미호출."""

    @pytest.mark.asyncio
    async def test_success_triggers_telegram(self):
        from unittest.mock import MagicMock, AsyncMock, patch

        fake_result = {"id": 1, "currency_pair": "USD_JPY"}
        mock_create_report = AsyncMock(return_value=fake_result)
        mock_send = AsyncMock(return_value=True)
        mock_task = MagicMock()

        with (
            patch("api.services.strategy_analysis_service.create_report", mock_create_report),
            patch("core.notifications.analysis_telegram.send_analysis_report_telegram", mock_send),
            patch("asyncio.create_task", mock_task),
        ):
            # create_task에 코루틴이 전달됐는지 검증
            from api.routes.strategy_analysis import create_report as route_create
            from unittest.mock import MagicMock as MM

            # DB mock
            db = MM()
            db.__aenter__ = AsyncMock(return_value=db)
            db.__aexit__ = AsyncMock(return_value=None)

            # body mock
            body = MM()
            body.exchange = "gmofx"
            body.currency_pair = "USD_JPY"
            body.report_type = "daily"
            body.reported_at = "2026-04-01T09:00:00+09:00"
            body.strategy_active = True
            body.strategy_id = None
            body.final_decision = "approved"
            body.final_rationale = None
            body.next_review = None
            body.analyses = []

            await route_create(body=body, db=db)

        mock_task.assert_called_once()
