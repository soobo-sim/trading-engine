"""
로그 레벨 회귀 테스트 — INFO → DEBUG 변경 검증.

LOGGING_ARCHITECTURE.md §2-2 변경 대상 항목이 실제로 DEBUG 레벨로 기록되는지,
§2-3 INFO 유지 대상 항목이 여전히 INFO인지 검증한다.

검증 범위:
  - DailyBriefing: start/stop/대기 → DEBUG
  - EventDetector: start(pair 없음)/start(pair 있음)/stop → DEBUG
  - BaseTrendManager: stop/stop_all/register_paper_pair/unregister_paper_pair → DEBUG
  - BoxMeanReversionManager: 캔들 부족/포지션 보유 중 → DEBUG
  - CfdTrendFollowingManager: FX 휴장 차단 → DEBUG
  - SafetyChecks: 메인터넌스 경고 스킵/Telegram 쿨다운 → DEBUG
  - Alerts: 레이첼 webhook 쿨다운 → DEBUG
  - INFO 유지: DailyBriefing 브리핑 전송 완료 → INFO
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────
# DailyBriefing
# ──────────────────────────────────────────────────────────────

def _make_daily_briefing():
    from core.monitoring.daily_briefing import DailyBriefing

    return DailyBriefing(
        session_factory=MagicMock(),
        trade_model=MagicMock(),
        pairs=["BTC_JPY"],
        bot_token="fake-token",
        chat_id="-999",
        adapter=None,
    )


@pytest.mark.asyncio
async def test_daily_briefing_start_logs_debug(caplog):
    """DailyBriefing.start() → DEBUG 로그."""
    briefing = _make_daily_briefing()
    with patch("asyncio.create_task") as mock_create_task:
        mock_task = MagicMock()
        mock_create_task.return_value = mock_task
        with caplog.at_level(logging.DEBUG, logger="core.monitoring.daily_briefing"):
            await briefing.start()

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("[DailyBriefing] 시작" in r.message for r in debug_msgs)


@pytest.mark.asyncio
async def test_daily_briefing_stop_logs_debug(caplog):
    """DailyBriefing.stop() → DEBUG 로그."""
    briefing = _make_daily_briefing()
    # task를 mocking하여 cancel/await 처리
    mock_task = AsyncMock()
    mock_task.cancel = MagicMock()
    mock_task.__await__ = lambda self: (yield from asyncio.coroutine(lambda: None)())
    # 직접 _task 설정
    briefing._task = asyncio.create_task(asyncio.sleep(999))
    with caplog.at_level(logging.DEBUG, logger="core.monitoring.daily_briefing"):
        await briefing.stop()

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("[DailyBriefing] 종료" in r.message for r in debug_msgs)


@pytest.mark.asyncio
async def test_daily_briefing_wait_logs_debug(caplog):
    """DailyBriefing._run() 대기 로그 → DEBUG."""
    from core.monitoring.daily_briefing import DailyBriefing

    briefing = _make_daily_briefing()
    # _send_briefing을 mock해서 실제 전송 없이 _run 내 대기 로그만 확인
    briefing._send_briefing = AsyncMock()
    briefing._seconds_until_next_briefing = MagicMock(return_value=0.001)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_sleep.side_effect = asyncio.CancelledError
        with caplog.at_level(logging.DEBUG, logger="core.monitoring.daily_briefing"):
            try:
                await briefing._run()
            except asyncio.CancelledError:
                pass

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("다음 브리핑까지" in r.message for r in debug_msgs)


@pytest.mark.asyncio
async def test_daily_briefing_send_complete_logs_info(caplog):
    """DailyBriefing 브리핑 전송 완료 → INFO 유지 (§2-3)."""
    from core.monitoring.daily_briefing import DailyBriefing

    briefing = _make_daily_briefing()
    mock_send = AsyncMock(return_value=True)
    with patch.object(briefing, "_build_briefing_text", new_callable=AsyncMock, return_value="test"):
        briefing._send_message = mock_send
        with caplog.at_level(logging.DEBUG, logger="core.monitoring.daily_briefing"):
            await briefing._send_briefing()

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("브리핑 전송 완료" in r.message for r in info_msgs), (
        "브리핑 전송 완료는 INFO를 유지해야 한다 (§2-3)"
    )


# ──────────────────────────────────────────────────────────────
# EventDetector
# ──────────────────────────────────────────────────────────────

def _make_event_detector(pairs=None):
    from core.monitoring.event_detector import EventDetector

    data_hub = MagicMock()
    data_hub.get_ticker = AsyncMock(return_value=None)
    data_hub.get_sentiment = AsyncMock(return_value=None)
    data_hub.get_upcoming_events = AsyncMock(return_value=[])
    _pairs = ["BTC_JPY"] if pairs is None else pairs
    return EventDetector(
        data_hub=data_hub,
        advisory_base_url="http://localhost:8001",
        exchange="bitflyer",
        pairs=_pairs,
    )


@pytest.mark.asyncio
async def test_event_detector_start_no_pairs_logs_debug(caplog):
    """pairs=[] 이면 시작 스킵 → DEBUG 로그."""
    det = _make_event_detector(pairs=[])
    with caplog.at_level(logging.DEBUG, logger="core.monitoring.event_detector"):
        await det.start()

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("감시 대상 pair 없음" in r.message for r in debug_msgs)
    # 태스크가 생성되지 않았는지 확인
    assert det._task is None


@pytest.mark.asyncio
async def test_event_detector_start_with_pairs_logs_debug(caplog):
    """pairs 있을 때 start() → DEBUG 로그 (pairs 정보 포함)."""
    det = _make_event_detector(pairs=["BTC_JPY"])
    with patch("asyncio.create_task") as mock_create_task:
        mock_create_task.return_value = MagicMock()
        with caplog.at_level(logging.DEBUG, logger="core.monitoring.event_detector"):
            await det.start()

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("[EventDetector] 시작" in r.message for r in debug_msgs)


@pytest.mark.asyncio
async def test_event_detector_stop_logs_debug(caplog):
    """stop() → DEBUG 로그."""
    det = _make_event_detector()
    det._task = asyncio.create_task(asyncio.sleep(999))
    with caplog.at_level(logging.DEBUG, logger="core.monitoring.event_detector"):
        await det.stop()

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("[EventDetector] 종료" in r.message for r in debug_msgs)
    assert det._task is None


# ──────────────────────────────────────────────────────────────
# BaseTrendManager
# ──────────────────────────────────────────────────────────────

def _make_trend_manager():
    """TrendFollowingManager(BaseTrendManager 서브클래스) 최소 인스턴스."""
    from core.strategy.plugins.trend_following.manager import TrendFollowingManager

    adapter = MagicMock()
    adapter.exchange_name = "bitflyer"

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    session_factory = MagicMock()

    candle_model = MagicMock()
    position_model = MagicMock()

    return TrendFollowingManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=candle_model,
        trend_position_model=position_model,
    )


@pytest.mark.asyncio
async def test_base_trend_stop_logs_debug(caplog):
    """stop(pair) → 추세추종 태스크 종료 → DEBUG."""
    mgr = _make_trend_manager()
    mgr._params["BTC_JPY"] = {}
    with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
        await mgr.stop("BTC_JPY")

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("추세추종 태스크 종료" in r.message for r in debug_msgs)


@pytest.mark.asyncio
async def test_base_trend_stop_all_logs_debug(caplog):
    """stop_all() → 전체 추세추종 인프라 종료 → DEBUG."""
    mgr = _make_trend_manager()
    mgr._params["BTC_JPY"] = {}
    with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
        await mgr.stop_all()

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("전체 추세추종 인프라 종료" in r.message for r in debug_msgs)


def test_base_trend_register_paper_pair_logs_debug(caplog):
    """register_paper_pair() → PaperExecutor 등록 → DEBUG."""
    mgr = _make_trend_manager()
    with patch("core.execution.executor.PaperExecutor") as mock_paper_exec:
        mock_paper_exec.return_value = MagicMock()
        with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
            mgr.register_paper_pair("BTC_JPY", strategy_id=99)

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("PaperExecutor 등록" in r.message for r in debug_msgs)
    assert any("strategy_id=99" in r.message for r in debug_msgs)


def test_base_trend_unregister_paper_pair_logs_debug(caplog):
    """unregister_paper_pair() → PaperExecutor 해제 → DEBUG."""
    mgr = _make_trend_manager()
    mgr._paper_executors["BTC_JPY"] = MagicMock()
    with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
        mgr.unregister_paper_pair("BTC_JPY")

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("PaperExecutor 해제" in r.message for r in debug_msgs)
    assert "BTC_JPY" not in mgr._paper_executors


# ──────────────────────────────────────────────────────────────
# BoxMeanReversionManager
# ──────────────────────────────────────────────────────────────

def _make_box_manager():
    from core.strategy.plugins.box_mean_reversion.manager import BoxMeanReversionManager

    adapter = MagicMock()
    adapter.exchange_name = "bitflyer"
    supervisor = MagicMock()
    supervisor.register = AsyncMock()
    supervisor.stop_group = AsyncMock()
    session_factory = MagicMock()

    candle_model = MagicMock()
    box_model = MagicMock()
    box_position_model = MagicMock()

    return BoxMeanReversionManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=candle_model,
        box_model=box_model,
        box_position_model=box_position_model,
        pair_column="pair",
    )


@pytest.mark.asyncio
async def test_box_mgr_candle_shortage_logs_debug(caplog):
    """캔들 부족 → DEBUG (조건 미충족 반복)."""
    mgr = _make_box_manager()
    params = {
        "box_min_touches": 3,
        "box_lookback_candles": 20,
        "basis_timeframe": "4h",
        "box_tolerance_pct": 1.0,
        "box_cluster_percentile": 100.0,
    }

    # 캔들 부족 조건: len < min_touches * 2 = 6
    with patch.object(mgr, "_get_active_box", new_callable=AsyncMock, return_value=None):
        with patch.object(mgr, "_has_open_position", new_callable=AsyncMock, return_value=False):
            with patch.object(mgr, "_get_completed_candles", new_callable=AsyncMock, return_value=[MagicMock(), MagicMock()]):  # 2개 < 6
                with caplog.at_level(logging.DEBUG, logger="core.strategy.plugins.box_mean_reversion.manager"):
                    result = await mgr._detect_and_create_box("BTC_JPY", params)

    assert result is None
    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("캔들 부족" in r.message for r in debug_msgs)


@pytest.mark.asyncio
async def test_box_mgr_position_held_logs_debug(caplog):
    """포지션 보유 중 신규 박스 생성 금지 → DEBUG."""
    mgr = _make_box_manager()
    params = {
        "box_min_touches": 3,
        "box_lookback_candles": 20,
        "basis_timeframe": "4h",
        "box_tolerance_pct": 1.0,
        "box_cluster_percentile": 100.0,
    }

    with patch.object(mgr, "_has_open_position", new_callable=AsyncMock, return_value=True):
        with caplog.at_level(logging.DEBUG, logger="core.strategy.plugins.box_mean_reversion.manager"):
            result = await mgr._detect_and_create_box("BTC_JPY", params)

    assert result is None
    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("포지션 보유 중" in r.message for r in debug_msgs)


@pytest.mark.asyncio
async def test_box_mgr_box_not_formed_logs_debug(caplog):
    """박스 불형성 (upper/lower=None) → DEBUG."""
    mgr = _make_box_manager()
    params = {
        "box_min_touches": 2,
        "box_lookback_candles": 20,
        "basis_timeframe": "4h",
        "box_tolerance_pct": 1.0,
        "box_cluster_percentile": 100.0,
    }

    # 캔들 4개 제공 (min_touches*2=4, 충분), _find_cluster는 None 반환
    fake_candles = [MagicMock() for _ in range(4)]
    with patch.object(mgr, "_has_open_position", new_callable=AsyncMock, return_value=False):
        with patch.object(mgr, "_get_active_box", new_callable=AsyncMock, return_value=None):
            with patch.object(mgr, "_get_completed_candles", new_callable=AsyncMock, return_value=fake_candles):
                with patch.object(mgr, "_find_cluster", return_value=(None, 0)):
                    with caplog.at_level(logging.DEBUG, logger="core.strategy.plugins.box_mean_reversion.manager"):
                        result = await mgr._detect_and_create_box("BTC_JPY", params)

    assert result is None
    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("박스 불형성" in r.message for r in debug_msgs)


# ──────────────────────────────────────────────────────────────
# CfdTrendFollowingManager
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cfd_mgr_fx_closed_logs_debug(caplog):
    """FX 시장 휴장/주말 임박 → 진입 차단 → DEBUG."""
    from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager

    adapter = MagicMock()
    adapter.exchange_name = "gmofx"
    supervisor = MagicMock()
    session_factory = MagicMock()
    candle_model = MagicMock()
    cfd_position_model = MagicMock()

    mgr = CfdTrendFollowingManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=candle_model,
        cfd_position_model=cfd_position_model,
    )
    params = {
        "position_size_pct": 10.0,
        "keep_rate_warn": 1.5,
    }
    # entry_ok 시그널, FX 휴장 상태
    with patch("core.strategy.plugins.cfd_trend_following.manager.should_close_for_weekend", return_value=True):
        with patch("core.strategy.plugins.cfd_trend_following.manager.is_fx_market_open", return_value=False):
            with caplog.at_level(logging.DEBUG, logger="core.strategy.plugins.cfd_trend_following.manager"):
                await mgr._on_entry_signal("USD_JPY", "entry_ok", 150.0, atr=0.5, params=params, signal_data={})

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("FX 시장 휴장" in r.message for r in debug_msgs)


# ──────────────────────────────────────────────────────────────
# SafetyChecks
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_safety_maintenance_warning_skip_logs_debug(caplog, monkeypatch):
    """메인터넌스 중 경고 스킵 → DEBUG."""
    from core.monitoring.safety_checks import SafetyChecksMixin
    from core.monitoring.health import SafetyCheck

    # SafetyChecksMixin 인스턴스 직접 생성
    checker = SafetyChecksMixin()

    # is_maintenance_window → True (함수 내부 import 경로 패치)
    with patch("core.monitoring.maintenance.is_maintenance_window", return_value=True):
        with caplog.at_level(logging.DEBUG, logger="core.monitoring.safety_checks"):
            await checker._send_safety_telegram_alert(checks=[
                SafetyCheck(id="SF-01", name="test", status="critical", severity="critical", detail="test")
            ])

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("정기 메인터넌스 중" in r.message for r in debug_msgs)
    assert any("경고 스킵" in r.message for r in debug_msgs)


@pytest.mark.asyncio
async def test_safety_telegram_cooldown_logs_debug(caplog):
    """Telegram 경고 쿨다운 중 → DEBUG."""
    from core.monitoring.safety_checks import SafetyChecksMixin
    from core.monitoring.health import SafetyCheck

    checker = SafetyChecksMixin()
    checker._telegram_alert_cooldown["safety"] = time.time()  # 방금 전송됨

    with patch("core.monitoring.safety_checks.is_maintenance_window", return_value=False):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"}):
            with caplog.at_level(logging.DEBUG, logger="core.monitoring.safety_checks"):
                await checker._send_safety_telegram_alert(checks=[
                    SafetyCheck(id="SF-01", name="test", status="critical", severity="critical", detail="test")
                ])

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("쿨다운 중" in r.message for r in debug_msgs)


# ──────────────────────────────────────────────────────────────
# Alerts (레이첼 webhook 쿨다운)
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alerts_rachel_webhook_cooldown_logs_debug(caplog):
    """레이첼 webhook 쿨다운 중 → DEBUG."""
    import api.services.monitoring.alerts as alerts_module

    # 쿨다운 상태 주입
    pair = "BTC_JPY_test_debug"
    alerts_module._last_alert_time[pair] = time.time()  # 방금 전송됨

    alert = {
        "pair": pair,
        "triggers": ["rsi_overbought"],
        "text": "RSI 과매수 테스트",
        "current_price": 10_000_000,
        "level": "warning",
    }

    # 모듈 레벨 컴스턴트를 직접 패치 (모듈 로드 시 불변 os.getenv 회피)
    with patch.object(alerts_module, "RACHEL_WEBHOOK_URL", "http://localhost:18791/webhook"):
        with patch.object(alerts_module, "RACHEL_WEBHOOK_TOKEN", "test-token"):
            with caplog.at_level(logging.DEBUG, logger="api.services.monitoring.alerts"):
                await alerts_module._trigger_rachel_analysis(pair=pair, alert=alert, has_position=True)

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("쿨다운 중" in r.message for r in debug_msgs)
    assert any(pair in r.message for r in debug_msgs)

    # 정리
    alerts_module._last_alert_time.pop(pair, None)


@pytest.mark.asyncio
async def test_alerts_rachel_info_on_success_not_debug(caplog):
    """레이첼 긴급 분석 트리거 성공 → INFO 유지 (§2-3)."""
    import api.services.monitoring.alerts as alerts_module

    pair = "BTC_JPY_test_info"
    # 쿨다운 없는 상태
    alerts_module._last_alert_time.pop(pair, None)
    alerts_module._consecutive_same.pop(pair, None)
    alerts_module._last_alert_level.pop(pair, None)

    alert = {
        "pair": pair,
        "triggers": ["rsi_overbought"],
        "text": "RSI 과매수",
        "current_price": 10_000_000,
        "level": "warning",
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch.object(alerts_module, "RACHEL_WEBHOOK_URL", "http://localhost:18791/webhook"):
        with patch.object(alerts_module, "RACHEL_WEBHOOK_TOKEN", "test-token"):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                with caplog.at_level(logging.DEBUG, logger="api.services.monitoring.alerts"):
                    await alerts_module._trigger_rachel_analysis(pair=pair, alert=alert, has_position=True)

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("트리거 성공" in r.message for r in info_msgs), (
        "레이첼 긴급 분석 트리거 성공은 INFO를 유지해야 한다 (§2-3)"
    )

    # 정리
    alerts_module._last_alert_time.pop(pair, None)
    alerts_module._consecutive_same.pop(pair, None)
    alerts_module._last_alert_level.pop(pair, None)
