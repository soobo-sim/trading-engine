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
        # Section I
        optimal_params=None, optimal_pnl=None, optimal_pnl_pct=None,
        actual_vs_optimal_diff_pct=None, optimal_long_term_ev=None,
        optimal_long_term_wr=None, optimal_long_term_sharpe=None,
        optimal_long_term_trades=None, optimal_overfit_risk=None,
        optimal_entry_timing=None, optimal_exit_timing=None, optimal_key_diff=None,
        # Section J
        root_cause_codes=None, root_cause_detail=None, decision_date=None,
        decision_by=None, info_gap_had=None, info_gap_new=None,
        # Section K
        action_items=None, prevention_checklist=None, review_quality_score=None,
        # BUG-025 파이프라인 필드
        exchange=None, position_type=None,
        pipeline_status=None, scheduled_at=None,
        pipeline_started_at=None, pipeline_completed_at=None,
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


@pytest.mark.asyncio
async def test_l2_min_repeat_filters_low_count():
    """min_repeat=3 시 2회 이하 원인은 top_causes에서 제외."""
    from api.routes.wake_up_reviews import get_lessons

    db = AsyncMock()

    count_result = MagicMock()
    count_result.scalar_one.return_value = 5
    agg_result = MagicMock()
    agg_result.one.return_value = (Decimal("-500"), Decimal("-2000"))
    # HAVING cnt >= 3 이 적용되어 cnt=2인 ENTRY_TIMING 제외 → REGIME_MISMATCH만
    cause_row = MagicMock()
    cause_row.cause_code = "REGIME_MISMATCH"
    cause_row.cnt = 3
    cause_result = MagicMock()
    cause_result.all.return_value = [cause_row]
    lesson_result = MagicMock()
    lesson_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[count_result, agg_result, cause_result, lesson_result])

    result = await get_lessons(limit=10, min_repeat=3, db=db)
    causes = result["summary"]["top_causes"]
    assert len(causes) == 1
    assert causes[0]["code"] == "REGIME_MISMATCH"
    assert causes[0]["count"] == 3


@pytest.mark.asyncio
async def test_l3_min_repeat_empty_when_no_match():
    """min_repeat가 높아서 아무것도 안 걸리면 top_causes = []."""
    from api.routes.wake_up_reviews import get_lessons

    db = AsyncMock()

    count_result = MagicMock()
    count_result.scalar_one.return_value = 2
    agg_result = MagicMock()
    agg_result.one.return_value = (Decimal("-100"), Decimal("-200"))
    cause_result = MagicMock()
    cause_result.all.return_value = []  # HAVING cnt >= 10 → 아무것도 없음
    lesson_result = MagicMock()
    lesson_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[count_result, agg_result, cause_result, lesson_result])

    result = await get_lessons(limit=10, min_repeat=10, db=db)
    assert result["summary"]["top_causes"] == []


# ── Section I/J/K Pydantic 유효성 검증 ───────────────────────

def test_n1_optimal_overfit_risk_invalid():
    """optimal_overfit_risk 유효하지 않은 값 거부."""
    from pydantic import ValidationError
    from api.routes.wake_up_reviews import WakeUpReviewCreate
    with pytest.raises(ValidationError, match="optimal_overfit_risk"):
        WakeUpReviewCreate(
            pair="BTC_JPY", entry_price=1, exit_price=1, realized_pnl=-100,
            cause_code="EXIT_TIMING", optimal_overfit_risk="VERY_HIGH",
        )


def test_n2_optimal_overfit_risk_valid():
    """optimal_overfit_risk 유효한 값 허용."""
    from api.routes.wake_up_reviews import WakeUpReviewCreate
    for risk in ("low", "medium", "high"):
        obj = WakeUpReviewCreate(
            pair="BTC_JPY", entry_price=1, exit_price=1, realized_pnl=-100,
            cause_code="EXIT_TIMING", optimal_overfit_risk=risk,
        )
        assert obj.optimal_overfit_risk == risk


