"""
BUG-032: add_position advisory_id 단위 쿨다운 1회 제한

C-01: 새 advisory_id → add_position 실행
C-02: 동일 advisory_id 2회차 → 스킵
C-03: advisory_id 없음(v1 폴백) → 쿨다운 미적용 (매번 실행 허용)
C-04: 다른 advisory_id → 새 피라미딩 허용
C-05: RachelAdvisory.decide()가 Decision.meta['advisory_id'] 기록
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.data.dto import Decision
from core.exchange.types import Position


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _make_result(advisory_id=None, action="add_position"):
    meta = {}
    if advisory_id is not None:
        meta["advisory_id"] = advisory_id
    decision = Decision(
        action=action, pair="btc_jpy", exchange="gmo_coin",
        confidence=0.8, size_pct=0.35, stop_loss=None, take_profit=None,
        reasoning="test", risk_factors=(), source="rachel_advisory",
        trigger="regular_4h", raw_signal="entry_ok", meta=meta,
    )
    result = MagicMock()
    result.action = action
    result.decision = decision
    result.judgment_id = None
    return result


def _make_mixin_with_position(pyramid_count=0, last_pyramid_advisory_id=None):
    from core.punisher.strategy._execution_mixin import ExecutionMixin

    mixin = ExecutionMixin.__new__(ExecutionMixin)
    mixin._log_prefix = "[GmocMgr]"
    mixin._position = {}
    mixin._add_to_position = AsyncMock()

    extra = {"side": "buy", "pyramid_count": pyramid_count}
    if last_pyramid_advisory_id is not None:
        extra["last_pyramid_advisory_id"] = last_pyramid_advisory_id

    pos = Position(
        pair="btc_jpy",
        entry_price=12_460_755.0,
        entry_amount=0.007,
        stop_loss_price=12_342_973.0,
        db_record_id=7,
        extra=extra,
    )
    mixin._position["btc_jpy"] = pos
    return mixin, pos


# ──────────────────────────────────────────────────────────────
# C-01: 새 advisory_id → 실행
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c01_new_advisory_id_executes():
    """첫 번째 advisory_id → add_position 실행."""
    mixin, pos = _make_mixin_with_position(pyramid_count=1, last_pyramid_advisory_id=None)
    result = _make_result(advisory_id=93)

    # add_to_position 호출 후 pyramid_count 증가 시뮬레이션
    async def fake_add(*a, **kw):
        pos.extra["pyramid_count"] = 2
        pos.extra["last_pyramid_advisory_id"] = 93

    mixin._add_to_position = fake_add

    snapshot = MagicMock()
    snapshot.current_price = 12_500_000.0
    snapshot.atr = None
    await mixin._handle_execution_result("btc_jpy", result, snapshot, {}, {})
    assert pos.extra.get("last_pyramid_advisory_id") == 93


# ──────────────────────────────────────────────────────────────
# C-02: 동일 advisory_id 2회차 → 스킵
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c02_same_advisory_id_skips():
    """동일 advisory_id(93)로 2회 호출 → 2회차 스킵."""
    mixin, pos = _make_mixin_with_position(pyramid_count=1, last_pyramid_advisory_id=93)
    result = _make_result(advisory_id=93)
    executed = []
    mixin._add_to_position = AsyncMock(side_effect=lambda *a, **kw: executed.append(True))

    snapshot = MagicMock()
    snapshot.current_price = 12_500_000.0
    snapshot.atr = None
    await mixin._handle_execution_result("btc_jpy", result, snapshot, {}, {})
    assert len(executed) == 0, "_add_to_position이 호출되면 안 됨"


# ──────────────────────────────────────────────────────────────
# C-03: advisory_id 없음 → 쿨다운 미적용
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c03_no_advisory_id_no_cooldown():
    """advisory_id 없음(v1 폴백) → 쿨다운 미적용, 실행됨."""
    mixin, pos = _make_mixin_with_position(pyramid_count=1, last_pyramid_advisory_id=None)
    result = _make_result(advisory_id=None)
    executed = []

    async def fake_add(*a, **kw):
        executed.append(True)
        pos.extra["pyramid_count"] = 2

    mixin._add_to_position = fake_add

    snapshot = MagicMock()
    snapshot.current_price = 12_500_000.0
    snapshot.atr = None
    await mixin._handle_execution_result("btc_jpy", result, snapshot, {}, {})
    assert len(executed) == 1, "advisory_id 없을 때도 실행돼야 함"


# ──────────────────────────────────────────────────────────────
# C-04: 다른 advisory_id → 새 피라미딩 허용
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c04_different_advisory_id_executes():
    """이전 advisory_id=93, 새 advisory_id=95 → 실행 허용."""
    mixin, pos = _make_mixin_with_position(pyramid_count=1, last_pyramid_advisory_id=93)
    result = _make_result(advisory_id=95)
    executed = []

    async def fake_add(*a, **kw):
        executed.append(True)
        pos.extra["pyramid_count"] = 2

    mixin._add_to_position = fake_add

    snapshot = MagicMock()
    snapshot.current_price = 12_500_000.0
    snapshot.atr = None
    await mixin._handle_execution_result("btc_jpy", result, snapshot, {}, {})
    assert len(executed) == 1, "다른 advisory_id는 허용돼야 함"


# ──────────────────────────────────────────────────────────────
# C-05: RachelAdvisory.decide() → meta['advisory_id'] 기록
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c05_rachel_advisory_records_advisory_id():
    """RachelAdvisory.decide()가 advisory.id를 Decision.meta에 삽입한다."""
    from core.judge.decision.rachel_advisory import RachelAdvisoryDecision
    from core.data.dto import SignalSnapshot

    advisory_mock = MagicMock()
    advisory_mock.id = 94
    advisory_mock.action = "hold"
    advisory_mock.confidence = 0.9
    advisory_mock.size_pct = None
    advisory_mock.stop_loss = None
    advisory_mock.take_profit = None
    advisory_mock.reasoning = "test hold"
    from datetime import datetime, timezone, timedelta
    advisory_mock.expires_at = datetime.now(timezone.utc) + timedelta(hours=5)

    rd = RachelAdvisoryDecision.__new__(RachelAdvisoryDecision)
    rd._fetch_advisory = AsyncMock(return_value=advisory_mock)
    rd._fallback = MagicMock()

    snapshot = MagicMock(spec=SignalSnapshot)
    snapshot.pair = "btc_jpy"
    snapshot.exchange = "gmo_coin"
    snapshot.signal = "entry_ok"
    snapshot.position = None
    snapshot.current_price = 12_500_000.0
    snapshot.rsi = 55.0
    snapshot.ema_slope_pct = 0.2
    snapshot.atr = None
    snapshot.exit_signal = {}
    snapshot.params = {}

    with patch("core.judge.decision.rachel_advisory.advisory_bypass") as bp:
        bp.is_active.return_value = False
        decision = await rd.decide(snapshot)

    assert decision.meta.get("advisory_id") == 94, \
        f"advisory_id가 meta에 없음: {decision.meta}"
