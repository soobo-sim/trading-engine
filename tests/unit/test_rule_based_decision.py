"""
core/decision/rule_based.py 단위 테스트 — RuleBasedDecision.

Given/When/Then 형식.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.data.dto import PositionDTO, SignalSnapshot
from core.decision.rule_based import RuleBasedDecision


def _make_snapshot(
    signal: str,
    exit_action: str = "hold",
    exit_reason: str = "",
    position: PositionDTO | None = None,
    exit_triggers: dict | None = None,
    rsi: float | None = None,
    params: dict | None = None,
) -> SignalSnapshot:
    ts = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    triggers = exit_triggers or {}
    return SignalSnapshot(
        pair="BTC_JPY",
        exchange="bitflyer",
        timestamp=ts,
        signal=signal,
        current_price=5_000_000.0,
        exit_signal={"action": exit_action, "reason": exit_reason, "triggers": triggers},
        rsi=rsi,
        stop_loss_price=4_900_000.0,
        position=position,
        params=params or {"position_size_pct": 1.0},
    )


def _pos(stop_tightened: bool = False) -> PositionDTO:
    return PositionDTO(
        pair="BTC_JPY",
        entry_price=4_800_000.0,
        entry_amount=0.01,
        stop_loss_price=4_700_000.0,
        stop_tightened=stop_tightened,
    )


# ── 진입 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_entry_long_when_entry_ok_no_position():
    """
    Given: signal=entry_ok, 포지션 없음
    When:  decide() 호출
    Then:  action=entry_long, source=rule_based_v1
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot("entry_ok", position=None)
    result = await dec.decide(snapshot)

    assert result.action == "entry_long"
    assert result.source == "rule_based_v1"
    assert result.trigger == "regular_4h"
    assert result.raw_signal == "entry_ok"
    assert result.stop_loss == 4_900_000.0


@pytest.mark.asyncio
async def test_entry_short_when_entry_sell_no_position():
    """
    Given: signal=entry_sell, 포지션 없음
    When:  decide() 호출
    Then:  action=entry_short
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot("entry_sell", position=None)
    result = await dec.decide(snapshot)

    assert result.action == "entry_short"


@pytest.mark.asyncio
async def test_hold_when_no_signal_no_position():
    """
    Given: signal=no_signal, 포지션 없음
    When:  decide() 호출
    Then:  action=hold
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot("no_signal", position=None)
    result = await dec.decide(snapshot)

    assert result.action == "hold"


# ── 청산 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exit_when_exit_warning_with_position():
    """
    Given: signal=exit_warning, 포지션 있음
    When:  decide() 호출
    Then:  action=exit, trigger=exit_warning
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot("exit_warning", position=_pos())
    result = await dec.decide(snapshot)

    assert result.action == "exit"
    assert result.trigger == "exit_warning"
    assert result.size_pct == 1.0  # 전량 청산


@pytest.mark.asyncio
async def test_exit_when_full_exit_ema_slope():
    """
    Given: signal=no_signal, exit_signal.action=full_exit, ema_slope_negative 트리거
    When:  decide() 호출
    Then:  action=exit, trigger=full_exit_ema_slope
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot(
        "no_signal",
        exit_action="full_exit",
        exit_triggers={"ema_slope_negative": True},
        position=_pos(),
    )
    result = await dec.decide(snapshot)

    assert result.action == "exit"
    assert result.trigger == "full_exit_ema_slope"


@pytest.mark.asyncio
async def test_exit_when_full_exit_rsi_breakdown():
    """
    Given: exit_signal.action=full_exit, rsi_breakdown 트리거
    When:  decide() 호출
    Then:  trigger=full_exit_rsi_breakdown
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot(
        "no_signal",
        exit_action="full_exit",
        exit_triggers={"rsi_breakdown": True},
        position=_pos(),
    )
    result = await dec.decide(snapshot)

    assert result.action == "exit"
    assert result.trigger == "full_exit_rsi_breakdown"


# ── 스탑 타이트닝 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tighten_stop_when_not_yet_tightened():
    """
    Given: exit_signal.action=tighten_stop, stop_tightened=False
    When:  decide() 호출
    Then:  action=tighten_stop
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot(
        "no_signal",
        exit_action="tighten_stop",
        position=_pos(stop_tightened=False),
    )
    result = await dec.decide(snapshot)

    assert result.action == "tighten_stop"
    assert result.size_pct == 0.0


@pytest.mark.asyncio
async def test_hold_when_tighten_stop_already_tightened():
    """
    Given: exit_signal.action=tighten_stop, stop_tightened=True (이미 적용됨)
    When:  decide() 호출
    Then:  action=hold (중복 적용 방지)
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot(
        "no_signal",
        exit_action="tighten_stop",
        position=_pos(stop_tightened=True),
    )
    result = await dec.decide(snapshot)

    assert result.action == "hold"


# ── 사이징 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_size_pct_comes_from_params():
    """
    Given: params.position_size_pct=0.5
    When:  entry_long 결정
    Then:  size_pct=0.5
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot("entry_ok", position=None, params={"position_size_pct": 0.5})
    result = await dec.decide(snapshot)

    assert result.size_pct == 0.5


