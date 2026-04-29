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
from core.judge.safety.guardrails import AiGuardrails


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


def _make_snapshot(signal: str = "long_setup") -> SignalSnapshot:
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
        raw_signal="long_setup",
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
    result = await g.check(d, _make_snapshot("short_setup"))
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


# ── GR-05: ATH 기반 포트폴리오 낙폭 ────────────────────────────

@pytest.mark.asyncio
async def test_gr05_no_balance_model_always_passes(guardrail):
    """
    Given: balance_model=None (GR-05 비활성화)
    When:  entry_long check()
    Then:  GR-05 violation 없음
    """
    d = _make_decision("entry_long")
    result = await guardrail.check(d, _make_snapshot())
    assert not any("GR-05" in v for v in result.violations)


@pytest.mark.asyncio
async def test_gr05_passes_when_under_threshold(guardrail_with_balance, session_factory):
    """
    Given: ATH=1,000,000 JPY, 현재=900,000 JPY → DD=10% < 기본 임계 15%
    When:  entry_long check()
    Then:  GR-05 통과
    """
    base_time = datetime(2026, 4, 1, 6, 0, 0, tzinfo=timezone.utc)
    async with session_factory() as db:
        db.add(TstBalance(currency="jpy", available=1_000_000.0, created_at=base_time))
        db.add(TstBalance(
            currency="jpy", available=900_000.0,
            created_at=base_time + timedelta(days=5)
        ))
        await db.commit()

    d = _make_decision("entry_long")
    result = await guardrail_with_balance.check(d, _make_snapshot())
    assert not any("GR-05" in v for v in result.violations)


@pytest.mark.asyncio
async def test_gr05_blocks_when_ath_dd_exceeds_threshold(session_factory):
    """
    Given: ATH=1,000,000 JPY, 현재=820,000 JPY → DD=18% > 기본 15%
    When:  entry_long check() (max_portfolio_dd_pct 기본값 15.0)
    Then:  approved=False, GR-05 violation
    """
    base_time = datetime(2026, 4, 1, 6, 0, 0, tzinfo=timezone.utc)
    async with session_factory() as db:
        db.add(TstBalance(currency="jpy", available=1_000_000.0, created_at=base_time))
        db.add(TstBalance(
            currency="jpy", available=820_000.0,
            created_at=base_time + timedelta(days=30)
        ))
        await db.commit()

    g = AiGuardrails(
        session_factory=session_factory,
        trade_model=TstTrade,
        balance_model=TstBalance,
        settings={
            "max_trades_per_day": 10,
            "max_daily_loss_pct": 50.0,
            "max_portfolio_dd_pct": 15.0,
        },
    )
    d = _make_decision("entry_long")
    result = await g.check(d, _make_snapshot())
    assert result.approved is False
    assert any("GR-05" in v for v in result.violations)


@pytest.mark.asyncio
async def test_gr05_exit_always_bypasses(guardrail_with_balance, session_factory):
    """
    Given: ATH=1,000,000 JPY, 현재=700,000 JPY → DD=30% (GR-05 초과)
    When:  action=exit check()
    Then:  청산이므로 GR-05 체크 스킵 → approved=True
    """
    base_time = datetime(2026, 4, 1, 6, 0, 0, tzinfo=timezone.utc)
    async with session_factory() as db:
        db.add(TstBalance(currency="jpy", available=1_000_000.0, created_at=base_time))
        db.add(TstBalance(
            currency="jpy", available=700_000.0,
            created_at=base_time + timedelta(days=10)
        ))
        await db.commit()

    d = _make_decision("exit")
    result = await guardrail_with_balance.check(d, _make_snapshot())
    assert result.approved is True


# ──────────────────────────────────────────────────────────────
# 로깅 검증 — Guardrails 승인 INFO 로그
# ──────────────────────────────────────────────────────────────

