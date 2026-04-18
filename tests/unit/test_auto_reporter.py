"""AutoReporter 단위 테스트."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.punisher.task.auto_reporter import (
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

        with patch("core.punisher.task.auto_reporter.asyncio.sleep", new_callable=AsyncMock):
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

        with patch("core.punisher.task.auto_reporter.asyncio.sleep", new_callable=AsyncMock):
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

        with patch("core.punisher.task.auto_reporter.asyncio.sleep", new_callable=AsyncMock):
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

        with patch("core.punisher.task.auto_reporter.httpx.AsyncClient", return_value=mock_client_instance):
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
        mock_state.normalize_pair = lambda p: p.lower()
        # trend_manager._regime_gate.active_strategy = None → regime 필터 미적용
        mock_state.trend_manager._regime_gate.active_strategy = None

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
            with patch.object(reporter, "_has_open_position", new_callable=AsyncMock, return_value=False):
                with patch("core.punisher.task.auto_reporter.send_telegram_message", new_callable=AsyncMock, return_value=True) as mock_send:
                    with patch("core.punisher.task.auto_reporter.is_maintenance_window", return_value=False):
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
        with patch("core.punisher.task.auto_reporter.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            async def cancel_after_first(*args):
                if mock_sleep.call_count >= 2:
                    raise asyncio.CancelledError()

            mock_sleep.side_effect = cancel_after_first

            with pytest.raises(asyncio.CancelledError):
                await reporter._loop()

        assert call_count >= 1  # 예외 발생 후에도 루프가 계속됨


class TestAutoReporterRegimeFilter:
    """AR-R01~R05: RegimeGate 체제 필터 — 체제에 맞지 않는 전략 보고 스킵."""

    def _make_reporter(self, state):
        return AutoReporter(
            session_factory=MagicMock(),
            state=state,
            bot_token="tok",
            chat_id="123",
        )

    def _make_state(self, regime_active_strategy):
        """Mock AppState 생성 헬퍼.

        Args:
            regime_active_strategy: RegimeGate.active_strategy 값 ("trend_following" / "box_mean_reversion" / None)
        """
        from adapters.database.models import create_strategy_model
        StrategyModel = create_strategy_model("gmoc")

        # RegimeGate mock
        mock_gate = MagicMock()
        mock_gate.active_strategy = regime_active_strategy

        # trend_manager
        mock_trend_mgr = MagicMock()
        mock_trend_mgr._regime_gate = mock_gate

        # AppState
        mock_state = MagicMock()
        mock_state.trend_manager = mock_trend_mgr
        mock_state.models.strategy = StrategyModel
        mock_state.prefix = "gmoc"
        mock_state.pair_column = "pair"
        mock_state.normalize_pair = lambda p: p.lower()

        mock_safety = MagicMock()
        mock_safety.status = "all_ok"
        mock_safety.checks = []
        mock_state.health_checker.check_safety_only = AsyncMock(return_value=mock_safety)

        return mock_state

    def _make_db_with_strategies(self, styles: list[str]):
        """지정된 trading_style 목록에 해당하는 활성 전략 mock DB 반환."""
        strategies = []
        for style in styles:
            s = MagicMock()
            s.parameters = {"pair": "btc_jpy", "trading_style": style}
            s.name = f"test_{style}"
            s.id = styles.index(style) + 1
            strategies.append(s)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = strategies

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=mock_db)

    @pytest.mark.asyncio
    async def test_ar_r01_trend_active_skips_box(self):
        """AR-R01: regime=trend_following 확정 시 box 포지션 없으면 스킵."""
        state = self._make_state(regime_active_strategy="trend_following")
        reporter = self._make_reporter(state)
        reporter._session_factory = self._make_db_with_strategies(
            ["trend_following", "box_mean_reversion"]
        )

        fake_report = {"success": True, "report": {"telegram_text": "보고"}}
        generated_styles = []

        async def mock_generate(style, pair, strategy, state, db):
            generated_styles.append(style)
            return fake_report

        async def mock_has_open_pos(style, pair, db):
            return False  # box 포지션 없음

        with patch.object(reporter, "_generate_report", side_effect=mock_generate):
            with patch.object(reporter, "_has_open_position", side_effect=mock_has_open_pos):
                with patch("core.punisher.task.auto_reporter.send_telegram_message", new_callable=AsyncMock, return_value=True):
                    with patch("core.punisher.task.auto_reporter.is_maintenance_window", return_value=False):
                        await reporter._run_once()

        assert generated_styles == ["trend_following"], (
            f"trend만 보고 생성되어야 함, 실제: {generated_styles}"
        )

    @pytest.mark.asyncio
    async def test_ar_r02_box_active_skips_trend(self):
        """AR-R02: regime=box_mean_reversion 확정 시 trend 포지션 없으면 스킵."""
        state = self._make_state(regime_active_strategy="box_mean_reversion")
        reporter = self._make_reporter(state)
        reporter._session_factory = self._make_db_with_strategies(
            ["trend_following", "box_mean_reversion"]
        )

        fake_report = {"success": True, "report": {"telegram_text": "보고"}}
        generated_styles = []

        async def mock_generate(style, pair, strategy, state, db):
            generated_styles.append(style)
            return fake_report

        async def mock_has_open_pos(style, pair, db):
            return False  # trend 포지션 없음

        with patch.object(reporter, "_generate_report", side_effect=mock_generate):
            with patch.object(reporter, "_has_open_position", side_effect=mock_has_open_pos):
                with patch("core.punisher.task.auto_reporter.send_telegram_message", new_callable=AsyncMock, return_value=True):
                    with patch("core.punisher.task.auto_reporter.is_maintenance_window", return_value=False):
                        await reporter._run_once()

        assert generated_styles == ["box_mean_reversion"], (
            f"box만 보고 생성되어야 함, 실제: {generated_styles}"
        )

    @pytest.mark.asyncio
    async def test_ar_r03_skipped_strategy_with_position_still_reports(self):
        """AR-R03: regime=trend_following이지만 DB에 box 포지션 있으면 box도 보고."""
        state = self._make_state(regime_active_strategy="trend_following")
        reporter = self._make_reporter(state)
        reporter._session_factory = self._make_db_with_strategies(
            ["trend_following", "box_mean_reversion"]
        )

        fake_report = {"success": True, "report": {"telegram_text": "보고"}}
        generated_styles = []

        async def mock_generate(style, pair, strategy, state, db):
            generated_styles.append(style)
            return fake_report

        async def mock_has_open_pos(style, pair, db):
            return style == "box_mean_reversion"  # DB에 box 포지션 있음

        with patch.object(reporter, "_generate_report", side_effect=mock_generate):
            with patch.object(reporter, "_has_open_position", side_effect=mock_has_open_pos):
                with patch("core.punisher.task.auto_reporter.send_telegram_message", new_callable=AsyncMock, return_value=True):
                    with patch("core.punisher.task.auto_reporter.is_maintenance_window", return_value=False):
                        await reporter._run_once()

        assert set(generated_styles) == {"trend_following", "box_mean_reversion"}, (
            f"DB 포지션 보유 중인 box도 보고되어야 함, 실제: {generated_styles}"
        )

    @pytest.mark.asyncio
    async def test_ar_r04_warmup_reports_both(self):
        """AR-R04: active_strategy=None (warm-up 중) → 양쪽 다 보고."""
        state = self._make_state(regime_active_strategy=None)
        reporter = self._make_reporter(state)
        reporter._session_factory = self._make_db_with_strategies(
            ["trend_following", "box_mean_reversion"]
        )

        fake_report = {"success": True, "report": {"telegram_text": "보고"}}
        generated_styles = []

        async def mock_generate(style, pair, strategy, state, db):
            generated_styles.append(style)
            return fake_report

        with patch.object(reporter, "_generate_report", side_effect=mock_generate):
            with patch("core.punisher.task.auto_reporter.send_telegram_message", new_callable=AsyncMock, return_value=True):
                with patch("core.punisher.task.auto_reporter.is_maintenance_window", return_value=False):
                    await reporter._run_once()

        assert set(generated_styles) == {"trend_following", "box_mean_reversion"}, (
            f"warm-up 중에는 양쪽 다 보고되어야 함, 실제: {generated_styles}"
        )

    @pytest.mark.asyncio
    async def test_ar_r05_no_regime_gate_reports_both(self):
        """AR-R05: _regime_gate 미설정 시 기존 동작 유지 (양쪽 다 보고)."""
        state = self._make_state(regime_active_strategy="trend_following")
        # trend_manager에서 _regime_gate 제거
        del state.trend_manager._regime_gate

        reporter = self._make_reporter(state)
        reporter._session_factory = self._make_db_with_strategies(
            ["trend_following", "box_mean_reversion"]
        )

        fake_report = {"success": True, "report": {"telegram_text": "보고"}}
        generated_styles = []

        async def mock_generate(style, pair, strategy, state, db):
            generated_styles.append(style)
            return fake_report

        with patch.object(reporter, "_generate_report", side_effect=mock_generate):
            with patch("core.punisher.task.auto_reporter.send_telegram_message", new_callable=AsyncMock, return_value=True):
                with patch("core.punisher.task.auto_reporter.is_maintenance_window", return_value=False):
                    await reporter._run_once()

        assert set(generated_styles) == {"trend_following", "box_mean_reversion"}, (
            f"regime_gate 없으면 필터 미적용, 실제: {generated_styles}"
        )

    @pytest.mark.asyncio
    async def test_ar_e1_no_trend_manager_reports_both(self):
        """AR-E1: state에 trend_manager 없으면 필터 미적용 (안전 폴백)."""
        state = self._make_state(regime_active_strategy="trend_following")
        del state.trend_manager  # trend_manager 제거

        reporter = self._make_reporter(state)
        reporter._session_factory = self._make_db_with_strategies(
            ["trend_following", "box_mean_reversion"]
        )

        generated_styles = []
        fake_report = {"success": True, "report": {"telegram_text": "보고"}}

        async def mock_generate(style, pair, strategy, state, db):
            generated_styles.append(style)
            return fake_report

        with patch.object(reporter, "_generate_report", side_effect=mock_generate):
            with patch("core.punisher.task.auto_reporter.send_telegram_message", new_callable=AsyncMock, return_value=True):
                with patch("core.punisher.task.auto_reporter.is_maintenance_window", return_value=False):
                    await reporter._run_once()

        assert set(generated_styles) == {"trend_following", "box_mean_reversion"}, (
            f"trend_manager 없으면 양쪽 다 보고, 실제: {generated_styles}"
        )

    @pytest.mark.asyncio
    async def test_ar_e2_db_error_returns_false_skips(self):
        """AR-E2: _has_open_position DB 오류 → False → 체제 불일치 전략 스킵."""
        state = self._make_state(regime_active_strategy="trend_following")
        reporter = self._make_reporter(state)
        reporter._session_factory = self._make_db_with_strategies(
            ["trend_following", "box_mean_reversion"]
        )

        generated_styles = []
        fake_report = {"success": True, "report": {"telegram_text": "보고"}}

        async def mock_generate(style, pair, strategy, state, db):
            generated_styles.append(style)
            return fake_report

        async def mock_has_open_pos_error(style, pair, db):
            # DB 오류 시 False 반환 (에러 처리는 _has_open_position 내부)
            return False

        with patch.object(reporter, "_generate_report", side_effect=mock_generate):
            with patch.object(reporter, "_has_open_position", side_effect=mock_has_open_pos_error):
                with patch("core.punisher.task.auto_reporter.send_telegram_message", new_callable=AsyncMock, return_value=True):
                    with patch("core.punisher.task.auto_reporter.is_maintenance_window", return_value=False):
                        await reporter._run_once()

        assert generated_styles == ["trend_following"], (
            f"DB 오류 → False → box 스킵되어야 함, 실제: {generated_styles}"
        )

    @pytest.mark.asyncio
    async def test_ar_e3_unknown_style_skips(self):
        """AR-E3: 알 수 없는 trading_style은 _has_open_position=False → 체제 불일치 시 스킵."""
        state = self._make_state(regime_active_strategy="trend_following")
        reporter = self._make_reporter(state)
        reporter._session_factory = self._make_db_with_strategies(
            ["trend_following", "unknown_custom_style"]
        )

        generated_styles = []
        fake_report = {"success": True, "report": {"telegram_text": "보고"}}

        async def mock_generate(style, pair, strategy, state, db):
            generated_styles.append(style)
            return fake_report

        async def mock_has_open_pos(style, pair, db):
            return False  # 알 수 없는 style → False

        with patch.object(reporter, "_generate_report", side_effect=mock_generate):
            with patch.object(reporter, "_has_open_position", side_effect=mock_has_open_pos):
                with patch("core.punisher.task.auto_reporter.send_telegram_message", new_callable=AsyncMock, return_value=True):
                    with patch("core.punisher.task.auto_reporter.is_maintenance_window", return_value=False):
                        await reporter._run_once()

        assert generated_styles == ["trend_following"], (
            f"알 수 없는 style은 스킵되어야 함, 실제: {generated_styles}"
        )


class TestHasOpenPosition:
    """AR-P01~P05: _has_open_position DB 기반 포지션 조회 단위 테스트."""

    def _make_reporter(self, state):
        return AutoReporter(
            session_factory=MagicMock(),
            state=state,
            bot_token="tok",
            chat_id="123",
        )

    def _make_state_with_models(self, has_trend_pos=False, has_box_pos=False):
        """DB mock이 포함된 AppState 생성."""
        from adapters.database.models import (
            create_trend_position_model,
            create_box_position_model,
        )
        TrendPos = create_trend_position_model("gmoc")
        BoxPos = create_box_position_model("gmoc", pair_column="pair")

        mock_state = MagicMock()
        mock_state.pair_column = "pair"
        mock_state.models.trend_position = TrendPos
        mock_state.models.box_position = BoxPos
        mock_state.models.cfd_position = None
        return mock_state

    def _make_db(self, row_id=None):
        """scalar_one_or_none이 row_id 또는 None을 반환하는 DB mock."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = row_id

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=mock_db)

    @pytest.mark.asyncio
    async def test_ar_p01_box_no_db_position_returns_false(self):
        """AR-P01: gmoc_box_positions 미청산 0건 → False."""
        state = self._make_state_with_models()
        reporter = self._make_reporter(state)
        reporter._session_factory = self._make_db(row_id=None)

        mock_db = AsyncMock()
        mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        result = await reporter._has_open_position("box_mean_reversion", "btc_jpy", mock_db)
        assert result is False

    @pytest.mark.asyncio
    async def test_ar_p02_box_has_db_position_returns_true(self):
        """AR-P02: gmoc_box_positions 미청산 1건 → True."""
        state = self._make_state_with_models()
        reporter = self._make_reporter(state)

        mock_db = AsyncMock()
        mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=42))

        result = await reporter._has_open_position("box_mean_reversion", "btc_jpy", mock_db)
        assert result is True

    @pytest.mark.asyncio
    async def test_ar_p03_trend_has_db_position_returns_true(self):
        """AR-P03: gmoc_trend_positions 미청산 1건 → True."""
        state = self._make_state_with_models()
        reporter = self._make_reporter(state)

        mock_db = AsyncMock()
        mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=1))

        result = await reporter._has_open_position("trend_following", "btc_jpy", mock_db)
        assert result is True

    @pytest.mark.asyncio
    async def test_ar_p04_unknown_style_returns_false(self):
        """AR-P04: 알 수 없는 style → False + DEBUG 로그."""
        state = self._make_state_with_models()
        reporter = self._make_reporter(state)

        mock_db = AsyncMock()
        result = await reporter._has_open_position("unknown_style", "btc_jpy", mock_db)
        assert result is False
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_ar_p05_db_exception_returns_false(self):
        """AR-P05: DB 조회 예외 → False + WARNING 로그."""
        state = self._make_state_with_models()
        reporter = self._make_reporter(state)

        mock_db = AsyncMock()
        mock_db.execute.side_effect = Exception("DB connection lost")

        result = await reporter._has_open_position("box_mean_reversion", "btc_jpy", mock_db)
        assert result is False


