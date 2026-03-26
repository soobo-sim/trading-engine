"""
WAKE_UP_REVIEW_AUTO — 서버 측 adversarial 테스트.

사만사 시나리오 A1~A7, B1~B4 커버.
DB 쿼리는 detect_and_notify_losses 전체를 모킹하지 않고,
감지 로직과 webhook 로직을 분리 테스트.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from adapters.database.models import create_trend_position_model

# 실제 SQLAlchemy 모델 사용
BfTrendPosition = create_trend_position_model("bf")


# --- Helpers ---

def _make_position(
    id: int = 1,
    pair: str = "BTC_JPY",
    status: str = "closed",
    realized_pnl_jpy=Decimal("-17.00"),
    loss_webhook_sent: bool = False,
    closed_at=None,
    entry_price=Decimal("11200000"),
    exit_price=Decimal("11197339"),
    exit_reason="exit_warning",
):
    pos = MagicMock()
    pos.id = id
    pos.pair = pair
    pos.status = status
    pos.realized_pnl_jpy = realized_pnl_jpy
    pos.loss_webhook_sent = loss_webhook_sent
    pos.closed_at = closed_at or datetime.now(timezone.utc) - timedelta(hours=1)
    pos.entry_price = entry_price
    pos.exit_price = exit_price
    pos.exit_reason = exit_reason
    return pos


async def _call_detect(positions, send_return=True):
    """Helper: DB 쿼리 결과를 모킹해서 detect_and_notify_losses 호출."""
    from core.task.loss_detector import detect_and_notify_losses

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = positions
    db.execute = AsyncMock(return_value=result_mock)

    with patch("core.task.loss_detector._send_loss_webhook", return_value=send_return) as mock_send:
        count = await detect_and_notify_losses(db, BfTrendPosition)
    return count, mock_send


# =============================================================================
# A: 서버 감지 로직
# =============================================================================

# A1: 정상 손실 포지션 → webhook 전송
@pytest.mark.asyncio
async def test_a1_normal_loss_sends_webhook():
    pos = _make_position(realized_pnl_jpy=Decimal("-500"))
    count, mock_send = await _call_detect([pos])
    assert count == 1
    assert pos.loss_webhook_sent is True
    mock_send.assert_called_once()


# A2: realized_pnl = NULL → 스킵 + 경고
@pytest.mark.asyncio
async def test_a2_null_pnl_skipped():
    pos = _make_position(realized_pnl_jpy=None)
    count, mock_send = await _call_detect([pos])
    assert count == 0
    mock_send.assert_not_called()
    assert pos.loss_webhook_sent is False


# A3: realized_pnl = 0 (무승부) → 스킵, 플래그 true
@pytest.mark.asyncio
async def test_a3_zero_pnl_skipped_flag_true():
    pos = _make_position(realized_pnl_jpy=Decimal("0.00"))
    count, mock_send = await _call_detect([pos])
    assert count == 0
    mock_send.assert_not_called()
    assert pos.loss_webhook_sent is True


# A4: 이익 포지션 → 스킵, 플래그 true
@pytest.mark.asyncio
async def test_a4_profit_skipped_flag_true():
    pos = _make_position(realized_pnl_jpy=Decimal("1200.50"))
    count, mock_send = await _call_detect([pos])
    assert count == 0
    assert pos.loss_webhook_sent is True


# A5: 빈 결과 (이미 sent=true인 건 쿼리 제외)
@pytest.mark.asyncio
async def test_a5_already_sent_not_returned():
    count, mock_send = await _call_detect([])
    assert count == 0
    mock_send.assert_not_called()


# A6: 복수 손실 포지션
@pytest.mark.asyncio
async def test_a6_multiple_losses():
    pos1 = _make_position(id=1, realized_pnl_jpy=Decimal("-100"))
    pos2 = _make_position(id=2, realized_pnl_jpy=Decimal("-200"))
    count, _ = await _call_detect([pos1, pos2])
    assert count == 2
    assert pos1.loss_webhook_sent is True
    assert pos2.loss_webhook_sent is True


# A7: 혼합 — 손실+이익+NULL
@pytest.mark.asyncio
async def test_a7_mixed_positions():
    loss = _make_position(id=1, realized_pnl_jpy=Decimal("-50"))
    profit = _make_position(id=2, realized_pnl_jpy=Decimal("300"))
    null_pnl = _make_position(id=3, realized_pnl_jpy=None)
    count, _ = await _call_detect([loss, profit, null_pnl])
    assert count == 1
    assert loss.loss_webhook_sent is True
    assert profit.loss_webhook_sent is True
    assert null_pnl.loss_webhook_sent is False


# =============================================================================
# B: webhook 전송
# =============================================================================

# B1: webhook 전송 실패 → 플래그 false 유지
@pytest.mark.asyncio
async def test_b1_webhook_failure_keeps_flag_false():
    pos = _make_position(realized_pnl_jpy=Decimal("-100"))
    count, _ = await _call_detect([pos], send_return=False)
    assert count == 0
    assert pos.loss_webhook_sent is False


# B2: RACHEL_WEBHOOK_TOKEN 미설정 → 전송 스킵
@pytest.mark.asyncio
async def test_b2_no_token_skips():
    from core.task.loss_detector import _send_loss_webhook
    pos = _make_position()
    with patch("core.task.loss_detector.RACHEL_WEBHOOK_TOKEN", ""):
        result = await _send_loss_webhook(pos)
    assert result is False


# B3: webhook HTTP 500 → False
@pytest.mark.asyncio
async def test_b3_http_error_returns_false():
    from core.task.loss_detector import _send_loss_webhook
    pos = _make_position()
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("core.task.loss_detector.RACHEL_WEBHOOK_TOKEN", "test-token"):
        result = await _send_loss_webhook(pos, client=mock_client)
    assert result is False


# B4: webhook 네트워크 예외 → False
@pytest.mark.asyncio
async def test_b4_network_exception_returns_false():
    from core.task.loss_detector import _send_loss_webhook
    pos = _make_position()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    with patch("core.task.loss_detector.RACHEL_WEBHOOK_TOKEN", "test-token"):
        result = await _send_loss_webhook(pos, client=mock_client)
    assert result is False


# B4s: webhook 성공 시 payload 검증
@pytest.mark.asyncio
async def test_b4s_webhook_payload_format():
    from core.task.loss_detector import _send_loss_webhook
    pos = _make_position(
        id=5, pair="BTC_JPY",
        realized_pnl_jpy=Decimal("-17.00"),
        exit_reason="exit_warning",
    )
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("core.task.loss_detector.RACHEL_WEBHOOK_TOKEN", "test-token"):
        result = await _send_loss_webhook(pos, client=mock_client)

    assert result is True
    call_args = mock_client.post.call_args
    payload = call_args.kwargs.get("json") or call_args[1].get("json")
    assert payload["name"] == "PositionLoss"
    assert payload["metadata"]["type"] == "position_closed_loss"
    assert payload["metadata"]["position_id"] == 5
    assert payload["metadata"]["realized_pnl"] == -17.0
    assert payload["metadata"]["review_at"] is not None
