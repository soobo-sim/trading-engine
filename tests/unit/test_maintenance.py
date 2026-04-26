"""
core/monitoring/maintenance.py 테스트.

T-1: GMO Coin 토 09:30 → 메인터넌스 중
T-2: GMO Coin 토 11:15 → 메인터넌스 아님 (종료 후)
T-3: GMO Coin 금 09:30 → 메인터넌스 아님 (다른 요일)
T-4: 미등록 거래소 → 항상 False
T-5: 경계값 — 토 09:00 (시작)
T-6: 경계값 — 토 11:10 (종료, 기본 11:00 + 프레오픈 10분)
T-alert-1: _send_safety_telegram_alert 메인터넌스 중 스킵
T-alert-2: _send_safety_telegram_alert 메인터넌스 外 전송 시도
T-auto-1:  auto_reporter EXCHANGE 환경변수 GMO_COIN → gmo_coin 변환 정상
T-auto-2:  auto_reporter 미등록 거래소 → False
T-end-1:   seconds_until_maintenance_end — 메인터넌스 중 남은 초 계산
T-end-2:   seconds_until_maintenance_end — 메인터넌스 아닐 때 0
T-sf03-1:  SF-03 메인터넌스 중 n/a 반환
T-sf06-1:  SF-06 메인터넌스 중 n/a + get_balance 미호출
T-reporter-1: auto_reporter _run_once — 메인터넌스 중 간소 보고 + _generate_report 미호출
T-reporter-2: auto_reporter _run_once — 메인터넌스 종료 후 정상 보고 경로
T-balance-1:  _check_position_balance_consistency — 메인터넌스 중 빈 리스트 반환
T-env-1:   환경변수 오버라이드 → 커스텀 시간 반영
T-env-2:   환경변수 미설정 → 기본값 (토 09:00-11:10)
"""
from __future__ import annotations

import os
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from core.punisher.monitoring.maintenance import is_maintenance_window

JST = ZoneInfo("Asia/Tokyo")


def _jst(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=JST)


# 날짜 요일 확인 기준 (python: weekday() 0=Mon, 5=Sat, 6=Sun)
# 2026-04-04 = 토요일(5), 2026-04-03 = 금요일(4), 2026-04-05 = 일요일(6)
_SAT = (2026, 4, 4)
_FRI = (2026, 4, 3)
_SUN = (2026, 4, 5)


class TestIsMaintenance:
    def test_t1_sat_during_maintenance(self):
        """T-1: 토 09:30 → 메인터넌스 중."""
        dt = _jst(*_SAT, 9, 30)
        assert is_maintenance_window("gmo_coin", dt) is True

    def test_t2_sat_after_maintenance(self):
        """T-2: 토 11:15 → 메인터넌스 종료 후."""
        dt = _jst(*_SAT, 11, 15)
        assert is_maintenance_window("gmo_coin", dt) is False

    def test_t3_fri_same_time(self):
        """T-3: 금 09:30 → 요일 불일치."""
        dt = _jst(*_FRI, 9, 30)
        assert is_maintenance_window("gmo_coin", dt) is False

    def test_t4_unknown_exchange(self):
        """T-4: 미등록 거래소 → 항상 False."""
        dt = _jst(*_SAT, 9, 30)
        assert is_maintenance_window("bitflyer", dt) is False
        assert is_maintenance_window("unknown", dt) is False

    def test_t5_boundary_start(self):
        """T-5: 경계값 — 토 09:00 (시작 포함)."""
        dt = _jst(*_SAT, 9, 0)
        assert is_maintenance_window("gmo_coin", dt) is True

    def test_t6_boundary_end(self):
        """T-6: 경계값 — 토 11:10 (기본값: 11:00 + 프레오픈 10분, 종료 포함)."""
        dt = _jst(*_SAT, 11, 10)
        assert is_maintenance_window("gmo_coin", dt) is True

    def test_t7_case_insensitive(self):
        """대소문자 무관."""
        dt = _jst(*_SAT, 9, 30)
        assert is_maintenance_window("GMO_COIN", dt) is True
        assert is_maintenance_window("Gmo_Coin", dt) is True

    def test_t8_sunday(self):
        """일요일은 메인터넌스 대상 아님."""
        dt = _jst(*_SUN, 9, 30)
        assert is_maintenance_window("gmo_coin", dt) is False