class TestGuardrailsLogging:
    """진입 승인 시 INFO 로그 (기존 DEBUG → INFO 변경) 검증."""

    @pytest.mark.asyncio
    async def test_approval_logs_info(self, guardrail, caplog):
        """
        Given: 진입 가능 상태 (GR-01~05 모두 통과)
        When:  check(entry_long)
        Then:  INFO 로그 — '[Guardrail] ... 진입 승인' 메시지 포함
        """
        import logging
        d = _make_decision("entry_long")
        with caplog.at_level(logging.INFO, logger="core.judge.safety.guardrails"):
            result = await guardrail.check(d, _make_snapshot())
        assert result.approved is True
        info = [r for r in caplog.records if r.levelname == "INFO" and "진입 승인" in r.message]
        assert len(info) == 1
        assert "Guardrail" in info[0].message

    @pytest.mark.asyncio
    async def test_approval_log_contains_trade_count_and_size(self, guardrail, caplog):
        """
        Given: 진입 승인
        When:  check(entry_long)
        Then:  INFO 로그 — '금일 거래' + '사이즈' 포함
        """
        import logging
        d = _make_decision("entry_long")
        with caplog.at_level(logging.INFO, logger="core.judge.safety.guardrails"):
            await guardrail.check(d, _make_snapshot())
        info = [r for r in caplog.records if "진입 승인" in r.message]
        assert len(info) == 1
        assert "금일 거래" in info[0].message
        assert "사이즈" in info[0].message

    @pytest.mark.asyncio
    async def test_rejection_logs_warning_not_info(self, guardrail, session_factory, caplog):
        """
        Given: GR-01 위반 상태 (당일 거래 max+1)
        When:  check(entry_long)
        Then:  WARNING 로그 — '진입 거부' 메시지 (INFO 아님)
        """
        import logging
        # max_trades=3 기본값. 완료된 거래 3개 삽입 (오늘 날짜 기준)
        today = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
        async with session_factory() as db:
            for i in range(3):
                db.add(TstTrade(
                    order_id=f"test-order-{i:04d}",
                    order_type="market_buy",
                    amount=0.01,
                    price=5_000_000.0,
                    status="completed",
                    reasoning="테스트",
                    created_at=today,
                    pair="BTC_JPY",
                ))
            await db.commit()
        d = _make_decision("entry_long")
        with caplog.at_level(logging.DEBUG, logger="core.judge.safety.guardrails"):
            result = await guardrail.check(d, _make_snapshot())
        assert result.approved is False
        warn = [r for r in caplog.records if r.levelname == "WARNING" and "진입 거부" in r.message]
        assert len(warn) == 1
        info = [r for r in caplog.records if r.levelname == "INFO" and "진입 승인" in r.message]
        assert len(info) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["exit", "tighten_stop", "hold"])
    async def test_non_entry_action_skips_guardrails_and_no_info_log(self, guardrail, caplog, action):
        """
        Given: decision=exit/tighten_stop/hold
        When:  check() 호출
        Then:  approved=True, INFO/WARNING 로그 없음 (GR 검사 완전 생략)
        """
        import logging
        d = _make_decision(action)
        with caplog.at_level(logging.DEBUG, logger="core.judge.safety.guardrails"):
            result = await guardrail.check(d, _make_snapshot())
        assert result.approved is True
        info = [r for r in caplog.records if r.levelname in {"INFO", "WARNING"}]
        assert len(info) == 0


# ──────────────────────────────────────────────────────────────
# GR-06: 피라미딩 총 사이즈 한도 검사
# ──────────────────────────────────────────────────────────────

def _make_pyramid_snapshot(
    position_size_pct: float = 20.0,
    existing_total_size_pct: float = 0.20,
) -> SignalSnapshot:
    """GR-06 테스트용 — params 및 position 포함 SignalSnapshot."""
    from core.data.dto import PositionDTO

    ts = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    pos = PositionDTO(
        pair="BTC_JPY",
        entry_price=9_800_000.0,
        entry_amount=0.3,
        stop_loss_price=9_500_000.0,
        stop_tightened=False,
        extra={
            "pyramid_count": 1,
            "total_size_pct": existing_total_size_pct,
        },
    )
    return SignalSnapshot(
        pair="BTC_JPY",
        exchange="gmo_coin",
        timestamp=ts,
        signal="long_setup",
        current_price=10_000_000.0,
        exit_signal={"action": "hold"},
        position=pos,
        params={"position_size_pct": position_size_pct},
    )


@pytest.mark.asyncio
async def test_gr06_blocks_when_total_size_would_exceed_limit(guardrail):
    """
    G-01: add_position + existing=0.20 + new=0.20 → total=0.40 > max(0.20) → 거부
    """
    snap = _make_pyramid_snapshot(position_size_pct=20.0, existing_total_size_pct=0.20)
    d = _make_decision("add_position", size_pct=0.20)
    result = await guardrail.check(d, snap)
    assert result.approved is False
    assert "GR-06" in (result.rejection_reason or "") or "총 사이즈" in (result.rejection_reason or "")


@pytest.mark.asyncio
async def test_gr06_allows_when_total_size_within_limit(guardrail):
    """
    G-02: add_position + existing=0.10 + new=0.10 → total=0.20 == max(0.20) → 승인 (경계값)
    """
    snap = _make_pyramid_snapshot(position_size_pct=20.0, existing_total_size_pct=0.10)
    d = _make_decision("add_position", size_pct=0.10)
    result = await guardrail.check(d, snap)
    assert result.approved is True

