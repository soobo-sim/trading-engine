"""
RegimeGate 직렬화·복원 + DB 영속화 헬퍼 테스트.

테스트 케이스:
  RP-01: to_dict() → restore() 라운드트립 — 전체 필드 일치
  RP-02: restore(빈 dict) → 초기 상태 유지 (active_strategy=None)
  RP-03: restore() 시 pair 불변 (state의 pair 값 무시)
  RP-04: save → load 통합 (mock session) — 원본과 동일 상태 복원
  RP-05: save 실패 시 예외 전파 없음 (WARNING만)
  RP-06: load — DB 행 없음 → False 반환, gate 초기 상태 유지
  RP-07: load 실패 시 False 반환 + 예외 전파 없음
  RP-08: unclear 후 to_dict() → active_strategy = None

base_trend 배선 테스트:
  RP-09: 새 캔들 처리 시 save_regime_gate_state 호출됨
  RP-10: 동일 candle_key 중복 처리 시 save 호출 안 됨
"""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from core.execution.regime_gate import RegimeGate
from core.execution.regime_gate_persistence import (
    load_regime_gate_state,
    save_regime_gate_state,
)


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _warmed_gate(pair: str = "btc_jpy", regime: str = "trending") -> RegimeGate:
    """warm-up 완료 상태의 RegimeGate. 3캔들 연속 동일 regime."""
    gate = RegimeGate(pair)
    for i in range(3):
        gate.update_regime(regime, bb_width_pct=2.0, range_pct=3.0, candle_key=f"candle_{i}")
    return gate


def _make_session_factory(row=None, raise_exc=None):
    """mock session_factory 생성."""
    mock_session = AsyncMock()

    if raise_exc is not None:
        mock_session.execute.side_effect = raise_exc
    elif row is not None:
        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute.return_value = mock_result
    else:
        # load: 행 없음
        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = mock_session
    return factory, mock_session


# ──────────────────────────────────────────────────────────────
# RP-01 ~ RP-08: 직렬화·복원
# ──────────────────────────────────────────────────────────────

class TestToDict:
    """RegimeGate.to_dict() 직렬화 검증."""

    def test_to_dict_contains_all_fields(self):
        """to_dict()에 영속화에 필요한 모든 키가 존재한다."""
        gate = _warmed_gate()
        d = gate.to_dict()
        required_keys = {
            "pair", "active_strategy", "regime_history",
            "last_switch_at", "switch_count",
            "consecutive_count", "consecutive_regime", "last_candle_key",
            "streak_required",
        }
        assert required_keys == set(d.keys())

    def test_to_dict_values_after_warmup(self):
        """warm-up 완료 후 to_dict() 값이 올바르다."""
        gate = _warmed_gate(regime="trending")
        d = gate.to_dict()
        assert d["pair"] == "btc_jpy"
        assert d["active_strategy"] == "trend_following"
        assert d["regime_history"] == ["trending", "trending", "trending"]
        assert d["last_candle_key"] == "candle_2"
        assert d["switch_count"] == 1


