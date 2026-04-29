"""
core/data/dto.py 단위 테스트.

Given/When/Then 형식으로 DTO 불변성 + modify_decision 헬퍼를 검증한다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.data.dto import (
    CandleDTO,
    Decision,
    ExecutionResult,
    GuardrailResult,
    LessonDTO,
    PositionDTO,
    SignalSnapshot,
    modify_decision,
)


# ── CandleDTO ─────────────────────────────────────────────────


def test_candle_dto_is_frozen():
    """CandleDTO는 frozen — 속성 수정 불가."""
    c = CandleDTO(
        open_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        open=100.0, high=110.0, low=95.0, close=105.0,
    )
    with pytest.raises((TypeError, AttributeError)):
        c.close = 200.0  # type: ignore[misc]


# ── PositionDTO ────────────────────────────────────────────────


def test_position_dto_is_frozen():
    """PositionDTO는 frozen."""
    p = PositionDTO(pair="BTC_JPY", entry_price=5_000_000.0, entry_amount=0.01)
    with pytest.raises((TypeError, AttributeError)):
        p.entry_price = 6_000_000.0  # type: ignore[misc]


def test_position_dto_defaults():
    """선택 필드 기본값 확인."""
    p = PositionDTO(pair="USD_JPY", entry_price=150.0, entry_amount=1.0)
    assert p.stop_loss_price is None
    assert p.stop_tightened is False
    assert p.extra == {}


# ── Decision ─────────────────────────────────────────────────


def _make_decision(**overrides) -> Decision:
    defaults = dict(
        action="entry_long",
        pair="BTC_JPY",
        exchange="bitflyer",
        confidence=0.7,
        size_pct=1.0,
        stop_loss=4_900_000.0,
        take_profit=None,
        reasoning="롱 진입",
        risk_factors=(),
        source="rule_based_v1",
        trigger="regular_4h",
        raw_signal="long_setup",
    )
    defaults.update(overrides)
    return Decision(**defaults)


def test_decision_is_frozen():
    """Decision은 frozen."""
    d = _make_decision()
    with pytest.raises((TypeError, AttributeError)):
        d.action = "hold"  # type: ignore[misc]


def test_modify_decision_returns_new_instance():
    """modify_decision은 원본을 변경하지 않고 새 인스턴스를 반환한다."""
    original = _make_decision(action="entry_long", size_pct=1.0)
    modified = modify_decision(original, action="blocked", size_pct=0.0)

    assert original.action == "entry_long"
    assert original.size_pct == 1.0
    assert modified.action == "blocked"
    assert modified.size_pct == 0.0
    assert original is not modified


# ── SignalSnapshot ────────────────────────────────────────────


def _make_snapshot(**overrides) -> SignalSnapshot:
    ts = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    defaults = dict(
        pair="BTC_JPY",
        exchange="bitflyer",
        timestamp=ts,
        signal="long_setup",
        current_price=5_000_000.0,
        exit_signal={"action": "hold", "reason": "추세 유지"},
    )
    defaults.update(overrides)
    return SignalSnapshot(**defaults)


def test_snapshot_is_frozen():
    """SignalSnapshot은 frozen."""
    s = _make_snapshot()
    with pytest.raises((TypeError, AttributeError)):
        s.signal = "no_signal"  # type: ignore[misc]


def test_snapshot_optional_fields_default_to_none():
    """선택 필드는 기본값 None/empty."""
    s = _make_snapshot()
    assert s.ema is None
    assert s.position is None
    assert s.macro is None
    assert s.news is None
    assert s.relevant_lessons is None


# ── GuardrailResult + ExecutionResult ────────────────────────


def test_guardrail_result_fields():
    """GuardrailResult 필드 접근."""
    d = _make_decision()
    gr = GuardrailResult(
        approved=True,
        final_decision=d,
        rejection_reason=None,
        violations=(),
    )
    assert gr.approved is True
    assert gr.violations == ()


def test_execution_result_blocked():
    """차단된 ExecutionResult."""
    er = ExecutionResult(action="blocked", executed=False, reason="GR-01 초과")
    assert er.action == "blocked"
    assert er.executed is False
    assert er.decision is None
