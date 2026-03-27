"""
strategy_changes API unit tests.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from decimal import Decimal

from adapters.database.models import StrategyChange, SC_CHANGE_TYPES, SC_STATUSES


def _make_sc(**kwargs):
    defaults = dict(
        id=1, pair="BTC_JPY",
        old_strategy_id=8, new_strategy_id=9,
        change_type="param_change",
        changed_params={"ema_slope_entry_min": {"old": -0.05, "new": 0.1}},
        trigger="백테스트 결과",
        rationale="slope 0.1 방어",
        alice_opinion=None, samantha_opinion=None, rachel_verdict=None,
        kill_conditions=[{"condition": "3연패", "action": "rollback to 0.0"}],
        observation_period="첫 5거래",
        status="active",
        kill_triggered_at=None,
        outcome_summary=None,
        created_at=None,
    )
    defaults.update(kwargs)
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


# ── Pydantic 유효성 검증 ──────────────────────────────────────

def test_p1_invalid_change_type_rejected():
    from pydantic import ValidationError
    from api.routes.strategy_changes import StrategyChangeCreate
    with pytest.raises(ValidationError, match="change_type"):
        StrategyChangeCreate(pair="BTC_JPY", new_strategy_id=9, change_type="WRONG")


def test_p2_invalid_status_rejected():
    from pydantic import ValidationError
    from api.routes.strategy_changes import StrategyChangeCreate
    with pytest.raises(ValidationError, match="status"):
        StrategyChangeCreate(pair="BTC_JPY", new_strategy_id=9, change_type="param_change", status="unknown")


def test_p3_all_change_types_accepted():
    from api.routes.strategy_changes import StrategyChangeCreate
    for ct in SC_CHANGE_TYPES:
        obj = StrategyChangeCreate(pair="BTC_JPY", new_strategy_id=9, change_type=ct)
        assert obj.change_type == ct


def test_p4_all_statuses_accepted():
    from api.routes.strategy_changes import StrategyChangeCreate
    for st in SC_STATUSES:
        obj = StrategyChangeCreate(pair="BTC_JPY", new_strategy_id=9, change_type="param_change", status=st)
        assert obj.status == st


def test_p5_patch_invalid_status_rejected():
    from pydantic import ValidationError
    from api.routes.strategy_changes import StrategyChangePatch
    with pytest.raises(ValidationError, match="status"):
        StrategyChangePatch(status="bad_status")


# ── POST 생성 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c1_create_success():
    from api.routes.strategy_changes import create_strategy_change, StrategyChangeCreate
    body = StrategyChangeCreate(
        pair="BTC_JPY", old_strategy_id=8, new_strategy_id=9,
        change_type="param_change",
        changed_params={"ema_slope_entry_min": {"old": -0.05, "new": 0.1}},
    )
    saved = _make_sc()
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    async def fake_refresh(obj):
        for k, v in vars(saved).items():
            if not k.startswith("_"):
                try:
                    setattr(obj, k, v)
                except Exception:
                    pass
    db.refresh = AsyncMock(side_effect=fake_refresh)

    result = await create_strategy_change(body, db)
    assert result is not None
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


# ── GET 목록 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_g1_invalid_status_filter_rejected():
    from fastapi import HTTPException
    from api.routes.strategy_changes import list_strategy_changes
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await list_strategy_changes(status="bad", db=db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_g2_invalid_change_type_filter_rejected():
    from fastapi import HTTPException
    from api.routes.strategy_changes import list_strategy_changes
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await list_strategy_changes(change_type="INVALID", db=db)
    assert exc.value.status_code == 400


# ── GET 단건 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_g3_not_found_returns_404():
    from fastapi import HTTPException
    from api.routes.strategy_changes import get_strategy_change
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)
    with pytest.raises(HTTPException) as exc:
        await get_strategy_change(change_id=999, db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_g4_found_returns_data():
    from api.routes.strategy_changes import get_strategy_change
    db = AsyncMock()
    saved = _make_sc(id=1)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = saved
    db.execute = AsyncMock(return_value=result_mock)
    result = await get_strategy_change(change_id=1, db=db)
    assert result["strategy_change"]["id"] == 1


# ── PATCH ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_u1_patch_not_found_returns_404():
    from fastapi import HTTPException
    from api.routes.strategy_changes import patch_strategy_change, StrategyChangePatch
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)
    with pytest.raises(HTTPException) as exc:
        await patch_strategy_change(change_id=999, body=StrategyChangePatch(status="killed"), db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_u2_kill_sets_kill_triggered_at_auto():
    """status=killed 전환 시 kill_triggered_at 자동 설정."""
    from api.routes.strategy_changes import patch_strategy_change, StrategyChangePatch

    class FakeRow:
        id = 1; pair = "BTC_JPY"; old_strategy_id = 8; new_strategy_id = 9
        change_type = "param_change"; changed_params = None; trigger = None
        rationale = None; alice_opinion = None; samantha_opinion = None
        rachel_verdict = None; kill_conditions = None; observation_period = None
        status = "active"; kill_triggered_at = None; outcome_summary = None; created_at = None

    row = FakeRow()
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result_mock)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    body = StrategyChangePatch(status="killed")
    await patch_strategy_change(change_id=1, body=body, db=db)
    assert row.kill_triggered_at is not None


@pytest.mark.asyncio
async def test_u3_graduated_no_auto_kill_time():
    """status=graduated 전환 시 kill_triggered_at 자동 설정 안 됨."""
    from api.routes.strategy_changes import patch_strategy_change, StrategyChangePatch
    db = AsyncMock()
    row = _make_sc(status="active", kill_triggered_at=None)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result_mock)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    body = StrategyChangePatch(status="graduated", outcome_summary="관찰 기간 통과")
    await patch_strategy_change(change_id=1, body=body, db=db)
    # kill_triggered_at 자동 설정 안 됨
    assert row.kill_triggered_at is None