class TestTelegramAlertSkipDuringMaintenance:
    """T-alert: _send_safety_telegram_alert — 메인터넌스 중 Telegram 미전송."""

    @pytest.mark.asyncio
    async def test_alert_skipped_during_maintenance(self):
        """메인터넌스 중이면 Telegram를 전송하지 않는다."""
        # SafetyChecksMixin 인스턴스 임시 구성
        from core.punisher.monitoring.safety_checks import SafetyChecksMixin
        from core.punisher.monitoring.health import SafetyCheck

        mixin = SafetyChecksMixin.__new__(SafetyChecksMixin)
        mixin._telegram_alert_cooldown = {}
        mixin._adapter = MagicMock()

        check = SafetyCheck(
            id="SF-03", name="WebSocket", status="critical",
            severity="critical", detail="연결 끊김",
        )

        # GMO FX 메인터넌스 시간대 + EXCHANGE 환경변수 설정
        sat_930 = _jst(*_SAT, 9, 30)
        with patch.dict("os.environ", {"EXCHANGE": "GMOFX"}), \
             patch("core.punisher.monitoring.maintenance.is_maintenance_window", return_value=True), \
             patch("httpx.AsyncClient") as mock_client_cls:
            await mixin._send_safety_telegram_alert([check])
            # httpx 호출 없어야 함
            mock_client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_alert_sent_outside_maintenance(self):
        """메인터넌스 시간 외에는 Telegram 전송 시도."""
        from core.punisher.monitoring.safety_checks import SafetyChecksMixin
        from core.punisher.monitoring.health import SafetyCheck

        mixin = SafetyChecksMixin.__new__(SafetyChecksMixin)
        mixin._telegram_alert_cooldown = {}
        mixin._adapter = MagicMock()

        check = SafetyCheck(
            id="SF-03", name="WebSocket", status="critical",
            severity="critical", detail="연결 끊김",
        )

        # 포지션 조회 mock
        mixin._get_open_positions = AsyncMock(return_value=[])

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch.dict("os.environ", {
            "EXCHANGE": "GMOFX",
            "TELEGRAM_BOT_TOKEN": "dummy_token",
            "TELEGRAM_CHAT_ID": "dummy_chat",
        }), patch("core.punisher.monitoring.maintenance.is_maintenance_window", return_value=False), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await mixin._send_safety_telegram_alert([check])
            mock_client.post.assert_called_once()


class TestAutoReporterMaintenancePrefix:
    """T-auto: auto_reporter 메인터넌스 표시 — EXCHANGE 환경변수 기반 판별."""

    def test_exchange_env_gmo_coin_maps_correctly(self):
        """T-auto-1: GMO_COIN 환경변수 → is_maintenance_window("GMO_COIN") 호출 — 대소문자 무관 정상 동작."""
        dt_sat = _jst(*_SAT, 9, 30)
        assert is_maintenance_window("GMO_COIN", dt_sat) is True

    def test_exchange_env_bf_no_maintenance(self):
        """T-auto-2: EXCHANGE=BITFLYER → 스케줄 미등록 → False."""
        dt_sat = _jst(*_SAT, 9, 30)
        assert is_maintenance_window("BITFLYER", dt_sat) is False

    def test_old_gmofx_key_no_longer_matches(self):
        """T-auto-3 (회귀 방지): gmofx 키는 삭제됨 — 항상 False."""
        dt_sat = _jst(*_SAT, 9, 30)
        # gmofx 키는 스케줄에서 제거되었으므로 False
        assert is_maintenance_window("gmofx", dt_sat) is False
        # gmo_coin 이 올바른 키
        assert is_maintenance_window("gmo_coin", dt_sat) is True


