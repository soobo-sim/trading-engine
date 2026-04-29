"""
core/execution/approval.py 단위 테스트 — IApprovalGate 구현체.

TelegramApprovalGate: Telegram API mock으로 테스트.
AutoApprovalGate:     telegram_gate mock으로 테스트.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.data.dto import Decision
from core.execution.approval import AutoApprovalGate, IApprovalGate, TelegramApprovalGate


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────


def _make_decision(
    action: str = "entry_long",
    confidence: float = 0.7,
    size_pct: float = 0.3,
    meta: dict | None = None,
) -> Decision:
    return Decision(
        action=action,
        pair="USD_JPY",
        exchange="gmo_fx",
        confidence=confidence,
        size_pct=size_pct,
        stop_loss=149.50,
        take_profit=151.00,
        reasoning="테스트 판단 근거 — 충분히 긴 문장입니다",
        risk_factors=(),
        source="ai_v2",
        trigger="regular_4h",
        raw_signal="long_setup",
        timestamp=datetime.now(timezone.utc),
        meta=meta or {},
    )


# ──────────────────────────────────────────────────────────────
# IApprovalGate Protocol
# ──────────────────────────────────────────────────────────────


def test_telegram_gate_implements_protocol():
    """TelegramApprovalGate가 IApprovalGate Protocol을 준수한다."""
    gate = TelegramApprovalGate(
        bot_token="dummy_token",
        chat_id="dummy_chat",
    )
    assert isinstance(gate, IApprovalGate)


def test_auto_gate_implements_protocol():
    """AutoApprovalGate가 IApprovalGate Protocol을 준수한다."""
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    gate = AutoApprovalGate(telegram_gate=tg_gate)
    assert isinstance(gate, IApprovalGate)


# ──────────────────────────────────────────────────────────────
# TelegramApprovalGate — 초기화 검증
# ──────────────────────────────────────────────────────────────


def test_telegram_gate_raises_on_empty_token():
    """bot_token 비어있으면 ValueError."""
    with pytest.raises(ValueError, match="bot_token"):
        TelegramApprovalGate(bot_token="", chat_id="chat")


def test_telegram_gate_raises_on_empty_chat_id():
    """chat_id 비어있으면 ValueError."""
    with pytest.raises(ValueError, match="chat_id"):
        TelegramApprovalGate(bot_token="token", chat_id="")


# ──────────────────────────────────────────────────────────────
# TelegramApprovalGate — request_approval 동작
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_gate_approve_returns_true():
    """
    Given: sendMessage 성공 → polling "approve" 수신
    When:  request_approval()
    Then:  True 반환
    """
    gate = TelegramApprovalGate(bot_token="tok", chat_id="chat", timeout_sec=5)

    with (
        patch.object(gate, "_send_message", new=AsyncMock(return_value=42)),
        patch.object(gate, "_poll_for_response", new=AsyncMock(return_value="approve")),
        patch.object(gate, "_edit_message", new=AsyncMock()),
    ):
        result = await gate.request_approval(_make_decision())

    assert result is True


@pytest.mark.asyncio
async def test_telegram_gate_reject_returns_false():
    """
    Given: polling "reject" 수신
    When:  request_approval()
    Then:  False 반환
    """
    gate = TelegramApprovalGate(bot_token="tok", chat_id="chat", timeout_sec=5)

    with (
        patch.object(gate, "_send_message", new=AsyncMock(return_value=42)),
        patch.object(gate, "_poll_for_response", new=AsyncMock(return_value="reject")),
        patch.object(gate, "_edit_message", new=AsyncMock()),
    ):
        result = await gate.request_approval(_make_decision())

    assert result is False


@pytest.mark.asyncio
async def test_telegram_gate_hold_returns_false():
    """
    Given: polling "hold" 수신
    When:  request_approval()
    Then:  False 반환
    """
    gate = TelegramApprovalGate(bot_token="tok", chat_id="chat", timeout_sec=5)

    with (
        patch.object(gate, "_send_message", new=AsyncMock(return_value=42)),
        patch.object(gate, "_poll_for_response", new=AsyncMock(return_value="hold")),
        patch.object(gate, "_edit_message", new=AsyncMock()),
    ):
        result = await gate.request_approval(_make_decision())

    assert result is False


@pytest.mark.asyncio
async def test_telegram_gate_timeout_returns_false():
    """
    Given: timeout_sec=0.01 → asyncio.wait_for 즉시 타임아웃
    When:  request_approval()
    Then:  False 반환
    """
    gate = TelegramApprovalGate(
        bot_token="tok", chat_id="chat", timeout_sec=1, poll_interval=0.01
    )

    async def _infinite_poll(_):
        await asyncio.sleep(10)  # 타임아웃보다 길게

    with (
        patch.object(gate, "_send_message", new=AsyncMock(return_value=42)),
        patch.object(gate, "_poll_for_response", new=_infinite_poll),
        patch.object(gate, "_edit_message", new=AsyncMock()),
    ):
        result = await asyncio.wait_for(
            gate.request_approval(_make_decision()), timeout=3.0
        )

    assert result is False


@pytest.mark.asyncio
async def test_telegram_gate_send_failure_returns_false():
    """
    Given: sendMessage API 오류 (예외)
    When:  request_approval()
    Then:  False 반환 (안전 방향)
    """
    gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")

    with patch.object(
        gate, "_send_message", new=AsyncMock(side_effect=RuntimeError("API 오류"))
    ):
        result = await gate.request_approval(_make_decision())

    assert result is False


# ──────────────────────────────────────────────────────────────
# TelegramApprovalGate — _format_proposal
# ──────────────────────────────────────────────────────────────


def test_format_proposal_contains_pair_and_action():
    """_format_proposal 결과에 pair/action 정보가 포함된다."""
    gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    d = _make_decision(action="entry_short", size_pct=0.4)
    text, reply_markup = gate._format_proposal(d, "abc123")

    assert "USD/JPY" in text
    assert "숏 진입" in text
    assert "abc123" in text
    assert len(reply_markup["inline_keyboard"][0]) == 3  # ✅ ❌ ⏸


def test_format_proposal_callback_data_format():
    """callback_data가 'action:approval_id' 형식이다."""
    gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    d = _make_decision()
    _, reply_markup = gate._format_proposal(d, "test_id_01")

    buttons = reply_markup["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "approve:test_id_01"
    assert buttons[1]["callback_data"] == "reject:test_id_01"
    assert buttons[2]["callback_data"] == "hold:test_id_01"


# ──────────────────────────────────────────────────────────────
# AutoApprovalGate — _should_auto_approve
# ──────────────────────────────────────────────────────────────


def test_auto_approve_all_conditions_met():
    """
    Given: confidence=0.7, samantha agree, size=0.35
    Then:  자동 승인 조건 충족
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.40)

    d = _make_decision(confidence=0.7, size_pct=0.35, meta={"samantha_verdict": "agree"})
    assert gate._should_auto_approve(d) is True


