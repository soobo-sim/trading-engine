"""
core/decision/ai_types.py 단위 테스트 — Agent DTO + serialize_snapshot().
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.data.dto import (
    EconomicEventDTO,
    LessonDTO,
    MacroSnapshotDTO,
    NewsDTO,
    PositionDTO,
    SentimentDTO,
    SignalSnapshot,
)
from core.decision.ai_types import (
    AliceProposal,
    RachelVerdict,
    SamanthaAudit,
    serialize_snapshot,
)


# ── 헬퍼 ─────────────────────────────────────────────────────


def _ts() -> datetime:
    return datetime(2026, 4, 11, 8, 0, 0, tzinfo=timezone.utc)


def _base_snapshot(**kwargs) -> SignalSnapshot:
    defaults = dict(
        pair="USD_JPY",
        exchange="gmo",
        timestamp=_ts(),
        signal="entry_ok",
        current_price=150.25,
        exit_signal={"action": "hold"},
    )
    defaults.update(kwargs)
    return SignalSnapshot(**defaults)


# ── AliceProposal ──────────────────────────────────────────────


def test_alice_proposal_frozen():
    """AliceProposal은 frozen dataclass — 수정 불가."""
    p = AliceProposal(
        action="entry_long",
        confidence=0.72,
        stop_loss=149.50,
        take_profit=151.20,
        situation_summary="박스 하단 근접",
        reasoning=("RSI 38", "DXY 하락"),
        risk_factors=("CPI 발표 21:30",),
        pessimistic_scenario="DXY 반등 시 손절",
    )
    with pytest.raises((TypeError, AttributeError)):
        p.confidence = 0.5  # type: ignore[misc]


def test_alice_proposal_hold():
    """hold 액션 시 stop_loss None 허용."""
    p = AliceProposal(
        action="hold",
        confidence=0.85,
        stop_loss=None,
        take_profit=None,
        situation_summary="셋업 없음",
        reasoning=("RSI 50 중립",),
        risk_factors=(),
        pessimistic_scenario="기회를 놓칠 수 있음",
    )
    assert p.action == "hold"
    assert p.stop_loss is None


# ── SamanthaAudit ─────────────────────────────────────────────


def test_samantha_audit_conditional():
    """conditional 감사에서 max_size_pct 설정 가능."""
    a = SamanthaAudit(
        verdict="conditional",
        confidence_adjustment=0.55,
        max_size_pct=0.30,
        worst_case_jpy=15000.0,
        reasoning="CPI 이벤트 미반영",
        missed_risks=("21:30 CPI 발표",),
    )
    assert a.verdict == "conditional"
    assert a.max_size_pct == 0.30


def test_samantha_audit_agree_no_limit():
    """agree 시 max_size_pct None (앨리스 유지)."""
    a = SamanthaAudit(
        verdict="agree",
        confidence_adjustment=0.72,
        max_size_pct=None,
        worst_case_jpy=10000.0,
        reasoning="약점 없음",
        missed_risks=(),
    )
    assert a.max_size_pct is None


# ── RachelVerdict ─────────────────────────────────────────────


def test_rachel_verdict_execute():
    """execute 판정 생성."""
    v = RachelVerdict(
        final_action="execute",
        final_confidence=0.70,
        final_size_pct=0.40,
        stop_loss=149.50,
        take_profit=151.20,
        alice_grade="data",
        samantha_grade="pattern",
        adopted_side="alice",
        reasoning="앨리스 데이터 근거 우위",
        failure_probability="DXY 반등 시 손절 위험",
    )
    assert v.final_action == "execute"
    assert v.final_confidence == 0.70


# ── serialize_snapshot ────────────────────────────────────────


class TestSerializeSnapshot:
    def test_minimal_snapshot_no_data(self):
        """
        Given: macro=None, news=None, sentiment=None
        When:  serialize_snapshot()
        Then:  "데이터 없음", "뉴스 없음" 포함
        """
        snap = _base_snapshot()
        result = serialize_snapshot(snap)

        assert "데이터 없음" in result   # 매크로
        assert "뉴스 없음" in result
        assert "포지션 없음" in result
        assert "관련 교훈 없음" in result
        assert "예정 이벤트 없음" in result

    def test_full_snapshot_all_sections(self):
        """
        Given: 전체 필드 채워진 snapshot
        When:  serialize_snapshot()
        Then:  7개 섹션 모두 포함
        """
        snap = _base_snapshot(
            ema=150.10,
            rsi=42.5,
            atr=0.80,
            regime="ranging",
            macro=MacroSnapshotDTO(
                us_10y=4.25, us_2y=4.80, vix=18.0, dxy=103.5,
                fetched_at=_ts(),
            ),
            news=(NewsDTO(
                title="Fed holds rates steady",
                source="Reuters",
                published_at=_ts(),
                category="forex",
                sentiment_score=-0.3,
            ),),
            sentiment=SentimentDTO(
                source="marketaux",
                score=35,
                classification="fear",
                timestamp=_ts(),
            ),
            upcoming_events=(EconomicEventDTO(
                name="US CPI",
                datetime_jst=_ts(),
                importance="High",
                currency="USD",
                forecast="3.1%",
                previous="3.2%",
            ),),
            relevant_lessons=(LessonDTO(
                lesson_id=1,
                situation_tags=("box_bottom", "dxy_down"),
                lesson_text="박스 하단 4회 중 3회 반등",
                outcome="win",
            ),),
            position=PositionDTO(
                pair="USD_JPY",
                entry_price=149.50,
                entry_amount=10000.0,
                stop_loss_price=149.00,
                stop_tightened=False,
            ),
        )
        result = serialize_snapshot(snap)

        # 기술 지표
        assert "USD_JPY" in result
        assert "150.25" in result
        assert "RSI: 42.5" in result
        assert "ranging" in result

        # 매크로
        assert "4.25" in result  # us_10y
        assert "VIX: 18.00" in result

        # 뉴스
        assert "Fed holds rates steady" in result
        assert "-0.30" in result  # sentiment_score

        # 센티먼트
        assert "35/100" in result
        assert "fear" in result

        # 경제 이벤트
        assert "US CPI" in result
        assert "High" in result
        assert "3.1%" in result

        # 포지션
        assert "149.50" in result

        # 교훈
        assert "박스 하단 4회 중 3회 반등" in result
        assert "수익" in result

    def test_position_pnl_displayed(self):
        """
        Given: position 있고 현재가 > 진입가
        When:  serialize_snapshot()
        Then:  미실현 P&L 퍼센트 포함
        """
        snap = _base_snapshot(
            current_price=151.00,
            position=PositionDTO(
                pair="USD_JPY",
                entry_price=150.00,
                entry_amount=10000.0,
            ),
        )
        result = serialize_snapshot(snap)
        assert "+0.67%" in result  # (151-150)/150*100

    def test_exit_signal_shown_when_not_hold(self):
        """
        Given: exit_signal.action != hold
        When:  serialize_snapshot()
        Then:  Exit 시그널 항목 포함
        """
        snap = _base_snapshot(
            exit_signal={"action": "tighten_stop", "reason": "EMA slope negative"}
        )
        result = serialize_snapshot(snap)
        assert "tighten_stop" in result

    def test_news_limited_to_5(self):
        """
        Given: 뉴스 10건
        When:  serialize_snapshot()
        Then:  최대 5건만 출력
        """
        many_news = tuple(
            NewsDTO(
                title=f"News {i}",
                source="Test",
                published_at=_ts(),
                category="forex",
            )
            for i in range(10)
        )
        snap = _base_snapshot(news=many_news)
        result = serialize_snapshot(snap)
        # "News 0"~"News 4" 포함, "News 5"~"News 9" 미포함
        assert "News 4" in result
        assert "News 5" not in result
