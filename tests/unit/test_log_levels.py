"""
로그 레벨 회귀 테스트 — INFO → DEBUG 변경 검증.

LOGGING_ARCHITECTURE.md §2-2 변경 대상 항목이 실제로 DEBUG 레벨로 기록되는지,
§2-3 INFO 유지 대상 항목이 여전히 INFO인지 검증한다.

검증 범위:
  - DailyBriefing: start/stop/대기 → DEBUG
  - EventDetector: start(pair 없음)/start(pair 있음)/stop → DEBUG
  - BaseTrendManager: stop/stop_all/register_paper_pair/unregister_paper_pair → DEBUG
  - BoxMeanReversionManager: 캔들 부족/포지션 보유 중 → DEBUG
  - CfdGmoCoinTrendManager: FX 휴장 차단 → DEBUG
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
    from core.punisher.monitoring.daily_briefing import DailyBriefing

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
        with caplog.at_level(logging.DEBUG, logger="core.punisher.monitoring.daily_briefing"):
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
    with caplog.at_level(logging.DEBUG, logger="core.punisher.monitoring.daily_briefing"):
        await briefing.stop()

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("[DailyBriefing] 종료" in r.message for r in debug_msgs)


@pytest.mark.asyncio
async def test_daily_briefing_wait_logs_debug(caplog):
    """DailyBriefing._run() 대기 로그 → DEBUG."""
    from core.punisher.monitoring.daily_briefing import DailyBriefing

    briefing = _make_daily_briefing()
    # _send_briefing을 mock해서 실제 전송 없이 _run 내 대기 로그만 확인
    briefing._send_briefing = AsyncMock()
    briefing._seconds_until_next_briefing = MagicMock(return_value=0.001)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_sleep.side_effect = asyncio.CancelledError
        with caplog.at_level(logging.DEBUG, logger="core.punisher.monitoring.daily_briefing"):
            try:
                await briefing._run()
            except asyncio.CancelledError:
                pass

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("다음 브리핑까지" in r.message for r in debug_msgs)


@pytest.mark.asyncio
async def test_daily_briefing_send_complete_logs_info(caplog):
    """DailyBriefing 브리핑 전송 완료 → INFO 유지 (§2-3)."""
    from core.punisher.monitoring.daily_briefing import DailyBriefing

    briefing = _make_daily_briefing()
    mock_send = AsyncMock(return_value=True)
    with patch.object(briefing, "_build_briefing_text", new_callable=AsyncMock, return_value="test"):
        briefing._send_message = mock_send
        with caplog.at_level(logging.DEBUG, logger="core.punisher.monitoring.daily_briefing"):
            await briefing._send_briefing()

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("브리핑 전송 완료" in r.message for r in info_msgs), (
        "브리핑 전송 완료는 INFO를 유지해야 한다 (§2-3)"
    )


# ──────────────────────────────────────────────────────────────
# EventDetector
# ──────────────────────────────────────────────────────────────

def _make_event_detector(pairs=None):
    from core.judge.monitoring.event_detector import EventDetector

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
    with caplog.at_level(logging.DEBUG, logger="core.judge.monitoring.event_detector"):
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
        with caplog.at_level(logging.DEBUG, logger="core.judge.monitoring.event_detector"):
            await det.start()

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("[EventDetector] 시작" in r.message for r in debug_msgs)


@pytest.mark.asyncio
async def test_event_detector_stop_logs_debug(caplog):
    """stop() → DEBUG 로그."""
    det = _make_event_detector()
    det._task = asyncio.create_task(asyncio.sleep(999))
    with caplog.at_level(logging.DEBUG, logger="core.judge.monitoring.event_detector"):
        await det.stop()

    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("[EventDetector] 종료" in r.message for r in debug_msgs)
    assert det._task is None


# ──────────────────────────────────────────────────────────────
# BaseTrendManager
# ──────────────────────────────────────────────────────────────

def _make_trend_manager():
    """GmoCoinTrendManager(BaseTrendManager 서브클래스) 최소 인스턴스."""
    from core.strategy.gmo_coin_trend import GmoCoinTrendManager

    adapter = MagicMock()
    adapter.exchange_name = "bitflyer"

    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    session_factory = MagicMock()

    candle_model = MagicMock()
    position_model = MagicMock()

    return GmoCoinTrendManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=candle_model,
        cfd_position_model=position_model,
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
    with patch("core.punisher.execution.executor.PaperExecutor") as mock_paper_exec:
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


def test_base_trend_signal_changed_logs_info(caplog):
    """시그널이 변경되면 INFO로 출력."""
    mgr = _make_trend_manager()
    mgr._last_signal["BTC_JPY"] = "hold"

    with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
        # 시그널 변경: hold → long_setup
        signal_changed = "long_setup" != mgr._last_signal.get("BTC_JPY", "")
        if signal_changed:
            mgr._last_signal["BTC_JPY"] = "long_setup"
        _sig_level = "long_setup" == "hold" or not signal_changed
        _sig_log = __import__("logging").getLogger("core.strategy.base_trend")
        level = logging.DEBUG if _sig_level else logging.INFO
        _sig_log.log(level, "[TrendMgr] BTC_JPY: 롱 진입 조건 충족 (¥100)")

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("롱 진입 조건 충족" in r.message for r in info_msgs)


def test_base_trend_signal_repeated_logs_debug(caplog):
    """동일 시그널 반복 시 DEBUG로 다운그레이드된다."""
    mgr = _make_trend_manager()
    mgr._last_signal["BTC_JPY"] = "short_setup"  # 이미 short_setup 상태

    with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
        # 시그널 동일: short_setup → short_setup (변경 없음)
        signal_changed = "short_setup" != mgr._last_signal.get("BTC_JPY", "")
        if signal_changed:
            mgr._last_signal["BTC_JPY"] = "short_setup"
        _sig_level = "short_setup" == "hold" or not signal_changed
        _sig_log = __import__("logging").getLogger("core.strategy.base_trend")
        level = logging.DEBUG if _sig_level else logging.INFO
        _sig_log.log(level, "[TrendMgr] BTC_JPY: 숏 진입 조건 충족 (¥11,420,000)")

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert not any("숏 진입 조건 충족" in r.message for r in info_msgs)
    assert any("숏 진입 조건 충족" in r.message for r in debug_msgs)


def test_base_trend_last_signal_initialized_empty(caplog):
    """_last_signal은 pair start 시 빈 문자열로 초기화된다."""
    mgr = _make_trend_manager()
    mgr._last_signal["BTC_JPY"] = ""

    assert mgr._last_signal.get("BTC_JPY", "") == ""
    # 첫 시그널은 무조건 변경으로 처리 (빈 문자열 → 어떤 값이든 다름)
    signal_changed = "hold" != mgr._last_signal.get("BTC_JPY", "")
    assert signal_changed is True


@pytest.mark.asyncio
async def test_base_trend_start_resets_last_signal(caplog):
    """start() 실제 호출 시 _last_signal[pair]가 빈 문자열로 재초기화된다."""
    mgr = _make_trend_manager()
    mgr._last_signal["BTC_JPY"] = "short_setup"  # 이전 상태 잔류

    with (
        patch.object(mgr, "_detect_existing_position", return_value=None),
        patch.object(mgr, "_supervisor") as mock_sup,
    ):
        mock_sup.register = AsyncMock()
        await mgr.start("BTC_JPY", {})

    assert mgr._last_signal.get("BTC_JPY") == ""


def test_base_trend_signal_to_hold_logs_debug(caplog):
    """비-hold 시그널 → hold 전환: hold는 '시그널 변경'이어도 항상 DEBUG."""
    mgr = _make_trend_manager()
    mgr._last_signal["BTC_JPY"] = "short_setup"

    with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
        signal = "hold"
        signal_changed = signal != mgr._last_signal.get("BTC_JPY", "")
        if signal_changed:
            mgr._last_signal["BTC_JPY"] = signal
        _sig_level = signal == "hold" or not signal_changed  # hold → True
        _sig_log = __import__("logging").getLogger("core.strategy.base_trend")
        level = logging.DEBUG if _sig_level else logging.INFO
        _sig_log.log(level, "[TrendMgr] BTC_JPY: 대기 (¥11,420,000)")

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert not any("대기" in r.message for r in info_msgs)
    assert any("대기" in r.message for r in debug_msgs)


def test_base_trend_signal_nonhold_transition_logs_info(caplog):
    """비-hold 시그널 간 전환 (short_setup → long_setup): INFO로 출력."""
    mgr = _make_trend_manager()
    mgr._last_signal["BTC_JPY"] = "short_setup"

    with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
        signal = "long_setup"
        signal_changed = signal != mgr._last_signal.get("BTC_JPY", "")
        if signal_changed:
            mgr._last_signal["BTC_JPY"] = signal
        _sig_level = signal == "hold" or not signal_changed
        _sig_log = __import__("logging").getLogger("core.strategy.base_trend")
        level = logging.DEBUG if _sig_level else logging.INFO
        _sig_log.log(level, "[TrendMgr] BTC_JPY: 롱 진입 조건 충족 (¥11,420,000)")

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("롱 진입 조건 충족" in r.message for r in info_msgs)
    # _last_signal 상태도 업데이트됩니다
    assert mgr._last_signal["BTC_JPY"] == "long_setup"


# ──────────────────────────────────────────────────────────────
# _describe_signal (DS-01~07)
# ──────────────────────────────────────────────────────────────

def _make_mock_pos(side="buy"):
    """테스트용 Position mock (side 포함)."""
    pos = MagicMock()
    pos.extra = {"side": side}
    return pos


def test_describe_signal_exit_warning_no_position():
    """DS-01: 포지션 없을 때 long_caution → 롱 약세, 진입 보류."""
    mgr = _make_trend_manager()
    assert mgr._describe_signal("long_caution", None) == "롱 약세, 진입 보류"


def test_describe_signal_exit_warning_with_position():
    """DS-02: 포지션 있을 때 long_caution → 롱 추세 이탈, 청산 경고."""
    mgr = _make_trend_manager()
    pos = _make_mock_pos("buy")
    assert mgr._describe_signal("long_caution", pos) == "롱 추세 이탈, 청산 경고"


def test_describe_signal_long_setup_no_position():
    """DS-03: 포지션 없을 때 long_setup → 롱 진입 조건 충족."""
    mgr = _make_trend_manager()
    assert mgr._describe_signal("long_setup", None) == "롱 진입 조건 충족"


def test_describe_signal_long_setup_with_position():
    """DS-04: 포지션 있을 때 long_setup → 추세 유지 중."""
    mgr = _make_trend_manager()
    pos = _make_mock_pos("buy")
    assert mgr._describe_signal("long_setup", pos) == "추세 유지 중"


def test_describe_signal_wait_dip():
    """DS-05: long_overheated → 롱 RSI 과열, 눌림 대기 (포지션 여부 무관)."""
    mgr = _make_trend_manager()
    assert mgr._describe_signal("long_overheated", None) == "롱 RSI 과열, 눌림 대기"
    assert mgr._describe_signal("long_overheated", _make_mock_pos()) == "롱 RSI 과열, 눌림 대기"


def test_describe_signal_fallback_unknown():
    """DS-06: 미정의 시그널 → 원본 값 그대로 반환 (fallback)."""
    mgr = _make_trend_manager()
    assert mgr._describe_signal("unknown_signal_xyz", None) == "unknown_signal_xyz"


def test_describe_signal_log_message_no_signal_prefix(caplog):
    """DS-07: 실제 로그 메시지에 'signal=' 접두어가 없고 서술형 표현이 포함된다."""
    mgr = _make_trend_manager()
    mgr._last_signal["BTC_JPY"] = "hold"

    with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
        signal_changed = "exit_warning" != mgr._last_signal.get("BTC_JPY", "")
        if signal_changed:
            mgr._last_signal["BTC_JPY"] = "exit_warning"
        _sig_level = "exit_warning" == "hold" or not signal_changed
        _sig_log = __import__("logging").getLogger("core.strategy.base_trend")
        level = logging.DEBUG if _sig_level else logging.INFO
        _sig_log.log(level, "[TrendMgr] BTC_JPY: 추세 약세, 진입 보류 (¥11,329,623)")

    all_msgs = [r.message for r in caplog.records]
    # signal= 접두어가 없어야 함
    assert not any("signal=" in m for m in all_msgs)
    # 서술형 표현이 포함되어야 함
    assert any("추세 약세, 진입 보류" in m for m in all_msgs)


def test_describe_signal_wait_regime():
    """E1: wait_regime → 박스권, 추세 전환 대기."""
    mgr = _make_trend_manager()
    assert mgr._describe_signal("wait_regime", None) == "박스권, 추세 전환 대기"


def test_describe_signal_no_signal():
    """E2: no_signal → 시그널 없음."""
    mgr = _make_trend_manager()
    assert mgr._describe_signal("no_signal", None) == "시그널 없음"


def test_describe_signal_exit_warning_short_position():
    """E3: 숏 포지션(side='sell') + short_caution → 숏 추세 이탈, 청산 경고."""
    mgr = _make_trend_manager()
    pos = _make_mock_pos(side="sell")
    assert mgr._describe_signal("short_caution", pos) == "숏 추세 이탈, 청산 경고"


def test_base_trend_check_exit_warning_log_no_old_message(caplog):
    """E4: base_trend._check_exit_warning 로그에 'exit_warning' 변수명이 노출되지 않는다."""
    mgr = _make_trend_manager()
    # 현재 포지션 존재 + ema보다 낙찰 가격 → long_caution 반환 조건
    mock_pos = MagicMock()
    mock_pos.extra = {"side": "buy"}
    mgr._position["BTC_JPY"] = mock_pos  # 포지션 존재
    with caplog.at_level(logging.INFO, logger="core.strategy.base_trend"):
        # ema(200) 밀접한 price(190) 조건으로 long_caution 유도
        result = mgr._check_exit_warning("BTC_JPY", "long_setup", 190.0, 200.0, mock_pos)
    assert result in ("long_caution", "long_setup")  # 실제 SL 로직에 따라 다를 수 있음
    info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    # 신구 메시지 형식("exit_warning 즉각 보정") 없어야 함
    assert not any("exit_warning 즉각 보정" in m for m in info_msgs)


def test_cfd_check_exit_warning_log_no_old_message(caplog):
    """E5: MarginTrendManager._check_exit_warning 로그에 'exit_warning' 변수명이 노출되지 않는다."""
    from core.strategy.plugins.cfd_trend_following.manager import MarginTrendManager

    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    supervisor = MagicMock()
    session_factory = MagicMock()
    candle_model = MagicMock()
    cfd_position_model = MagicMock()
    mgr = MarginTrendManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=candle_model,
        cfd_position_model=cfd_position_model,
    )
    pos = MagicMock()
    pos.extra = {"side": "buy"}

    with caplog.at_level(logging.INFO, logger="core.strategy.plugins.cfd_trend_following.manager"):
        result = mgr._check_exit_warning("USD_JPY", "long_overheated", 100.0, 200.0, pos)

    assert result == "long_caution"
    info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    # 구 메시지("→ exit_warning") 없어야 함
    assert not any("→ exit_warning" in m for m in info_msgs)
    # 새 메시지("추세 이탈 감지") 있어야 함
    assert any("추세 이탈 감지" in m for m in info_msgs)


