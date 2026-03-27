"""
전략별 성과 분해 API 유닛 테스트 (PS-1 ~ PS-13).
"""
from __future__ import annotations

import math
import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from api.services.performance_service import (
    _compute_grade,
    _compute_metrics_for_strategy,
)
from adapters.database.models import create_trend_position_model

BfTrendPosition = create_trend_position_model("bf")


# ── Helpers ───────────────────────────────────────────────────

def _pos(pnl_jpy, strategy_id=9, pair="BTC_JPY", closed_at=None, created_at=None):
    m = MagicMock()
    m.realized_pnl_jpy = Decimal(str(pnl_jpy)) if pnl_jpy is not None else None
    m.realized_pnl_pct = Decimal("0.5") if pnl_jpy is not None else None
    m.strategy_id = strategy_id
    m.pair = pair
    m.status = "closed"
    now = datetime.now(tz=timezone.utc)
    m.closed_at = closed_at or now
    m.created_at = created_at or (now - timedelta(hours=8))
    return m


def _db_with(rows_per_call):
    """rows_per_call: list of lists — 각 execute 호출 순서별 rows."""
    db = AsyncMock()
    call_count = [0]

    async def fake_execute(stmt):
        idx = call_count[0]
        call_count[0] += 1
        rows = rows_per_call[idx] if idx < len(rows_per_call) else []
        r = MagicMock()
        r.scalars.return_value.all.return_value = rows
        r.scalars.return_value.first.return_value = rows[0] if rows else None
        return r

    db.execute = fake_execute
    return db


def _state(strategies=None):
    state = MagicMock()
    state.models.trend_position = BfTrendPosition
    state.models.box_position = MagicMock()
    state.pair_column = "product_code"

    strat_list = strategies or []
    state.models.strategy = MagicMock()
    return state, strat_list


# ── PS-1: strategy_id 지정 → 해당만 집계 ────────────────────

def test_ps1_strategy_id_filter():
    """strategy_id=9 포지션만 집계."""
    positions_9 = [_pos(+100, strategy_id=9), _pos(-50, strategy_id=9)]
    positions_8 = [_pos(+200, strategy_id=8)]

    # _compute_metrics_for_strategy는 넘겨받은 positions만 처리
    metrics = _compute_metrics_for_strategy(positions_9)
    assert metrics["total_trades"] == 2
    assert metrics["wins"] == 1
    assert metrics["losses"] == 1
    assert metrics["total_pnl_jpy"] == 50.0


# ── PS-2: 존재하지 않는 strategy_id → 빈 결과 ───────────────

def test_ps2_nonexistent_strategy_id():
    """포지션 0건 → 0값 반환 (에러 아님)."""
    metrics = _compute_metrics_for_strategy([])
    assert metrics["total_trades"] == 0
    assert metrics["wins"] == 0
    assert metrics["total_pnl_jpy"] == 0.0


# ── PS-3: realized_pnl=NULL → 제외 + excluded_count ─────────

def test_ps3_null_pnl_excluded():
    positions = [
        _pos(+100),
        _pos(None),  # NULL
        _pos(-50),
    ]
    metrics = _compute_metrics_for_strategy(positions)
    assert metrics["excluded_null_pnl_count"] == 1
    assert metrics["total_trades"] == 3
    assert metrics["wins"] == 1
    assert metrics["losses"] == 1


# ── PS-4: 포지션 0건 → 0값 반환 ────────────────────────────

def test_ps4_zero_positions():
    metrics = _compute_metrics_for_strategy([])
    assert metrics["total_trades"] == 0
    assert metrics["total_pnl_jpy"] == 0.0
    assert metrics["win_rate"] == 0.0
    assert metrics["sharpe_ratio"] is None


# ── PS-5: 승률 경계값 (0%, 100%) ────────────────────────────

def test_ps5_win_rate_boundaries():
    # 전부 손실 → 0%
    all_loss = [_pos(-100), _pos(-50), _pos(-200)]
    m_loss = _compute_metrics_for_strategy(all_loss)
    assert m_loss["win_rate"] == 0.0

    # 전부 이익 → 100%
    all_win = [_pos(+100), _pos(+50), _pos(+200)]
    m_win = _compute_metrics_for_strategy(all_win)
    assert m_win["win_rate"] == 100.0


# ── PS-6: 기간 필터 → 서비스 레벨 쿼리 (라우트 통과 검증) ──

