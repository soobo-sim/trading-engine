"""
BUG-031: approve 후 파이프라인 테스트
① 최신 ticker 재취득, ② 시그널 재평가, ③ TTL 30초
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _make_manager():
    """최소한의 GmoCoinTrendManager mock 구성."""
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager

    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.is_margin_trading = True

    collateral_mock = MagicMock()
    collateral_mock.collateral = 100_000.0
    collateral_mock.require_collateral = 10_000.0
    collateral_mock.keep_rate = 0.0  # BUG-030: keep_rate=0 → _pre_entry_checks 통과해야
    adapter.get_collateral = AsyncMock(return_value=collateral_mock)

    ticker_mock = MagicMock()
    ticker_mock.ask = 5_000_000.0
    ticker_mock.bid = 4_990_000.0
    adapter.get_ticker = AsyncMock(return_value=ticker_mock)

    order_mock = MagicMock()
    order_mock.order_id = "TEST-ORDER-001"
    order_mock.price = 5_000_000.0
    order_mock.amount = 0.001
    adapter.place_order = AsyncMock(return_value=order_mock)

    supervisor = MagicMock()
    supervisor.register = MagicMock()

    session_factory = MagicMock()

    candle_model = MagicMock()

    mgr = GmoCoinTrendManager.__new__(GmoCoinTrendManager)
    mgr._adapter = adapter
    mgr._supervisor = supervisor
    mgr._session_factory = session_factory
    mgr._candle_model = candle_model
    mgr._pair_column = "product_code"
    mgr._position = {}
    mgr._params = {"btc_jpy": {"basis_timeframe": "4h", "position_size_pct": 10.0, "max_leverage": 2.0}}
    mgr._log_prefix = "[GmocMgr]"
    mgr._last_keep_rate = {}
    return mgr, adapter


# ──────────────────────────────────────────────────────────────
# T-01: TTL 내 — 정상 진입
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t01_ttl_within_executes():
    """승인 후 10초 경과 → TTL(30s) 이내 → 진입 실행."""
    mgr, adapter = _make_manager()

    approved_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    signal_data = {"approved_at": approved_at}

    fresh_signal = {"signal": "entry_ok", "current_price": 5_000_000.0, "atr": None,
                    "ema_slope_pct": 0.5, "rsi": 50.0}
    with patch.object(mgr, "_compute_signal", AsyncMock(return_value=fresh_signal)), \
         patch.object(mgr, "_record_open", AsyncMock()):
        await mgr._open_position("btc_jpy", "buy", 5_000_000.0, None,
                                  mgr._params["btc_jpy"], signal_data=signal_data)

    adapter.place_order.assert_called_once()


# ──────────────────────────────────────────────────────────────
# T-02: TTL 초과 — 진입 차단
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t02_ttl_expired_blocks():
    """승인 후 35초 경과 → TTL(30s) 초과 → 진입 차단."""
    mgr, adapter = _make_manager()

    approved_at = (datetime.now(timezone.utc) - timedelta(seconds=35)).isoformat()
    signal_data = {"approved_at": approved_at}

    await mgr._open_position("btc_jpy", "buy", 5_000_000.0, None,
                               mgr._params["btc_jpy"], signal_data=signal_data)

    adapter.place_order.assert_not_called()


# ──────────────────────────────────────────────────────────────
# T-03: 시그널 소멸 — 진입 차단
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t03_signal_expired_blocks():
    """approve 후 재평가 시그널=no_signal → 진입 차단."""
    mgr, adapter = _make_manager()

    approved_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    signal_data = {"approved_at": approved_at}

    fresh_signal = {"signal": "no_signal", "current_price": 5_000_000.0, "atr": None,
                    "ema_slope_pct": -0.1, "rsi": 30.0}
    with patch.object(mgr, "_compute_signal", AsyncMock(return_value=fresh_signal)):
        await mgr._open_position("btc_jpy", "buy", 5_000_000.0, None,
                                   mgr._params["btc_jpy"], signal_data=signal_data)

    adapter.place_order.assert_not_called()


# ──────────────────────────────────────────────────────────────
# T-04: 시그널 재평가 실패 — 기존 시그널 유지(진입)
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t04_signal_reeval_failure_blocks():
    """재평가 예외 → fail-safe → 진입 차단."""
    mgr, adapter = _make_manager()

    approved_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    signal_data = {"approved_at": approved_at}

    with patch.object(mgr, "_compute_signal", AsyncMock(side_effect=RuntimeError("DB down"))):
        await mgr._open_position("btc_jpy", "buy", 5_000_000.0, None,
                                   mgr._params["btc_jpy"], signal_data=signal_data)

    # 재평가 실패 → fail-safe → 주문 차단
    adapter.place_order.assert_not_called()


# ──────────────────────────────────────────────────────────────
# T-05: approved_at 없음(게이트 없는 경우) — 재평가 스킵
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t05_no_approved_at_skips_reeval():
    """approved_at 없음 → TTL/재평가 모두 스킵 → 정상 진입."""
    mgr, adapter = _make_manager()

    compute_called = []
    async def fake_compute(*a, **kw):
        compute_called.append(True)
        return {"signal": "entry_ok"}

    with patch.object(mgr, "_compute_signal", fake_compute), \
         patch.object(mgr, "_record_open", AsyncMock()):
        await mgr._open_position("btc_jpy", "buy", 5_000_000.0, None,
                                   mgr._params["btc_jpy"], signal_data=None)

    assert not compute_called, "_compute_signal should not be called without approved_at"
    adapter.place_order.assert_called_once()


# ──────────────────────────────────────────────────────────────
# T-06: 슬리피지 초과 — 진입 차단 (최신 ticker 기준)
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t06_slippage_exceeded_with_fresh_ticker():
    """approved_at 없음 + 스프레드 초과(cap 0.3%) → 차단.
    ask=bid*1.006 → spread≈0.6% > 0.3%."""
    mgr, adapter = _make_manager()

    base = 5_000_000.0
    # spread = (ask - bid) / bid * 100 = 0.6% > 0.3%
    ticker_mock = MagicMock()
    ticker_mock.ask = base * 1.006
    ticker_mock.bid = base
    adapter.get_ticker = AsyncMock(return_value=ticker_mock)

    params = {**mgr._params["btc_jpy"], "max_slippage_pct": 0.3}
    # approved_at 없음 → 재평가 스킵, 스프레드 체크만
    await mgr._open_position("btc_jpy", "buy", base, None,
                               params, signal_data=None)

    adapter.place_order.assert_not_called()


# ──────────────────────────────────────────────────────────────
# T-07: TelegramApprovalGate — 승인 시 approved_at 기록
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t07_approval_gate_records_approved_at():
    """TelegramApprovalGate 승인 → decision.meta['approved_at'] 기록."""
    from core.execution.approval import TelegramApprovalGate
    from core.data.dto import Decision
    import dataclasses

    gate = TelegramApprovalGate.__new__(TelegramApprovalGate)
    gate._bot_token = "test"
    gate._chat_id = "12345"
    gate._timeout_sec = 10
    gate._poll_interval = 2.0
    gate._last_update_id = 0

    decision = Decision(
        action="entry_long", pair="btc_jpy", exchange="gmo_coin",
        confidence=0.8, size_pct=0.1, stop_loss=None, take_profit=None,
        reasoning="test", risk_factors=(), source="rule_based_v1",
        trigger="regular_4h", raw_signal="entry_ok", meta={},
    )

    with patch.object(gate, "_send_message", AsyncMock(return_value=1)), \
         patch.object(gate, "_poll_for_response", AsyncMock(return_value="approve")), \
         patch.object(gate, "_edit_message", AsyncMock()):
        result = await gate.request_approval(decision)

    assert result is True
    assert "approved_at" in decision.meta
    # UTC ISO 8601 형식인지 확인
    ts = datetime.fromisoformat(decision.meta["approved_at"])
    assert ts.tzinfo is not None
