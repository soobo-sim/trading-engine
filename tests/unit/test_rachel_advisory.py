"""
core/decision/rachel_advisory.py 단위 테스트 — RachelAdvisoryDecision.

advisory DB 조회를 AsyncMock으로 mock하여 외부 의존 없이 검증한다.

테스트 케이스 설계 (재설계 분석서 기반):
  - advisory 미만료 + entry_ok signal → entry_long 진입
  - advisory entry_long + hold signal → hold (타이밍 미충족)
  - advisory hold + entry_ok signal → hold (레이첼 보류 존중)
  - advisory exit + 포지션 있음 → exit
  - advisory 만료됨 → v1 폴백 (source=rachel_fallback_v1)
  - advisory 없음 → v1 폴백 + WARNING
  - exit_warning signal → 항상 exit (advisory 무관)
  - tighten_stop signal → 항상 tighten_stop
  - advisory entry_long + 만료 근접 (< 1H) → 진입 억제
  - advisory DB 조회 예외 → v1 폴백
  - entry_short advisory + entry_sell signal → entry_short 진입
  - advisory entry_short + hold signal → hold
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.data.dto import Decision, PositionDTO, SignalSnapshot
from core.decision.rachel_advisory import RachelAdvisoryDecision

# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

_NOW = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)


def _snapshot(
    signal: str = "entry_ok",
    exit_action: str = "hold",
    position: PositionDTO | None = None,
) -> SignalSnapshot:
    return SignalSnapshot(
        pair="BTC_JPY",
        exchange="bitflyer",
        timestamp=_NOW,
        signal=signal,
        current_price=10_000_000.0,
        exit_signal={"action": exit_action, "reason": ""},
        position=position,
        params={"position_size_pct": 1.0},
    )


def _advisory(
    action: str = "entry_long",
    confidence: float = 0.65,
    size_pct: float | None = 0.5,
    stop_loss: float | None = 9_700_000.0,
    take_profit: float | None = None,
    expires_offset_hours: float = 4.0,
    reasoning: str = "EMA 상향 + RSI 52 + 매크로 금리 동결 기대",
) -> SimpleNamespace:
    """테스트용 RachelAdvisory 유사 객체."""
    return SimpleNamespace(
        id=1,
        pair="BTC_JPY",
        exchange="bitflyer",
        action=action,
        confidence=confidence,
        size_pct=size_pct,
        stop_loss=stop_loss,
        take_profit=take_profit,
        regime="trending",
        reasoning=reasoning,
        expires_at=_NOW + timedelta(hours=expires_offset_hours),
    )


def _pos() -> PositionDTO:
    return PositionDTO(
        pair="BTC_JPY",
        entry_price=9_800_000.0,
        entry_amount=0.5,
        stop_loss_price=9_600_000.0,
        stop_tightened=False,
    )


def _make_decision(
    advisory: SimpleNamespace | None = None,
    fallback_action: str = "hold",
) -> tuple[RachelAdvisoryDecision, AsyncMock]:
    """RachelAdvisoryDecision + fallback mock 반환.

    _fetch_advisory를 직접 AsyncMock으로 교체 — SQLAlchemy select() 호출 없이
    advisory 반환값만 제어한다.
    """
    fallback = AsyncMock()
    fallback.decide.return_value = Decision(
        action=fallback_action,
        pair="BTC_JPY",
        exchange="bitflyer",
        confidence=0.3,
        size_pct=0.5,
        stop_loss=None,
        take_profit=None,
        reasoning="v1 폴백",
        risk_factors=(),
        source="rule_based_v1",
        trigger="regular_4h",
        raw_signal="entry_ok",
        timestamp=_NOW,
    )

    dec = RachelAdvisoryDecision(
        session_factory=MagicMock(),
        advisory_model=MagicMock(),
        fallback=fallback,
    )
    # SQLAlchemy ORM 대신 직접 반환값 설정
    dec._fetch_advisory = AsyncMock(return_value=advisory)
    return dec, fallback


# ──────────────────────────────────────────────────────────────
# 진입 테스트
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_entry_long_advisory_with_entry_ok_signal():
    """
    Given: advisory=entry_long(미만료), signal=entry_ok, 포지션 없음
    When:  decide()
    Then:  action=entry_long, source=rachel_advisory, confidence=advisory.confidence
    """
    adv = _advisory(action="entry_long", confidence=0.65)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok"))

    assert result.action == "entry_long"
    assert result.confidence == 0.65
    assert result.source == "rachel_advisory"
    assert result.stop_loss == adv.stop_loss


@pytest.mark.asyncio
async def test_entry_long_advisory_with_hold_signal_returns_hold():
    """
    Given: advisory=entry_long(미만료), signal=hold, 포지션 없음
    When:  decide()
    Then:  action=hold (타이밍 미충족)
    """
    adv = _advisory(action="entry_long")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="hold"))

    assert result.action == "hold"
    assert result.source == "rachel_advisory"
    assert "타이밍 미충족" in result.reasoning


@pytest.mark.asyncio
async def test_entry_short_advisory_with_entry_sell_signal():
    """
    Given: advisory=entry_short(미만료), signal=entry_sell
    When:  decide()
    Then:  action=entry_short
    """
    adv = _advisory(action="entry_short")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_sell"))

    assert result.action == "entry_short"
    assert result.source == "rachel_advisory"


@pytest.mark.asyncio
async def test_entry_short_advisory_with_wrong_signal_returns_hold():
    """
    Given: advisory=entry_short, signal=entry_ok (방향 불일치)
    When:  decide()
    Then:  action=hold
    """
    adv = _advisory(action="entry_short")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok"))

    assert result.action == "hold"


# ──────────────────────────────────────────────────────────────
# hold / exit advisory
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hold_advisory_overrides_entry_signal():
    """
    Given: advisory=hold, signal=entry_ok
    When:  decide()
    Then:  action=hold (레이첼 보류 존중)
    """
    adv = _advisory(action="hold")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok"))

    assert result.action == "hold"
    assert "레이첼 advisory hold" in result.reasoning


@pytest.mark.asyncio
async def test_exit_advisory_with_position_triggers_exit():
    """
    Given: advisory=exit, 포지션 있음
    When:  decide()
    Then:  action=exit
    """
    adv = _advisory(action="exit")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(position=_pos()))

    assert result.action == "exit"
    assert result.source == "rachel_advisory"


@pytest.mark.asyncio
async def test_exit_advisory_without_position_returns_hold():
    """
    Given: advisory=exit, 포지션 없음
    When:  decide()
    Then:  hold (exit할 포지션이 없으면 advisory exit 규칙이 적용되지 않음)
    """
    adv = _advisory(action="exit")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(position=None))

    assert result.action == "hold"


# ──────────────────────────────────────────────────────────────
# 실시간 시그널 우선 (advisory 무관)
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exit_warning_signal_always_exits():
    """
    Given: advisory=entry_long, signal=exit_warning, 포지션 있음
    When:  decide()
    Then:  action=exit (긴급 시그널 우선)
    """
    adv = _advisory(action="entry_long")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(
            _snapshot(signal="hold", exit_action="exit_warning", position=_pos())
        )

    assert result.action == "exit"
    assert "긴급 시그널" in result.reasoning


@pytest.mark.asyncio
async def test_tighten_stop_signal_always_applies():
    """
    Given: advisory=hold, signal=tighten_stop, 포지션 있음
    When:  decide()
    Then:  action=tighten_stop
    """
    adv = _advisory(action="hold")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(
            _snapshot(signal="hold", exit_action="tighten_stop", position=_pos())
        )

    assert result.action == "tighten_stop"


# ──────────────────────────────────────────────────────────────
# 만료 + 폴백
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_advisory_falls_back_to_v1():
    """
    Given: advisory.expires_at < now (만료됨)
    When:  decide()
    Then:  source=rachel_fallback_v1, v1 폴백 호출
    """
    # expires_at을 1시간 전으로 설정
    adv = _advisory(expires_offset_hours=-1.0)  # 과거
    dec, fallback = _make_decision(advisory=adv, fallback_action="hold")

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot())

    assert result.source == "rachel_fallback_v1"
    fallback.decide.assert_called_once()


@pytest.mark.asyncio
async def test_no_advisory_falls_back_to_v1():
    """
    Given: DB에 advisory 없음 (None 반환)
    When:  decide()
    Then:  source=rachel_fallback_v1
    """
    dec, fallback = _make_decision(advisory=None, fallback_action="entry_long")

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot())

    assert result.source == "rachel_fallback_v1"
    fallback.decide.assert_called_once()


@pytest.mark.asyncio
async def test_db_exception_falls_back_to_v1():
    """
    Given: _fetch_advisory()가 None 반환 (DB 예외 내부 처리 → None)
    When:  decide()
    Then:  source=rachel_fallback_v1 (예외 삼킴 후 폴백)

    Note: 실제 _fetch_advisory는 DB 예외를 try-except로 catch하여 None을
    반환한다. 여기서는 그 결과(None)를 직접 mock하여 폴백 경로를 검증한다.
    """
    dec, fallback = _make_decision(advisory=None, fallback_action="hold")

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot())

    assert result.source == "rachel_fallback_v1"
    fallback.decide.assert_called_once()


@pytest.mark.asyncio
async def test_expiry_guard_suppresses_entry():
    """
    Given: advisory=entry_long, 만료까지 30분 남음 (< 1H 임계값)
    When:  decide()
    Then:  action=hold (만료 임박 진입 억제)
    """
    adv = _advisory(action="entry_long", expires_offset_hours=0.5)  # 30분 후 만료
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok"))

    assert result.action == "hold"
    assert "만료 임박" in result.reasoning


# ──────────────────────────────────────────────────────────────
# 큐니 보강 — 추가 엣지케이스
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_entry_long_advisory_with_existing_position_returns_hold():
    """
    Given: advisory=entry_long, signal=entry_ok, 포지션 이미 있음
    When:  decide()
    Then:  action=hold (재진입 차단 — has_position=True로 entry 조건 미충족)
    """
    adv = _advisory(action="entry_long")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok", position=_pos()))

    assert result.action == "hold"
    assert result.source == "rachel_advisory"


@pytest.mark.asyncio
async def test_full_exit_signal_triggers_exit_regardless_of_advisory():
    """
    Given: advisory=hold, exit_signal=full_exit, 포지션 있음
    When:  decide()
    Then:  action=exit (full_exit도 긴급 시그널로 처리)
    """
    adv = _advisory(action="hold")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(
            _snapshot(signal="hold", exit_action="full_exit", position=_pos())
        )

    assert result.action == "exit"
    assert "긴급 시그널" in result.reasoning


@pytest.mark.asyncio
async def test_advisory_without_size_pct_uses_none():
    """
    Given: advisory.size_pct=None (재량에 맡김), entry_long + entry_ok
    When:  decide()
    Then:  Decision.size_pct=None (엔진이 기본값 사용)
    """
    adv = _advisory(action="entry_long", size_pct=None)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok"))

    assert result.action == "entry_long"
    assert result.size_pct is None


@pytest.mark.asyncio
async def test_expired_advisory_with_exit_warning_delegates_to_fallback():
    """
    Given: advisory 만료됨, exit_warning 시그널, 포지션 있음
    When:  decide()
    Then:  source=rachel_fallback_v1 (v1 fallback이 exit_warning 처리)

    Note: advisory 만료 시 fallback(v1)에 전체 처리를 위임한다.
    exit_warning이 v1에서도 exit로 처리되어야 하지만, 이 테스트는
    advisory 만료 → fallback 위임 경로가 올바른지만 검증한다.
    """
    adv = _advisory(expires_offset_hours=-1.0)  # 만료됨
    dec, fallback = _make_decision(advisory=adv, fallback_action="exit")

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(
            _snapshot(signal="hold", exit_action="exit_warning", position=_pos())
        )

    assert result.source == "rachel_fallback_v1"
    fallback.decide.assert_called_once()


@pytest.mark.asyncio
async def test_high_confidence_advisory_propagates_to_decision():
    """
    Given: advisory confidence=0.95 (최고 확신도)
    When:  decide() → entry_long 진입
    Then:  Decision.confidence == 0.95 (advisory 확신도 그대로 전달)
    """
    adv = _advisory(action="entry_long", confidence=0.95)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok"))

    assert result.action == "entry_long"
    assert result.confidence == pytest.approx(0.95)


# ── adjust_risk 테스트 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_adjust_risk_returns_adjust_risk_when_position_exists():
    """
    Given: advisory action=adjust_risk, adjustments={stop_loss_pct:1.5}, 포지션 있음
    When:  decide()
    Then:  action=adjust_risk, meta["adjustments"] 포함
    """
    adv = SimpleNamespace(
        id=10,
        pair="BTC_JPY",
        exchange="bitflyer",
        action="adjust_risk",
        confidence=0.7,
        size_pct=None,
        stop_loss=None,
        take_profit=None,
        adjustments={"stop_loss_pct": 1.5},
        regime="ranging",
        reasoning="변동성 확대 → SL 확대",
        expires_at=_NOW + timedelta(hours=4.0),
    )
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="hold", position=_pos()))

    assert result.action == "adjust_risk"
    assert result.meta.get("adjustments") == {"stop_loss_pct": 1.5}


@pytest.mark.asyncio
async def test_adjust_risk_returns_hold_when_no_position():
    """
    Given: advisory action=adjust_risk, 포지션 없음
    When:  decide()
    Then:  action=hold (조정할 포지션 없음)
    """
    adv = SimpleNamespace(
        id=11,
        pair="BTC_JPY",
        exchange="bitflyer",
        action="adjust_risk",
        confidence=0.7,
        size_pct=None,
        stop_loss=None,
        take_profit=None,
        adjustments={"stop_loss_pct": 2.0},
        regime="ranging",
        reasoning="포지션 없는데 adjust_risk",
        expires_at=_NOW + timedelta(hours=4.0),
    )
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        # position=None — 포지션 없음
        result = await dec.decide(_snapshot(signal="hold"))

    assert result.action == "hold"


@pytest.mark.asyncio
async def test_adjust_risk_empty_adjustments_still_returns_adjust_risk():
    """
    Given: advisory action=adjust_risk, adjustments=None (빈값), 포지션 있음
    When:  decide()
    Then:  action=adjust_risk, meta["adjustments"]={} (빈 dict)
    """
    adv = SimpleNamespace(
        id=12,
        pair="BTC_JPY",
        exchange="bitflyer",
        action="adjust_risk",
        confidence=0.6,
        size_pct=None,
        stop_loss=9_500_000.0,
        take_profit=None,
        adjustments=None,  # None → {}로 처리
        regime="ranging",
        reasoning="adjustments 없음",
        expires_at=_NOW + timedelta(hours=2.0),
    )
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="hold", position=_pos()))

    assert result.action == "adjust_risk"
    assert result.meta.get("adjustments") == {}