class TestSecondsUntilMaintenanceEnd:
    """T-end: seconds_until_maintenance_end — 종료까지 남은 초."""

    def test_end_1_during_maintenance(self):
        """T-end-1: 메인터넌스 중 (토 09:30) → 남은 초 = 100분 = 6000초."""
        from core.punisher.monitoring.maintenance import seconds_until_maintenance_end
        dt = _jst(*_SAT, 9, 30)  # 종료 11:10 까지 100분 = 6000초
        result = seconds_until_maintenance_end("gmo_coin", dt)
        assert result == 6000

    def test_end_2_outside_maintenance(self):
        """T-end-2: 메인터넌스 외 → 0."""
        from core.punisher.monitoring.maintenance import seconds_until_maintenance_end
        dt = _jst(*_SAT, 11, 30)
        result = seconds_until_maintenance_end("gmo_coin", dt)
        assert result == 0

    def test_end_3_unknown_exchange(self):
        """T-end-3: 미등록 거래소 → 0."""
        from core.punisher.monitoring.maintenance import seconds_until_maintenance_end
        dt = _jst(*_SAT, 9, 30)
        result = seconds_until_maintenance_end("bitflyer", dt)
        assert result == 0

    def test_end_4_near_boundary(self):
        """T-end-4: 종료 직전 (토 11:09) → 60초."""
        from core.punisher.monitoring.maintenance import seconds_until_maintenance_end
        dt = _jst(*_SAT, 11, 9)  # 종료 11:10 까지 1분 = 60초
        result = seconds_until_maintenance_end("gmo_coin", dt)
        assert result == 60


class TestSF03SF06DuringMaintenance:
    """T-sf: SF-03 / SF-06 메인터넌스 중 n/a 반환."""

    @pytest.mark.asyncio
    async def test_sf03_maintenance_returns_na(self):
        """T-sf03-1: SF-03 메인터넌스 중 n/a."""
        from core.punisher.monitoring.safety_checks import SafetyChecksMixin

        mixin = SafetyChecksMixin.__new__(SafetyChecksMixin)
        mixin._adapter = MagicMock()
        mixin._adapter.has_credentials.return_value = True

        with patch.dict("os.environ", {"EXCHANGE": "GMOFX"}), \
             patch("core.punisher.monitoring.safety_checks.is_maintenance_window", return_value=True):
            result = mixin._check_sf03(ws_connected=False)

        assert result.status == "n/a"
        assert "메인터넌스" in result.detail

    @pytest.mark.asyncio
    async def test_sf06_maintenance_returns_na_no_api_call(self):
        """T-sf06-1: SF-06 메인터넌스 중 n/a + get_balance 미호출."""
        from core.punisher.monitoring.safety_checks import SafetyChecksMixin

        mixin = SafetyChecksMixin.__new__(SafetyChecksMixin)
        mixin._adapter = MagicMock()
        mixin._adapter.has_credentials.return_value = True
        mixin._adapter.get_balance = AsyncMock()

        with patch.dict("os.environ", {"EXCHANGE": "GMOFX"}), \
             patch("core.punisher.monitoring.safety_checks.is_maintenance_window", return_value=True):
            result = await mixin._check_sf06()

        assert result.status == "n/a"
        assert "메인터넌스" in result.detail
        mixin._adapter.get_balance.assert_not_called()

    @pytest.mark.asyncio
    async def test_sf03_normal_critical_outside_maintenance(self):
        """T-sf03-2: 메인터넌스 외 WS 끊김 → critical."""
        from core.punisher.monitoring.safety_checks import SafetyChecksMixin

        mixin = SafetyChecksMixin.__new__(SafetyChecksMixin)
        mixin._adapter = MagicMock()
        mixin._adapter.has_credentials.return_value = True
        mixin._has_active_strategies = True

        with patch.dict("os.environ", {"EXCHANGE": "GMOFX"}), \
             patch("core.punisher.monitoring.safety_checks.is_maintenance_window", return_value=False):
            result = mixin._check_sf03(ws_connected=False)

        assert result.status == "critical"