def test_n3_root_cause_codes_invalid_value():
    """root_cause_codes에 유효하지 않은 코드 포함 시 거부."""
    from pydantic import ValidationError
    from api.routes.wake_up_reviews import WakeUpReviewCreate
    with pytest.raises(ValidationError, match="root_cause_codes"):
        WakeUpReviewCreate(
            pair="BTC_JPY", entry_price=1, exit_price=1, realized_pnl=-100,
            cause_code="EXIT_TIMING", root_cause_codes=["NO_GRID_SEARCH", "INVALID_CODE"],
        )


def test_n4_root_cause_codes_valid():
    """root_cause_codes 유효한 값 배열 허용."""
    from api.routes.wake_up_reviews import WakeUpReviewCreate
    obj = WakeUpReviewCreate(
        pair="BTC_JPY", entry_price=1, exit_price=1, realized_pnl=-100,
        cause_code="EXIT_TIMING",
        root_cause_codes=["NO_GRID_SEARCH", "STALE_PARAMS"],
    )
    assert obj.root_cause_codes == ["NO_GRID_SEARCH", "STALE_PARAMS"]


def test_n5_action_items_and_section_ijk_accepted():
    """action_items, prevention_checklist, review_quality_score 포함 생성."""
    from api.routes.wake_up_reviews import WakeUpReviewCreate
    obj = WakeUpReviewCreate(
        pair="BTC_JPY", entry_price=1, exit_price=1, realized_pnl=-100,
        cause_code="PARAM_SUBOPTIMAL",
        action_items=[{"id": "K1-1", "action": "SL 변경", "assignee": "maria", "status": "open"}],
        prevention_checklist=[{"item": "그리드 서치 200+", "checked": False}],
        review_quality_score=7.5,
        root_cause_codes=["NO_GRID_SEARCH"],
        optimal_overfit_risk="low",
        optimal_pnl=-500.0,
    )
    assert obj.review_quality_score == 7.5
    assert obj.action_items[0]["id"] == "K1-1"
    assert obj.root_cause_codes == ["NO_GRID_SEARCH"]


# ── GET /open-actions ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_oa1_open_actions_returns_only_open():
    """action_items 중 status=open인 것만 추출."""
    from api.routes.wake_up_reviews import get_open_actions

    review = _make_review(
        id=10, strategy_id=7, pair="BTC_JPY", cause_code="EXIT_TIMING",
        action_items=[
            {"id": "K1-1", "action": "SL 변경", "assignee": "maria", "status": "open", "deadline": "2026-04-10"},
            {"id": "K1-2", "action": "WF 추가", "assignee": "rachel", "status": "done"},
        ],
    )

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [review]
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_open_actions(assignee=None, db=db)
    assert result["count"] == 1
    action = result["open_actions"][0]
    assert action["id"] == "K1-1"
    assert action["review_id"] == 10
    assert action["assignee"] == "maria"


@pytest.mark.asyncio
async def test_oa2_open_actions_assignee_filter():
    """assignee 필터 적용 시 해당 담당자만."""
    from api.routes.wake_up_reviews import get_open_actions

    review = _make_review(
        id=11, strategy_id=7, pair="BTC_JPY", cause_code="EXIT_TIMING",
        action_items=[
            {"id": "K2-1", "action": "분석", "assignee": "rachel", "status": "open"},
            {"id": "K2-2", "action": "코드", "assignee": "maria", "status": "open"},
        ],
    )

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [review]
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_open_actions(assignee="maria", db=db)
    assert result["count"] == 1
    assert result["open_actions"][0]["id"] == "K2-2"


@pytest.mark.asyncio
async def test_oa3_open_actions_empty():
    """모든 items done → open_actions = []."""
    from api.routes.wake_up_reviews import get_open_actions

    review = _make_review(
        id=12, action_items=[{"id": "K3-1", "action": "완료됨", "assignee": "maria", "status": "done"}]
    )

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [review]
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_open_actions(db=db)
    assert result["count"] == 0
    assert result["open_actions"] == []


