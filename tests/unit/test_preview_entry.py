"""
프리뷰 진입(미완성 캔들 기반 선진입) 단위 테스트.

커버:
  - RuleBasedDecision: entry_preview → entry_long (confidence 0.56)
  - RuleBasedDecision: entry_preview + 포지션 있음 → hold
  - RachelAdvisoryDecision: advisory=entry_long × signal=entry_preview → confidence×0.85, size×0.7
  - RachelAdvisoryDecision: advisory=hold × signal=entry_preview → hold
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.data.dto import Decision, PositionDTO, SignalSnapshot
from core.judge.decision.rachel_advisory import RachelAdvisoryDecision
from core.judge.decision.rule_based import RuleBasedDecision

# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

_NOW = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)


def _snapshot(
    signal: str = "entry_preview",
    position: PositionDTO | None = None,
    params: dict | None = None,
    is_preview: bool = True,
) -> SignalSnapshot:
    return SignalSnapshot(
        pair="BTC_JPY",
        exchange="bitflyer",
        timestamp=_NOW,
        signal=signal,
        current_price=10_000_000.0,
        exit_signal={"action": "hold", "reason": ""},
        position=position,
        params=params or {"position_size_pct": 1.0},
        is_preview=is_preview,
    )


def _pos() -> PositionDTO:
    return PositionDTO(
        pair="BTC_JPY",
        entry_price=9_800_000.0,
        entry_amount=0.5,
        stop_loss_price=9_600_000.0,
        stop_tightened=False,
    )


def _advisory(
    action: str = "entry_long",
    confidence: float = 0.65,
    size_pct: float = 0.5,
    expires_offset_hours: float = 4.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        pair="BTC_JPY",
        exchange="bitflyer",
        action=action,
        confidence=confidence,
        size_pct=size_pct,
        stop_loss=9_700_000.0,
        take_profit=None,
        regime="trending",
        reasoning="EMA 상향 + RSI 52",
        expires_at=_NOW + timedelta(hours=expires_offset_hours),
    )


def _make_rachel(
    advisory: SimpleNamespace | None = None,
    fallback_action: str = "hold",
) -> tuple[RachelAdvisoryDecision, AsyncMock]:
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
        raw_signal="entry_preview",
        timestamp=_NOW,
    )
    dec = RachelAdvisoryDecision(
        session_factory=MagicMock(),
        advisory_model=MagicMock(),
        fallback=fallback,
    )
    dec._fetch_advisory = AsyncMock(return_value=advisory)
    return dec, fallback


# ──────────────────────────────────────────────────────────────
# RuleBasedDecision: entry_preview
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rule_based_entry_preview_no_position():
    """
    Given: signal=entry_preview, 포지션 없음
    When:  RuleBasedDecision.decide()
    Then:  action=entry_long, confidence=0.56, source=rule_based_v1
    """
    dec = RuleBasedDecision()
    result = await dec.decide(_snapshot(signal="entry_preview", position=None))

    assert result.action == "entry_long"
    assert result.confidence == 0.56
    assert result.source == "rule_based_v1"
    assert result.raw_signal == "entry_preview"


@pytest.mark.asyncio
async def test_rule_based_entry_preview_with_position():
    """
    Given: signal=entry_preview, 포지션 있음
    When:  RuleBasedDecision.decide()
    Then:  action=hold (이미 포지션 보유 중)
    """
    dec = RuleBasedDecision()
    result = await dec.decide(_snapshot(signal="entry_preview", position=_pos()))

    assert result.action == "hold"


@pytest.mark.asyncio
async def test_rule_based_entry_preview_confidence_is_discounted():
    """
    Given: entry_ok → confidence 0.7 이지만
           entry_preview → confidence×0.8=0.56 으로 할인 확인
    """
    dec = RuleBasedDecision()
    preview_result = await dec.decide(
        _snapshot(signal="entry_preview", position=None, params={"position_size_pct": 1.0})
    )
    ok_result = await dec.decide(
        _snapshot(signal="entry_ok", position=None, params={"position_size_pct": 1.0})
    )

    assert preview_result.confidence < ok_result.confidence
    assert preview_result.confidence == pytest.approx(ok_result.confidence * 0.8, rel=0.01)


# ──────────────────────────────────────────────────────────────
# RachelAdvisoryDecision: entry_preview
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rachel_entry_long_advisory_with_entry_preview_signal():
    """
    Given: advisory=entry_long(미만료), signal=entry_preview, 포지션 없음
    When:  RachelAdvisoryDecision.decide()
    Then:  action=entry_long, confidence=advisory.confidence × 0.85, source=rachel_advisory
    """
    adv = _advisory(action="entry_long", confidence=0.65, size_pct=0.5)
    dec, _ = _make_rachel(advisory=adv)

    with patch("core.judge.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_preview"))

    assert result.action == "entry_long"
    assert result.confidence == pytest.approx(0.65 * 0.85, rel=0.01)
    assert result.source == "rachel_advisory"


@pytest.mark.asyncio
async def test_rachel_entry_preview_size_is_reduced():
    """
    Given: advisory=entry_long, size_pct=0.5, signal=entry_preview
    When:  RachelAdvisoryDecision.decide()
    Then:  size_pct = 0.5 × 0.7 = 0.35
    """
    adv = _advisory(action="entry_long", confidence=0.65, size_pct=0.5)
    dec, _ = _make_rachel(advisory=adv)

    with patch("core.judge.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_preview"))

    assert result.size_pct == pytest.approx(0.5 * 0.7, rel=0.01)


@pytest.mark.asyncio
async def test_rachel_hold_advisory_with_entry_preview_returns_hold():
    """
    Given: advisory=hold, signal=entry_preview
    When:  RachelAdvisoryDecision.decide()
    Then:  action=hold (레이첼 보류 → 프리뷰여도 진입 안 함)
    """
    adv = _advisory(action="hold", confidence=0.3)
    dec, _ = _make_rachel(advisory=adv)

    with patch("core.judge.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_preview"))

    assert result.action == "hold"


@pytest.mark.asyncio
async def test_rachel_entry_preview_less_confident_than_entry_ok():
    """
    Given: 동일 advisory(entry_long), signal=entry_preview vs signal=entry_ok
    When:  두 케이스 비교
    Then:  entry_preview의 confidence 및 size_pct < entry_ok
    """
    adv = _advisory(action="entry_long", confidence=0.65, size_pct=0.5)

    dec_preview, _ = _make_rachel(advisory=adv)
    dec_ok, _ = _make_rachel(advisory=adv)

    with patch("core.judge.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        preview_result = await dec_preview.decide(_snapshot(signal="entry_preview"))
        ok_result = await dec_ok.decide(_snapshot(signal="entry_ok"))

    assert preview_result.confidence < ok_result.confidence
    assert preview_result.size_pct < ok_result.size_pct


@pytest.mark.asyncio
async def test_rachel_no_advisory_with_entry_preview_falls_back_to_v1():
    """
    Given: advisory 없음, signal=entry_preview
    When:  RachelAdvisoryDecision.decide()
    Then:  v1 폴백 호출됨 (source=rule_based_v1)
    """
    dec, fallback = _make_rachel(advisory=None)

    with patch("core.judge.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_preview"))

    fallback.decide.assert_called_once()
    # fallback은 rule_based를 감싼 rachel_fallback_v1 소스로 반환됨
    assert result.source in ("rule_based_v1", "rachel_fallback_v1")


@pytest.mark.asyncio
async def test_is_preview_flag_on_signal_snapshot():
    """
    Given: SignalSnapshot에 is_preview=True 설정
    When:  dto 필드 확인
    Then:  is_preview=True 반환
    """
    snap = _snapshot(signal="entry_preview", is_preview=True)
    assert snap.is_preview is True


@pytest.mark.asyncio
async def test_is_preview_default_false():
    """
    Given: SignalSnapshot을 is_preview 미지정으로 생성
    When:  is_preview 확인
    Then:  False (기본값)
    """
    snap = SignalSnapshot(
        pair="BTC_JPY",
        exchange="bitflyer",
        timestamp=_NOW,
        signal="entry_ok",
        current_price=10_000_000.0,
        exit_signal={"action": "hold", "reason": ""},
    )
    assert snap.is_preview is False


# ──────────────────────────────────────────────────────────────
# 엣지 케이스 보강
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rule_based_entry_preview_size_pct_from_params():
    """
    Given: signal=entry_preview, position_size_pct=0.5
    When:  RuleBasedDecision.decide()
    Then:  size_pct는 position_size_pct 그대로 (rule_based는 size 할인 없음)
          confidence만 0.56으로 줄어들어야 함
    """
    dec = RuleBasedDecision()
    result_preview = await dec.decide(
        _snapshot(signal="entry_preview", position=None, params={"position_size_pct": 0.5})
    )
    result_ok = await dec.decide(
        _snapshot(signal="entry_ok", position=None, params={"position_size_pct": 0.5})
    )

    # size_pct는 동일 (rule_based는 프리뷰라도 사이즈 할인 안 함)
    assert result_preview.size_pct == result_ok.size_pct
    # confidence만 할인
    assert result_preview.confidence < result_ok.confidence


@pytest.mark.asyncio
async def test_rachel_entry_preview_zero_size_stays_zero():
    """
    Given: advisory.size_pct = None (사이즈 미지정)
    When:  entry_preview 처리
    Then:  size_pct = None × 0.7 → 0.0 (예외 없음)
    """
    adv = _advisory(action="entry_long", confidence=0.65, size_pct=None)
    dec, _ = _make_rachel(advisory=adv)

    with patch("core.judge.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_preview"))

    assert result.action == "entry_long"
    assert result.size_pct == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_rachel_entry_preview_confidence_precision():
    """
    Given: advisory.confidence=0.70
    When:  entry_preview → confidence × 0.85
    Then:  결과 confidence = round(0.70 × 0.85, 4) = 0.595
    """
    adv = _advisory(action="entry_long", confidence=0.70, size_pct=0.5)
    dec, _ = _make_rachel(advisory=adv)

    with patch("core.judge.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_preview"))

    assert result.confidence == pytest.approx(round(0.70 * 0.85, 4))


@pytest.mark.asyncio
async def test_rachel_expiry_near_with_entry_preview_suppressed():
    """
    Given: advisory 만료까지 30분 미만 (< EXPIRY_GUARD_SEC=1H) + signal=entry_preview
    When:  RachelAdvisoryDecision.decide()
    Then:  진입 억제 (hold 또는 v1 폴백)
    """
    # 만료 30분 남음 — _EXPIRY_GUARD_SEC(3600초) 이내이므로 만료 근접 억제
    adv = _advisory(action="entry_long", confidence=0.65, expires_offset_hours=0.5)
    dec, _ = _make_rachel(advisory=adv)

    with patch("core.judge.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_preview"))

    # 만료 근접 → 진입 억제 (hold)
    assert result.action == "hold"