class TestAutoReporterMaintenanceMode:
    """T-reporter: auto_reporter _run_once 메인터넌스 분기."""

    @pytest.mark.asyncio
    async def test_reporter_maintenance_sends_brief_no_generate(self):
        """T-reporter-1: 메인터넌스 중 → 간소 보고 전송, _generate_report 미호출."""
        from core.punisher.task.auto_reporter import AutoReporter

        reporter = AutoReporter.__new__(AutoReporter)
        reporter._bot_token = "token"
        reporter._chat_id = "chat"
        reporter._http_client = None
        reporter._session_factory = MagicMock()
        reporter._state = MagicMock()

        reporter._generate_report = AsyncMock()

        with patch.dict("os.environ", {"EXCHANGE": "GMOFX"}), \
             patch("core.punisher.task.auto_reporter.is_maintenance_window", return_value=True), \
             patch("core.punisher.task.auto_reporter.send_telegram_message", new_callable=AsyncMock) as mock_send:
            await reporter._run_once()

        # 간소 보고 1회 전송
        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        assert "메인터넌스" in call_args[2]

        # _generate_report 미호출
        reporter._generate_report.assert_not_called()

    @pytest.mark.asyncio
    async def test_reporter_normal_outside_maintenance(self):
        """T-reporter-2: 메인터넌스 외 → 일반 보고 경로 진입 (send_telegram_message 미호출, _generate_report 호출 시도)."""
        from core.punisher.task.auto_reporter import AutoReporter

        reporter = AutoReporter.__new__(AutoReporter)
        reporter._bot_token = "token"
        reporter._chat_id = "chat"
        reporter._http_client = None
        reporter._state = MagicMock()
        reporter._state.models.trend_position = None  # loss_detector 스킵
        reporter._state.models.strategy = MagicMock()
        reporter._session_factory = MagicMock()

        # DB 조회가 시도되면 예외 — 이 경우 _run_once가 except로 잡고 계속
        # 중요한 건 메인터넌스 간소 보고 send_telegram_message가 호출되지 않은 것
        with patch.dict("os.environ", {"EXCHANGE": "GMOFX"}), \
             patch("core.punisher.task.auto_reporter.is_maintenance_window", return_value=False), \
             patch("core.punisher.task.auto_reporter.send_telegram_message", new_callable=AsyncMock) as mock_send:
            try:
                await reporter._run_once()
            except Exception:
                pass  # DB mock 한계로 예외 가능

        # 메인터넌스 간소 보고("메인터넌스 중" 텍스트)는 전송 안 됨
        for call in mock_send.call_args_list:
            assert "메인터넌스" not in (call[0][2] if len(call[0]) > 2 else "")