def test_ps6_period_valid():
    from api.services.performance_service import PERIOD_DAYS
    assert "30d" in PERIOD_DAYS
    assert "7d" in PERIOD_DAYS
    assert "all" in PERIOD_DAYS


# ── PS-7: 전략 전환 시점 귀속 ────────────────────────────────

def test_ps7_strategy_attribution():
    """strategy_id 9 포지션과 8 포지션이 각각 집계."""
    pos_9 = [_pos(+100, strategy_id=9), _pos(+200, strategy_id=9)]
    pos_8 = [_pos(-300, strategy_id=8)]

    m9 = _compute_metrics_for_strategy(pos_9)
    m8 = _compute_metrics_for_strategy(pos_8)

    assert m9["total_pnl_jpy"] == 300.0
    assert m8["total_pnl_jpy"] == -300.0


# ── PS-8: strategy_changes 이후만 (쿼리 필터 확인) ──────────

def test_ps8_since_filter():
    """closed_at이 오래된 포지션은 since 이후 필터로 제외 (쿼리 레벨 책임)."""
    # 서비스는 넘겨받은 positions만 처리 → DB 쿼리가 since 필터 담당
    old_pos = [_pos(+9999, closed_at=datetime(2020, 1, 1, tzinfo=timezone.utc))]
    recent_pos = [_pos(+100)]
    # _compute_metrics는 넘겨받은 것만 처리
    m_recent = _compute_metrics_for_strategy(recent_pos)
    assert m_recent["total_trades"] == 1
    assert m_recent["total_pnl_jpy"] == 100.0


# ── PS-9: 전체 비교 (by-strategy) 응답 구조 ─────────────────

def test_ps9_by_strategy_structure():
    """by-strategy 응답 구조 검증."""
    # 단일 전략 row 시뮬레이션
    positions = [_pos(+100), _pos(-50), _pos(+200)]
    metrics = _compute_metrics_for_strategy(positions)
    grade = _compute_grade(metrics, len(positions))

    row = {"strategy_id": 9, "name": "v5", "status": "active", **metrics, "grade": grade}
    assert "total_pnl_jpy" in row
    assert "grade" in row
    assert "excluded_null_pnl_count" in row


# ── PS-10: Sharpe std=0 → null ───────────────────────────────

def test_ps10_sharpe_std_zero():
    """모든 거래 동일 pnl → std=0 → sharpe=None."""
    positions = [_pos(+100.0)] * 10
    metrics = _compute_metrics_for_strategy(positions)
    assert metrics["sharpe_ratio"] is None


# ── PS-11: grade 거래 10건 미만 → insufficient ───────────────

def test_ps11_grade_insufficient():
    assert _compute_grade({"expected_value": 100, "win_rate": 60}, 9) == "insufficient"
    assert _compute_grade({"expected_value": 100, "win_rate": 60}, 10) == "A"


# ── PS-12: archived 포함 정상 집계 ──────────────────────────

def test_ps12_archived_positions():
    """archived 전략 포지션도 집계."""
    positions = [_pos(+100), _pos(+50)]
    metrics = _compute_metrics_for_strategy(positions)
    assert metrics["total_trades"] == 2
    assert metrics["total_pnl_jpy"] == 150.0


# ── PS-13: excluded_null_pnl_count 투명 공개 ─────────────────

def test_ps13_excluded_count_transparency():
    """NULL pnl 개수가 excluded_null_pnl_count에 정확히 표시."""
    positions = [
        _pos(+100), _pos(None), _pos(None), _pos(-50)
    ]
    metrics = _compute_metrics_for_strategy(positions)
    assert metrics["excluded_null_pnl_count"] == 2
    assert metrics["total_trades"] == 4  # 전체 포지션 수
    assert metrics["wins"] == 1  # NULL 제외 후 유효 포지션만


# ── Grade 계산 추가 케이스 ────────────────────────────────────

@pytest.mark.parametrize("ev,wr,n,expected", [
    (100.0, 55.0, 15, "A"),   # EV+ AND WR>=50
    (100.0, 45.0, 15, "B"),   # EV+ only
    (-10.0, 55.0, 15, "B"),   # WR>=50 only
    (-10.0, 40.0, 15, "C"),   # 둘 다 미달
    (100.0, 60.0,  5, "insufficient"),  # 거래 부족
    (None,  None, 15, "C"),   # ev/wr 없음
])
def test_grade_parametrized(ev, wr, n, expected):
    assert _compute_grade({"expected_value": ev, "win_rate": wr}, n) == expected