@pytest.mark.asyncio
async def test_rsi_risk_factor_added_when_high():
    """
    Given: RSI=62 (> 60)
    When:  entry_long 결정
    Then:  risk_factors에 RSI 경고 포함
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot("entry_ok", position=None, rsi=62.0)
    result = await dec.decide(snapshot)

    assert result.action == "entry_long"
    assert any("RSI" in f for f in result.risk_factors)


# ── 엣지케이스 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exit_warning_with_no_position_returns_hold():
    """
    Given: signal=exit_warning, 포지션 없음 (비정상 상태)
    When:  decide() 호출
    Then:  action=hold — 포지션 없으면 청산 시도 안함
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot("exit_warning", position=None)
    result = await dec.decide(snapshot)

    assert result.action == "hold"


@pytest.mark.asyncio
async def test_entry_ok_with_existing_position_returns_hold():
    """
    Given: signal=entry_ok, 포지션 이미 있음
    When:  decide() 호출
    Then:  action=hold — 이중 진입 방지
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot(
        "entry_ok",
        position=_pos(),
        exit_action="hold",
    )
    result = await dec.decide(snapshot)

    assert result.action == "hold"


@pytest.mark.asyncio
async def test_full_exit_with_no_triggers_uses_generic_trigger():
    """
    Given: exit_signal.action=full_exit, triggers 없음
    When:  decide() 호출
    Then:  trigger="full_exit" (generic)
    """
    dec = RuleBasedDecision()
    snapshot = _make_snapshot(
        "no_signal",
        exit_action="full_exit",
        exit_triggers={},     # 트리거 없음
        position=_pos(),
    )
    result = await dec.decide(snapshot)

    assert result.action == "exit"
    assert result.trigger == "full_exit"


@pytest.mark.asyncio
async def test_decision_has_timestamp():
    """모든 Decision에 timestamp가 설정됨."""
    dec = RuleBasedDecision()
    snapshot = _make_snapshot("entry_ok", position=None)
    result = await dec.decide(snapshot)

    assert result.timestamp is not None


@pytest.mark.asyncio
async def test_source_is_always_rule_based_v1():
    """source는 항상 rule_based_v1."""
    dec = RuleBasedDecision()
    for signal, pos in [("entry_ok", None), ("exit_warning", _pos()), ("no_signal", None)]:
        snapshot = _make_snapshot(signal, position=pos)
        result = await dec.decide(snapshot)
        assert result.source == "rule_based_v1", f"signal={signal}: source={result.source}"


# ──────────────────────────────────────────────────────────────
# 로깅 검증 — RuleBasedDecision 서사 로그
# ──────────────────────────────────────────────────────────────

class TestRuleBasedDecisionLogging:
    """decide() 분기별 로그 레벨과 메시지 내용 검증."""

    @pytest.mark.asyncio
    async def test_entry_long_logs_info(self, caplog):
        """
        Given: signal=entry_ok, 포지션 없음
        When:  decide()
        Then:  INFO 로그 1건 — action=entry_long 포함
        """
        import logging
        dec = RuleBasedDecision()
        snapshot = _make_snapshot("entry_ok", position=None)
        with caplog.at_level(logging.INFO, logger="core.decision.rule_based"):
            result = await dec.decide(snapshot)
        assert result.action == "entry_long"
        info = [r for r in caplog.records if r.levelname == "INFO" and "RuleBasedDecision" in r.message]
        assert len(info) == 1
        assert "entry_long" in info[0].message

    @pytest.mark.asyncio
    async def test_hold_logs_debug_not_info(self, caplog):
        """
        Given: signal=hold, 포지션 없음
        When:  decide()
        Then:  INFO 로그 없음 (DEBUG만)
        """
        import logging
        dec = RuleBasedDecision()
        snapshot = _make_snapshot("hold", position=None)
        with caplog.at_level(logging.DEBUG, logger="core.decision.rule_based"):
            result = await dec.decide(snapshot)
        assert result.action == "hold"
        info = [r for r in caplog.records if r.levelname == "INFO" and "RuleBasedDecision" in r.message]
        assert len(info) == 0
        debug = [r for r in caplog.records if r.levelname == "DEBUG" and "RuleBasedDecision" in r.message]
        assert len(debug) == 1
        assert "hold" in debug[0].message

    @pytest.mark.asyncio
    async def test_exit_warning_logs_info(self, caplog):
        """
        Given: signal=exit_warning, 포지션 있음
        When:  decide()
        Then:  INFO 로그 1건 — action=exit 포함
        """
        import logging
        dec = RuleBasedDecision()
        snapshot = _make_snapshot("exit_warning", position=_pos())
        with caplog.at_level(logging.INFO, logger="core.decision.rule_based"):
            result = await dec.decide(snapshot)
        assert result.action == "exit"
        info = [r for r in caplog.records if r.levelname == "INFO" and "RuleBasedDecision" in r.message]
        assert len(info) == 1
        assert "exit" in info[0].message

    @pytest.mark.asyncio
    async def test_tighten_stop_logs_info(self, caplog):
        """
        Given: exit_action=tighten_stop, stop_tightened=False
        When:  decide()
        Then:  INFO 로그 1건 — action=tighten_stop 포함 (DEBUG 아님)
        """
        import logging
        dec = RuleBasedDecision()
        snapshot = _make_snapshot("no_signal", exit_action="tighten_stop", position=_pos(stop_tightened=False))
        with caplog.at_level(logging.DEBUG, logger="core.decision.rule_based"):
            result = await dec.decide(snapshot)
        assert result.action == "tighten_stop"
        info = [r for r in caplog.records if r.levelname == "INFO" and "RuleBasedDecision" in r.message]
        assert len(info) == 1
        assert "tighten_stop" in info[0].message
        debug = [r for r in caplog.records if r.levelname == "DEBUG" and "RuleBasedDecision" in r.message]
        assert len(debug) == 0