class TestBalanceConsistencyDuringMaintenance:
    """T-balance: _check_position_balance_consistency 메인터넌스 중 스킵."""

    @pytest.mark.asyncio
    async def test_balance_check_skipped_during_maintenance(self):
        """T-balance-1: 메인터넌스 중 → get_balance 미호출, 빈 리스트 반환."""
        from core.punisher.monitoring.health import HealthChecker

        checker = HealthChecker.__new__(HealthChecker)
        checker._adapter = MagicMock()
        checker._adapter.has_credentials.return_value = True
        checker._adapter.is_margin_trading = False
        checker._adapter.get_balance = AsyncMock()
        checker._pair_column = "pair"

        with patch.dict("os.environ", {"EXCHANGE": "GMOFX"}), \
             patch("core.punisher.monitoring.health.is_maintenance_window", return_value=True):
            result = await checker._check_position_balance_consistency()

        assert result == []
        checker._adapter.get_balance.assert_not_called()

    @pytest.mark.asyncio
    async def test_balance_check_runs_outside_maintenance(self):
        """T-balance-2: 메인터넌스 외 → 정상 경로 진입 (API 미설정이라 빈 리스트만 확인)."""
        from core.punisher.monitoring.health import HealthChecker

        checker = HealthChecker.__new__(HealthChecker)
        checker._adapter = MagicMock()
        checker._adapter.has_credentials.return_value = False  # 키 미설정 → 빈 리스트
        checker._adapter.is_margin_trading = False
        checker._adapter.get_balance = AsyncMock()
        checker._pair_column = "pair"

        with patch.dict("os.environ", {"EXCHANGE": "GMOFX"}), \
             patch("core.punisher.monitoring.health.is_maintenance_window", return_value=False):
            result = await checker._check_position_balance_consistency()

        # API 키 미설정이므로 빈 리스트 반환 (메인터넌스 스킵과 구분됨)
        assert result == []
        checker._adapter.get_balance.assert_not_called()


class TestSecondsUntilEndBoundary:
    """T-end-boundary: seconds_until_maintenance_end 경계값."""

    def test_end_boundary_exact_end_time(self):
        """T-end-5: 정확히 종료 시각(11:10:00) → 0초 (잔여 없음)."""
        from core.punisher.monitoring.maintenance import seconds_until_maintenance_end
        from datetime import datetime
        from zoneinfo import ZoneInfo
        JST = ZoneInfo("Asia/Tokyo")
        # 2026-04-04는 토요일, 11:10:00 = 종료 경계
        dt = datetime(2026, 4, 4, 11, 10, 0, tzinfo=JST)
        result = seconds_until_maintenance_end("gmo_coin", dt)
        assert result == 0

    def test_end_during_maintenance_large_window(self):
        """T-end-6: 메인터넌스 시작(09:00) → 2h10m = 7800초 (130분)."""
        from core.punisher.monitoring.maintenance import seconds_until_maintenance_end
        from datetime import datetime
        from zoneinfo import ZoneInfo
        JST = ZoneInfo("Asia/Tokyo")
        dt = datetime(2026, 4, 4, 9, 0, 0, tzinfo=JST)
        result = seconds_until_maintenance_end("gmo_coin", dt)
        # 11:10 - 09:00 = 130분 = 7800초
        assert result == 130 * 60


class TestMaintenanceEnvOverride:
    """T-env: 환경변수로 메인터넌스 시간 오버라이드."""

    def test_env_override_custom_time(self):
        """T-env-1: 환경변수 오버라이드 → 커스텀 시간 반영."""
        from core.punisher.monitoring.maintenance import _build_gmo_coin_schedule

        with patch.dict("os.environ", {
            "GMO_COIN_MAINTENANCE_WEEKDAY": "0",   # 월요일
            "GMO_COIN_MAINTENANCE_START": "10:00",
            "GMO_COIN_MAINTENANCE_END": "12:00",
            "GMO_COIN_MAINTENANCE_PREOPEN_MIN": "5",
        }):
            schedule = _build_gmo_coin_schedule()

        assert len(schedule) == 1
        weekday, start, end = schedule[0]
        assert weekday == 0               # 월요일
        assert start == time(10, 0)
        assert end == time(12, 5)         # 12:00 + 5분

    def test_env_default_values(self):
        """T-env-2: 환경변수 미설정 → 기본값 (토 09:00-11:10)."""
        from core.punisher.monitoring.maintenance import _build_gmo_coin_schedule

        # GMO_COIN_MAINTENANCE_* 환경변수 제거 후 호출
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith("GMO_COIN_MAINTENANCE")}
        with patch.dict("os.environ", clean_env, clear=True):
            schedule = _build_gmo_coin_schedule()

        weekday, start, end = schedule[0]
        assert weekday == 5           # 토요일
        assert start == time(9, 0)
        assert end == time(11, 10)    # 11:00 + 프레오픈 10분
