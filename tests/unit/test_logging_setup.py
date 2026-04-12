"""
main.setup_logging() 단위 테스트.

검증 대상:
  - 루트 로거 레벨 = DEBUG
  - 콘솔 핸들러 등록 (StreamHandler, 기본 INFO)
  - 파일 핸들러 등록 (TimedRotatingFileHandler, DEBUG)
  - 노이즈 로거 억제 확인 (uvicorn.access / httpx / httpcore / websockets / asyncio)
  - `websockets.client` DEBUG가 파일 핸들러에 도달하지 않음 (핵심 회귀 방지)
  - JSONFormatter 타임스탬프가 JST (UTC+9) 인지 확인
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone


def _call_setup_logging(exchange: str = "test") -> None:
    """setup_logging을 임시 디렉토리에서 호출."""
    orig_dir = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            from main import setup_logging
            setup_logging(exchange)
        finally:
            os.chdir(orig_dir)


# ──────────────────────────────────────────────────────────────
# 기본 구조 검증
# ──────────────────────────────────────────────────────────────

def test_root_logger_level_is_debug():
    """루트 로거 레벨 = DEBUG (파일에 전부 기록)."""
    _call_setup_logging()
    root = logging.getLogger()
    assert root.level == logging.DEBUG


def test_handlers_registered():
    """콘솔(StreamHandler) + 파일(TimedRotatingFileHandler) 핸들러 모두 등록."""
    _call_setup_logging()
    root = logging.getLogger()
    handler_types = {type(h) for h in root.handlers}
    assert logging.StreamHandler in handler_types
    assert logging.handlers.TimedRotatingFileHandler in handler_types


def test_file_handler_level_is_debug():
    """파일 핸들러 레벨 = DEBUG."""
    _call_setup_logging()
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert file_handlers[0].level == logging.DEBUG


# ──────────────────────────────────────────────────────────────
# 노이즈 로거 억제 검증 (핵심)
# ──────────────────────────────────────────────────────────────

def test_websockets_client_suppressed_to_warning():
    """websockets.client 로거 ≥ WARNING (DEBUG/INFO 차단)."""
    _call_setup_logging()
    assert logging.getLogger("websockets.client").level == logging.WARNING


def test_websockets_suppressed_to_warning():
    """websockets 부모 로거 ≥ WARNING."""
    _call_setup_logging()
    assert logging.getLogger("websockets").level == logging.WARNING


def test_asyncio_suppressed_to_warning():
    """asyncio 로거 ≥ WARNING."""
    _call_setup_logging()
    assert logging.getLogger("asyncio").level == logging.WARNING


def test_httpx_suppressed_to_warning():
    """httpx 로거 ≥ WARNING (기존 항목)."""
    _call_setup_logging()
    assert logging.getLogger("httpx").level == logging.WARNING


def test_httpcore_suppressed_to_warning():
    """httpcore 로거 ≥ WARNING (기존 항목)."""
    _call_setup_logging()
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_uvicorn_access_suppressed_to_warning():
    """uvicorn.access 로거 ≥ WARNING (기존 항목)."""
    _call_setup_logging()
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


# ──────────────────────────────────────────────────────────────
# 실제 전파 차단 검증 (핵심 회귀 방지)
# ──────────────────────────────────────────────────────────────

def test_websockets_debug_does_not_reach_file_handler():
    """
    websockets.client DEBUG 메시지가 파일 핸들러에 도달하지 않음.
    (BUG-030 후속: Ticker 필드 수정 + 로거 억제 양쪽 모두 성립해야 진짜 안전)
    """
    _call_setup_logging()
    ws_logger = logging.getLogger("websockets.client")
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    fh = file_handlers[0]

    # websockets.client 레벨이 WARNING이므로 DEBUG 레코드는 isEnabledFor False
    records_before = fh.stream.tell() if hasattr(fh.stream, "tell") else None

    # 직접 emit 시도 — 로거 레벨에서 차단되어야 함
    assert not ws_logger.isEnabledFor(logging.DEBUG)
    assert not ws_logger.isEnabledFor(logging.INFO)
    assert ws_logger.isEnabledFor(logging.WARNING)


# ──────────────────────────────────────────────────────────────
# JSONFormatter 타임스탬프 JST 검증
# ──────────────────────────────────────────────────────────────

def test_json_formatter_ts_is_jst():
    """JSONFormatter 의 ts 필드가 JST(+09:00) 오프셋으로 출력되어야 한다."""
    _call_setup_logging()
    from main import JSONFormatter

    JST = timezone(timedelta(hours=9))
    fmt = JSONFormatter(exchange="test")

    record = logging.LogRecord(
        name="test", level=logging.INFO,
        pathname="", lineno=0, msg="hello", args=(), exc_info=None,
    )
    output = json.loads(fmt.format(record))
    ts_str = output["ts"]
    ts = datetime.fromisoformat(ts_str)
    assert ts.utcoffset() == timedelta(hours=9), (
        f"ts 타임존이 JST(+09:00)이어야 하는데 실제: {ts.utcoffset()}"
    )


def test_json_formatter_ts_not_utc():
    """JSONFormatter 의 ts 필드가 UTC(+00:00) 가 아니어야 한다 (회귀 방지)."""
    _call_setup_logging()
    from main import JSONFormatter

    fmt = JSONFormatter(exchange="test")
    record = logging.LogRecord(
        name="test", level=logging.INFO,
        pathname="", lineno=0, msg="hello", args=(), exc_info=None,
    )
    output = json.loads(fmt.format(record))
    ts = datetime.fromisoformat(output["ts"])
    assert ts.utcoffset() != timedelta(0), (
        "ts가 여전히 UTC로 출력됨 — JST 변경 누락"
    )