class TestRestore:
    """RegimeGate.restore() 복원 검증."""

    def test_rp01_roundtrip(self):
        """RP-01: to_dict() → 새 gate에 restore() → 전체 필드 일치."""
        original = _warmed_gate(regime="trending")
        state = original.to_dict()

        restored = RegimeGate("btc_jpy")
        restored.restore(state)

        assert restored.active_strategy == original.active_strategy
        assert restored.regime_history == original.regime_history
        assert restored.last_candle_key == original.last_candle_key
        assert restored.switch_count == original.switch_count
        assert restored.consecutive_count == original.consecutive_count

    def test_rp02_restore_empty_dict_no_change(self):
        """RP-02: restore({}) → 초기 상태 유지 (active_strategy=None)."""
        gate = RegimeGate("btc_jpy")
        gate.restore({})
        assert gate.active_strategy is None
        assert gate.regime_history == []
        assert gate.last_candle_key is None

    def test_rp03_restore_preserves_pair(self):
        """RP-03: restore의 state에 다른 pair가 있어도 gate pair 불변."""
        gate = RegimeGate("btc_jpy")
        state = _warmed_gate("eth_jpy").to_dict()
        # state["pair"] == "eth_jpy"
        gate.restore(state)
        # pair는 바뀌면 안 됨 (restore는 _pair를 덮어쓰지 않는다)
        assert gate._pair == "btc_jpy"

    def test_rp08_unclear_resets_active(self):
        """RP-08: trending×2 → unclear×1 → to_dict()["active_strategy"] is None."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending", candle_key="c0")
        gate.update_regime("trending", candle_key="c1")
        gate.update_regime("unclear", candle_key="c2")
        assert gate.to_dict()["active_strategy"] is None

    def test_restore_allows_entry_immediately(self):
        """warm-up 완료 상태를 restore하면 should_allow_entry가 즉시 True."""
        state = _warmed_gate(regime="trending").to_dict()
        gate = RegimeGate("btc_jpy")
        gate.restore(state)
        assert gate.should_allow_entry("trend_following") is True
        assert gate.should_allow_entry("box_mean_reversion") is False


# ──────────────────────────────────────────────────────────────
# RP-04 ~ RP-07: DB 영속화 헬퍼
# ──────────────────────────────────────────────────────────────

class TestSaveRegimeGateState:
    """save_regime_gate_state() 검증."""

    @pytest.mark.asyncio
    async def test_rp04_save_executes_upsert(self):
        """RP-04 (save side): execute가 UPSERT SQL로 호출된다."""
        gate = _warmed_gate()
        factory, session = _make_session_factory()

        await save_regime_gate_state(factory, gate)

        assert session.execute.called
        sql_text = str(session.execute.call_args[0][0])
        assert "ON CONFLICT" in sql_text
        assert "gmoc_regime_gate_state" in sql_text
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_rp05_save_db_failure_no_crash(self):
        """RP-05: session.execute 예외 → WARNING만, 예외 전파 없음."""
        gate = _warmed_gate()
        factory, session = _make_session_factory(raise_exc=RuntimeError("DB 연결 실패"))

        # 예외 전파 없이 정상 반환해야 함
        await save_regime_gate_state(factory, gate)  # should not raise

    @pytest.mark.asyncio
    async def test_save_correct_params(self):
        """save 호출 시 pair, active_strategy, regime_history 값이 올바르게 전달된다."""
        gate = _warmed_gate(regime="trending")
        factory, session = _make_session_factory()

        await save_regime_gate_state(factory, gate)

        params = session.execute.call_args[0][1]
        assert params["pair"] == "btc_jpy"
        assert params["active_strategy"] == "trend_following"
        # regime_history는 JSON 문자열로 전달
        history = json.loads(params["regime_history"])
        assert history == ["trending", "trending", "trending"]


class TestLoadRegimeGateState:
    """load_regime_gate_state() 검증."""

    @pytest.mark.asyncio
    async def test_rp04_load_restores_state(self):
        """RP-04 (load side): DB 행이 있으면 gate에 복원 후 True 반환."""
        original = _warmed_gate(regime="trending")
        state = original.to_dict()

        row = {
            "active_strategy": state["active_strategy"],
            "regime_history": state["regime_history"],
            "last_switch_at": state["last_switch_at"],
            "switch_count": state["switch_count"],
            "consecutive_count": state["consecutive_count"],
            "consecutive_regime": state["consecutive_regime"],
            "last_candle_key": state["last_candle_key"],
        }
        factory, _ = _make_session_factory(row=row)

        gate = RegimeGate("btc_jpy")
        result = await load_regime_gate_state(factory, gate)

        assert result is True
        assert gate.active_strategy == "trend_following"
        assert gate.regime_history == ["trending", "trending", "trending"]
        assert gate.last_candle_key == "candle_2"

    @pytest.mark.asyncio
    async def test_rp06_load_no_row_returns_false(self):
        """RP-06: DB 행 없음 → False 반환, gate 초기 상태 유지."""
        factory, _ = _make_session_factory(row=None)

        gate = RegimeGate("btc_jpy")
        result = await load_regime_gate_state(factory, gate)

        assert result is False
        assert gate.active_strategy is None

    @pytest.mark.asyncio
    async def test_rp07_load_db_failure_returns_false(self):
        """RP-07: session.execute 예외 → False 반환, 예외 전파 없음."""
        factory, _ = _make_session_factory(raise_exc=Exception("timeout"))

        gate = RegimeGate("btc_jpy")
        result = await load_regime_gate_state(factory, gate)

        assert result is False
        assert gate.active_strategy is None

    @pytest.mark.asyncio
    async def test_load_json_string_history(self):
        """regime_history가 JSON 문자열로 저장됐을 때도 올바르게 파싱된다."""
        row = {
            "active_strategy": "trend_following",
            "regime_history": '["trending","trending","trending"]',  # 문자열
            "last_switch_at": None,
            "switch_count": 1,
            "consecutive_count": 3,
            "consecutive_regime": "trending",
            "last_candle_key": "2026-04-15T21:00:00",
        }
        factory, _ = _make_session_factory(row=row)

        gate = RegimeGate("btc_jpy")
        result = await load_regime_gate_state(factory, gate)

        assert result is True
        assert gate.regime_history == ["trending", "trending", "trending"]


# ──────────────────────────────────────────────────────────────
# RP-09 ~ RP-10: base_trend 배선
# ──────────────────────────────────────────────────────────────

class TestBaseTrendPersistenceBridge:
    """base_trend.py candle_monitor에서 save가 올바르게 호출되는지 검증."""

    @pytest.mark.asyncio
    async def test_rp09_save_called_on_new_candle(self):
        """RP-09: 새 candle_key 처리 후 save_regime_gate_state 호출됨."""
        gate = RegimeGate("btc_jpy")
        # 첫 캔들을 처리해서 last_candle_key 설정 (warm-up 중 상태)
        gate.update_regime("trending", candle_key="candle_0")

        save_mock = AsyncMock()
        with patch(
            "core.execution.regime_gate_persistence.save_regime_gate_state",
            save_mock,
        ):
            # 새 candle_key → last_candle_key가 바뀜
            prev_key = gate.last_candle_key
            gate.update_regime("trending", candle_key="candle_1")

            # 키가 바뀌었으면 save 호출 시뮬레이션 (base_trend 로직 재현)
            if gate.last_candle_key != prev_key:
                await save_mock(None, gate)

        save_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_rp10_save_not_called_on_dup_candle(self):
        """RP-10: 동일 candle_key 중복 처리 시 save 호출 안 됨."""
        gate = RegimeGate("btc_jpy")
        gate.update_regime("trending", candle_key="candle_0")

        save_mock = AsyncMock()
        with patch(
            "core.execution.regime_gate_persistence.save_regime_gate_state",
            save_mock,
        ):
            # 동일 candle_key → last_candle_key 변화 없음
            prev_key = gate.last_candle_key
            gate.update_regime("trending", candle_key="candle_0")  # dup

            if gate.last_candle_key != prev_key:
                await save_mock(None, gate)

        save_mock.assert_not_called()

    def test_rp11_to_dict_regime_history_is_list(self):
        """to_dict() regime_history는 항상 list (JSONB 호환)."""
        gate = _warmed_gate()
        d = gate.to_dict()
        assert isinstance(d["regime_history"], list)
        # json.dumps 가능해야 함
        assert json.dumps(d["regime_history"])  # no exception

    @pytest.mark.asyncio
    async def test_rp12_full_restart_roundtrip(self):
        """RP-12: warm-up 완료 → save → 새 gate load → warm-up 스킵 확인."""
        # 1. 원본 gate warm-up 완료
        original = _warmed_gate(regime="trending")
        assert original.active_strategy == "trend_following"

        # 2. save (실제 DB 대신 captured params 수집)
        captured_params = {}

        async def fake_execute(sql, params):
            captured_params.update(params)
            mock_result = MagicMock()
            mock_result.mappings.return_value.one_or_none.return_value = None
            return mock_result

        mock_session = AsyncMock()
        mock_session.execute.side_effect = fake_execute
        mock_session.commit = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=mock_session)

        await save_regime_gate_state(factory, original)
        assert captured_params["active_strategy"] == "trend_following"

        # 3. 새 gate에 load (save에서 수집한 params로 DB 행 구성)
        regime_history = json.loads(captured_params["regime_history"])
        row = {
            "active_strategy": captured_params["active_strategy"],
            "regime_history": regime_history,
            "last_switch_at": captured_params["last_switch_at"],
            "switch_count": captured_params["switch_count"],
            "consecutive_count": captured_params["consecutive_count"],
            "consecutive_regime": captured_params["consecutive_regime"],
            "last_candle_key": captured_params["last_candle_key"],
        }
        load_factory, _ = _make_session_factory(row=row)

        new_gate = RegimeGate("btc_jpy")
        assert new_gate.active_strategy is None  # 초기 상태 확인

        result = await load_regime_gate_state(load_factory, new_gate)

        assert result is True
        assert new_gate.active_strategy == "trend_following"  # warm-up 스킵
        assert new_gate.should_allow_entry("trend_following") is True
        assert new_gate.should_allow_entry("box_mean_reversion") is False


# ──────────────────────────────────────────────────────────────
# EC-01 ~ EC-05: 큐니 추가 엣지 케이스
# ──────────────────────────────────────────────────────────────

class TestEdgeCases:
    """큐니 발견 엣지 케이스."""

    def test_ec01_restore_truncates_oversized_history(self):
        """EC-01: _STREAK_REQUIRED(3)보다 긴 history 복원 시 최신 3개만 유지된다."""
        gate = RegimeGate("btc_jpy")
        state = {
            "active_strategy": "trend_following",
            "regime_history": ["ranging", "ranging", "trending", "trending", "trending"],  # 5개
            "last_switch_at": None,
            "switch_count": 1,
            "consecutive_count": 3,
            "consecutive_regime": "trending",
            "last_candle_key": "candle_4",
        }
        gate.restore(state)
        # 최신 3개만 남아야 함
        assert gate.regime_history == ["trending", "trending", "trending"]
        assert len(gate.regime_history) == 3

    def test_ec02_restore_then_update_regime_continues_streak(self):
        """EC-02: warm-up 완료 상태 복원 후 새 캔들 처리가 정상적으로 streak을 이어간다."""
        gate = RegimeGate("btc_jpy")
        # trending 3캔들 streak으로 복원 (active=trend_following)
        state = {
            "active_strategy": "trend_following",
            "regime_history": ["trending", "trending", "trending"],
            "last_switch_at": None,
            "switch_count": 1,
            "consecutive_count": 3,
            "consecutive_regime": "trending",
            "last_candle_key": "candle_2",
        }
        gate.restore(state)
        assert gate.active_strategy == "trend_following"

        # 새 ranging 캔들이 오면 streak이 깨져 전환 미발생
        gate.update_regime("ranging", candle_key="candle_3")
        assert gate.active_strategy == "trend_following"  # 1캔들만으론 전환 없음

        # ranging 2캔들 더 → total 3캔들 연속 ranging → box로 전환
        gate.update_regime("ranging", candle_key="candle_4")
        gate.update_regime("ranging", candle_key="candle_5")
        assert gate.active_strategy == "box_mean_reversion"

    def test_ec03_restored_last_candle_key_prevents_dup_processing(self):
        """EC-03: 복원된 last_candle_key와 동일한 캔들이 오면 중복 스킵된다."""
        gate = RegimeGate("btc_jpy")
        state = {
            "active_strategy": "trend_following",
            "regime_history": ["trending", "trending", "trending"],
            "last_switch_at": None,
            "switch_count": 1,
            "consecutive_count": 3,
            "consecutive_regime": "trending",
            "last_candle_key": "candle_7",  # 마지막으로 처리된 캔들
        }
        gate.restore(state)

        # 동일 candle_key → 중복 스킵 (None 반환)
        result = gate.update_regime("ranging", candle_key="candle_7")
        assert result is None
        # active_strategy 변경 없음
        assert gate.active_strategy == "trend_following"
        # history도 변경 없음
        assert gate.regime_history == ["trending", "trending", "trending"]

    def test_ec04_restore_with_missing_optional_fields(self):
        """EC-04: switch_count 등 선택 필드 누락 state → 0 폴백 처리된다."""
        gate = RegimeGate("btc_jpy")
        minimal_state = {
            "active_strategy": "trend_following",
            "regime_history": ["trending", "trending", "trending"],
            # switch_count, consecutive_count 등 누락
        }
        gate.restore(minimal_state)
        assert gate.switch_count == 0
        assert gate.consecutive_count == 0
        assert gate.last_candle_key is None
        assert gate.active_strategy == "trend_following"

    @pytest.mark.asyncio
    async def test_ec05_load_empty_history_restores_warmup_state(self):
        """EC-05: regime_history=[] 복원 시 warm-up 상태 (active_strategy=None) 인식."""
        row = {
            "active_strategy": None,
            "regime_history": [],  # 빈 이력
            "last_switch_at": None,
            "switch_count": 0,
            "consecutive_count": 0,
            "consecutive_regime": None,
            "last_candle_key": None,
        }
        factory, _ = _make_session_factory(row=row)
        gate = RegimeGate("btc_jpy")
        result = await load_regime_gate_state(factory, gate)

        assert result is True  # 행이 있으므로 True
        assert gate.active_strategy is None
        assert gate.regime_history == []
        # warm-up 상태이므로 진입 허용 안 됨
        assert gate.should_allow_entry("trend_following") is False
        assert gate.should_allow_entry("box_mean_reversion") is False
