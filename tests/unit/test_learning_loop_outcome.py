"""
학습 루프 Stage 1 — Outcome Backfill 단위 테스트.

검증 항목:
  1. _update_judgment_outcome: 수익 거래 → outcome="win"
  2. _update_judgment_outcome: 손실 거래 → outcome="loss"
  3. _update_judgment_outcome: DB 실패 → WARNING 로그만
  4. _update_judgment_outcome: session_factory=None → 스킵
  5. _handle_execution_result entry_long: judgment_id가 Position.extra에 저장
  6. _handle_execution_result entry_long: judgment_id=None이면 Position.extra에 저장 안 함
  7. Short PnL 부호 검증 (entry_short, entry_price > exit_price → win)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import AiJudgment
from adapters.database.session import Base
from core.data.dto import Decision, ExecutionResult, SignalSnapshot
from core.exchange.types import Position


# ── 픽스처 ────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_and_factory():
    """SQLite in-memory + ai_judgments 테이블 + 행 1건 사전 삽입."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    ai_table = Base.metadata.tables["ai_judgments"]
    async with engine.begin() as conn:
        await conn.run_sync(ai_table.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # 테스트용 행 삽입
    async with factory() as session:
        row = AiJudgment(
            trigger_type="regular_4h",
            timestamp=datetime(2026, 4, 11, 4, 0, 0, tzinfo=timezone.utc),
            pair="USD_JPY",
            exchange="gmo_fx",
            final_action="entry_long",
            final_confidence=0.70,
            source="rule_based_v1",
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        inserted_id = row.id

    yield factory, inserted_id
    await engine.dispose()


def _make_minimal_manager(session_factory=None):
    """테스트용 최소 BaseTrendManager 인스턴스 생성.

    추상 메서드를 MagicMock으로 채운 서브클래스를 동적 생성한다.
    """
    from core.strategy.base_trend import BaseTrendManager
    from core.punisher.task.supervisor import TaskSupervisor

    class _TestMgr(BaseTrendManager):
        _task_prefix = "test"
        _log_prefix = "[TestMgr]"
        _supports_short = True  # 학습 루프 테스트: 숏 차단 없이 판단 연결 검증

        async def _detect_existing_position(self, pair):
            return None

        async def _sync_position_state(self, pair):
            pass

        async def _open_position(self, pair, side, price, atr, params, *, signal_data=None):
            pass

        async def _close_position_impl(self, pair, reason):
            self._position[pair] = None

        async def _apply_stop_tightening(self, pair, current_price, atr, params):
            pass

        async def _record_open(self, **kwargs):
            return None

        async def _record_close(self, **kwargs):
            pass

        async def _on_candle_extra_checks(self, pair, params):
            return True

        def _get_entry_side(self, signal):
            return "long"

    adapter = MagicMock()
    adapter.exchange_name = "gmo_fx"
    supervisor = MagicMock(spec=TaskSupervisor)
    candle_model = MagicMock()
    position_model = MagicMock()

    mgr = _TestMgr(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=candle_model,
        position_model=position_model,
    )
    return mgr


def _make_decision(action="entry_long", confidence=0.70) -> Decision:
    return Decision(
        action=action,
        pair="USD_JPY",
        exchange="gmo_fx",
        confidence=confidence,
        size_pct=0.40,
        stop_loss=149.0,
        take_profit=152.0,
        reasoning="테스트",
        risk_factors=(),
        source="rule_based_v1",
        trigger="regular_4h",
        raw_signal="long_setup",
    )


def _make_execution_result(action="entry_long", judgment_id=42, confidence=0.70) -> ExecutionResult:
    return ExecutionResult(
        action=action,
        executed=False,
        decision=_make_decision(action=action, confidence=confidence),
        judgment_id=judgment_id,
    )


def _make_snapshot() -> SignalSnapshot:
    return SignalSnapshot(
        pair="USD_JPY",
        exchange="gmo_fx",
        timestamp=datetime(2026, 4, 11, 4, 0, 0, tzinfo=timezone.utc),
        signal="long_setup",
        current_price=150.50,
        exit_signal={"action": "hold"},
    )


def _make_signal_data() -> dict:
    return {
        "signal": "long_setup",
        "current_price": 150.50,
        "exit_signal": {"action": "hold"},
    }


# ── 테스트: _update_judgment_outcome ─────────────────────────


@pytest.mark.asyncio
async def test_outcome_win_updates_db(db_and_factory):
    """
    Given: 수익 거래 (pnl > 0)
    When:  _update_judgment_outcome() 호출
    Then:  outcome='win', realized_pnl > 0, hold_duration_hours > 0 저장
    """
    db_factory, judgment_id = db_and_factory
    mgr = _make_minimal_manager(session_factory=db_factory)

    entry_time = datetime(2026, 4, 11, 0, 0, 0, tzinfo=timezone.utc)
    await mgr._update_judgment_outcome(
        "USD_JPY", judgment_id, realized_pnl=500.0, realized_pnl_pct=0.5,
        entry_time=entry_time, confidence=0.70,
    )

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment).where(AiJudgment.id == judgment_id))).scalar_one()

    assert row.outcome == "win"
    assert row.realized_pnl == pytest.approx(500.0)
    assert row.hold_duration_hours is not None
    assert row.hold_duration_hours > 0  # 어느 시간대든 양수
    assert row.confidence_error == pytest.approx(abs(0.70 - 1.0))
    assert row.updated_at is not None