class TestFormatSafetySummary:
    """format_safety_summary n/a 제외 테스트."""

    def _make_report(self, checks, status="all_ok"):
        from core.punisher.monitoring.health import SafetyReport, SafetyCheck
        sc = [SafetyCheck(id=f"SF-{i+1:02d}", name=c[0], status=c[1], severity="critical", detail="")
              for i, c in enumerate(checks)]
        return SafetyReport(status=status, checks=sc, last_checked="2026-03-25T00:00:00Z")

    def test_all_ok_no_na(self):
        from core.punisher.monitoring.health import format_safety_summary
        r = self._make_report([("WS", "ok"), ("태스크", "ok")], "all_ok")
        s = format_safety_summary(r)
        assert "✅" in s
        assert "(2/2)" in s

    def test_all_ok_with_na(self):
        from core.punisher.monitoring.health import format_safety_summary
        r = self._make_report([("WS", "ok"), ("태스크", "ok"), ("사만사", "n/a")], "all_ok")
        s = format_safety_summary(r)
        assert "✅" in s
        assert "(3/3)" in s  # n/a 포함 전체 카운트 (대시보드 UI 일치)

    def test_warning_with_na(self):
        from core.punisher.monitoring.health import format_safety_summary
        r = self._make_report([("WS", "ok"), ("잔고", "warning"), ("사만사", "n/a")], "degraded")
        s = format_safety_summary(r)
        assert "🟡" in s
        assert "잔고" in s
        assert "(2/3)" in s  # n/a 포함 전체 카운트 (대시보드 UI 일치)

    def test_critical(self):
        from core.punisher.monitoring.health import format_safety_summary
        r = self._make_report([("WS", "critical"), ("태스크", "ok")], "critical")
        s = format_safety_summary(r)
        assert "🔴" in s
        assert "WS" in s
        assert "(1/2)" in s
