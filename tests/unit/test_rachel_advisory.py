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
    hold_override_policy: str = "none",
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
        hold_override_policy=hold_override_policy,
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
async def test_entry_long_advisory_with_existing_position_auto_converts_to_add_position():
    """
    AP-01 (BUG-036): advisory=entry_long + 포지션 보유(롱, 수익 중, pyramid=0)
    When:  decide()
    Then:  action=add_position (자동 전환 후 4중 안전장치 통과)

    변경 (BUG-036): 기존에는 has_position=True로 entry 조건 미충족 → hold 낙하.
    수정 후: 동일 방향 포지션 보유 시 entry_long → add_position 자동 전환.
    """
    adv = _advisory(action="entry_long")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok", position=_pos()))

    # _pos() — entry_price=9.8M < current_price=10M → 수익 중
    # pyramid_count 미설정(0), side 미설정("buy") → same_direction + 안전장치 통과
    assert result.action == "add_position"
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


@pytest.mark.asyncio
async def test_fetch_advisory_matches_lowercase_pair():
    """
    Given: snapshot.pair='btc_jpy' (소문자 — GMO Coin 스타일)
           DB에 저장된 advisory pair='btc_jpy' (소문자 — POST /api/advisories 그대로 저장)
    When:  _fetch_advisory('btc_jpy', 'gmo_coin')
    Then:  pair 그대로 WHERE 조회 → 레코드 반환

    배경: POST /api/advisories는 body.pair 값을 그대로 저장하며, 레이첼은 'btc_jpy'
    소문자로 POST한다. _fetch_advisory도 pair를 그대로 조회해야 일치한다.
    과거: pair.upper()로 'BTC_JPY' 변환 조회 → DB에 'btc_jpy'로 저장된 레코드와
    불일치하여 항상 "advisory 없음" WARNING이 발생하던 버그 (2026-04-15 수정).
    """
    from sqlalchemy import select as sa_select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from adapters.database.models import RachelAdvisory
    from adapters.database.session import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        target = [Base.metadata.tables["rachel_advisories"]]
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=target))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # 소문자 btc_jpy로 저장 (실제 레이첼 POST 형식)
    async with factory() as session:
        row = RachelAdvisory(
            pair="btc_jpy",
            exchange="gmo_coin",
            action="hold",
            confidence=0.3,
            reasoning="대소문자 정합성 테스트용 advisory (20자 이상)",
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
        session.add(row)
        await session.commit()

    dec = RachelAdvisoryDecision(
        session_factory=factory,
        advisory_model=RachelAdvisory,
        fallback=AsyncMock(),
    )

    # 소문자 btc_jpy로 조회 → 그대로 매칭
    result = await dec._fetch_advisory("btc_jpy", "gmo_coin")
    assert result is not None, "pair 소문자 입력 시 advisory를 찾아야 함"
    assert result.pair == "btc_jpy"
    assert result.action == "hold"

    await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_advisory_uppercase_input_matches_lowercase_db():
    """
    ADV-NP-01 (BUG-038): DB에 소문자 pair로 저장된 advisory를
                          대문자 pair로 조회해도 매칭되어야 한다.

    Given: DB에 pair='btc_jpy' advisory 존재
    When:  _fetch_advisory('BTC_JPY', 'gmo_coin')  ← 대문자 입력
    Then:  normalize_pair 정규화 후 매칭 → advisory 반환
    """
    from sqlalchemy import select as sa_select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from adapters.database.models import RachelAdvisory
    from adapters.database.session import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        target = [Base.metadata.tables["rachel_advisories"]]
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=target))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # 소문자로 저장 (엔진 내부 표준)
    async with factory() as session:
        row = RachelAdvisory(
            pair="btc_jpy",
            exchange="gmo_coin",
            action="entry_long",
            confidence=0.8,
            reasoning="BUG-038 대소문자 매칭 테스트용 advisory (20자 이상)",
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
        session.add(row)
        await session.commit()

    dec = RachelAdvisoryDecision(
        session_factory=factory,
        advisory_model=RachelAdvisory,
        fallback=AsyncMock(),
    )

    # 대문자 입력으로 조회 → normalize_pair 후 매칭
    result = await dec._fetch_advisory("BTC_JPY", "gmo_coin")
    assert result is not None, "대문자 pair 입력 시에도 advisory를 찾아야 함 (BUG-038)"
    assert result.pair == "btc_jpy"
    assert result.action == "entry_long"

    await engine.dispose()


@pytest.mark.asyncio
async def test_advisory_post_normalizes_pair_to_lowercase():
    """
    ADV-NP-02 (BUG-038): POST /api/advisories 로 대문자 pair 저장 시
                          DB에는 소문자로 저장되어야 한다.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from adapters.database.models import RachelAdvisory
    from adapters.database.session import Base
    from core.pair import normalize_pair

    # normalize_pair가 대문자를 소문자로 변환함을 검증
    assert normalize_pair("BTC_JPY") == "btc_jpy"
    assert normalize_pair("BTC_JPY") == normalize_pair("btc_jpy")


# ──────────────────────────────────────────────────────────────
# 피라미딩 (add_position) 테스트 — P-01 ~ P-08
# ──────────────────────────────────────────────────────────────

def _pos_with_pyramid(
    entry_price: float = 9_800_000.0,
    pyramid_count: int = 0,
    unrealized_pnl: float = 200_000.0,
) -> PositionDTO:
    """피라미딩 테스트용 PositionDTO."""
    return PositionDTO(
        pair="BTC_JPY",
        entry_price=entry_price,
        entry_amount=0.3,
        stop_loss_price=9_500_000.0,
        stop_tightened=False,
        extra={"pyramid_count": pyramid_count, "unrealized_pnl": unrealized_pnl},
    )


@pytest.mark.asyncio
async def test_p01_add_position_profitable_under_limit():
    """
    P-01: add_position advisory + 포지션 있음 + P&L>0 + pyramid_count=0 → action=add_position
    """
    adv = _advisory(action="add_position", confidence=0.80, size_pct=0.2)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        pos = _pos_with_pyramid(pyramid_count=0, unrealized_pnl=200_000.0)
        result = await dec.decide(_snapshot(signal="entry_ok", position=pos))

    assert result.action == "add_position"
    assert result.source == "rachel_advisory"
    assert result.confidence == pytest.approx(0.80)


@pytest.mark.asyncio
async def test_p02_add_position_blocks_when_pyramid_limit_reached():
    """
    P-02: pyramid_count=3 (상한 도달) → hold
    """
    adv = _advisory(action="add_position", confidence=0.80)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        pos = _pos_with_pyramid(pyramid_count=3, unrealized_pnl=300_000.0)
        result = await dec.decide(_snapshot(signal="entry_ok", position=pos))

    assert result.action == "hold"
    assert "상한 도달" in result.reasoning


@pytest.mark.asyncio
async def test_p03_add_position_blocks_when_loss_position():
    """
    P-03: P&L ≤ 0 (물타기 방지) → hold
    """
    adv = _advisory(action="add_position", confidence=0.80)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        pos = _pos_with_pyramid(
            entry_price=10_500_000.0,  # 진입가 > 현재가(10M) → 손실
            pyramid_count=0,
            unrealized_pnl=-50_000.0,
        )
        snap = _snapshot(signal="entry_ok", position=pos)
        # current_price < entry_price → is_profitable=False
        result = await dec.decide(snap)

    assert result.action == "hold"
    assert "물타기 방지" in result.reasoning


@pytest.mark.asyncio
async def test_p04_add_position_blocks_when_exit_signal_active():
    """
    P-04: tighten_stop exit_signal → tighten_stop (긴급 시그널이 add_position 어드바이저리보다 우선)

    Note: tighten_stop/exit_warning은 글로벌 긴급 시그널 처리기가 먼저 처리한다.
    여기서 add_position advisory가 있더라도 긴급 시그널이 이를 오버라이드한다.
    """
    adv = _advisory(action="add_position", confidence=0.80)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        pos = _pos_with_pyramid(pyramid_count=0, unrealized_pnl=200_000.0)
        result = await dec.decide(
            _snapshot(signal="entry_ok", exit_action="tighten_stop", position=pos)
        )

    # 긴급 시그널(tighten_stop)이 add_position advisory보다 우선 처리됨
    assert result.action == "tighten_stop"


@pytest.mark.asyncio
async def test_p05_add_position_blocks_near_expiry():
    """
    P-05: advisory 만료 30분 전 (< 1H guard) → hold (만료 임박 억제)
    """
    adv = _advisory(action="add_position", confidence=0.80, expires_offset_hours=0.5)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        pos = _pos_with_pyramid(pyramid_count=0, unrealized_pnl=200_000.0)
        result = await dec.decide(_snapshot(signal="entry_ok", position=pos))

    assert result.action == "hold"
    assert "만료 임박" in result.reasoning


@pytest.mark.asyncio
async def test_p06_add_position_without_open_position_returns_hold():
    """
    P-06: add_position advisory + 포지션 없음 → hold (진입할 포지션 없음)
    """
    adv = _advisory(action="add_position", confidence=0.80)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok", position=None))

    assert result.action == "hold"
    assert result.source == "rachel_advisory"


@pytest.mark.asyncio
async def test_p07_add_position_second_pyramid_allowed():
    """
    P-07: pyramid_count=1 (2번째 추가), 수익 중 → action=add_position
    """
    adv = _advisory(action="add_position", confidence=0.75)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        pos = _pos_with_pyramid(pyramid_count=1, unrealized_pnl=500_000.0)
        result = await dec.decide(_snapshot(signal="entry_ok", position=pos))

    assert result.action == "add_position"
    assert result.source == "rachel_advisory"


@pytest.mark.asyncio
async def test_p08_add_position_full_exit_signal_blocks():
    """
    P-08: exit_signal=full_exit → exit (긴급 시그널이 add_position 어드바이저리 오버라이드)

    Note: full_exit은 글로벌 긴급 시그널 처리기가 먼저 처리한다.
    add_position advisory가 있더라도 full_exit이 이를 오버라이드한다.
    """
    adv = _advisory(action="add_position", confidence=0.80)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        pos = _pos_with_pyramid(pyramid_count=0, unrealized_pnl=300_000.0)
        result = await dec.decide(
            _snapshot(signal="entry_ok", exit_action="full_exit", position=pos)
        )

    # 긴급 시그널(full_exit)이 add_position advisory보다 우선 처리됨
    assert result.action == "exit"
    assert "긴급 시그널" in result.reasoning


# ──────────────────────────────────────────────────────────────
# 큐니 보강 — 피라미딩 엣지 케이스
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_p_short_profitable_allows_add_position():
    """
    P-EC1: 숏 포지션 수익 중 (current < entry) → add_position 허용.

    숏 P&L = (entry - current) * amount > 0 이면 수익.
    current_price=9,500,000 < entry=10,000,000 이므로 숏 수익.
    """
    adv = _advisory(action="add_position", confidence=0.75)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW

        # 숏 포지션: entry=10M, current=9.5M → 숏 수익
        short_pos = PositionDTO(
            pair="BTC_JPY",
            entry_price=10_000_000.0,
            entry_amount=0.3,
            stop_loss_price=10_500_000.0,
            stop_tightened=False,
            extra={"side": "sell", "pyramid_count": 0, "total_size_pct": 0.20},
        )
        snap = SignalSnapshot(
            pair="BTC_JPY",
            exchange="gmo_coin",
            timestamp=_NOW,
            signal="entry_ok",
            current_price=9_500_000.0,   # entry(10M) > current(9.5M) → 숏 수익
            exit_signal={"action": "hold"},
            position=short_pos,
            params={"position_size_pct": 20.0},
        )
        result = await dec.decide(snap)

    assert result.action == "add_position"
    assert result.source == "rachel_advisory"


@pytest.mark.asyncio
async def test_p_short_losing_blocks_add_position():
    """
    P-EC2: 숏 포지션 손실 중 (current > entry) → add_position 차단.

    숏 P&L = (entry - current) * amount < 0이면 손실.
    current_price=10,500,000 > entry=10,000,000 이므로 숏 손실.
    """
    adv = _advisory(action="add_position", confidence=0.80)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW

        short_pos = PositionDTO(
            pair="BTC_JPY",
            entry_price=10_000_000.0,
            entry_amount=0.3,
            stop_loss_price=10_600_000.0,
            stop_tightened=False,
            extra={"side": "sell", "pyramid_count": 0, "total_size_pct": 0.20},
        )
        snap = SignalSnapshot(
            pair="BTC_JPY",
            exchange="gmo_coin",
            timestamp=_NOW,
            signal="entry_ok",
            current_price=10_500_000.0,  # entry(10M) < current(10.5M) → 숏 손실
            exit_signal={"action": "hold"},
            position=short_pos,
            params={"position_size_pct": 20.0},
        )
        result = await dec.decide(snap)

    assert result.action == "hold"
    assert "물타기 방지" in result.reasoning


@pytest.mark.asyncio
async def test_p_pyramid_count_missing_from_extra_defaults_to_zero():
    """
    P-EC3: pyramid_count 키가 extra에 없는 경우 — 기본값 0으로 처리.

    Position.extra에 pyramid_count가 없어도 크래시 없이 add_position 허용.
    """
    adv = _advisory(action="add_position", confidence=0.70)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW

        # extra에 pyramid_count 키 없음
        pos_no_pyramid_key = PositionDTO(
            pair="BTC_JPY",
            entry_price=9_800_000.0,
            entry_amount=0.3,
            stop_loss_price=9_500_000.0,
            stop_tightened=False,
            extra={"side": "buy"},  # pyramid_count 키 없음
        )
        snap = SignalSnapshot(
            pair="BTC_JPY",
            exchange="gmo_coin",
            timestamp=_NOW,
            signal="entry_ok",
            current_price=10_000_000.0,  # 수익 중
            exit_signal={"action": "hold"},
            position=pos_no_pyramid_key,
            params={"position_size_pct": 20.0},
        )
        result = await dec.decide(snap)

    # pyramid_count=0으로 기본값 처리 → add_position 허용
    assert result.action == "add_position"


# ──────────────────────────────────────────────────────────────
# TS: trading_style 분리 테스트
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ts01_fetch_advisory_called_with_pair_and_exchange():
    """TS-01: decide()이 _fetch_advisory를 pair/exchange만으로 호출한다.

    변경 (2026-04-15): trading_style 필터 제거. Rachel은 체제 이미 고려한
    시장 판단 1건을 생성. 전략 선택은 RegimeGate가 담당.
    """
    adv = _advisory(action="entry_long")
    dec, _ = _make_decision(advisory=adv)

    snap = SignalSnapshot(
        pair="BTC_JPY",
        exchange="gmo_coin",
        timestamp=_NOW,
        signal="entry_ok",
        current_price=10_000_000.0,
        exit_signal={"action": "hold"},
        params={},
        strategy_type="box_mean_reversion",
    )
    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        await dec.decide(snap)

    dec._fetch_advisory.assert_awaited_once_with("BTC_JPY", "gmo_coin")


@pytest.mark.asyncio
async def test_ts02_strategy_type_default_is_trend_following():
    """TS-02: SignalSnapshot.strategy_type 기본값은 "trend_following"."""
    snap = SignalSnapshot(
        pair="BTC_JPY",
        exchange="gmo_coin",
        timestamp=_NOW,
        signal="entry_ok",
        current_price=10_000_000.0,
        exit_signal={"action": "hold"},
        params={},
    )
    assert snap.strategy_type == "trend_following"


@pytest.mark.asyncio
async def test_ts03_different_strategy_type_goes_to_fallback():
    """TS-03: advisory 없는 경우 (trading_style 불일치 시뮬레이션) → v1 폴백.

    session이 None을 반환 → advisory 없음 → fallback.decide() 호출 확인.
    """
    fallback = AsyncMock()
    fallback.decide.return_value = Decision(
        action="hold",
        pair="BTC_JPY",
        exchange="gmo_coin",
        confidence=0.3,
        size_pct=None,
        stop_loss=None,
        take_profit=None,
        reasoning="v1 폴백",
        risk_factors=(),
        source="rule_based_v1",
        trigger="regular_4h",
        raw_signal="no_signal",
        timestamp=_NOW,
    )

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_session.execute.return_value = mock_result
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    dec = RachelAdvisoryDecision(
        session_factory=MagicMock(return_value=mock_session),
        advisory_model=MagicMock(),
        fallback=fallback,
    )
    snap = SignalSnapshot(
        pair="BTC_JPY",
        exchange="gmo_coin",
        timestamp=_NOW,
        signal="entry_ok",
        current_price=10_000_000.0,
        exit_signal={"action": "hold"},
        params={},
        strategy_type="box_mean_reversion",
    )
    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(snap)

    assert result.source == "rachel_fallback_v1"
    fallback.decide.assert_awaited_once()


@pytest.mark.asyncio
async def test_ts04_fetch_advisory_uses_pair_exchange_only():
    """TS-04: strategy_type 무관하게 _fetch_advisory(pair, exchange)만 호출."""
    adv = _advisory(action="hold")
    dec, _ = _make_decision(advisory=adv)

    snap = _snapshot(signal="entry_ok")  # strategy_type 기본값 "trend_following"
    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        await dec.decide(snap)

    dec._fetch_advisory.assert_awaited_once_with("BTC_JPY", "bitflyer")


# ──────────────────────────────────────────────────────────────
# EC: 공유 advisory 엔지케이스 (trading_style 필터 제거 후 검증)
# 성곩: 박스매니저도 추세매니저도 동일한 advisory를 받는다
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ec01_box_manager_snapshot_queries_same_advisory():
    """
    EC-01: strategy_type='box_mean_reversion' 스냅샷도 (pair, exchange)만으로
           _fetch_advisory 호출 — trading_style 필터 없음.

    배경: 롤백 전엔는 box_mean_reversion 스냅샷이 _fetch_advisory(…,
    'box_mean_reversion')를 호출 → advisory 없음 → WARNING WARNING 반복.
    롤백 후엔는 strategy_type 무관하게 (pair, exchange)만 호출.
    """
    adv = _advisory(action="hold")
    dec, _ = _make_decision(advisory=adv)

    box_snap = SignalSnapshot(
        pair="BTC_JPY",
        exchange="gmo_coin",
        timestamp=_NOW,
        signal="entry_ok",
        current_price=10_000_000.0,
        exit_signal={"action": "hold"},
        params={},
        strategy_type="box_mean_reversion",
    )
    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(box_snap)

    # trading_style 인자 없이 (pair, exchange)만으로 호출
    dec._fetch_advisory.assert_awaited_once_with("BTC_JPY", "gmo_coin")
    # advisory action=hold → entry 없음
    assert result.action == "hold"
    assert result.source == "rachel_advisory"


@pytest.mark.asyncio
async def test_ec02_different_strategy_types_get_same_advisory():
    """
    EC-02: trend_following 스냅샷과 box_mean_reversion 스냅샷이
           동일한 advisory를 받아야 한다.

    이 테스트는 두 카리의 decide()가 동일한 advisory를 변환하는지를 확인.
    RegimeGate가 어느 카리의 진입을 허용할지 담당 — advisory는 전략 무관하게 1건.
    """
    adv = _advisory(action="entry_long", confidence=0.75)
    # trend 매니저
    dec_trend, _ = _make_decision(advisory=adv)
    # box 매니저
    dec_box, _ = _make_decision(advisory=adv)

    trend_snap = SignalSnapshot(
        pair="BTC_JPY", exchange="gmo_coin",
        timestamp=_NOW, signal="entry_ok", current_price=10_000_000.0,
        exit_signal={"action": "hold"}, params={},
        strategy_type="trend_following",
    )
    box_snap = SignalSnapshot(
        pair="BTC_JPY", exchange="gmo_coin",
        timestamp=_NOW, signal="entry_ok", current_price=10_000_000.0,
        exit_signal={"action": "hold"}, params={},
        strategy_type="box_mean_reversion",
    )
    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result_trend = await dec_trend.decide(trend_snap)
        result_box = await dec_box.decide(box_snap)

    # 둘 다 동일한 advisory로 entry_long
    assert result_trend.action == "entry_long"
    assert result_box.action == "entry_long"
    assert result_trend.confidence == pytest.approx(0.75)
    assert result_box.confidence == pytest.approx(0.75)
    # 호출 시그니쳐는 strategy_type 없이 (pair, exchange)만
    dec_trend._fetch_advisory.assert_awaited_once_with("BTC_JPY", "gmo_coin")
    dec_box._fetch_advisory.assert_awaited_once_with("BTC_JPY", "gmo_coin")


# ──────────────────────────────────────────────────────────────
# RL: 로그 prefix 매니저 구분 테스트 — RL-01 ~ RL-02
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rl01_log_prefix_includes_trading_style(caplog):
    """
    RL-01: snapshot.params에 trading_style='trend_following'이 있으면
           'advisory 읽음' 로그 prefix가 [RachelAdvisory:trend_following]이어야 한다.

    배경: trend + box 매니저 2개가 동일 advisory를 읽을 때 어느 매니저 호출인지
    구분하기 위해 trading_style을 로그 prefix에 포함한다 (2026-04-15).
    """
    import logging

    adv = _advisory(action="hold")
    dec, _ = _make_decision(advisory=adv)

    snap = SignalSnapshot(
        pair="btc_jpy",
        exchange="gmo_coin",
        timestamp=_NOW,
        signal="wait_dip",
        current_price=10_000_000.0,
        exit_signal={"action": "hold", "reason": ""},
        params={"position_size_pct": 0.5, "trading_style": "trend_following"},
    )
    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        with caplog.at_level(logging.INFO, logger="core.decision.rachel_advisory"):
            await dec.decide(snap)

    assert any(
        "[RachelAdvisory:trend_following]" in r.message and "advisory 읽음" in r.message
        for r in caplog.records
    ), "로그에 [RachelAdvisory:trend_following] prefix가 포함되어야 함"


@pytest.mark.asyncio
async def test_rl02_log_prefix_fallback_when_no_trading_style(caplog):
    """
    RL-02: snapshot.params에 trading_style이 없으면 prefix가 [RachelAdvisory:?]로
           표시된다 (폴백 동작).
    """
    import logging

    adv = _advisory(action="hold")
    dec, _ = _make_decision(advisory=adv)

    snap = SignalSnapshot(
        pair="btc_jpy",
        exchange="gmo_coin",
        timestamp=_NOW,
        signal="wait_dip",
        current_price=10_000_000.0,
        exit_signal={"action": "hold", "reason": ""},
        params={},  # trading_style 없음
    )
    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        with caplog.at_level(logging.INFO, logger="core.decision.rachel_advisory"):
            await dec.decide(snap)

    assert any(
        "[RachelAdvisory:?]" in r.message and "advisory 읽음" in r.message
        for r in caplog.records
    ), "로그에 [RachelAdvisory:?] 폴백 prefix가 포함되어야 함"


# ──────────────────────────────────────────────────────────────
# hold_override_policy 테스트 — HO-01 ~ HO-08 (BUG-037)
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ho01_hold_override_entry_ok_returns_entry_long():
    """
    HO-01: advisory=hold, policy=signal_entry_ok, signal=entry_ok, 포지션 없음
    → action=entry_long, confidence 30% 할인(0.65×0.7=0.455)
    """
    adv = _advisory(action="hold", confidence=0.65, hold_override_policy="signal_entry_ok")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok"))

    assert result.action == "entry_long"
    assert abs(result.confidence - round(0.65 * 0.7, 4)) < 1e-6
    assert result.source == "rachel_advisory"
    assert "hold override" in result.reasoning


@pytest.mark.asyncio
async def test_ho02_hold_override_entry_sell_returns_entry_short():
    """
    HO-02: advisory=hold, policy=signal_entry_ok, signal=entry_sell, 포지션 없음
    → action=entry_short, confidence 30% 할인
    """
    adv = _advisory(action="hold", confidence=0.65, hold_override_policy="signal_entry_ok")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_sell"))

    assert result.action == "entry_short"
    assert abs(result.confidence - round(0.65 * 0.7, 4)) < 1e-6
    assert "hold override" in result.reasoning


@pytest.mark.asyncio
async def test_ho03_hold_override_wait_dip_stays_hold():
    """
    HO-03: advisory=hold, policy=signal_entry_ok, signal=wait_dip, 포지션 없음
    → 시그널 미충족 → action=hold 유지
    """
    adv = _advisory(action="hold", confidence=0.65, hold_override_policy="signal_entry_ok")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="wait_dip"))

    assert result.action == "hold"
    assert "hold override" not in result.reasoning


@pytest.mark.asyncio
async def test_ho04_hold_policy_none_entry_ok_stays_hold():
    """
    HO-04: advisory=hold, policy=none, signal=entry_ok, 포지션 없음
    → override 비허용 → action=hold
    """
    adv = _advisory(action="hold", confidence=0.65, hold_override_policy="none")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok"))

    assert result.action == "hold"
    assert "레이첼 advisory hold" in result.reasoning


@pytest.mark.asyncio
async def test_ho05_hold_override_with_position_stays_hold():
    """
    HO-05: advisory=hold, policy=signal_entry_ok, signal=entry_ok, 포지션 있음
    → 포지션 보유 중 신규 진입 조건 미충족 → hold 유지
    """
    adv = _advisory(action="hold", confidence=0.65, hold_override_policy="signal_entry_ok")
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok", position=_pos()))

    assert result.action == "hold"


@pytest.mark.asyncio
async def test_ho06_hold_override_near_expiry_suppresses_entry():
    """
    HO-06: advisory=hold, policy=signal_entry_ok, signal=entry_ok, 만료 0.5H 남음
    → override 가능하나 만료 임박(< 1H) → 진입 억제, action=hold
    """
    adv = _advisory(
        action="hold",
        confidence=0.65,
        hold_override_policy="signal_entry_ok",
        expires_offset_hours=0.5,  # 30분 후 만료
    )
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok"))

    assert result.action == "hold"
    assert "만료 임박" in result.reasoning


@pytest.mark.asyncio
async def test_ho07_hold_override_policy_missing_attribute_falls_back():
    """
    HO-07: advisory에 hold_override_policy 속성 없음 (구 레코드, 하위호환)
    → getattr 폴백 → "none" → hold 유지
    """
    adv = SimpleNamespace(
        id=99,
        pair="BTC_JPY",
        exchange="bitflyer",
        action="hold",
        confidence=0.5,
        size_pct=0.5,
        stop_loss=None,
        take_profit=None,
        regime="trending",
        reasoning="hold_override_policy 필드 없는 구 레코드 (하위호환 테스트)",
        expires_at=_NOW + timedelta(hours=4.0),
        # hold_override_policy 없음 — 의도적
    )
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok"))

    assert result.action == "hold", "구 레코드는 hold 유지"


@pytest.mark.asyncio
async def test_ho08_non_hold_advisory_ignores_override_policy():
    """
    HO-08: advisory=entry_long (hold 아님), policy=signal_entry_ok, signal=entry_ok
    → hold advisory가 아니므로 override 경로 비진입
    → 기존 entry_long 경로 정상 동작: action=entry_long, confidence 할인 없음
    """
    adv = _advisory(
        action="entry_long",
        confidence=0.65,
        hold_override_policy="signal_entry_ok",  # 무시됨
    )
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        result = await dec.decide(_snapshot(signal="entry_ok"))

    assert result.action == "entry_long"
    # confidence 할인 없음 (override 경로가 아니라 기존 entry_long 경로)
    assert abs(result.confidence - 0.65) < 1e-6
    assert "hold override" not in result.reasoning


# ──────────────────────────────────────────────────────────────
# AP: entry_long/entry_short + 포지션 보유 자동 전환 (BUG-036)
# ──────────────────────────────────────────────────────────────


def _pos_long(
    entry_price: float = 9_800_000.0,
    pyramid_count: int = 0,
) -> PositionDTO:
    """롱 포지션 (side=buy)."""
    return PositionDTO(
        pair="BTC_JPY",
        entry_price=entry_price,
        entry_amount=0.3,
        stop_loss_price=9_500_000.0,
        stop_tightened=False,
        extra={"side": "buy", "pyramid_count": pyramid_count},
    )


def _pos_short(
    entry_price: float = 10_200_000.0,
    pyramid_count: int = 0,
) -> PositionDTO:
    """숏 포지션 (side=sell)."""
    return PositionDTO(
        pair="BTC_JPY",
        entry_price=entry_price,
        entry_amount=0.3,
        stop_loss_price=10_600_000.0,
        stop_tightened=False,
        extra={"side": "sell", "pyramid_count": pyramid_count},
    )


@pytest.mark.asyncio
async def test_ap02_entry_long_plus_long_position_loss_returns_hold():
    """
    AP-02 (BUG-036): advisory=entry_long + 롱 포지션 보유 + 손실 중
    → add_position 자동 전환 후 물타기 방지 안전장치 차단 → hold

    entry_price=10.5M > current_price=10M → 롱 손실
    """
    adv = _advisory(action="entry_long", confidence=0.75, size_pct=0.2)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        # entry_price > current_price → 손실 구간
        pos = _pos_long(entry_price=10_500_000.0, pyramid_count=0)
        result = await dec.decide(_snapshot(signal="entry_ok", position=pos))

    assert result.action == "hold"
    assert "물타기 방지" in result.reasoning


@pytest.mark.asyncio
async def test_ap03_entry_long_plus_long_position_pyramid_limit_returns_hold():
    """
    AP-03 (BUG-036): advisory=entry_long + 롱 포지션 보유(pyramid_count=3)
    → add_position 자동 전환 후 피라미딩 상한 안전장치 차단 → hold
    """
    adv = _advisory(action="entry_long", confidence=0.80)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        pos = _pos_long(entry_price=9_800_000.0, pyramid_count=3)
        result = await dec.decide(_snapshot(signal="entry_ok", position=pos))

    assert result.action == "hold"
    assert "상한 도달" in result.reasoning


@pytest.mark.asyncio
async def test_ap04_entry_long_plus_short_position_direction_mismatch_returns_hold():
    """
    AP-04 (BUG-036): advisory=entry_long + 숏 포지션 보유
    → 방향 불일치 → hold (포지션 flip 불허)
    """
    adv = _advisory(action="entry_long", confidence=0.75)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        pos = _pos_short(entry_price=10_200_000.0, pyramid_count=0)
        result = await dec.decide(_snapshot(signal="entry_ok", position=pos))

    assert result.action == "hold"
    assert "방향 불일치" in result.reasoning


@pytest.mark.asyncio
async def test_ap05_entry_short_plus_short_position_profitable_converts_to_add_position():
    """
    AP-05 (BUG-036): advisory=entry_short + 숏 포지션 보유(수익 중, pyramid=1)
    → add_position 자동 전환 + 4중 안전장치 통과 → action=add_position

    숏 수익 조건: current_price < entry_price
    current=9_500_000 < entry=10_200_000 → 숏 수익
    """
    adv = _advisory(action="entry_short", confidence=0.70, size_pct=0.15)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        pos = _pos_short(entry_price=10_200_000.0, pyramid_count=1)
        snap = SignalSnapshot(
            pair="BTC_JPY",
            exchange="bitflyer",
            timestamp=_NOW,
            signal="entry_sell",
            current_price=9_500_000.0,   # entry > current → 숏 수익
            exit_signal={"action": "hold", "reason": ""},
            position=pos,
            params={"position_size_pct": 1.0},
        )
        result = await dec.decide(snap)

    assert result.action == "add_position"
    assert result.source == "rachel_advisory"


@pytest.mark.asyncio
async def test_ap07_entry_long_plus_long_position_near_expiry_returns_hold():
    """
    AP-07 (BUG-036): advisory=entry_long + 롱 포지션 보유 + 만료 30분 전
    → add_position 자동 전환 후 만료 임박 안전장치 차단 → hold
    """
    adv = _advisory(action="entry_long", confidence=0.75, expires_offset_hours=0.5)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        pos = _pos_long(entry_price=9_800_000.0, pyramid_count=0)
        result = await dec.decide(_snapshot(signal="entry_ok", position=pos))

    assert result.action == "hold"
    assert "만료 임박" in result.reasoning


@pytest.mark.asyncio
async def test_ec01_entry_short_plus_long_position_direction_mismatch_logs_warning(caplog):
    """
    EC-01 (BUG-036): advisory=entry_short + 롱 포지션 보유
    → 방향 불일치 → hold + WARNING 로그

    entry_short + side=buy (롱) → 불일치
    """
    import logging

    adv = _advisory(action="entry_short", confidence=0.65)
    dec, _ = _make_decision(advisory=adv)

    with patch("core.decision.rachel_advisory.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        with caplog.at_level(logging.WARNING, logger="core.decision.rachel_advisory"):
            pos = _pos_long(entry_price=9_800_000.0)
            result = await dec.decide(_snapshot(signal="entry_sell", position=pos))

    assert result.action == "hold"
    assert "방향 불일치" in result.reasoning
    assert any("방향 불일치" in r.message for r in caplog.records), \
        "방향 불일치 시 WARNING 로그가 출력되어야 한다"