def test_auto_approve_confidence_below_threshold():
    """
    Given: confidence=0.5 (0.65 미만)
    Then:  자동 승인 조건 미충족
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.40)

    d = _make_decision(confidence=0.5, size_pct=0.3, meta={"samantha_verdict": "agree"})
    assert gate._should_auto_approve(d) is False


def test_auto_approve_samantha_oppose():
    """
    Given: samantha_verdict="oppose"
    Then:  자동 승인 조건 미충족
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.40)

    d = _make_decision(confidence=0.8, size_pct=0.3, meta={"samantha_verdict": "oppose"})
    assert gate._should_auto_approve(d) is False


def test_auto_approve_size_exceeds_max():
    """
    Given: size_pct=0.6 (0.40 초과)
    Then:  자동 승인 조건 미충족
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.40)

    d = _make_decision(confidence=0.8, size_pct=0.6, meta={"samantha_verdict": "conditional"})
    assert gate._should_auto_approve(d) is False


def test_auto_approve_v1_source_no_meta():
    """
    Given: meta={} (v1 source — samantha_verdict 없음)
    When:  confidence, size 조건 충족
    Then:  자동 승인 통과 (samantha_verdict="" != "oppose")
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.40)

    d = _make_decision(confidence=0.7, size_pct=0.35, meta={})
    assert gate._should_auto_approve(d) is True


