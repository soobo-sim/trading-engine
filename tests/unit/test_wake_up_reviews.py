"""
wake_up_reviews API adversarial 테스트.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal

from adapters.database.models import WakeUpReview, CAUSE_CODES, REVIEW_STATUSES


def _make_review(**kwargs):
    defaults = dict(
        id=1, position_id=5, strategy_id=7,
        pair="BTC_JPY", entry_price=Decimal("11200000"),
        exit_price=Decimal("11197339"), realized_pnl=Decimal("-16.65"),
        cause_code="REGIME_MISMATCH", review_status="draft",
        created_at=None,
        cause_detail=None, sub_cause=None, holding_duration_min=None,
        entry_regime=None, actual_regime=None,
        simulation_hold_pnl=None, simulation_best_exit_pnl=None, simulation_verdict=None,
        capital_at_entry=None, position_size_pct=None,
        alice_analysis=None, samantha_audit=None,
        rachel_verdict=None, rachel_rationale=None, lessons_learned=None,
        param_changes=None, optimistic_ev=None, pessimistic_ev=None,
        pessimistic_max_loss=None, grid_search_result=None, overfit_risk=None,
        kill_condition_met=False, kill_condition_text=None, safety_check_ok=None,
        stop_loss_price=None, actual_stop_hit_price=None, rejection_count=0,
    )
    defaults.update(kwargs)
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


# ── Pydantic 유효성 검증 ─────────────────────────────────────

def test_p1_invalid_cause_code_rejected():
    """유효하지 않은 cause_code 거부."""
    from pydantic import ValidationError
    from api.routes.wake_up_reviews import WakeUpReviewCreate
    with pytest.raises(ValidationError, match="cause_code"):
        WakeUpReviewCreate(
            pair="BTC_JPY", entry_price=1, exit_price=1, realized_pnl=-100,
            cause_code="INVALID_CAUSE",
        )


def test_p2_invalid_review_status_rejected():
    from pydantic import ValidationError
    from api.routes.wake_up_reviews import WakeUpReviewCreate
    with pytest.raises(ValidationError, match="review_status"):
        WakeUpReviewCreate(
            pair="BTC_JPY", entry_price=1, exit_price=1, realized_pnl=-100,
            cause_code="EXIT_TIMING", review_status="unknown_status",
        )


def test_p3_invalid_rachel_verdict_rejected():
    from pydantic import ValidationError
    from api.routes.wake_up_reviews import WakeUpReviewCreate
    with pytest.raises(ValidationError, match="rachel_verdict"):
        WakeUpReviewCreate(
            pair="BTC_JPY", entry_price=1, exit_price=1, realized_pnl=-100,
            cause_code="EXIT_TIMING", rachel_verdict="wrong",
        )


def test_p4_all_cause_codes_accepted():
    """모든 8개 cause_code 허용."""
    from api.routes.wake_up_reviews import WakeUpReviewCreate
    for code in CAUSE_CODES:
        obj = WakeUpReviewCreate(
            pair="BTC_JPY", entry_price=1, exit_price=1, realized_pnl=-100,
            cause_code=code,
        )
        assert obj.cause_code == code


# ── POST 비즈니스 규칙 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_b1_rachel_verdict_without_alice_analysis_rejected():
    """rachel_verdict 저장 시 alice_analysis 없으면 422."""
    from fastapi import HTTPException
    from api.routes.wake_up_reviews import create_review, WakeUpReviewCreate

    body = WakeUpReviewCreate(
        pair="BTC_JPY", entry_price=1, exit_price=1, realized_pnl=-100,
        cause_code="EXIT_TIMING",
        review_status="samantha_approved",
        rachel_verdict="maintain",
        alice_analysis=None,  # 없음
    )
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await create_review(body, db)
    assert exc.value.status_code == 422
    assert "alice_analysis" in exc.value.detail


@pytest.mark.asyncio
async def test_b2_rachel_verdict_without_samantha_approved_rejected():
    """rachel_verdict 저장 시 review_status != samantha_approved → 422."""
    from fastapi import HTTPException
    from api.routes.wake_up_reviews import create_review, WakeUpReviewCreate

    body = WakeUpReviewCreate(
        pair="BTC_JPY", entry_price=1, exit_price=1, realized_pnl=-100,
        cause_code="EXIT_TIMING",
        review_status="draft",  # samantha_approved 아님
        rachel_verdict="maintain",
        alice_analysis="분석 완료",
    )
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await create_review(body, db)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_b3_valid_rachel_verdict_accepted():
    """alice_analysis + samantha_approved 갖춘 rachel_verdict → 정상 저장."""
    from api.routes.wake_up_reviews import create_review, WakeUpReviewCreate

    body = WakeUpReviewCreate(
        pair="BTC_JPY", entry_price=11200000, exit_price=11197339, realized_pnl=-16.65,
        cause_code="REGIME_MISMATCH",
        review_status="samantha_approved",
        rachel_verdict="maintain",
        alice_analysis="체제 불일치 확인",
    )
    saved = _make_review(
        cause_code="REGIME_MISMATCH", review_status="samantha_approved",
        rachel_verdict="maintain", alice_analysis="체제 불일치 확인",
    )
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    with patch("api.routes.wake_up_reviews._review_to_dict", return_value={"id": 1}):
        with patch.object(db, "refresh", new_callable=AsyncMock):
            # DB add 후 rec에 값 설정되도록 mock
            async def fake_refresh(obj):
                for k, v in vars(saved).items():
                    if not k.startswith("_"):
                        try:
                            setattr(obj, k, v)
                        except Exception:
                            pass
            db.refresh.side_effect = fake_refresh
            result = await create_review(body, db)
    assert result is not None


# ── GET 목록 필터 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_g1_invalid_cause_filter_rejected():
    """목록 조회 시 유효하지 않은 cause_code 필터 → 400."""
    from fastapi import HTTPException
    from api.routes.wake_up_reviews import list_reviews

    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await list_reviews(cause_code="INVALID", db=db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_g2_invalid_status_filter_rejected():
    """목록 조회 시 유효하지 않은 review_status → 400."""
    from fastapi import HTTPException
    from api.routes.wake_up_reviews import list_reviews

    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await list_reviews(review_status="bad_status", db=db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_g3_position_not_found():
    """단건 조회 — 없는 position_id → 404."""
    from fastapi import HTTPException
    from api.routes.wake_up_reviews import get_review

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(HTTPException) as exc:
        await get_review(position_id=999, db=db)
    assert exc.value.status_code == 404


# ── Lessons 집계 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_l1_lessons_empty():
    """리뷰 없을 때 lessons → total_reviews=0."""
    from api.routes.wake_up_reviews import get_lessons

    db = AsyncMock()

    # count → 0
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    # agg → (None, None)
    agg_result = MagicMock()
    agg_result.one.return_value = (None, None)
    # cause rows → []
    cause_result = MagicMock()
    cause_result.all.return_value = []
    # lessons rows → []
    lesson_result = MagicMock()
    lesson_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[count_result, agg_result, cause_result, lesson_result])

    result = await get_lessons(limit=10, db=db)
    assert result["summary"]["total_reviews"] == 0
    assert result["summary"]["top_causes"] == []
    assert result["recent_lessons"] == []