@pytest.mark.asyncio
async def test_outcome_loss_updates_db(db_and_factory):
    """
    Given: 손실 거래 (pnl < 0)
    When:  _update_judgment_outcome() 호출
    Then:  outcome='loss', confidence_error = |confidence - 0.0|
    """
    db_factory, judgment_id = db_and_factory
    mgr = _make_minimal_manager(session_factory=db_factory)

    entry_time = datetime(2026, 4, 11, 2, 0, 0, tzinfo=timezone.utc)
    await mgr._update_judgment_outcome(
        "USD_JPY", judgment_id, realized_pnl=-300.0, realized_pnl_pct=-0.3,
        entry_time=entry_time, confidence=0.70,
    )

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment).where(AiJudgment.id == judgment_id))).scalar_one()

    assert row.outcome == "loss"
    assert row.realized_pnl == pytest.approx(-300.0)
    # 손실 outcome 기대값=0.0 → confidence_error = |0.70 - 0.0| = 0.70
    assert row.confidence_error == pytest.approx(0.70)


@pytest.mark.asyncio
async def test_outcome_zero_pnl_is_win(db_and_factory):
    """
    Given: PnL = 0 (본전)
    When:  _update_judgment_outcome() 호출
    Then:  outcome='win' (pnl >= 0 기준)
    """
    db_factory, judgment_id = db_and_factory
    mgr = _make_minimal_manager(session_factory=db_factory)

    entry_time = datetime(2026, 4, 11, 3, 0, 0, tzinfo=timezone.utc)
    await mgr._update_judgment_outcome(
        "USD_JPY", judgment_id, realized_pnl=0.0, realized_pnl_pct=0.0,
        entry_time=entry_time, confidence=0.50,
    )

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment).where(AiJudgment.id == judgment_id))).scalar_one()

    assert row.outcome == "win"


