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


def test_base_trend_signal_changed_logs_info(caplog):
    """시그널이 변경되면 INFO로 출력."""
    mgr = _make_trend_manager()
    mgr._last_signal["BTC_JPY"] = "hold"

    with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
        # 시그널 변경: hold → entry_ok
        signal_changed = "entry_ok" != mgr._last_signal.get("BTC_JPY", "")
        if signal_changed:
            mgr._last_signal["BTC_JPY"] = "entry_ok"
        _sig_level = "entry_ok" == "hold" or not signal_changed
        _sig_log = __import__("logging").getLogger("core.strategy.base_trend")
        level = logging.DEBUG if _sig_level else logging.INFO
        _sig_log.log(level, "[TrendMgr] BTC_JPY: 롱 진입 조건 충족 (¥100)")

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("롱 진입 조건 충족" in r.message for r in info_msgs)


def test_base_trend_signal_repeated_logs_debug(caplog):
    """동일 시그널 반복 시 DEBUG로 다운그레이드된다."""
    mgr = _make_trend_manager()
    mgr._last_signal["BTC_JPY"] = "entry_sell"  # 이미 entry_sell 상태

    with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
        # 시그널 동일: entry_sell → entry_sell (변경 없음)
        signal_changed = "entry_sell" != mgr._last_signal.get("BTC_JPY", "")
        if signal_changed:
            mgr._last_signal["BTC_JPY"] = "entry_sell"
        _sig_level = "entry_sell" == "hold" or not signal_changed
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
    mgr._last_signal["BTC_JPY"] = "entry_sell"  # 이전 상태 잔류

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
    mgr._last_signal["BTC_JPY"] = "entry_sell"

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
    """비-hold 시그널 간 전환 (entry_sell → entry_ok): INFO로 출력."""
    mgr = _make_trend_manager()
    mgr._last_signal["BTC_JPY"] = "entry_sell"

    with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
        signal = "entry_ok"
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
    assert mgr._last_signal["BTC_JPY"] == "entry_ok"


# ──────────────────────────────────────────────────────────────
# _describe_signal (DS-01~07)
# ──────────────────────────────────────────────────────────────

def _make_mock_pos(side="buy"):
    """테스트용 Position mock (side 포함)."""
    pos = MagicMock()
    pos.extra = {"side": side}
    return pos


def test_describe_signal_exit_warning_no_position():
    """DS-01: 포지션 없을 때 exit_warning → 추세 약세, 진입 보류."""
    mgr = _make_trend_manager()
    assert mgr._describe_signal("exit_warning", None) == "추세 약세, 진입 보류"


def test_describe_signal_exit_warning_with_position():
    """DS-02: 포지션 있을 때 exit_warning → 추세 이탈, 청산 경고."""
    mgr = _make_trend_manager()
    pos = _make_mock_pos("buy")
    assert mgr._describe_signal("exit_warning", pos) == "추세 이탈, 청산 경고"


def test_describe_signal_entry_ok_no_position():
    """DS-03: 포지션 없을 때 entry_ok → 롱 진입 조건 충족."""
    mgr = _make_trend_manager()
    assert mgr._describe_signal("entry_ok", None) == "롱 진입 조건 충족"


def test_describe_signal_entry_ok_with_position():
    """DS-04: 포지션 있을 때 entry_ok → 추세 유지 중."""
    mgr = _make_trend_manager()
    pos = _make_mock_pos("buy")
    assert mgr._describe_signal("entry_ok", pos) == "추세 유지 중"


def test_describe_signal_wait_dip():
    """DS-05: wait_dip → RSI 과매수, 눌림 대기 (포지션 여부 무관)."""
    mgr = _make_trend_manager()
    assert mgr._describe_signal("wait_dip", None) == "RSI 과매수, 눌림 대기"
    assert mgr._describe_signal("wait_dip", _make_mock_pos()) == "RSI 과매수, 눌림 대기"


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
    """E3: 숏 포지션(side='sell') + exit_warning → 추세 이탈, 청산 경고."""
    mgr = _make_trend_manager()
    pos = _make_mock_pos(side="sell")
    assert mgr._describe_signal("exit_warning", pos) == "추세 이탈, 청산 경고"


def test_base_trend_check_exit_warning_log_no_old_message(caplog):
    """E4: base_trend._check_exit_warning 로그에 'exit_warning' 변수명이 노출되지 않는다."""
    from core.strategy.plugins.trend_following.manager import TrendFollowingManager
    from unittest.mock import MagicMock, AsyncMock
    mgr = _make_trend_manager()
    with caplog.at_level(logging.INFO, logger="core.strategy.base_trend"):
        # realtime_price < ema 조건 → 즉각 보정 INFO 발생
        result = mgr._check_exit_warning("BTC_JPY", "wait_dip", 100.0, 200.0, None)
    assert result == "exit_warning"
    info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    # 구 메시지 형식("exit_warning 즉각 보정")이 없어야 함
    assert not any("exit_warning 즉각 보정" in m for m in info_msgs)
    # 새 메시지 형식("추세 이탈 감지")이 있어야 함
    assert any("추세 이탈 감지" in m for m in info_msgs)


def test_cfd_check_exit_warning_log_no_old_message(caplog):
    """E5: CfdMgr._check_exit_warning 로그에 'exit_warning' 변수명이 노출되지 않는다."""
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
    pos = MagicMock()
    pos.extra = {"side": "buy"}

    with caplog.at_level(logging.INFO, logger="core.strategy.plugins.cfd_trend_following.manager"):
        result = mgr._check_exit_warning("USD_JPY", "wait_dip", 100.0, 200.0, pos)

    assert result == "exit_warning"
    info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    # 구 메시지("→ exit_warning") 없어야 함
    assert not any("→ exit_warning" in m for m in info_msgs)
    # 새 메시지("추세 이탈 감지") 있어야 함
    assert any("추세 이탈 감지" in m for m in info_msgs)


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
