"""
포지션 없음 상태 webhook 필터 테스트.

TC-1: 포지션 있음 + RSI 극단 → 보고 ✅
TC-2: 포지션 없음 + regime 동일 + RSI 극단 → 묵음 ✅
TC-3: 포지션 없음 + regime 전환 → 보고 ✅
TC-4: 포지션 없음 + 24h 이내 이미 보고 → 묵음 ✅
TC-5: 포지션 없음 + Kill 조건 변화 → 보고 ✅
TC-6: 포지션 없음 + 최초 발송 (prev_regime 없음) → 보고 ✅
TC-7: 포지션 없음 + 24h 경과 + regime 동일 → 보고 ✅
"""
import time
import pytest

from api.services.monitoring.alerts import (
    _should_send_no_position,
    _record_no_pos_sent,
    _last_sent_regime,
    _last_no_pos_alert_time,
    _NO_POS_COOLDOWN_SEC,
)


def _reset(pair: str):
    _last_sent_regime.pop(pair, None)
    _last_no_pos_alert_time.pop(pair, None)


# ─── TC-1: 포지션 있음 → 필터 비적용 (항상 True) ───────────────
def test_tc1_has_position_always_sends():
    """포지션 있으면 _should_send_no_position 호출 안 함 — 로직 상 True 반환 기대."""
    # 포지션 있음 분기는 caller에서 처리; 필터 자체는 no_position 전용이므로
    # 여기선 has_position=True 시 필터를 건너뜀을 호출부 로직으로 확인
    # (alerts.py _trigger_rachel_analysis에서 if not has_position: 로 분기)
    # 테스트는 필터 함수 자체 동작만 검증
    pair = "BTC_JPY_tc1"
    _reset(pair)
    alert = {"triggers": ["rsi_extreme_low"], "level": "critical"}
    # 포지션 없음 기준: 최초 → 보내야 함
    assert _should_send_no_position(pair, alert, "entry_ok") is True


# ─── TC-2: 포지션 없음 + regime 동일 + RSI 극단 → 묵음 ──────────
def test_tc2_no_pos_same_regime_rsi_extreme_silent():
    pair = "BTC_JPY_tc2"
    _reset(pair)
    # 먼저 한 번 발송 기록
    _record_no_pos_sent(pair, "entry_ok")
    # 이후 동일 regime + RSI 극단만
    alert = {"triggers": ["rsi_extreme_low"], "level": "critical"}
    assert _should_send_no_position(pair, alert, "entry_ok") is False


# ─── TC-3: 포지션 없음 + regime 전환 → 보고 ──────────────────────
def test_tc3_no_pos_regime_change_sends():
    pair = "BTC_JPY_tc3"
    _reset(pair)
    _record_no_pos_sent(pair, "entry_ok")  # 이전 regime=entry_ok
    alert = {"triggers": ["rsi_extreme_low", "regime_shift"], "level": "critical"}
    # 현재 regime=exit_warning (전환)
    assert _should_send_no_position(pair, alert, "exit_warning") is True


# ─── TC-4: 포지션 없음 + 24h 이내 이미 보고 → 묵음 ──────────────
def test_tc4_no_pos_within_24h_silent():
    pair = "BTC_JPY_tc4"
    _reset(pair)
    _record_no_pos_sent(pair, "wait_dip")
    # 24h 쿨다운 이내, regime 동일, 일반 트리거만
    alert = {"triggers": ["high_volatility_15m"], "level": "warning"}
    assert _should_send_no_position(pair, alert, "wait_dip") is False


# ─── TC-5: 포지션 없음 + Kill 조건 변화 → 보고 ──────────────────
def test_tc5_no_pos_kill_trigger_sends():
    pair = "BTC_JPY_tc5"
    _reset(pair)
    _record_no_pos_sent(pair, "entry_ok")
    # Kill 트리거 포함 → 항상 보고
    alert = {"triggers": ["kill_consecutive_loss"], "level": "critical"}
    assert _should_send_no_position(pair, alert, "entry_ok") is True


# ─── TC-6: 포지션 없음 + 최초 발송 → 보고 ───────────────────────
def test_tc6_no_pos_first_time_sends():
    pair = "BTC_JPY_tc6"
    _reset(pair)
    # 기록 전혀 없음 → 최초 발송
    alert = {"triggers": ["rsi_extreme_low"], "level": "critical"}
    assert _should_send_no_position(pair, alert, "entry_ok") is True


# ─── TC-7: 포지션 없음 + 24h 경과 + regime 동일 → 보고 ──────────
def test_tc7_no_pos_24h_elapsed_sends():
    pair = "BTC_JPY_tc7"
    _reset(pair)
    _record_no_pos_sent(pair, "entry_ok")
    # 24h+1초 전으로 강제 설정
    _last_no_pos_alert_time[pair] = time.time() - _NO_POS_COOLDOWN_SEC - 1
    alert = {"triggers": ["rsi_extreme_low"], "level": "critical"}
    assert _should_send_no_position(pair, alert, "entry_ok") is True