@pytest.mark.asyncio
async def test_outcome_db_failure_logs_warning(db_and_factory, caplog):
    """
    Given: DB UPDATE 실패 (세션 오류)
    When:  _update_judgment_outcome() 호출
    Then:  WARNING 로그만, 예외 전파 없음 (거래 흐름 보호)
    """
    db_factory, judgment_id = db_and_factory

    class _FailSession:
        async def __aenter__(self):
            raise RuntimeError("DB unavailable")
        async def __aexit__(self, *_):
            pass

    mgr = _make_minimal_manager(session_factory=lambda: _FailSession())

    entry_time = datetime(2026, 4, 11, 3, 0, 0, tzinfo=timezone.utc)
    with caplog.at_level(logging.WARNING, logger="core.strategy.base_trend"):
        await mgr._update_judgment_outcome(
            "USD_JPY", judgment_id, realized_pnl=100.0, realized_pnl_pct=0.1,
            entry_time=entry_time, confidence=0.60,
        )

    assert any("outcome 업데이트 실패" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_outcome_no_session_factory_skips(db_and_factory):
    """
    Given: session_factory=None
    When:  _update_judgment_outcome() 호출
    Then:  아무것도 하지 않음 (예외 없음)
    """
    _, judgment_id = db_and_factory
    mgr = _make_minimal_manager(session_factory=None)

    entry_time = datetime(2026, 4, 11, 3, 0, 0, tzinfo=timezone.utc)
    # 예외 없이 정상 완료 여부만 검증
    await mgr._update_judgment_outcome(
        "USD_JPY", judgment_id, realized_pnl=100.0, realized_pnl_pct=0.1,
        entry_time=entry_time, confidence=0.60,
    )


# ── 테스트: _handle_execution_result → Position.extra ─────────


@pytest.mark.asyncio
async def test_entry_long_saves_judgment_id_in_position(db_and_factory):
    """
    Given: entry_long 결과 + judgment_id=42
    When:  _handle_execution_result() 호출 (_open_position이 Position 생성)
    Then:  Position.extra["judgment_id"] == 42
    """
    db_factory, _ = db_and_factory
    mgr = _make_minimal_manager(session_factory=db_factory)

    pair = "USD_JPY"
    mgr._params[pair] = {}
    mgr._latest_price[pair] = 150.50

    # _open_position 호출 시 Position을 생성해 _position에 저장하는 모킹
    async def _mock_open_position(p, side, price, atr, params, *, signal_data=None):
        mgr._position[p] = Position(
            pair=p, entry_price=price, entry_amount=1000.0
        )

    mgr._open_position = _mock_open_position  # type: ignore[assignment]

    result = _make_execution_result(action="entry_long", judgment_id=42, confidence=0.70)
    await mgr._handle_execution_result(
        pair, result, _make_snapshot(), _make_signal_data(), {}
    )

    pos = mgr._position.get(pair)
    assert pos is not None
    assert pos.extra.get("judgment_id") == 42
    assert "entry_time" in pos.extra
    assert pos.extra.get("confidence") == pytest.approx(0.70)
    assert pos.extra.get("side") == "long"


@pytest.mark.asyncio
async def test_entry_long_no_judgment_id_skips_extra(db_and_factory):
    """
    Given: entry_long 결과 + judgment_id=None (v1 모드 or DB 실패)
    When:  _handle_execution_result() 호출
    Then:  Position.extra에 judgment_id 저장 안 함 (기존 동작 100% 유지)
    """
    db_factory, _ = db_and_factory
    mgr = _make_minimal_manager(session_factory=db_factory)

    pair = "USD_JPY"
    mgr._params[pair] = {}
    mgr._latest_price[pair] = 150.50

    async def _mock_open_position(p, side, price, atr, params, *, signal_data=None):
        mgr._position[p] = Position(pair=p, entry_price=price, entry_amount=1000.0)

    mgr._open_position = _mock_open_position  # type: ignore[assignment]

    result = _make_execution_result(action="entry_long", judgment_id=None)
    await mgr._handle_execution_result(
        pair, result, _make_snapshot(), _make_signal_data(), {}
    )

    pos = mgr._position.get(pair)
    assert pos is not None
    assert "judgment_id" not in pos.extra


@pytest.mark.asyncio
async def test_entry_short_saves_side_short(db_and_factory):
    """
    Given: entry_short 결과 + judgment_id=99
    When:  _handle_execution_result() 호출
    Then:  Position.extra["side"] == "short"
    """
    db_factory, _ = db_and_factory
    mgr = _make_minimal_manager(session_factory=db_factory)

    pair = "USD_JPY"
    mgr._params[pair] = {}
    mgr._latest_price[pair] = 150.50

    # base._on_entry_signal은 long_setup만 처리 → entry_short는 서브클래스 override 담당
    # 여기서는 _on_entry_signal을 직접 모킹해 Position을 생성
    async def _mock_on_entry(p, signal, price, atr, params, signal_data):
        mgr._position[p] = Position(pair=p, entry_price=price, entry_amount=1000.0)

    mgr._on_entry_signal = _mock_on_entry  # type: ignore[method-assign]

    result = _make_execution_result(action="entry_short", judgment_id=99, confidence=0.65)
    snap = SignalSnapshot(
        pair=pair, exchange="gmo_fx",
        timestamp=datetime(2026, 4, 11, 4, 0, 0, tzinfo=timezone.utc),
        signal="short_setup", current_price=150.50,
        exit_signal={"action": "hold"},
    )
    await mgr._handle_execution_result(
        pair, result, snap, {"signal": "short_setup", "current_price": 150.50, "exit_signal": {"action": "hold"}}, {}
    )

    pos = mgr._position.get(pair)
    assert pos is not None
    assert pos.extra.get("side") == "short"
    assert pos.extra.get("judgment_id") == 99


# ── 엣지 케이스 보강 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_position_no_judgment_id_skips_backfill(db_and_factory):
    """
    Given: Position.extra에 judgment_id 없음 (v1 rule-based 포지션)
    When:  _close_position() 호출
    Then:  ai_judgments UPDATE 없음 — 기존 v1 동작 100% 유지
    """
    db_factory, judgment_id = db_and_factory
    mgr = _make_minimal_manager(session_factory=db_factory)

    pair = "USD_JPY"
    mgr._params[pair] = {}
    mgr._latest_price[pair] = 151.0
    # judgment_id 없는 v1 포지션
    mgr._position[pair] = Position(pair=pair, entry_price=150.0, entry_amount=1000.0)

    await mgr._close_position(pair, "stop_loss")

    # outcome이 NULL인 채로 유지됨
    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment).where(AiJudgment.id == judgment_id))).scalar_one()

    assert row.outcome is None  # backfill 발생하지 않음


