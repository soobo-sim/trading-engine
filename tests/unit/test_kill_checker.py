"""
Kill 조건 자동 모니터링 유닛 테스트 (K-1 ~ K-12).
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from core.punisher.monitoring.kill_checker import (
    KillResult,
    check_kill_conditions,
    eval_consecutive_losses,
    eval_no_trade_days,
    eval_first_n_win_rate,
    eval_max_drawdown,
    trigger_kill,
    send_kill_webhook,
)
from adapters.database.models import create_trend_position_model

# 실제 ORM 모델 (select/where 호환)
BfTrendPosition = create_trend_position_model("bf")


# ── Helpers ───────────────────────────────────────────────────

def _pos(pnl: float, closed_at=None, created_at=None, strategy_id=9, status="closed"):
    m = MagicMock()
    m.realized_pnl_jpy = Decimal(str(pnl))
    m.closed_at = closed_at or datetime.now(tz=timezone.utc)
    m.created_at = created_at or datetime.now(tz=timezone.utc)
    m.strategy_id = strategy_id
    m.status = status
    return m


def _sc(kill_conditions=None, status="active", kill_triggered_at=None, created_at=None, sc_id=1):
    m = MagicMock()
    m.id = sc_id
    m.new_strategy_id = 9
    m.pair = "BTC_JPY"
    m.kill_conditions = kill_conditions or {}
    m.status = status
    m.kill_triggered_at = kill_triggered_at
    m.created_at = created_at or datetime.now(tz=timezone.utc)
    return m


def _db_with(rows):
    db = AsyncMock()

    # 첫 번째 execute 호출용 (주 쿼리)
    main_result = MagicMock()
    main_result.scalars.return_value.all.return_value = rows
    main_result.scalars.return_value.first.return_value = rows[0] if rows else None

    db.execute = AsyncMock(return_value=main_result)
    return db


class FakePosModel:
    """사용 안 함 (BfTrendPosition으로 대체)."""
    pass


# ── K-1: 3연패 → Kill 발동 ───────────────────────────────────

@pytest.mark.asyncio
async def test_k1_consecutive_losses_triggers():
    pos_model = BfTrendPosition
    positions = [_pos(-100), _pos(-200), _pos(-50)]
    db = _db_with(positions)
    sc = _sc(kill_conditions={"consecutive_losses": 3})
    result = await check_kill_conditions(sc, db, pos_model)
    assert result is not None
    assert result.evaluator == "consecutive_losses"


# ── K-2: 2연패 → 미발동 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_k2_two_consecutive_losses_no_trigger():
    pos_model = BfTrendPosition
    # 3거래 중 최근 2개 손실, 1개 이익 → 미발동
    positions = [_pos(+100), _pos(-200), _pos(-50)]
    db = _db_with(positions)
    sc = _sc(kill_conditions={"consecutive_losses": 3})
    result = await check_kill_conditions(sc, db, pos_model)
    assert result is None


# ── K-3: 10일 무거래 → Kill 발동 ────────────────────────────

@pytest.mark.asyncio
async def test_k3_no_trade_days_triggers():
    pos_model = BfTrendPosition
    db = _db_with([])  # 최근 10일 거래 없음
    old_created = datetime.now(tz=timezone.utc) - timedelta(days=11)
    sc = _sc(kill_conditions={"no_trade_days": 10}, created_at=old_created)
    result = await check_kill_conditions(sc, db, pos_model)
    assert result is not None
    assert result.evaluator == "no_trade_days"


# ── K-4: 9일 무거래 → 미발동 ────────────────────────────────

@pytest.mark.asyncio
async def test_k4_nine_days_no_trigger():
    pos_model = BfTrendPosition
    db = _db_with([])
    recent_created = datetime.now(tz=timezone.utc) - timedelta(days=9)
    sc = _sc(kill_conditions={"no_trade_days": 10}, created_at=recent_created)
    result = await check_kill_conditions(sc, db, pos_model)
    assert result is None


# ── K-5: 첫 5거래 승률 < 60% → Kill 발동 ───────────────────

@pytest.mark.asyncio
async def test_k5_first_n_low_win_rate_triggers():
    pos_model = BfTrendPosition
    # 5거래 중 2승 3패 → 40% < 60%
    positions = [_pos(+100), _pos(-50), _pos(-50), _pos(+100), _pos(-50)]
    db = _db_with(positions)
    sc = _sc(kill_conditions={"first_n_trades_win_rate": {"n": 5, "min_rate": 60}})
    result = await check_kill_conditions(sc, db, pos_model)
    assert result is not None
    assert result.evaluator == "first_n_trades_win_rate"


# ── K-6: 첫 5거래 승률 = 60% → 미발동 (경계값 통과) ─────────

@pytest.mark.asyncio
async def test_k6_win_rate_at_boundary_no_trigger():
    pos_model = BfTrendPosition
    # 5거래 중 3승 2패 → 60% == min_rate → 미발동
    positions = [_pos(+100), _pos(+100), _pos(+100), _pos(-50), _pos(-50)]
    db = _db_with(positions)
    sc = _sc(kill_conditions={"first_n_trades_win_rate": {"n": 5, "min_rate": 60}})
    result = await check_kill_conditions(sc, db, pos_model)
    assert result is None


# ── K-7: DB 쿼리 실패 → skip + RuntimeError ─────────────────

@pytest.mark.asyncio
async def test_k7_db_failure_raises_runtime_error():
    pos_model = BfTrendPosition
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=Exception("DB 연결 실패"))
    sc = _sc(kill_conditions={"consecutive_losses": 3})
    with pytest.raises(RuntimeError, match="Kill 체크 실패"):
        await check_kill_conditions(sc, db, pos_model)


# ── K-8: Kill 발동 후 자동 롤백 없음 ────────────────────────

@pytest.mark.asyncio
async def test_k8_kill_does_not_archive_strategy():
    """trigger_kill은 strategy_change.status만 killed로 변경, 전략 archive 안 함."""
    sc = _sc()
    sc.status = "active"
    sc.kill_triggered_at = None

    db = AsyncMock()
    db.commit = AsyncMock()

    kill_result = KillResult(evaluator="consecutive_losses", detail="3연패")
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value=MagicMock(status_code=200))

    with patch.dict("os.environ", {"RACHEL_WEBHOOK_TOKEN": "test_token"}):
        await trigger_kill(sc, kill_result, db, http_client)

    assert sc.status == "killed"
    assert sc.kill_triggered_at is not None
    # strategy archive는 호출 안 됨 (별도 API 없음)
    db.commit.assert_awaited_once()


# ── K-9: webhook 전송 실패 → 다음 주기 재시도 가능 ──────────

@pytest.mark.asyncio
async def test_k9_webhook_failure_does_not_revert_db():
    """webhook 실패해도 DB kill_triggered_at은 유지 (다음 주기 중복 방지 idempotent)."""
    sc = _sc()
    sc.status = "active"
    sc.kill_triggered_at = None

    db = AsyncMock()
    db.commit = AsyncMock()

    kill_result = KillResult(evaluator="consecutive_losses", detail="3연패")
    http_client = AsyncMock()
    http_client.post = AsyncMock(side_effect=Exception("network error"))

    with patch.dict("os.environ", {"RACHEL_WEBHOOK_TOKEN": "test_token"}):
        await trigger_kill(sc, kill_result, db, http_client)

    # DB는 여전히 killed (webhook 실패해도 롤백 없음)
    assert sc.status == "killed"
    assert sc.kill_triggered_at is not None


# ── K-10: 이미 killed → 중복 무시 ───────────────────────────

@pytest.mark.asyncio
async def test_k10_already_killed_idempotent():
    pos_model = BfTrendPosition
    db = AsyncMock()
    sc = _sc(
        kill_conditions={"consecutive_losses": 3},
        kill_triggered_at=datetime.now(tz=timezone.utc),  # 이미 발동
    )
    result = await check_kill_conditions(sc, db, pos_model)
    assert result is None
    db.execute.assert_not_called()


# ── K-11: max_drawdown 초과 → Kill 발동 ─────────────────────

@pytest.mark.asyncio
async def test_k11_max_drawdown_triggers():
    from core.punisher.monitoring.kill_checker import eval_max_drawdown
    pos_model = BfTrendPosition
    old = datetime.now(tz=timezone.utc) - timedelta(days=1)
    # 누적: +1000 → peak=1000, 이후 -900 → dd=90% > 50%
    positions = [
        _pos(+1000, closed_at=old),
        _pos(-900, closed_at=datetime.now(tz=timezone.utc)),
    ]
    db = _db_with(positions)
    sc = _sc(created_at=old - timedelta(days=1))
    result = await eval_max_drawdown(50.0, sc, db, pos_model)
    assert result is not None
    assert result.evaluator == "max_drawdown_pct"


# ── K-12: kill_conditions에 해당 evaluator 없음 → skip ──────

@pytest.mark.asyncio
async def test_k12_unknown_evaluator_is_skipped():
    pos_model = BfTrendPosition
    db = AsyncMock()
    # 알 수 없는 조건만 있음 → 에러 아니고 None 반환
    sc = _sc(kill_conditions={"unknown_future_condition": 99})
    result = await check_kill_conditions(sc, db, pos_model)
    assert result is None
    db.execute.assert_not_called()
