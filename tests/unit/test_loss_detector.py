"""
WAKE_UP_REVIEW_AUTO — 서버 측 adversarial 테스트 (BUG-025 재작성).

DB 직접 기록 방식으로 전환됨 (webhook 의존 제거).
A1~A7: 감지 로직 (trend 포지션)
B1~B3: DB 기록 실패 / Telegram 비필수
C1~C3: 박스 포지션 감지
D1~D2: wake_up_trigger
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from adapters.database.models import create_trend_position_model, create_box_position_model

# 실제 SQLAlchemy 모델 사용
BfTrendPosition = create_trend_position_model("bf")
BfBoxPosition = create_box_position_model("bf")


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
    strategy_id=None,
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
    pos.strategy_id = strategy_id
    return pos


def _make_db(positions):
    """DB mock: execute → positions 반환, flush/commit는 no-op."""
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = positions
    db.execute = AsyncMock(return_value=result_mock)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


async def _call_detect(positions, *, record_ok=True, box_positions=None):
    """Helper: DB 기록을 모킹해서 detect_and_notify_losses 호출."""
    from core.punisher.task.loss_detector import detect_and_notify_losses

    db = _make_db(positions)

    # _record_loss 모킹
    with patch("core.punisher.task.loss_detector._record_loss", new_callable=AsyncMock, return_value=record_ok) as mock_record:
        if box_positions is not None:
            # box 포지션용 별도 DB execute mock (두 번째 호출)
            box_result = MagicMock()
            box_result.scalars.return_value.all.return_value = box_positions
            db.execute = AsyncMock(side_effect=[
                MagicMock(**{"scalars.return_value.all.return_value": positions}),
                MagicMock(**{"scalars.return_value.all.return_value": box_positions}),
            ])
            count = await detect_and_notify_losses(
                db, BfTrendPosition, box_position_model=BfBoxPosition, prefix="bf"
            )
        else:
            count = await detect_and_notify_losses(db, BfTrendPosition, prefix="bf")
    return count, mock_record


# =============================================================================
# A: 감지 로직 (trend 포지션)
# =============================================================================

# A1: 정상 손실 포지션 → DB 기록
@pytest.mark.asyncio
async def test_a1_normal_loss_records_db():
    pos = _make_position(realized_pnl_jpy=Decimal("-500"))
    count, mock_record = await _call_detect([pos])
    assert count == 1
    assert pos.loss_webhook_sent is True
    mock_record.assert_called_once()


# A2: realized_pnl = NULL → 스킵 + 경고
@pytest.mark.asyncio
async def test_a2_null_pnl_skipped():
    pos = _make_position(realized_pnl_jpy=None)
    count, mock_record = await _call_detect([pos])
    assert count == 0
    mock_record.assert_not_called()
    assert pos.loss_webhook_sent is False


# A3: realized_pnl = 0 (무승부) → 스킵, 플래그 true
@pytest.mark.asyncio
async def test_a3_zero_pnl_skipped_flag_true():
    pos = _make_position(realized_pnl_jpy=Decimal("0.00"))
    count, mock_record = await _call_detect([pos])
    assert count == 0
    mock_record.assert_not_called()
    assert pos.loss_webhook_sent is True


# A4: 이익 포지션 → 스킵, 플래그 true
@pytest.mark.asyncio
async def test_a4_profit_skipped_flag_true():
    pos = _make_position(realized_pnl_jpy=Decimal("1200.50"))
    count, mock_record = await _call_detect([pos])
    assert count == 0
    assert pos.loss_webhook_sent is True


# A5: 빈 결과 (이미 sent=true인 건 쿼리 제외)
@pytest.mark.asyncio
async def test_a5_already_sent_not_returned():
    count, mock_record = await _call_detect([])
    assert count == 0
    mock_record.assert_not_called()


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
# B: DB 기록 / Telegram (비필수)
# =============================================================================

# B1: DB 기록 실패 → 플래그 false 유지
@pytest.mark.asyncio
async def test_b1_db_failure_keeps_flag_false():
    pos = _make_position(realized_pnl_jpy=Decimal("-100"))
    count, _ = await _call_detect([pos], record_ok=False)
    assert count == 0
    assert pos.loss_webhook_sent is False


# B2: Telegram 토큰 미설정 → DB 기록은 성공 (Telegram은 optional)
@pytest.mark.asyncio
async def test_b2_no_telegram_token_still_records():
    from core.punisher.task.loss_detector import _record_loss
    from adapters.database.models import WakeUpReview

    pos = _make_position()
    db = AsyncMock()
    review_mock = MagicMock()
    review_mock.id = 42
    db.flush = AsyncMock()

    with patch("core.punisher.task.loss_detector.TELEGRAM_BOT_TOKEN", ""), \
         patch("core.punisher.task.loss_detector.TELEGRAM_CHAT_ID", ""), \
         patch("core.punisher.task.loss_detector.WakeUpReview", return_value=review_mock):
        result = await _record_loss(db, pos, position_type="trend", prefix="bf", http_client=None)

    assert result is True  # DB 기록 성공


# B3: WakeUpReview 생성 예외 → False 반환
@pytest.mark.asyncio
async def test_b3_wake_up_review_exception_returns_false():
    from core.punisher.task.loss_detector import _record_loss

    pos = _make_position()
    db = AsyncMock()
    db.flush = AsyncMock(side_effect=Exception("DB error"))

    with patch("core.punisher.task.loss_detector.WakeUpReview", side_effect=Exception("model error")):
        result = await _record_loss(db, pos, position_type="trend", prefix="bf", http_client=None)

    assert result is False


# =============================================================================
# C: 박스 포지션 감지
# =============================================================================

# C1: 박스 포지션 손실 → DB 기록 (position_type=box)
@pytest.mark.asyncio
async def test_c1_box_position_loss_recorded():
    trend_pos = _make_position(id=1, realized_pnl_jpy=Decimal("-100"))
    box_pos = _make_position(id=10, realized_pnl_jpy=Decimal("-200"))
    count, mock_record = await _call_detect([trend_pos], box_positions=[box_pos])
    # trend 1 + box 1 = 2
    assert count == 2
    # _record_loss가 trend, box 순서로 호출됨
    calls = mock_record.call_args_list
    position_types = [c.kwargs.get("position_type") or c[1].get("position_type") for c in calls]
    assert "trend" in position_types
    assert "box" in position_types


# C2: box_position_model=None → 박스 스킵, trend만 처리
@pytest.mark.asyncio
async def test_c2_no_box_model_skips_box():
    pos = _make_position(realized_pnl_jpy=Decimal("-300"))

    from core.punisher.task.loss_detector import detect_and_notify_losses
    db = _make_db([pos])

    with patch("core.punisher.task.loss_detector._record_loss", new_callable=AsyncMock, return_value=True) as mock_record:
        count = await detect_and_notify_losses(db, BfTrendPosition, box_position_model=None, prefix="bf")

    assert count == 1
    # position_type=box로 호출된 것 없음
    calls = mock_record.call_args_list
    position_types = [c.kwargs.get("position_type") or c[1].get("position_type") for c in calls]
    assert "box" not in position_types


# C3: loss_webhook_sent 필드가 박스 모델에 존재하는지 확인
def test_c3_box_model_has_loss_webhook_sent():
    model = create_box_position_model("bf")
    cols = {c.name for c in model.__table__.columns}
    assert "loss_webhook_sent" in cols, f"loss_webhook_sent 없음. cols={cols}"


# =============================================================================
# D: wake_up_trigger
# =============================================================================

# D1: scheduled_at 경과 pending_pipeline 리뷰 → pipeline_status=triggered
@pytest.mark.asyncio
async def test_d1_trigger_pending_review():
    from core.punisher.task.wake_up_trigger import trigger_pending_reviews

    review = MagicMock()
    review.id = 1
    review.pair = "BTC_JPY"
    review.position_id = 5
    review.position_type = "trend"
    review.exchange = "bf"
    review.realized_pnl = Decimal("-500")
    review.pipeline_status = "pending_pipeline"

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [review]
    db.execute = AsyncMock(return_value=result_mock)
    db.commit = AsyncMock()

    with patch("core.punisher.task.wake_up_trigger._send_pipeline_webhook", new_callable=AsyncMock, return_value=True):
        count = await trigger_pending_reviews(db)

    assert count == 1
    assert review.pipeline_status == "triggered"
    assert review.pipeline_started_at is not None


# D2: webhook 전송 실패 → status 유지 (pending_pipeline)
@pytest.mark.asyncio
async def test_d2_trigger_webhook_failure_keeps_status():
    from core.punisher.task.wake_up_trigger import trigger_pending_reviews

    review = MagicMock()
    review.id = 2
    review.pair = "USD_JPY"
    review.position_id = 7
    review.position_type = "box"
    review.exchange = "gmo"
    review.realized_pnl = Decimal("-100")
    review.pipeline_status = "pending_pipeline"

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [review]
    db.execute = AsyncMock(return_value=result_mock)
    db.commit = AsyncMock()

    with patch("core.punisher.task.wake_up_trigger._send_pipeline_webhook", new_callable=AsyncMock, return_value=False):
        count = await trigger_pending_reviews(db)

    assert count == 0
    # status 변경 없음
    assert review.pipeline_status == "pending_pipeline"


# =============================================================================
# E: gmoc prefix 전용 — DB 컬럼 누락 회귀 방지 (UndefinedColumnError 재발 방지)
# =============================================================================

# E1: gmoc TrendPosition 모델에 loss_webhook_sent 컬럼이 존재한다
def test_e1_gmoc_trend_position_has_loss_webhook_sent():
    """2026-04-12 BUG: create_gmo_coin_tables.sql에 loss_webhook_sent 누락 → 회귀 방지."""
    GmocTrendPosition = create_trend_position_model("gmoc")
    cols = {c.name for c in GmocTrendPosition.__table__.columns}
    assert "loss_webhook_sent" in cols, f"loss_webhook_sent 없음. cols={cols}"


# E2: gmoc TrendPosition 모델에 entry_* 스냅샷 컬럼 5개가 모두 존재한다
def test_e2_gmoc_trend_position_has_entry_snapshot_columns():
    GmocTrendPosition = create_trend_position_model("gmoc")
    cols = {c.name for c in GmocTrendPosition.__table__.columns}
    expected = {"entry_rsi", "entry_ema_slope", "entry_atr", "entry_regime", "entry_bb_width"}
    missing = expected - cols
    assert not missing, f"누락된 컬럼: {missing}"


# E3: gmoc prefix로 detect_and_notify_losses 호출 시 손실 감지 정상 동작
@pytest.mark.asyncio
async def test_e3_gmoc_detect_and_notify_losses_works():
    GmocTrendPosition = create_trend_position_model("gmoc")
    pos = _make_position(realized_pnl_jpy=Decimal("-999"))

    from core.punisher.task.loss_detector import detect_and_notify_losses
    db = _make_db([pos])

    with patch("core.punisher.task.loss_detector._record_loss", new_callable=AsyncMock, return_value=True):
        count = await detect_and_notify_losses(db, GmocTrendPosition, prefix="gmoc")

    assert count == 1
    assert pos.loss_webhook_sent is True