def test_auto_approve_samantha_conditional_passes():
    """
    Given: samantha_verdict="conditional" (반대 아님), 조건 충족
    Then:  자동 승인 통과 — "oppose"만 차단
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.40)

    d = _make_decision(confidence=0.7, size_pct=0.35, meta={"samantha_verdict": "conditional"})
    assert gate._should_auto_approve(d) is True


# ──────────────────────────────────────────────────────────────
# AutoApprovalGate — request_approval 동작
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_approve_conditions_met_returns_true():
    """
    Given: 자동 승인 조건 충족
    When:  request_approval()
    Then:  True 반환, telegram_gate.request_approval 미호출
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    tg_gate.request_approval = AsyncMock()
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.40)

    d = _make_decision(confidence=0.7, size_pct=0.35, meta={"samantha_verdict": "agree"})

    with patch.object(gate, "_send_post_report", new=AsyncMock()):
        result = await gate.request_approval(d)

    assert result is True
    tg_gate.request_approval.assert_not_called()


@pytest.mark.asyncio
async def test_auto_approve_fallback_to_telegram():
    """
    Given: 자동 승인 조건 미충족 (confidence 낮음)
    When:  request_approval()
    Then:  telegram_gate.request_approval 호출됨
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    tg_gate.request_approval = AsyncMock(return_value=True)
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.40)

    d = _make_decision(confidence=0.5, size_pct=0.3, meta={"samantha_verdict": "agree"})
    result = await gate.request_approval(d)

    assert result is True
    tg_gate.request_approval.assert_called_once_with(d)


@pytest.mark.asyncio
async def test_auto_approve_post_report_failure_does_not_block():
    """
    Given: 자동 승인 조건 충족 + 사후 보고 실패
    When:  request_approval()
    Then:  True 반환 (거래 진행) — 보고 실패 무시
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.40)

    d = _make_decision(confidence=0.7, size_pct=0.35, meta={"samantha_verdict": "agree"})

    async def _fail_report(*args, **kwargs):
        raise RuntimeError("보고 실패")

    with patch.object(gate, "_send_post_report", new=_fail_report):
        # _send_post_report는 create_task로 fire-and-forget이므로 직접 호출하면 오류
        # _should_auto_approve=True 경우 실제 create_task 호출. 태스크 완료 전 검사.
        result = await gate.request_approval(d)
        # 실행 중인 태스크 취소 방지 — 이벤트 루프 한 턴 실행
        await asyncio.sleep(0)

    assert result is True


# ──────────────────────────────────────────────────────────────
# AutoApprovalGate — max_auto_size=0.60 (운영 설정값) 검증
# AUTO_APPROVAL_MAX_SIZE=0.60 (.env) 변경에 따른 경계값 테스트
# ──────────────────────────────────────────────────────────────


def test_auto_approve_size_0_50_with_max_0_60_passes():
    """
    Given: max_auto_size=0.60 (운영 설정값), size=0.50 (GMO Coin 전략 position_size_pct)
    Then:  자동 승인 조건 충족 — GMO Coin 숏 진입 시 수동 승인 요청 불필요
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.60)

    d = _make_decision(confidence=0.7, size_pct=0.50, meta={})
    assert gate._should_auto_approve(d) is True


def test_auto_approve_size_0_60_boundary_passes():
    """
    Given: max_auto_size=0.60, size=0.60 (경계값 정확히 일치)
    Then:  자동 승인 통과 (≤ 조건)
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.60)

    d = _make_decision(confidence=0.7, size_pct=0.60, meta={})
    assert gate._should_auto_approve(d) is True


def test_auto_approve_size_0_61_exceeds_max_0_60():
    """
    Given: max_auto_size=0.60, size=0.61 (경계 초과)
    Then:  자동 승인 조건 미충족 → 수동 승인 폴백
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.60)

    d = _make_decision(confidence=0.8, size_pct=0.61, meta={})
    assert gate._should_auto_approve(d) is False


def test_auto_approve_size_0_80_still_blocked_with_max_0_60():
    """
    Given: max_auto_size=0.60, size=0.80 (어제 스크린샷 케이스 — 기존 40% 한도에서 수동승인 받던 경우)
    Then:  여전히 자동 승인 불가 → 수동 승인 필요 (사이즈가 비정상적으로 클 경우 보호)
    """
    tg_gate = TelegramApprovalGate(bot_token="tok", chat_id="chat")
    gate = AutoApprovalGate(telegram_gate=tg_gate, min_confidence=0.65, max_auto_size=0.60)

    d = _make_decision(confidence=0.7, size_pct=0.80, meta={})
    assert gate._should_auto_approve(d) is False
