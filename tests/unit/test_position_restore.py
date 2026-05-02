"""
포지션 복원 재시도 테스트 — BUG-039 Option B

  PR-01: pos=None + DB open 레코드 있음 + _detect_existing_position 성공 → 포지션 복원 + 텔레그램
  PR-02: pos=None + DB open 레코드 없음 → _detect_existing_position 미호출
  PR-03: pos=None + DB open 레코드 있음 + _detect_existing_position 실패(None) → WARNING 로그만
  PR-04: pos=None + DB open 레코드 있음 + _detect_existing_position 예외 → WARNING 로그만
  PR-05: pos가 있으면 _try_restore_position 미호출 (기존 동기화 로직만 실행)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.exchange.types import Position


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _make_async_session_factory(open_record=None):
    """DB에서 open 레코드를 반환하는 async_sessionmaker 모킹."""
    scalars_mock = MagicMock()
    scalars_mock.first.return_value = open_record

    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_mock

    db_session = AsyncMock()
    db_session.execute = AsyncMock(return_value=execute_result)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=db_session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    session_factory = MagicMock(return_value=session_cm)
    return session_factory


def _patch_select():
    """sqlalchemy.select 호출이 MagicMock을 인자로 받아도 예외 없이 동작하도록 패치."""
    mock_query = MagicMock()
    mock_query.where.return_value = mock_query
    mock_query.limit.return_value = mock_query

    return patch(
        "core.punisher.strategy.plugins.gmo_coin_trend.base.select",
        return_value=mock_query,
    )


def _make_margin_mgr(pair: str = "btc_jpy", open_db_record=None):
    """GmoCoinTrendManager 최소 인스턴스 (DB 모킹 포함).

    MarginTrendManager의 _try_restore_position / _sync_position_state를 상속하므로
    동일하게 테스트 가능. circular import 회피를 위해 gmo_coin_trend 경로로 import.
    """
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager

    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.is_margin_trading = True
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    session_factory = _make_async_session_factory(open_db_record)

    mgr = GmoCoinTrendManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )
    mgr._params[pair] = {}
    return mgr


def _make_open_db_record():
    """open 상태 DB 레코드 모킹."""
    rec = MagicMock()
    rec.id = 99
    rec.status = "open"
    rec.stop_loss_price = 10_500_000.0
    rec.entry_price = 11_000_000.0
    return rec


def _make_detected_pos(pair: str = "btc_jpy") -> Position:
    return Position(
        pair=pair,
        entry_price=11_000_000.0,
        entry_amount=0.004,
        stop_loss_price=None,
        extra={"side": "buy"},
    )


# ──────────────────────────────────────────────────────────────
# PR-01: DB open + 감지 성공 → 복원 + 텔레그램
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pr01_restore_success_on_db_open_record():
    """PR-01: pos=None + DB open + 감지 성공 → 인메모리 복원 + 텔레그램 발송."""
    rec = _make_open_db_record()
    mgr = _make_margin_mgr(open_db_record=rec)
    detected_pos = _make_detected_pos()

    mgr._detect_existing_position = AsyncMock(return_value=detected_pos)
    mgr._recover_db_position_id = AsyncMock(return_value=rec.id)

    with _patch_select():
        with patch(
            "core.punisher.strategy.plugins.gmo_coin_trend.base.asyncio.ensure_future",
        ) as mock_future:
            mock_future.return_value = None
            with patch(
                "core.shared.logging.telegram_handlers._send_telegram",
                new_callable=AsyncMock,
            ):
                await mgr._sync_position_state("btc_jpy")

    assert mgr._position.get("btc_jpy") is detected_pos, "포지션이 복원돼야 함"
    mgr._detect_existing_position.assert_called_once_with("btc_jpy")
    mgr._recover_db_position_id.assert_called_once_with("btc_jpy")
    assert mock_future.called, "복원 성공 시 텔레그램 ensure_future가 호출돼야 함"


# ──────────────────────────────────────────────────────────────
# PR-02: DB open 없음 → 재시도 안 함
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pr02_no_retry_when_no_db_open_record():
    """PR-02: pos=None + DB open 없음 → _detect_existing_position 미호출."""
    mgr = _make_margin_mgr(open_db_record=None)  # DB에 레코드 없음
    mgr._detect_existing_position = AsyncMock(return_value=None)

    with _patch_select():
        await mgr._sync_position_state("btc_jpy")

    mgr._detect_existing_position.assert_not_called()
    assert mgr._position.get("btc_jpy") is None


# ──────────────────────────────────────────────────────────────
# PR-03: DB open + 감지 실패(None) → WARNING 로그, 복원 없음
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pr03_no_restore_when_detect_returns_none():
    """PR-03: pos=None + DB open + _detect_existing_position → None → WARNING만, 포지션 없음 유지."""
    rec = _make_open_db_record()
    mgr = _make_margin_mgr(open_db_record=rec)

    mgr._detect_existing_position = AsyncMock(return_value=None)  # 거래소 API 실패

    with _patch_select():
        await mgr._sync_position_state("btc_jpy")

    assert mgr._position.get("btc_jpy") is None, "복원 실패 시 None 유지"
    mgr._detect_existing_position.assert_called_once_with("btc_jpy")


# ──────────────────────────────────────────────────────────────
# PR-04: DB open + 감지 예외 → WARNING 로그, 복원 없음
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pr04_no_restore_when_detect_raises():
    """PR-04: _detect_existing_position 예외 → WARNING만, 포지션 없음 유지."""
    rec = _make_open_db_record()
    mgr = _make_margin_mgr(open_db_record=rec)

    mgr._detect_existing_position = AsyncMock(side_effect=RuntimeError("API timeout"))

    with _patch_select():
        await mgr._sync_position_state("btc_jpy")

    assert mgr._position.get("btc_jpy") is None, "예외 발생 시 None 유지"


# ──────────────────────────────────────────────────────────────
# PR-05: 포지션 있으면 기존 동기화 로직 실행 (restore 미호출)
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pr05_no_restore_when_position_exists():
    """PR-05: 인메모리 포지션이 있으면 _try_restore_position 미호출 → 기존 동기화 실행."""
    rec = _make_open_db_record()
    mgr = _make_margin_mgr(open_db_record=rec)

    existing_pos = _make_detected_pos()
    mgr._position["btc_jpy"] = existing_pos

    mgr._try_restore_position = AsyncMock()

    # 어댑터 get_positions: 동일 수량 반환 → 드리프트 없음
    fx_pos = MagicMock()
    fx_pos.size = existing_pos.entry_amount
    mgr._adapter.get_positions = AsyncMock(return_value=[fx_pos])

    await mgr._sync_position_state("btc_jpy")

    mgr._try_restore_position.assert_not_called()
    # 포지션은 그대로
    assert mgr._position.get("btc_jpy") is existing_pos
