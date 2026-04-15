"""
RegimeGate 단위 테스트.

테스트 케이스 목록:
  RG-01: warm-up 차단 (3캔들 미만)
  RG-02: 3캔들 trending → trend_following 전환
  RG-03: 3캔들 ranging → box_mean_reversion 전환
  RG-04: unclear 끼면 active_strategy = None
  RG-05: 동일 전략 유지 (이미 같음)
  RG-06: 진입 허용 일치
  RG-07: 진입 차단 불일치
  RG-08: switch_count 증가
  RG-09: regime_history 최대 크기 유지
  RG-10: 전환 시 WARNING 로그 + ⭐⭐⭐⭐ 포함
  RG-11: streak 미달 시 INFO 로그
"""
import logging
import pytest
from core.execution.regime_gate import RegimeGate


class TestWarmup:
    """RG-01: warm-up 차단"""

    def test_warmup_blocks_entry(self):
        """3캔들 미만이면 should_allow_entry=False."""
        gate = RegimeGate("btc_jpy")
        assert gate.should_allow_entry("trend_following") is False

    def test_1_candle_still_blocks(self):
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending")
        assert gate.should_allow_entry("trend_following") is False

    def test_2_candle_still_blocks(self):
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending")
        gate.update_regime("trending")
        assert gate.should_allow_entry("trend_following") is False


class TestTrendingStreak:
    """RG-02: 3캔들 trending → trend_following 전환"""

    def test_3_trending_returns_trend_following(self):
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending")
        gate.update_regime("trending")
        result = gate.update_regime("trending", bb_width_pct=8.0, range_pct=12.0)
        assert result == "trend_following"

    def test_3_trending_sets_active_strategy(self):
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("trending")
        assert gate.active_strategy == "trend_following"

    def test_first_2_return_none(self):
        gate = RegimeGate("btc_jpy")
        assert gate.update_regime("trending") is None
        assert gate.update_regime("trending") is None


class TestRangingStreak:
    """RG-03: 3캔들 ranging → box_mean_reversion 전환"""

    def test_3_ranging_returns_box_mean_reversion(self):
        gate = RegimeGate("btc_jpy")
        gate.update_regime("ranging")
        gate.update_regime("ranging")
        result = gate.update_regime("ranging", bb_width_pct=2.0, range_pct=3.5)
        assert result == "box_mean_reversion"

    def test_3_ranging_sets_active_strategy(self):
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("ranging")
        assert gate.active_strategy == "box_mean_reversion"


