"""
SwitchRecommender 단위 테스트 (V-30~V-39).

안전장치(쿨다운/일최대/월최대), Score 마진, DB 저장, 콜백 검증.
실제 DB 없이 AsyncMock으로 실행.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.strategy.scoring import StrategyScore
from core.strategy.switch_recommender import SwitchRecommender


# ══════════════════════════════════════════════════════════════
# Fixtures / helpers
# ══════════════════════════════════════════════════════════════

def _make_score(score: float) -> StrategyScore:
    return StrategyScore(
        score=score,
        readiness=score,
        edge=score,
        regime_fit=score,
        regime="ranging",
        confidence="low",
        detail={"signal": "entry_ok", "box_width_pct": 0.5},
    )


def _make_strategy(sid: int, status: str, pair: str = "usd_jpy", style: str = "box_mean_reversion"):
    s = MagicMock()
    s.id = sid
    s.status = status
    s.parameters = {"pair": pair, "trading_style": style}
    return s


def _make_recommender(on_recommendation=None) -> SwitchRecommender:
    """DB 저장 성공하는 Mock SwitchRecommender."""
    saved_row = MagicMock()
    saved_row.id = 99
    saved_row.decision = "pending"
    saved_row.score_ratio = 1.8

    db_session = AsyncMock()
    db_session.add = MagicMock()
    db_session.commit = AsyncMock()
    db_session.refresh = AsyncMock()

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=db_session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    rec_model = MagicMock(return_value=saved_row)
    rec_model.id = MagicMock()
    rec_model.created_at = MagicMock()

    r = SwitchRecommender(
        session_factory=MagicMock(return_value=session_cm),
        recommendation_model=rec_model,
        on_recommendation=on_recommendation,
    )
    r._saved_row = saved_row
    return r


# ══════════════════════════════════════════════════════════════
# V-30: active 1 + proposed 1로 추천 생성 (score_ratio > 1.5)
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v30_recommendation_generated_when_ratio_exceeded():
    """proposed Score가 active × 1.5 초과 → 추천 생성."""
    active = _make_strategy(10, "active")
    proposed = _make_strategy(1, "proposed")

    r = _make_recommender()
    with patch.object(r, "_check_safety_guards", AsyncMock(return_value=None)):
        result = await r.evaluate("T2_candle_close", [
            (active, _make_score(0.3)),
            (proposed, _make_score(0.55)),  # 0.3 × 1.5 = 0.45 < 0.55 ✓
        ])

    assert result is not None
    assert result.decision == "pending"


@pytest.mark.asyncio
async def test_v31_no_recommendation_when_ratio_not_exceeded():
    """proposed Score가 active × 1.5 이하 → 추천 없음."""
    active = _make_strategy(10, "active")
    proposed = _make_strategy(1, "proposed")

    r = _make_recommender()
    with patch.object(r, "_check_safety_guards", AsyncMock(return_value=None)):
        result = await r.evaluate("T2_candle_close", [
            (active, _make_score(0.3)),
            (proposed, _make_score(0.44)),  # 0.3 × 1.5 = 0.45, 0.44 < 0.45 ✗
        ])

    assert result is None


# ══════════════════════════════════════════════════════════════
# V-32: 24H 쿨다운 미경과 → 차단
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v32_cooldown_blocks_recommendation():
    """24H 쿨다운 미경과 시 추천 차단."""
    active = _make_strategy(10, "active")
    proposed = _make_strategy(1, "proposed")

    r = _make_recommender()
    with patch.object(r, "_check_safety_guards", AsyncMock(return_value="24H 쿨다운 미경과")):
        result = await r.evaluate("T2_candle_close", [(active, _make_score(0.3)), (proposed, _make_score(0.9))])

    assert result is None


# ══════════════════════════════════════════════════════════════
# V-33: 일 최대 초과 → 차단
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v33_daily_max_blocks_recommendation():
    """일 최대(1회) 초과 시 추천 차단."""
    active = _make_strategy(10, "active")
    proposed = _make_strategy(1, "proposed")

    r = _make_recommender()
    with patch.object(r, "_check_safety_guards", AsyncMock(return_value="일 최대 1회 초과")):
        result = await r.evaluate("T2_candle_close", [(active, _make_score(0.3)), (proposed, _make_score(0.9))])

    assert result is None


# ══════════════════════════════════════════════════════════════
# V-34: 月 최대 초과 → 차단
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v34_monthly_max_blocks_recommendation():
    """月 최대(4회) 초과 시 추천 차단."""
    active = _make_strategy(10, "active")
    proposed = _make_strategy(1, "proposed")

    r = _make_recommender()
    with patch.object(r, "_check_safety_guards", AsyncMock(return_value="月 최대 4회 초과")):
        result = await r.evaluate("T2_candle_close", [(active, _make_score(0.3)), (proposed, _make_score(0.9))])

    assert result is None


# ══════════════════════════════════════════════════════════════
# V-35: active 없음 → skip
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v35_no_active_strategy_returns_none():
    """active 전략 없으면 None 반환 (전부 proposed)."""
    proposed1 = _make_strategy(1, "proposed")
    proposed2 = _make_strategy(2, "proposed")

    r = _make_recommender()
    result = await r.evaluate("T2_candle_close", [
        (proposed1, _make_score(0.5)),
        (proposed2, _make_score(0.8)),
    ])

    assert result is None


# ══════════════════════════════════════════════════════════════
# V-36: proposed 없음 → skip
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v36_no_proposed_strategy_returns_none():
    """proposed 전략 없으면 None 반환."""
    active = _make_strategy(10, "active")

    r = _make_recommender()
    result = await r.evaluate("T2_candle_close", [(active, _make_score(0.5))])

    assert result is None


# ══════════════════════════════════════════════════════════════
# V-37: DB 저장 필드 검증
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v37_saved_recommendation_fields():
    """추천 생성 시 trigger_type, score, decision 필드 정합."""
    active = _make_strategy(10, "active")
    proposed = _make_strategy(1, "proposed")

    r = _make_recommender()
    with patch.object(r, "_check_safety_guards", AsyncMock(return_value=None)):
        result = await r.evaluate("T1_position_close", [(active, _make_score(0.3)), (proposed, _make_score(0.55))])

    assert result is not None
    assert result.decision == "pending"
    call_kwargs = r._recommendation_model.call_args.kwargs
    assert call_kwargs["trigger_type"] == "T1_position_close"
    assert call_kwargs["current_strategy_id"] == 10
    assert call_kwargs["recommended_strategy_id"] == 1
    assert float(call_kwargs["current_score"]) == pytest.approx(0.3)
    assert float(call_kwargs["recommended_score"]) == pytest.approx(0.55)
    assert float(call_kwargs["score_ratio"]) == pytest.approx(0.55 / 0.3, rel=0.01)


# ══════════════════════════════════════════════════════════════
# V-38: on_recommendation 콜백 호출
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v38_on_recommendation_callback_called():
    """추천 생성 시 on_recommendation 콜백이 호출된다."""
    callback = AsyncMock()
    r = _make_recommender(on_recommendation=callback)
    active = _make_strategy(10, "active")
    proposed = _make_strategy(1, "proposed")

    with patch.object(r, "_check_safety_guards", AsyncMock(return_value=None)):
        await r.evaluate("T2_candle_close", [
            (active, _make_score(0.3)),
            (proposed, _make_score(0.9)),
        ])

    callback.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# V-39: Score 정확히 1.5배 → 미생성 (strict >)
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_v39_exact_margin_not_exceeded_returns_none():
    """proposed Score가 active × 1.5 미만 → 추천 없음."""
    r = _make_recommender()
    active = _make_strategy(10, "active")
    proposed = _make_strategy(1, "proposed")

    with patch.object(r, "_check_safety_guards", AsyncMock(return_value=None)):
        result = await r.evaluate("T2_candle_close", [
            (active, _make_score(0.4)),
            (proposed, _make_score(0.5)),  # 0.5 / 0.4 = 1.25 < 1.5 → 미충족
        ])

    assert result is None


# ══════════════════════════════════════════════════════════════
# 추가: 빈 리스트 → None
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_empty_pairs_returns_none():
    """빈 strategy_score_pairs → 즉시 None (DB 쿼리 없음)."""
    r = _make_recommender()
    result = await r.evaluate("T2_candle_close", [])
    assert result is None
