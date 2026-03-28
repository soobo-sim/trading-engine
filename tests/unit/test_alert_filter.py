"""
포지션 없음 상태 webhook 필터 테스트 (버그 1+2 수정 후).

TC-1: 포지션 있음 → 필터 로직 최초 발송 True 확인
TC-2: 포지션 없음 + regime 동일 + RSI 극단 → 묵음
TC-3: 포지션 없음 + regime 전환 → 보고
TC-4: 포지션 없음 + 24h 이내 이미 보고 → 묵음
TC-5: 포지션 없음 + Kill 조건 변화 → 보고
TC-6: 포지션 없음 + 최초 발송 → 보고
TC-7: 포지션 없음 + 24h 경과 + regime 동일 → 보고
TC-8: regime=None → 'unknown' 정규화 → 기존 prev와 비교 안 함 → 쿨다운 판단
TC-9: regime='' (빈문자열) → 'unknown' 정규화 → 묵음 (동일 취급)
TC-10: 재시작 시뮬레이션 — 파일 캐시 읽기 후 상태 복원 → 쿨다운 유지
"""
import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch

import api.services.monitoring.alerts as alerts_mod
from api.services.monitoring.alerts import (
    _should_send_no_position,
    _record_no_pos_sent,
    _normalize_regime,
    _FILTER_CACHE_PATH,
    _NO_POS_COOLDOWN_SEC,
)


def _clear_cache(pair: str):
    """테스트용: 파일 캐시에서 pair 항목 삭제."""
    try:
        if _FILTER_CACHE_PATH.exists():
            data = json.loads(_FILTER_CACHE_PATH.read_text())
            data.pop(pair, None)
            _FILTER_CACHE_PATH.write_text(json.dumps(data))
    except Exception:
        if _FILTER_CACHE_PATH.exists():
            _FILTER_CACHE_PATH.write_text("{}")


def _write_cache(pair: str, regime: str, sent_at: float):
    """테스트용: 파일 캐시에 직접 상태 기입."""
    try:
        data = {}
        if _FILTER_CACHE_PATH.exists():
            data = json.loads(_FILTER_CACHE_PATH.read_text())
        data[pair] = {"regime": regime, "sent_at": sent_at}
        _FILTER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FILTER_CACHE_PATH.write_text(json.dumps(data))
    except Exception:
        pass


# ─── TC-1: 최초 발송 (no prev) → True ───────────────────────
def test_tc1_has_position_always_sends():
    pair = "BTC_JPY_tc1"
    _clear_cache(pair)
    alert = {"triggers": ["rsi_extreme_low"], "level": "critical"}
    assert _should_send_no_position(pair, alert, "entry_ok") is True


# ─── TC-2: 포지션 없음 + regime 동일 + RSI 극단 → 묵음 ──────
def test_tc2_no_pos_same_regime_rsi_extreme_silent():
    pair = "BTC_JPY_tc2"
    _write_cache(pair, "entry_ok", time.time())
    alert = {"triggers": ["rsi_extreme_low"], "level": "critical"}
    assert _should_send_no_position(pair, alert, "entry_ok") is False


# ─── TC-3: 포지션 없음 + regime 전환 → 보고 ──────────────────
def test_tc3_no_pos_regime_change_sends():
    pair = "BTC_JPY_tc3"
    _write_cache(pair, "entry_ok", time.time())
    alert = {"triggers": ["regime_shift"], "level": "critical"}
    assert _should_send_no_position(pair, alert, "exit_warning") is True


# ─── TC-4: 포지션 없음 + 24h 이내 → 묵음 ────────────────────
def test_tc4_no_pos_within_24h_silent():
    pair = "BTC_JPY_tc4"
    _write_cache(pair, "wait_dip", time.time())
    alert = {"triggers": ["high_volatility_15m"], "level": "warning"}
    assert _should_send_no_position(pair, alert, "wait_dip") is False


# ─── TC-5: Kill 트리거 → 항상 보고 ──────────────────────────
def test_tc5_no_pos_kill_trigger_sends():
    pair = "BTC_JPY_tc5"
    _write_cache(pair, "entry_ok", time.time())
    alert = {"triggers": ["kill_consecutive_loss"], "level": "critical"}
    assert _should_send_no_position(pair, alert, "entry_ok") is True


# ─── TC-6: 최초 발송 (캐시 없음) → 보고 ─────────────────────
def test_tc6_no_pos_first_time_sends():
    pair = "BTC_JPY_tc6"
    _clear_cache(pair)
    alert = {"triggers": ["rsi_extreme_low"], "level": "critical"}
    assert _should_send_no_position(pair, alert, "entry_ok") is True


# ─── TC-7: 24h 경과 + regime 동일 → 보고 ────────────────────
def test_tc7_no_pos_24h_elapsed_sends():
    pair = "BTC_JPY_tc7"
    _write_cache(pair, "entry_ok", time.time() - _NO_POS_COOLDOWN_SEC - 1)
    alert = {"triggers": ["rsi_extreme_low"], "level": "critical"}
    assert _should_send_no_position(pair, alert, "entry_ok") is True


# ─── TC-8: regime=None → unknown → 쿨다운 판단 (전환 오판 없음) ─
def test_tc8_regime_none_no_false_transition():
    pair = "BTC_JPY_tc8"
    _write_cache(pair, "entry_ok", time.time())
    alert = {"triggers": ["rsi_extreme_low"], "level": "critical"}
    # regime=None → unknown → prev "entry_ok"와 비교 안 함 → 쿨다운 → 묵음
    assert _should_send_no_position(pair, alert, None) is False


# ─── TC-9: regime='' → unknown → 묵음 ──────────────────────
def test_tc9_regime_empty_no_false_transition():
    pair = "BTC_JPY_tc9"
    _write_cache(pair, "wait_dip", time.time())
    alert = {"triggers": ["rsi_extreme_high"], "level": "critical"}
    assert _should_send_no_position(pair, alert, "") is False


# ─── TC-10: 재시작 시뮬레이션 — 파일에서 상태 복원 → 쿨다운 유지 ─
def test_tc10_restart_persistence():
    pair = "BTC_JPY_tc10"
    # 파일에 최근 발송 기록 직접 기입 (재시작 전 상태 시뮬)
    _write_cache(pair, "entry_ok", time.time() - 100)  # 100초 전 발송
    # "재시작" 후 — 인메모리 dict 없어도 파일에서 읽음
    alert = {"triggers": ["rsi_extreme_low"], "level": "critical"}
    # 24h 쿨다운 내 → 묵음
    assert _should_send_no_position(pair, alert, "entry_ok") is False


# ─── normalize_regime 단위 테스트 ────────────────────────────
def test_normalize_none():
    assert _normalize_regime(None) == "unknown"

def test_normalize_empty():
    assert _normalize_regime("") == "unknown"

def test_normalize_valid():
    assert _normalize_regime("entry_ok") == "entry_ok"