# ── GET /{review_id}/action-items ────────────────────────────

@pytest.mark.asyncio
async def test_ai1_get_action_items_returns_list():
    """review_id로 action_items 반환."""
    from api.routes.wake_up_reviews import get_action_items

    items = [{"id": "K1-1", "action": "SL변경", "status": "open"}]
    checklist = [{"item": "그리드서치", "checked": False}]
    review = _make_review(id=5, action_items=items, prevention_checklist=checklist)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = review
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_action_items(review_id=5, db=db)
    assert result["review_id"] == 5
    assert result["action_items"] == items
    assert result["prevention_checklist"] == checklist


@pytest.mark.asyncio
async def test_ai2_get_action_items_not_found():
    """없는 review_id → 404."""
    from fastapi import HTTPException
    from api.routes.wake_up_reviews import get_action_items

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(HTTPException) as exc:
        await get_action_items(review_id=999, db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_ai3_get_action_items_empty_when_none():
    """action_items=None인 경우 빈 배열 반환."""
    from api.routes.wake_up_reviews import get_action_items

    review = _make_review(id=6, action_items=None, prevention_checklist=None)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = review
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_action_items(review_id=6, db=db)
    assert result["action_items"] == []
    assert result["prevention_checklist"] == []


# ── PATCH /{review_id}/action-items/{item_id} ────────────────

@pytest.mark.asyncio
async def test_pa1_patch_action_item_status_done():
    """item_id 매치 → status=done, completed_at 설정."""
    from api.routes.wake_up_reviews import patch_action_item, ActionItemPatch

    items = [
        {"id": "K1-1", "action": "SL변경", "status": "open", "completed_at": None},
        {"id": "K1-2", "action": "WF", "status": "open", "completed_at": None},
    ]
    review = _make_review(id=7, action_items=items)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = review

    refresh_result = MagicMock()
    refresh_result.scalar_one_or_none.return_value = review
    db.execute = AsyncMock(return_value=result_mock)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        body = ActionItemPatch(status="done", result="WF EV > 0 확인")
        result = await patch_action_item(review_id=7, item_id="K1-1", body=body, db=db)

    assert result["review_id"] == 7
    updated = result["updated_item"]
    assert updated["status"] == "done"
    assert updated["result"] == "WF EV > 0 확인"
    assert updated["completed_at"] is not None


@pytest.mark.asyncio
async def test_pa2_patch_action_item_not_found_review():
    """없는 review_id → 404."""
    from fastapi import HTTPException
    from api.routes.wake_up_reviews import patch_action_item, ActionItemPatch

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(HTTPException) as exc:
        await patch_action_item(review_id=999, item_id="K1-1", body=ActionItemPatch(status="done"), db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_pa3_patch_action_item_not_found_item():
    """존재하지 않는 item_id → 404."""
    from fastapi import HTTPException
    from api.routes.wake_up_reviews import patch_action_item, ActionItemPatch

    items = [{"id": "K1-1", "action": "SL변경", "status": "open"}]
    review = _make_review(id=8, action_items=items)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = review
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(HTTPException) as exc:
        await patch_action_item(review_id=8, item_id="NONEXISTENT", body=ActionItemPatch(status="done"), db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_pa4_patch_action_item_invalid_status():
    """status가 open/done/skipped 아닌 경우 Pydantic 거부."""
    from pydantic import ValidationError
    from api.routes.wake_up_reviews import ActionItemPatch
    with pytest.raises(ValidationError):
        ActionItemPatch(status="completed")


# ── PATCH /{review_id}/checklist/{item_id} ───────────────────

@pytest.mark.asyncio
async def test_cl1_patch_checklist_check():
    """체크리스트 항목 checked=True 반영."""
    from api.routes.wake_up_reviews import patch_checklist_item, ChecklistItemPatch

    checklist = [
        {"id": "CL-1", "item": "그리드 서치 200+", "checked": False},
        {"id": "CL-2", "item": "EMA slope 확인", "checked": False},
    ]
    review = _make_review(id=10, prevention_checklist=checklist)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = review
    db.execute = AsyncMock(return_value=result_mock)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        body = ChecklistItemPatch(checked=True)
        result = await patch_checklist_item(review_id=10, item_id="CL-1", body=body, db=db)

    assert result["review_id"] == 10
    updated = result["updated_item"]
    assert updated["id"] == "CL-1"
    assert updated["checked"] is True


@pytest.mark.asyncio
async def test_cl2_patch_checklist_uncheck():
    """체크리스트 항목 checked=False (언체크)."""
    from api.routes.wake_up_reviews import patch_checklist_item, ChecklistItemPatch

    checklist = [{"id": "CL-1", "item": "그리드 서치", "checked": True}]
    review = _make_review(id=11, prevention_checklist=checklist)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = review
    db.execute = AsyncMock(return_value=result_mock)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        body = ChecklistItemPatch(checked=False)
        result = await patch_checklist_item(review_id=11, item_id="CL-1", body=body, db=db)

    assert result["updated_item"]["checked"] is False


@pytest.mark.asyncio
async def test_cl3_patch_checklist_review_not_found():
    """없는 review_id → 404."""
    from fastapi import HTTPException
    from api.routes.wake_up_reviews import patch_checklist_item, ChecklistItemPatch

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(HTTPException) as exc:
        await patch_checklist_item(review_id=999, item_id="CL-1", body=ChecklistItemPatch(checked=True), db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cl4_patch_checklist_item_not_found():
    """존재하지 않는 item_id → 404."""
    from fastapi import HTTPException
    from api.routes.wake_up_reviews import patch_checklist_item, ChecklistItemPatch

    checklist = [{"id": "CL-1", "item": "SL 확인", "checked": False}]
    review = _make_review(id=12, prevention_checklist=checklist)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = review
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(HTTPException) as exc:
        await patch_checklist_item(review_id=12, item_id="CL-NONEXISTENT", body=ChecklistItemPatch(checked=True), db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cl5_patch_checklist_null_list():
    """prevention_checklist=None인 review → 존재하지 않는 item으로 404."""
    from fastapi import HTTPException
    from api.routes.wake_up_reviews import patch_checklist_item, ChecklistItemPatch

    review = _make_review(id=13, prevention_checklist=None)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = review
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(HTTPException) as exc:
        await patch_checklist_item(review_id=13, item_id="CL-1", body=ChecklistItemPatch(checked=True), db=db)
    assert exc.value.status_code == 404


def test_cl6_checklist_item_patch_schema():
    """ChecklistItemPatch: checked 필드 필수."""
    from pydantic import ValidationError
    from api.routes.wake_up_reviews import ChecklistItemPatch

    # 정상
    obj = ChecklistItemPatch(checked=True)
    assert obj.checked is True

    # checked 없으면 ValidationError
    with pytest.raises(ValidationError):
        ChecklistItemPatch()


# ── _review_to_dict 신규 필드 직렬화 검증 ────────────────────

def test_serialize_section_ijk_fields():
    """Section I/J/K 신규 필드가 _review_to_dict에서 올바르게 직렬화된다."""
    import datetime as dt
    from decimal import Decimal
    from api.routes.wake_up_reviews import _review_to_dict

    review = _make_review(
        # Section I
        optimal_params={"stop_loss_pct": 1.0},
        optimal_pnl=Decimal("500.00"),
        optimal_pnl_pct=Decimal("2.50"),
        actual_vs_optimal_diff_pct=Decimal("-1.20"),
        optimal_long_term_ev=Decimal("0.5000"),
        optimal_long_term_wr=Decimal("0.4200"),
        optimal_long_term_sharpe=Decimal("1.2000"),
        optimal_long_term_trades=34,
        optimal_overfit_risk="low",
        optimal_entry_timing="더 일찍",
        optimal_exit_timing="동일",
        optimal_key_diff="SL 2.0→1.0",
        # Section J
        root_cause_codes=["NO_GRID_SEARCH", "STALE_PARAMS"],
        root_cause_detail="그리드 서치 미실행",
        decision_date=dt.date(2026, 3, 28),
        decision_by="alice",
        info_gap_had="4H WF 결과",
        info_gap_new="FX regime 임계값",
        # Section K
        action_items=[{"id": "K1-1", "action": "SL변경", "status": "open"}],
        prevention_checklist=[{"item": "그리드서치200+", "checked": False}],
        review_quality_score=Decimal("8.50"),
    )

    d = _review_to_dict(review)

    # Section I
    assert d["optimal_params"] == {"stop_loss_pct": 1.0}
    assert d["optimal_pnl"] == 500.0
    assert d["optimal_pnl_pct"] == 2.5
    assert d["actual_vs_optimal_diff_pct"] == -1.2
    assert d["optimal_long_term_ev"] == 0.5
    assert d["optimal_long_term_wr"] == 0.42
    assert d["optimal_long_term_sharpe"] == 1.2
    assert d["optimal_long_term_trades"] == 34
    assert d["optimal_overfit_risk"] == "low"
    assert d["optimal_entry_timing"] == "더 일찍"
    assert d["optimal_exit_timing"] == "동일"
    assert d["optimal_key_diff"] == "SL 2.0→1.0"

    # Section J
    assert d["root_cause_codes"] == ["NO_GRID_SEARCH", "STALE_PARAMS"]
    assert d["root_cause_detail"] == "그리드 서치 미실행"
    assert d["decision_date"] == "2026-03-28"
    assert d["decision_by"] == "alice"
    assert d["info_gap_had"] == "4H WF 결과"
    assert d["info_gap_new"] == "FX regime 임계값"

    # Section K
    assert d["action_items"][0]["id"] == "K1-1"
    assert d["prevention_checklist"][0]["item"] == "그리드서치200+"
    assert d["review_quality_score"] == 8.5


def test_serialize_section_ijk_all_none():
    """신규 필드 전부 None → None으로 직렬화."""
    from api.routes.wake_up_reviews import _review_to_dict

    review = _make_review()  # 모두 None 기본값
    d = _review_to_dict(review)

    assert d["optimal_params"] is None
    assert d["optimal_pnl"] is None
    assert d["root_cause_codes"] is None
    assert d["decision_date"] is None
    assert d["action_items"] is None
    assert d["review_quality_score"] is None


def test_serialize_root_cause_codes_list_conversion():
    """root_cause_codes가 list-like일 때 list로 변환."""
    from api.routes.wake_up_reviews import _review_to_dict

    # PostgreSQL ARRAY는 list로 반환되지만 다른 iterable도 대응
    review = _make_review(root_cause_codes=["NO_WF", "REGIME_BLIND"])
    d = _review_to_dict(review)
    assert isinstance(d["root_cause_codes"], list)
    assert d["root_cause_codes"] == ["NO_WF", "REGIME_BLIND"]


# ── GET /by-id/{review_id} ────────────────────────────────────

@pytest.mark.asyncio
async def test_bid1_get_review_by_id_found():
    """PK 기준 단건 조회 — 존재하는 review_id."""
    from api.routes.wake_up_reviews import get_review_by_id

    review = _make_review(id=42, pair="BTC_JPY", realized_pnl=-100)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = review
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_review_by_id(review_id=42, db=db)
    assert result["review"]["id"] == 42
    assert result["review"]["pair"] == "BTC_JPY"


@pytest.mark.asyncio
async def test_bid2_get_review_by_id_not_found():
    """PK 기준 단건 조회 — 없으면 404."""
    from fastapi import HTTPException
    from api.routes.wake_up_reviews import get_review_by_id

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(HTTPException) as exc:
        await get_review_by_id(review_id=9999, db=db)
    assert exc.value.status_code == 404


# ── GET /patterns mock 기반 테스트 ──────────────────────────

@pytest.mark.asyncio
async def test_pt1_patterns_empty():
    """root_cause_codes 있는 리뷰 없을 때 patterns = []."""
    from api.routes.wake_up_reviews import get_patterns

    db = AsyncMock()

    # unnest raw SQL result → []
    raw_result = MagicMock()
    raw_result.all.return_value = []
    # count result → 0
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0

    db.execute = AsyncMock(side_effect=[raw_result, count_result])

    result = await get_patterns(db=db)
    assert result["patterns"] == []
    assert result["total_reviews_with_codes"] == 0


@pytest.mark.asyncio
async def test_pt2_patterns_aggregated():
    """root_cause_codes unnest 집계 정상."""
    from api.routes.wake_up_reviews import get_patterns

    db = AsyncMock()

    row1 = MagicMock()
    row1.code = "NO_GRID_SEARCH"
    row1.cnt = 4
    row2 = MagicMock()
    row2.code = "STALE_PARAMS"
    row2.cnt = 2

    raw_result = MagicMock()
    raw_result.all.return_value = [row1, row2]
    count_result = MagicMock()
    count_result.scalar_one.return_value = 5  # 5개 리뷰 중 codes 있음

    db.execute = AsyncMock(side_effect=[raw_result, count_result])

    result = await get_patterns(db=db)
    assert result["total_reviews_with_codes"] == 5
    assert len(result["patterns"]) == 2
    assert result["patterns"][0]["code"] == "NO_GRID_SEARCH"
    assert result["patterns"][0]["count"] == 4
    assert result["patterns"][0]["pct"] == 80.0
    assert result["patterns"][1]["code"] == "STALE_PARAMS"
    assert result["patterns"][1]["pct"] == 40.0


# ── switch approve warning 단위 테스트 ─────────────────────

@pytest.mark.asyncio
async def test_sw1_approve_with_open_actions_includes_warning():
    """approve 시 현재 전략에 open action_items 있으면 warning 포함."""
    from api.routes.strategy_scores import approve_switch_recommendation, ApproveRequest
    from unittest.mock import AsyncMock, MagicMock, patch

    # switch recommendation row
    rec = MagicMock()
    rec.decision = "pending"
    rec.current_strategy_id = 7
    rec.recommended_strategy_id = 12

    # WakeUpReview with open action
    wur = MagicMock()
    wur.action_items = [
        {"id": "K1-1", "action": "SL변경", "assignee": "maria", "status": "open", "deadline": "2026-04-10"},
        {"id": "K1-2", "action": "WF추가", "assignee": "rachel", "status": "done"},
    ]

    db = AsyncMock()
    rec_result = MagicMock()
    rec_result.scalars.return_value.first.return_value = rec
    wur_result = MagicMock()
    wur_result.scalars.return_value.all.return_value = [wur]
    db.execute = AsyncMock(side_effect=[rec_result, wur_result])
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    state = MagicMock()
    state.models.switch_recommendation = MagicMock()

    # select 자체를 patch → state.models.switch_recommendation MagicMock으로 인한 SA 에러 회피
    with patch("api.routes.strategy_scores.select", return_value=MagicMock()):
        with patch("api.routes.strategy_scores._unregister_recommended_paper", new_callable=AsyncMock):
            with patch("api.routes.strategy_scores._rec_to_dict", return_value={"id": 99, "decision": "approved"}):
                result = await approve_switch_recommendation(
                    rec_id=99,
                    body=ApproveRequest(decided_by="soobo"),
                    state=state,
                    db=db,
                )

    assert "warning" in result
    assert result["warning"]["open_action_count"] == 1
    assert any("K1-1" in item for item in result["warning"]["items"])


@pytest.mark.asyncio
async def test_sw2_approve_no_open_actions_no_warning():
    """현재 전략에 open action 없으면 warning 미포함."""
    from api.routes.strategy_scores import approve_switch_recommendation, ApproveRequest
    from unittest.mock import AsyncMock, MagicMock, patch

    rec = MagicMock()
    rec.decision = "pending"
    rec.current_strategy_id = 7
    rec.recommended_strategy_id = 12

    wur = MagicMock()
    wur.action_items = [{"id": "K1-1", "action": "WF추가", "status": "done"}]

    db = AsyncMock()
    rec_result = MagicMock()
    rec_result.scalars.return_value.first.return_value = rec
    wur_result = MagicMock()
    wur_result.scalars.return_value.all.return_value = [wur]
    db.execute = AsyncMock(side_effect=[rec_result, wur_result])
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    state = MagicMock()
    state.models.switch_recommendation = MagicMock()

    with patch("api.routes.strategy_scores.select", return_value=MagicMock()):
        with patch("api.routes.strategy_scores._unregister_recommended_paper", new_callable=AsyncMock):
            with patch("api.routes.strategy_scores._rec_to_dict", return_value={"id": 1, "decision": "approved"}):
                result = await approve_switch_recommendation(
                    rec_id=1,
                    body=ApproveRequest(decided_by="soobo"),
                    state=state,
                    db=db,
                )

    assert "warning" not in result


# ── BUG-025: 파이프라인 필드 직렬화 검증 ──────────────────────

def test_pipeline_fields_in_review_statuses():
    """pending_pipeline이 REVIEW_STATUSES 상수에 포함된다."""
    assert "pending_pipeline" in REVIEW_STATUSES, (
        f"REVIEW_STATUSES에 pending_pipeline 없음: {REVIEW_STATUSES}"
    )


def test_serialize_pipeline_fields():
    """BUG-025 신규 필드(exchange, position_type, pipeline_status, scheduled_at)가 _review_to_dict에 포함된다."""
    import datetime as dt
    from api.routes.wake_up_reviews import _review_to_dict

    now = dt.datetime(2026, 4, 5, 12, 0, 0, tzinfo=dt.timezone.utc)
    review = _make_review(
        exchange="gmo",
        position_type="box",
        pipeline_status="pending_pipeline",
        scheduled_at=now,
        pipeline_started_at=None,
        pipeline_completed_at=None,
    )
    d = _review_to_dict(review)

    assert d["exchange"] == "gmo"
    assert d["position_type"] == "box"
    assert d["pipeline_status"] == "pending_pipeline"
    assert d["scheduled_at"] == now.isoformat()
    assert d["pipeline_started_at"] is None
    assert d["pipeline_completed_at"] is None


def test_serialize_pipeline_fields_all_none():
    """exchange/position_type/pipeline 필드 모두 None 시 None 직렬화."""
    from api.routes.wake_up_reviews import _review_to_dict

    review = _make_review()  # 기본값 모두 None
    d = _review_to_dict(review)

    assert d["exchange"] is None
    assert d["position_type"] is None
    assert d["pipeline_status"] is None
    assert d["scheduled_at"] is None


@pytest.mark.asyncio
async def test_pipeline_webhook_skipped_when_no_token():
    """RACHEL_WEBHOOK_TOKEN 미설정 시 _send_pipeline_webhook → False."""
    from core.task.wake_up_trigger import _send_pipeline_webhook

    review = MagicMock()
    review.id = 1
    review.pair = "USD_JPY"
    review.position_id = 9
    review.position_type = "trend"
    review.exchange = "gmo"
    review.realized_pnl = Decimal("-200")

    with patch("core.task.wake_up_trigger.RACHEL_WEBHOOK_TOKEN", ""):
        result = await _send_pipeline_webhook(review, http_client=None)

    assert result is False