class TestUnclear:
    """RG-04: unclear 끼면 active_strategy = None"""

    def test_unclear_last_sets_active_none(self):
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending")
        gate.update_regime("trending")
        result = gate.update_regime("unclear")
        assert result is None
        assert gate.active_strategy is None

    def test_unclear_first_no_change(self):
        """이전 캔들이 unclear이고 마지막이 trending이면 active 변경 없음."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("unclear")
        gate.update_regime("trending")
        result = gate.update_regime("trending")
        # 3캔들 중 unclear 포함 + 마지막은 trending → streak 미달
        assert result is None

    def test_unclear_during_active_trend(self):
        """이미 active=trend_following인데 unclear 캔들 오면 None으로."""
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("trending")
        assert gate.active_strategy == "trend_following"
        gate.update_regime("unclear")
        assert gate.active_strategy is None


class TestAlreadySame:
    """RG-05: 동일 전략 유지 (이미 같음)"""

    def test_same_strategy_returns_none(self):
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("trending")
        # 이미 trend_following 활성 상태에서 3캔들 trending 재확인
        for _ in range(2):
            gate.update_regime("trending")
        result = gate.update_regime("trending")
        assert result is None
        assert gate.active_strategy == "trend_following"


class TestAllowEntry:
    """RG-06/07: 진입 허용/차단"""

    def test_allow_entry_matching_strategy(self):
        """RG-06: active=trend_following → trend_following 허용"""
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("trending")
        assert gate.should_allow_entry("trend_following") is True

    def test_block_entry_mismatching_strategy(self):
        """RG-07: active=trend_following → box_mean_reversion 차단"""
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("trending")
        assert gate.should_allow_entry("box_mean_reversion") is False

    def test_block_both_when_unclear(self):
        """unclear → 양쪽 모두 차단"""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("unclear")
        assert gate.should_allow_entry("trend_following") is False
        assert gate.should_allow_entry("box_mean_reversion") is False


class TestSwitchCount:
    """RG-08: switch_count 증가"""

    def test_switch_count_increments(self):
        gate = RegimeGate("btc_jpy")
        assert gate.switch_count == 0

        for _ in range(3):
            gate.update_regime("trending")
        assert gate.switch_count == 1

        for _ in range(3):
            gate.update_regime("ranging")
        assert gate.switch_count == 2

    def test_no_switch_on_same_strategy(self):
        gate = RegimeGate("btc_jpy")
        for _ in range(6):
            gate.update_regime("trending")
        assert gate.switch_count == 1  # 처음 한 번만


class TestHistoryMaxSize:
    """RG-09: regime_history 최대 크기 유지"""

    def test_history_max_streak_required(self):
        gate = RegimeGate("btc_jpy")
        for _ in range(10):
            gate.update_regime("trending")
        assert len(gate.regime_history) == 3

    def test_history_is_copy(self):
        """regime_history 프로퍼티는 복사본 반환 (외부 변경 불가)"""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending")
        h = gate.regime_history
        h.append("tampered")
        assert len(gate.regime_history) == 1


class TestLogging:
    """RG-10/11: 로그 검증"""

    def test_warning_log_on_switch_with_stars(self, caplog):
        """RG-10: 전환 발생 시 WARNING + ⭐⭐⭐⭐ 포함"""
        gate = RegimeGate("btc_jpy")
        with caplog.at_level(logging.WARNING, logger="core.execution.regime_gate"):
            gate.update_regime("ranging")
            gate.update_regime("ranging")
            gate.update_regime("ranging")
        assert any("⭐⭐⭐⭐" in r.message for r in caplog.records)
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_info_log_on_streak_miss(self, caplog):
        """RG-11: streak 미달 시 INFO 로그"""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending")
        gate.update_regime("ranging")
        with caplog.at_level(logging.INFO, logger="core.execution.regime_gate"):
            gate.update_regime("ranging")
        assert any(r.levelno == logging.INFO for r in caplog.records)

    def test_log_includes_bb_width(self, caplog):
        """로그에 BB폭과 가격범위 수치 포함"""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("ranging")
        gate.update_regime("ranging")
        with caplog.at_level(logging.WARNING, logger="core.execution.regime_gate"):
            gate.update_regime("ranging", bb_width_pct=2.1, range_pct=3.8)
        assert any("2.1" in r.message for r in caplog.records)
        assert any("3.8" in r.message for r in caplog.records)


class TestConsecutiveCount:
    """연속 횟수 카운터 검증"""

    def test_consecutive_count_increments(self):
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending")
        assert gate.consecutive_count == 1
        gate.update_regime("trending")
        assert gate.consecutive_count == 2
        gate.update_regime("trending")
        assert gate.consecutive_count == 3

    def test_consecutive_count_resets_on_change(self):
        gate = RegimeGate("btc_jpy")
        for _ in range(5):
            gate.update_regime("trending")
        assert gate.consecutive_count == 5
        gate.update_regime("ranging")
        assert gate.consecutive_count == 1

    def test_consecutive_count_keeps_counting_beyond_streak(self):
        """3캔들 streak 넘어서도 연속 횟수는 계속 증가"""
        gate = RegimeGate("btc_jpy")
        for i in range(10):
            gate.update_regime("ranging")
        assert gate.consecutive_count == 10

    def test_consecutive_count_in_log(self, caplog):
        """로그에 연속 횟수 포함"""
        gate = RegimeGate("btc_jpy")
        for _ in range(4):
            gate.update_regime("trending")
        with caplog.at_level(logging.INFO, logger="core.execution.regime_gate"):
            gate.update_regime("trending")
        assert any("연속 5회" in r.message for r in caplog.records)

    def test_unclear_resets_consecutive_count(self):
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("trending")
        assert gate.consecutive_count == 3
        gate.update_regime("unclear")
        assert gate.consecutive_count == 1


class TestCrossSwitch:
    """추세→박스→추세 전환 사이클"""

    def test_trend_to_box_to_trend(self):
        gate = RegimeGate("btc_jpy")
        for _ in range(3):
            gate.update_regime("trending")
        assert gate.active_strategy == "trend_following"

        for _ in range(3):
            gate.update_regime("ranging")
        assert gate.active_strategy == "box_mean_reversion"
        assert gate.switch_count == 2

        for _ in range(3):
            gate.update_regime("trending")
        assert gate.active_strategy == "trend_following"
        assert gate.switch_count == 3


class TestCandleKeyIdempotency:
    """DUP-01~05: candle_key 기반 중복 호출 방지 (TrendManager+BoxManager 공유 gate 버그 수정)"""

    def test_dup01_same_key_called_twice(self):
        """DUP-01: 동일 캔들 키로 2회 호출 → history 길이 1, consecutive_count=1."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending", candle_key="2026-04-15T04:00")
        gate.update_regime("trending", candle_key="2026-04-15T04:00")
        assert len(gate._regime_history) == 1
        assert gate._consecutive_count == 1

    def test_dup02_different_keys_sequential(self):
        """DUP-02: 다른 캔들 키 순차 호출 → history 길이 2, consecutive_count=2."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending", candle_key="2026-04-15T04:00")
        gate.update_regime("trending", candle_key="2026-04-15T08:00")
        assert len(gate._regime_history) == 2
        assert gate._consecutive_count == 2

    def test_dup03_none_key_no_idempotency(self):
        """DUP-03: candle_key=None 하위호환 — 멱등성 체크 스킵, 2회 호출 시 2배 쌓임."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending")  # candle_key 미전달
        gate.update_regime("trending")  # candle_key 미전달
        assert len(gate._regime_history) == 2

    def test_dup04_warmup_with_duplicate_key(self):
        """DUP-04: warm-up 중 동일 키 중복 호출 → warm-up 1/3, 연속 1회."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending", candle_key="2026-04-15T04:00")
        gate.update_regime("trending", candle_key="2026-04-15T04:00")  # 중복 → 스킵
        assert len(gate._regime_history) == 1
        assert gate.should_allow_entry("trend_following") is False

    def test_dup05_streak_correct_with_key(self):
        """DUP-05: 3개의 고유 캔들 키로 trending → active_strategy 전환 정확히 3캔들 후."""
        gate = RegimeGate("btc_jpy")
        # 각 캔들에 대해 TrendManager + BoxManager 각 1회씩 호출 (총 6회, 유효 3회)
        for i in range(1, 4):
            candle_key = f"2026-04-15T{i * 4:02d}:00"
            gate.update_regime("trending", candle_key=candle_key)
            gate.update_regime("trending", candle_key=candle_key)  # 중복 → 스킵
        assert gate.active_strategy == "trend_following"
        assert gate.switch_count == 1

    def test_dup06_duplicate_returns_none(self):
        """DUP-06: 중복 호출 시 반환값이 None이어야 함."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending", candle_key="2026-04-15T04:00")
        result = gate.update_regime("trending", candle_key="2026-04-15T04:00")
        assert result is None

    def test_dup07_old_key_after_new_key_counts(self):
        """DUP-07: key1 → key2 → key1 순서 — key1이 다시 오면 새 캔들로 처리.
        (_last_candle_key는 바로 이전 것만 기억 — 롤백 시나리오는 실제로 발생 안 함)"""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending", candle_key="T04")
        gate.update_regime("trending", candle_key="T08")
        gate.update_regime("trending", candle_key="T04")  # 이전 키 → 새 캔들로 처리
        assert len(gate._regime_history) == 3

    def test_dup08_none_then_key_not_skipped(self):
        """DUP-08: candle_key=None으로 호출 후 key="T04" 호출 → 스킵 안 됨."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending")  # key=None
        gate.update_regime("trending", candle_key="2026-04-15T04:00")  # key 있음
        assert len(gate._regime_history) == 2