@pytest.mark.asyncio
async def test_close_position_no_position_skips_backfill(db_and_factory):
    """
    Given: 포지션 없는 상태에서 _close_position() 호출 (no-op)
    When:  _close_position() 호출
    Then:  예외 없이 종료, backfill 발생하지 않음
    """
    db_factory, judgment_id = db_and_factory
    mgr = _make_minimal_manager(session_factory=db_factory)

    pair = "USD_JPY"
    mgr._params[pair] = {}
    mgr._position[pair] = None  # 포지션 없음

    await mgr._close_position(pair, "stop_loss")  # 예외 없어야 함

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment).where(AiJudgment.id == judgment_id))).scalar_one()

    assert row.outcome is None


@pytest.mark.asyncio
async def test_close_position_invalid_entry_time_uses_now(db_and_factory):
    """
    Given: Position.extra["entry_time"]이 파싱 불가능한 값
    When:  _close_position() 호출
    Then:  fromisoformat 실패 → now() fallback, backfill 정상 완료
    """
    db_factory, judgment_id = db_and_factory
    mgr = _make_minimal_manager(session_factory=db_factory)

    pair = "USD_JPY"
    mgr._params[pair] = {}
    mgr._latest_price[pair] = 151.0
    pos = Position(pair=pair, entry_price=150.0, entry_amount=1000.0)
    pos.extra["judgment_id"] = judgment_id
    pos.extra["entry_time"] = "INVALID-FORMAT"  # 파싱 실패 케이스
    pos.extra["confidence"] = 0.70
    pos.extra["side"] = "long"
    mgr._position[pair] = pos

    await mgr._close_position(pair, "trailing_stop")

    # create_task이므로 완료 대기 필요
    import asyncio
    await asyncio.sleep(0.05)

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment).where(AiJudgment.id == judgment_id))).scalar_one()

    assert row.outcome == "win"  # exit 150→151, pnl 양수
    assert row.hold_duration_hours is not None
    assert row.hold_duration_hours >= 0


