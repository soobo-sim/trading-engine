"""
core/safety/guardrails.py 단위 테스트 — AiGuardrails.

DB는 SQLite 인메모리. 진입 액션만 체크하는 것, 청산은 무조건 통과, 사이즈 조정 등을 검증.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import (
    create_balance_entry_model,
    create_trade_model,
    create_strategy_model,
)
from adapters.database.session import Base
from core.data.dto import Decision, SignalSnapshot, modify_decision
from core.safety.guardrails import AiGuardrails


TstTrade = create_trade_model("tst3", order_id_length=40, pair_column="pair")
TstStrategy = create_strategy_model("tst3")
TstBalance = create_balance_entry_model("tst3")


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        tables = [
            Base.metadata.tables[t]
            for t in Base.metadata.tables
            if t.startswith("tst3_") or t == "strategy_techniques"
        ]
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
def guardrail(session_factory):
    return AiGuardrails(
        session_factory=session_factory,
        trade_model=TstTrade,
        settings={"max_trades_per_day": 3, "max_daily_loss_pct": 5.0},
    )


@pytest_asyncio.fixture
def guardrail_with_balance(session_factory):
    """GR-04 테스트용 — balance_model 포함."""
    return AiGuardrails(
        session_factory=session_factory,
        trade_model=TstTrade,
        balance_model=TstBalance,
        settings={"max_trades_per_day": 10, "max_daily_loss_pct": 50.0},
    )


def _make_snapshot(signal: str = "entry_ok") -> SignalSnapshot:
    ts = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    return SignalSnapshot(
        pair="BTC_JPY",
        exchange="bitflyer",
        timestamp=ts,
        signal=signal,
        current_price=5_000_000.0,
        exit_signal={"action": "hold"},
    )


def _make_decision(action: str = "entry_long", size_pct: float = 1.0) -> Decision:
    return Decision(
        action=action,
        pair="BTC_JPY",
        exchange="bitflyer",
        confidence=0.7,
        size_pct=size_pct,
        stop_loss=4_900_000.0,
        take_profit=None,
        reasoning="테스트",
        risk_factors=(),
        source="rule_based_v1",
        trigger="regular_4h",
        raw_signal="entry_ok",
    )


# ── 청산·hold는 무조건 통과 ────────────────────────────────────


@pytest.mark.asyncio
async def test_exit_always_approved(guardrail, session_factory):
    """
    Given: action=exit
    When:  check() 호출
    Then:  approved=True (안전장치 체크 없음)
    """
    d = _make_decision(action="exit", size_pct=1.0)
    result = await guardrail.check(d, _make_snapshot())
    assert result.approved is True


@pytest.mark.asyncio
async def test_hold_always_approved(guardrail):
    """action=hold 무조건 통과."""
    d = _make_decision(action="hold", size_pct=0.0)
    result = await guardrail.check(d, _make_snapshot())
    assert result.approved is True


@pytest.mark.asyncio
async def test_tighten_stop_always_approved(guardrail):
    """action=tighten_stop 무조건 통과."""
    d = _make_decision(action="tighten_stop", size_pct=0.0)
    result = await guardrail.check(d, _make_snapshot())
    assert result.approved is True


# ── GR-01: 일일 최대 거래 횟수 ────────────────────────────────


@pytest.mark.asyncio
async def test_gr01_blocks_when_daily_count_exceeded(session_factory):
    """
    Given: 당일 거래 3건 이미 있음 (max_trades_per_day=3)
    When:  entry_long check()
    Then:  approved=False, GR-01 violation
    """
    today = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
    async with session_factory() as db:
        for i in range(3):
            trade = TstTrade(
                order_id=f"ORD-{i:04d}",
                pair="BTC_JPY",
                order_type="buy",
                amount=0.01,
                price=5_000_000.0,
                status="completed",
                reasoning="테스트",
                created_at=today,
            )
            db.add(trade)
        await db.commit()

    g = AiGuardrails(
        session_factory=session_factory,
        trade_model=TstTrade,
        settings={"max_trades_per_day": 3, "max_daily_loss_pct": 5.0},
    )
    d = _make_decision("entry_long")
    result = await g.check(d, _make_snapshot())
    assert result.approved is False
    assert any("GR-01" in v for v in result.violations)


@pytest.mark.asyncio
async def test_gr01_approves_when_under_limit(guardrail):
    """
    Given: 당일 거래 0건
    When:  entry_long check()
    Then:  GR-01 통과
    """
    d = _make_decision("entry_long")
    result = await guardrail.check(d, _make_snapshot())
    # GR-01은 통과 (위반 없음)
    assert not any("GR-01" in v for v in result.violations)


# ── GR-03: 사이즈 클램핑 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_gr03_clamps_size_above_max(guardrail):
    """
    Given: size_pct=1.2 (80% 초과)
    When:  check()
    Then:  final_decision.size_pct <= 0.8
    """
    d = _make_decision("entry_long", size_pct=1.2)
    result = await guardrail.check(d, _make_snapshot())
    assert result.final_decision.size_pct <= 0.8


@pytest.mark.asyncio
async def test_gr03_does_not_change_valid_size(guardrail):
    """
    Given: size_pct=0.5 (허용 범위)
    When:  check()
    Then:  size_pct 변경 없음
    """
    d = _make_decision("entry_long", size_pct=0.5)
    result = await guardrail.check(d, _make_snapshot())
    assert result.final_decision.size_pct == 0.5


# ── entry_short도 동일 규칙 적용 (GR-01) ─────────────────────


@pytest.mark.asyncio
async def test_gr01_also_blocks_entry_short(session_factory):
    """
    Given: 당일 거래 3건 완료 (max_trades_per_day=3), action=entry_short
    When:  check()
    Then:  approved=False — entry_short도 entry_*이므로 동일 체크
    """
    today = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
    async with session_factory() as db:
        for i in range(3):
            trade = TstTrade(
                order_id=f"ORDS-{i:04d}",
                pair="USD_JPY",
                order_type="sell",
                amount=10000.0,
                price=150.0,
                status="completed",
                reasoning="숏 테스트",
                created_at=today,
            )
            db.add(trade)
        await db.commit()

    g = AiGuardrails(
        session_factory=session_factory,
        trade_model=TstTrade,
        settings={"max_trades_per_day": 3, "max_daily_loss_pct": 5.0},
    )
    d = _make_decision("entry_short")
    result = await g.check(d, _make_snapshot("entry_sell"))
    assert result.approved is False
    assert any("GR-01" in v for v in result.violations)


# ── BUG fix: pending 거래는 카운트 안 됨 ────────────────────────


@pytest.mark.asyncio
async def test_gr01_pending_trades_not_counted(session_factory):
    """
    Given: 당일 pending 거래 10건 (max_trades_per_day=3)
    When:  entry_long check()
    Then:  approved=True — pending은 카운트 대상 아님 (status="pending")
    """
    today = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
    async with session_factory() as db:
        for i in range(10):
            trade = TstTrade(
                order_id=f"ORDP-{i:04d}",
                pair="BTC_JPY",
                order_type="buy",
                amount=0.01,
                price=5_000_000.0,
                status="pending",          # pending — 카운트 안 됨
                reasoning="미체결 주문",
                created_at=today,
            )
            db.add(trade)
        await db.commit()

    g = AiGuardrails(
        session_factory=session_factory,
        trade_model=TstTrade,
        settings={"max_trades_per_day": 3, "max_daily_loss_pct": 5.0},
    )
    d = _make_decision("entry_long")
    result = await g.check(d, _make_snapshot())
    assert not any("GR-01" in v for v in result.violations)


# ── GR-02: 일일 손실 차단 ────────────────────────────────────


@pytest.mark.asyncio
async def test_gr02_blocks_when_daily_loss_exceeded(session_factory):
    """
    Given: 당일 손실 -6% (max_daily_loss_pct=5.0)
    When:  entry_long check()
    Then:  approved=False, GR-02 violation
    """
    today = datetime.now(timezone.utc).replace(hour=3, minute=0, second=0, microsecond=0)
    async with session_factory() as db:
        trade = TstTrade(
            order_id="ORD-LOSS-001",
            pair="BTC_JPY",
            order_type="buy",
            amount=0.01,
            price=5_000_000.0,
            status="completed",
            reasoning="손절",
            profit_loss=-300_000.0,
            profit_loss_percentage=-6.0,   # -6% 손실
            closed_at=today,
            created_at=today,
        )
        db.add(trade)
        await db.commit()

    g = AiGuardrails(
        session_factory=session_factory,
        trade_model=TstTrade,
        settings={"max_trades_per_day": 10, "max_daily_loss_pct": 5.0},
    )
    d = _make_decision("entry_long")
    result = await g.check(d, _make_snapshot())
    assert result.approved is False
    assert any("GR-02" in v for v in result.violations)


# ── GR-04: 포트폴리오 낙폭 ──────────────────────────────────


@pytest.mark.asyncio
async def test_gr04_no_balance_model_always_passes(guardrail):
    """
    Given: balance_model=None (미설정)
    When:  entry_long check()
    Then:  GR-04 위반 없음 (비활성화)
    """
    d = _make_decision("entry_long")
    result = await guardrail.check(d, _make_snapshot())
    assert not any("GR-04" in v for v in result.violations)


@pytest.mark.asyncio
async def test_gr04_passes_when_under_threshold(guardrail_with_balance, session_factory):
    """
    Given: 피크 1,000,000 JPY, 현재 950,000 JPY → DD 5% < 15%
    When:  entry_long check()
    Then:  GR-04 통과
    """
    base_time = datetime(2026, 4, 1, 6, 0, 0, tzinfo=timezone.utc)
    async with session_factory() as db:
        db.add(TstBalance(currency="jpy", available=1_000_000.0, created_at=base_time))
        db.add(TstBalance(
            currency="jpy", available=950_000.0,
            created_at=base_time + timedelta(hours=1)
        ))
        await db.commit()

    d = _make_decision("entry_long")
    result = await guardrail_with_balance.check(d, _make_snapshot())
    assert not any("GR-04" in v for v in result.violations)


@pytest.mark.asyncio
async def test_gr04_blocks_when_dd_exceeds_threshold(guardrail_with_balance, session_factory):
    """
    Given: 피크 1,000,000 JPY, 현재 840,000 JPY → DD 16% ≥ 15%
    When:  entry_long check()
    Then:  approved=False, GR-04 violation
    """
    base_time = datetime(2026, 4, 1, 6, 0, 0, tzinfo=timezone.utc)
    async with session_factory() as db:
        db.add(TstBalance(currency="jpy", available=1_000_000.0, created_at=base_time))
        db.add(TstBalance(
            currency="jpy", available=840_000.0,
            created_at=base_time + timedelta(hours=1)
        ))
        await db.commit()

    d = _make_decision("entry_long")
    result = await guardrail_with_balance.check(d, _make_snapshot())
    assert result.approved is False
    assert any("GR-04" in v for v in result.violations)


@pytest.mark.asyncio
async def test_gr04_passes_when_no_balance_records(guardrail_with_balance):
    """
    Given: balance_entries 테이블 비어있음
    When:  entry_long check()
    Then:  GR-04 통과 (데이터 없음 → 안전 방향)
    """
    d = _make_decision("entry_long")
    result = await guardrail_with_balance.check(d, _make_snapshot())
    assert not any("GR-04" in v for v in result.violations)


@pytest.mark.asyncio
async def test_gr04_passes_when_peak_equals_current(guardrail_with_balance, session_factory):
    """
    Given: 피크 = 현재 = 1,000,000 JPY → DD 0%
    When:  entry_long check()
    Then:  GR-04 통과
    """
    base_time = datetime(2026, 4, 1, 6, 0, 0, tzinfo=timezone.utc)
    async with session_factory() as db:
        db.add(TstBalance(currency="jpy", available=1_000_000.0, created_at=base_time))
        await db.commit()

    d = _make_decision("entry_long")
    result = await guardrail_with_balance.check(d, _make_snapshot())
    assert not any("GR-04" in v for v in result.violations)


@pytest.mark.asyncio
async def test_gr04_custom_threshold(session_factory):
    """
    Given: max_portfolio_dd_pct=10.0, 피크 1,000,000, 현재 890,000 → DD 11%
    When:  entry_long check()
    Then:  approved=False, GR-04 violation (임계 10% 초과)
    """
    base_time = datetime(2026, 4, 1, 6, 0, 0, tzinfo=timezone.utc)
    async with session_factory() as db:
        db.add(TstBalance(currency="jpy", available=1_000_000.0, created_at=base_time))
        db.add(TstBalance(
            currency="jpy", available=890_000.0,
            created_at=base_time + timedelta(hours=1)
        ))
        await db.commit()

    g = AiGuardrails(
        session_factory=session_factory,
        trade_model=TstTrade,
        balance_model=TstBalance,
        settings={
            "max_trades_per_day": 10,
            "max_daily_loss_pct": 50.0,
            "max_portfolio_dd_pct": 10.0,
        },
    )
    d = _make_decision("entry_long")
    result = await g.check(d, _make_snapshot())
    assert result.approved is False
    assert any("GR-04" in v for v in result.violations)


@pytest.mark.asyncio
async def test_gr04_passes_when_asset_increased(guardrail_with_balance, session_factory):
    """
    Given: 피크 1,000,000 JPY, 현재 1,100,000 JPY → DD 음수 (자산 증가)
    When:  entry_long check()
    Then:  GR-04 통과 — 낙폭이 아닌 상승이므로 차단 불필요
    """
    base_time = datetime(2026, 4, 1, 6, 0, 0, tzinfo=timezone.utc)
    async with session_factory() as db:
        db.add(TstBalance(currency="jpy", available=1_000_000.0, created_at=base_time))
        db.add(TstBalance(
            currency="jpy", available=1_100_000.0,
            created_at=base_time + timedelta(hours=1)
        ))
        await db.commit()

    d = _make_decision("entry_long")
    result = await guardrail_with_balance.check(d, _make_snapshot())
    assert not any("GR-04" in v for v in result.violations)