@pytest.mark.asyncio
async def test_close_position_short_win_backfill(db_and_factory):
    """
    Given: Short 포지션, entry_price > exit_price
    When:  _close_position() 호출
    Then:  outcome='win' (Short에서 가격 하락 = 수익)
    """
    db_factory, judgment_id = db_and_factory
    mgr = _make_minimal_manager(session_factory=db_factory)

    pair = "USD_JPY"
    mgr._params[pair] = {}
    mgr._latest_price[pair] = 149.0   # exit < entry → short win
    pos = Position(pair=pair, entry_price=150.0, entry_amount=1000.0)
    pos.extra["judgment_id"] = judgment_id
    pos.extra["entry_time"] = datetime(2026, 4, 11, 0, 0, 0, tzinfo=timezone.utc).isoformat()
    pos.extra["confidence"] = 0.65
    pos.extra["side"] = "short"
    mgr._position[pair] = pos

    await mgr._close_position(pair, "target")

    import asyncio
    await asyncio.sleep(0.05)

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment).where(AiJudgment.id == judgment_id))).scalar_one()

    assert row.outcome == "win"
    assert row.realized_pnl == pytest.approx((150.0 - 149.0) * 1000.0)


@pytest.mark.asyncio
async def test_close_position_long_loss_backfill(db_and_factory):
    """
    Given: Long 포지션, exit_price < entry_price
    When:  _close_position() 호출
    Then:  outcome='loss'
    """
    db_factory, judgment_id = db_and_factory
    mgr = _make_minimal_manager(session_factory=db_factory)

    pair = "USD_JPY"
    mgr._params[pair] = {}
    mgr._latest_price[pair] = 148.0   # exit < entry → long loss
    pos = Position(pair=pair, entry_price=150.0, entry_amount=1000.0)
    pos.extra["judgment_id"] = judgment_id
    pos.extra["entry_time"] = datetime(2026, 4, 11, 2, 0, 0, tzinfo=timezone.utc).isoformat()
    pos.extra["confidence"] = 0.70
    pos.extra["side"] = "long"
    mgr._position[pair] = pos

    await mgr._close_position(pair, "stop_loss")

    import asyncio
    await asyncio.sleep(0.05)

    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment).where(AiJudgment.id == judgment_id))).scalar_one()

    assert row.outcome == "loss"
    assert row.realized_pnl == pytest.approx((148.0 - 150.0) * 1000.0)


@pytest.mark.asyncio
async def test_process_decision_error_returns_no_judgment_id():
    """
    Given: IDecisionMaker.decide() 예외 발생 (판단 오류 경로)
    When:  process() 호출
    Then:  ExecutionResult.action='hold', judgment_id=None (DB 저장 없음)
    """
    from core.execution.orchestrator import ExecutionOrchestrator
    from core.data.dto import GuardrailResult

    dm = AsyncMock()
    dm.decide.side_effect = RuntimeError("LLM timeout")

    gr = AsyncMock()

    orch = ExecutionOrchestrator(decision_maker=dm, guardrail=gr)

    snap = _make_snapshot()
    result = await orch.process(snap)

    assert result.action == "hold"
    assert result.judgment_id is None


@pytest.mark.asyncio
async def test_paper_pair_skips_backfill(db_and_factory):
    """
    Given: paper pair 포지션 (paper_executors에 등록)
    When:  _close_position() 호출
    Then:  paper exit 처리만, ai_judgments backfill 없음
    """
    db_factory, judgment_id = db_and_factory
    mgr = _make_minimal_manager(session_factory=db_factory)

    pair = "USD_JPY"
    mgr._params[pair] = {}
    mgr._latest_price[pair] = 151.0

    # paper pair 설정
    paper_exec = AsyncMock()
    mgr._paper_executors[pair] = paper_exec
    mgr._paper_positions[pair] = {
        "paper_trade_id": 99,
        "entry_price": 150.0,
        "direction": "long",
    }
    # position None이어도 paper path는 paper_positions 기준
    mgr._position[pair] = None

    await mgr._close_position(pair, "target")

    paper_exec.record_paper_exit.assert_called_once()

    # ai_judgments는 변경 없음
    async with db_factory() as session:
        row = (await session.execute(select(AiJudgment).where(AiJudgment.id == judgment_id))).scalar_one()

    assert row.outcome is None


# ── 서사 로그 검증 ─────────────────────────────────────────────────────────────


class TestNarrativeLogging:
    """실행→학습 서사 로그가 올바른 레벨·내용으로 출력되는지 검증."""

    @pytest.mark.asyncio
    async def test_entry_long_judgment_linked_logs_info(self, db_and_factory, caplog):
        """
        Given: entry_long + judgment_id=42, position 생성 성공
        When:  _handle_execution_result()
        Then:  INFO '판단→실행 연결' 로그 — judgment_id + 확신도 + side 포함
        """
        import logging
        db_factory, _ = db_and_factory
        mgr = _make_minimal_manager(session_factory=db_factory)

        pair = "USD_JPY"
        mgr._params[pair] = {}
        mgr._latest_price[pair] = 150.50

        async def _mock_open(p, side, price, atr, params, *, signal_data=None):
            mgr._position[p] = Position(pair=p, entry_price=price, entry_amount=1000.0)

        mgr._open_position = _mock_open  # type: ignore[assignment]

        result = _make_execution_result(action="entry_long", judgment_id=42, confidence=0.70)
        with caplog.at_level(logging.INFO, logger="core.strategy.base_trend"):
            await mgr._handle_execution_result(
                pair, result, _make_snapshot(), _make_signal_data(), {}
            )

        info = [r for r in caplog.records if "entry_long 완료" in r.message and r.levelname == "INFO"]
        assert len(info) == 1
        assert "judgment_id=42" in info[0].message
        assert "확신도=70%" in info[0].message

    @pytest.mark.asyncio
    async def test_entry_long_no_judgment_id_no_link_log(self, db_and_factory, caplog):
        """
        Given: entry_long + judgment_id=None (v1 모드)
        When:  _handle_execution_result()
        Then:  '판단→실행 연결' INFO 로그 없음
        """
        import logging
        db_factory, _ = db_and_factory
        mgr = _make_minimal_manager(session_factory=db_factory)

        pair = "USD_JPY"
        mgr._params[pair] = {}
        mgr._latest_price[pair] = 150.50

        async def _mock_open(p, side, price, atr, params, *, signal_data=None):
            mgr._position[p] = Position(pair=p, entry_price=price, entry_amount=1000.0)

        mgr._open_position = _mock_open  # type: ignore[assignment]

        result = _make_execution_result(action="entry_long", judgment_id=None)
        with caplog.at_level(logging.INFO, logger="core.strategy.base_trend"):
            await mgr._handle_execution_result(
                pair, result, _make_snapshot(), _make_signal_data(), {}
            )

        info = [r for r in caplog.records if "entry_long 완료" in r.message]
        assert len(info) == 0

    @pytest.mark.asyncio
    async def test_entry_short_judgment_linked_logs_side_short(self, db_and_factory, caplog):
        """
        Given: entry_short + judgment_id=99
        When:  _handle_execution_result()
        Then:  INFO 로그 — side=short 포함
        """
        import logging
        db_factory, _ = db_and_factory
        mgr = _make_minimal_manager(session_factory=db_factory)

        pair = "USD_JPY"
        mgr._params[pair] = {}
        mgr._latest_price[pair] = 150.50

        async def _mock_on_entry(p, signal, price, atr, params, signal_data):
            mgr._position[p] = Position(pair=p, entry_price=price, entry_amount=1000.0)

        mgr._on_entry_signal = _mock_on_entry  # type: ignore[method-assign]

        result = _make_execution_result(action="entry_short", judgment_id=99, confidence=0.65)
        snap = SignalSnapshot(
            pair=pair, exchange="gmo_fx",
            timestamp=datetime(2026, 4, 11, 4, 0, 0, tzinfo=timezone.utc),
            signal="short_setup", current_price=150.50,
            exit_signal={"action": "hold"},
        )
        with caplog.at_level(logging.INFO, logger="core.strategy.base_trend"):
            await mgr._handle_execution_result(
                pair, result, snap,
                {"signal": "short_setup", "current_price": 150.50, "exit_signal": {"action": "hold"}},
                {}
            )

        info = [r for r in caplog.records if "entry_short 완료" in r.message and r.levelname == "INFO"]
        assert len(info) == 1
        assert "judgment_id=99" in info[0].message

    @pytest.mark.asyncio
    async def test_close_position_learning_link_logs_info(self, db_and_factory, caplog):
        """
        Given: Position에 judgment_id 있음
        When:  _close_position()
        Then:  INFO '청산→학습 연결' 로그 — judgment_id + pnl + side 포함
        """
        import logging
        import asyncio
        db_factory, judgment_id = db_and_factory
        mgr = _make_minimal_manager(session_factory=db_factory)

        pair = "USD_JPY"
        mgr._params[pair] = {}
        mgr._latest_price[pair] = 151.0

        pos = Position(pair=pair, entry_price=150.0, entry_amount=1000.0)
        pos.extra["judgment_id"] = judgment_id
        pos.extra["entry_time"] = datetime(2026, 4, 11, 0, 0, 0, tzinfo=timezone.utc).isoformat()
        pos.extra["confidence"] = 0.70
        pos.extra["side"] = "long"
        mgr._position[pair] = pos

        with caplog.at_level(logging.INFO, logger="core.strategy.base_trend"):
            await mgr._close_position(pair, "stop_loss")
            await asyncio.sleep(0)

        info = [r for r in caplog.records if "청산→학습 연결" in r.message and r.levelname == "INFO"]
        assert len(info) == 1
        assert f"judgment_id={judgment_id}" in info[0].message
        assert "side=long" in info[0].message

    @pytest.mark.asyncio
    async def test_close_position_no_judgment_no_learning_log(self, caplog):
        """
        Given: Position에 judgment_id 없음 (v1 모드)
        When:  _close_position()
        Then:  '청산→학습 연결' INFO 없음
        """
        import logging
        mgr = _make_minimal_manager(session_factory=None)

        pair = "USD_JPY"
        mgr._params[pair] = {}
        mgr._latest_price[pair] = 151.0

        pos = Position(pair=pair, entry_price=150.0, entry_amount=1000.0)
        mgr._position[pair] = pos

        with caplog.at_level(logging.INFO, logger="core.strategy.base_trend"):
            await mgr._close_position(pair, "stop_loss")

        info = [r for r in caplog.records if "청산→학습 연결" in r.message]
        assert len(info) == 0

    @pytest.mark.asyncio
    async def test_update_judgment_outcome_logs_info_not_debug(self, db_and_factory, caplog):
        """
        Given: _update_judgment_outcome 정상 실행
        When:  호출
        Then:  INFO 로그 (기존 DEBUG에서 변경) — outcome + pnl + hold 포함
        """
        import logging
        db_factory, judgment_id = db_and_factory
        mgr = _make_minimal_manager(session_factory=db_factory)

        entry_time = datetime(2026, 4, 11, 0, 0, 0, tzinfo=timezone.utc)
        with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
            await mgr._update_judgment_outcome(
                "USD_JPY", judgment_id, realized_pnl=500.0, realized_pnl_pct=0.5,
                entry_time=entry_time, confidence=0.70,
            )

        outcome_logs = [r for r in caplog.records if f"ai_judgments[{judgment_id}]" in r.message]
        assert len(outcome_logs) == 1
        assert outcome_logs[0].levelname == "INFO"
        assert "outcome=win" in outcome_logs[0].message
        assert "pnl=500.00" in outcome_logs[0].message

    @pytest.mark.asyncio
    async def test_update_judgment_outcome_no_debug_level_log(self, db_and_factory, caplog):
        """
        Given: _update_judgment_outcome 정상 실행
        When:  호출
        Then:  동일 메시지가 DEBUG 레벨로 출력되지 않음 (INFO로만)
        """
        import logging
        db_factory, judgment_id = db_and_factory
        mgr = _make_minimal_manager(session_factory=db_factory)

        entry_time = datetime(2026, 4, 11, 0, 0, 0, tzinfo=timezone.utc)
        with caplog.at_level(logging.DEBUG, logger="core.strategy.base_trend"):
            await mgr._update_judgment_outcome(
                "USD_JPY", judgment_id, realized_pnl=500.0, realized_pnl_pct=0.5,
                entry_time=entry_time, confidence=0.70,
            )

        debug_logs = [
            r for r in caplog.records
            if f"ai_judgments[{judgment_id}]" in r.message and r.levelname == "DEBUG"
        ]
        assert len(debug_logs) == 0

    @pytest.mark.asyncio
    async def test_post_analyzer_log_contains_pair(self, db_and_factory, caplog):
        """
        Given: PostAnalyzer.analyze() 정상 실행
        When:  호출
        Then:  INFO 로그 — pair 포함 (기존 judgment_id만 있던 형식에서 변경)
        """
        import logging
        from core.punisher.learning.post_analyzer import PostAnalyzer

        db_factory, judgment_id = db_and_factory

        llm_client = AsyncMock()
        llm_client.chat = AsyncMock(return_value={"analysis": "테스트 사후 분석"})

        from adapters.database.models import AiJudgment as _AiJudgment
        analyzer = PostAnalyzer(
            llm_client=llm_client,
            session_factory=db_factory,
            judgment_model=_AiJudgment,
        )

        with caplog.at_level(logging.INFO, logger="core.punisher.learning.post_analyzer"):
            await analyzer.analyze(
                judgment_id=judgment_id,
                outcome="win",
                realized_pnl=500.0,
                hold_duration_hours=4.2,
            )

        info = [r for r in caplog.records if r.levelname == "INFO" and "사후 분석 완료" in r.message]
        assert len(info) == 1
        assert "USD_JPY" in info[0].message        # pair 포함 확인
        assert f"judgment_id={judgment_id}" in info[0].message

    @pytest.mark.asyncio
    async def test_entry_long_decision_none_logs_zero_confidence(self, db_and_factory, caplog):
        """
        Given: entry_long + judgment_id=42, decision=None (비정상 케이스)
        When:  _handle_execution_result()
        Then:  INFO '판단→실행 연결' 로그 — 확신도=0% (fallback), judgment_id 포함
        """
        import logging
        from core.data.dto import ExecutionResult as _ExecResult

        db_factory, _ = db_and_factory
        mgr = _make_minimal_manager(session_factory=db_factory)

        pair = "USD_JPY"
        mgr._params[pair] = {}
        mgr._latest_price[pair] = 150.50

        async def _mock_open(p, side, price, atr, params, *, signal_data=None):
            mgr._position[p] = Position(pair=p, entry_price=price, entry_amount=1000.0)

        mgr._open_position = _mock_open  # type: ignore[assignment]

        # decision=None이지만 judgment_id는 있는 경우
        result = _ExecResult(action="entry_long", executed=False, decision=None, judgment_id=42)

        with caplog.at_level(logging.INFO, logger="core.strategy.base_trend"):
            await mgr._handle_execution_result(
                pair, result, _make_snapshot(), _make_signal_data(), {}
            )

        info = [r for r in caplog.records if "entry_long 완료" in r.message and r.levelname == "INFO"]
        assert len(info) == 1
        assert "확신도=0%" in info[0].message
        assert "judgment_id=42" in info[0].message

    @pytest.mark.asyncio
    async def test_close_position_negative_pnl_no_plus_sign(self, db_and_factory, caplog):
        """
        Given: 손실 청산 (exit_price < entry_price, long)
        When:  _close_position()
        Then:  '청산→학습 연결' 로그 — pnl 앞에 '+' 없음, 음수값 그대로 포함
        """
        import logging
        import asyncio

        db_factory, judgment_id = db_and_factory
        mgr = _make_minimal_manager(session_factory=db_factory)

        pair = "USD_JPY"
        mgr._params[pair] = {}
        mgr._latest_price[pair] = 148.0  # entry 150.0보다 낮음 → 손실

        pos = Position(pair=pair, entry_price=150.0, entry_amount=1000.0)
        pos.extra["judgment_id"] = judgment_id
        pos.extra["entry_time"] = datetime(2026, 4, 11, 0, 0, 0, tzinfo=timezone.utc).isoformat()
        pos.extra["confidence"] = 0.70
        pos.extra["side"] = "long"
        mgr._position[pair] = pos

        with caplog.at_level(logging.INFO, logger="core.strategy.base_trend"):
            await mgr._close_position(pair, "stop_loss")
            await asyncio.sleep(0)

        info = [r for r in caplog.records if "청산→학습 연결" in r.message and r.levelname == "INFO"]
        assert len(info) == 1
        # pnl = (148 - 150) * 1000 = -2000
        assert "-2000円" in info[0].message
        assert "+-2000円" not in info[0].message  # '+' 앞에 붙지 않아야 함
