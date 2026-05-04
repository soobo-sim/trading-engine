"""
JIT Advisory 테스트 모음 — gate / client / context / audit

설계서 §13 TC-01~TC-25 기준.

TC-01  GO → entry_long 통과
TC-02  NO_GO → hold
TC-03  ADJUST size_pct 반영
TC-04  ADJUST action 변경
TC-05  타임아웃 fail-soft → hold
TC-06  exit / tighten_stop / hold → JIT 호출 없이 즉시 통과
TC-07  감사 로그 저장 확인
TC-08  token 미설정 → JIT 스킵 → fail-soft hold
TC-09  마크다운 fence 제거 후 JSON 파싱
TC-10  잘못된 JSON → None (오류 무시)
TC-11  decision 값 이상 → None
TC-12  adjusted_size_pct 범위 오류 → None
TC-13  adjusted_action 이상 → adjusted_action=None (무시)
TC-14  HTTP 4xx → fail-soft hold
TC-15  HTTP 5xx → fail-soft hold (재시도 없음)
TC-16  context 빌더: 기본 snapshot → request 정상 생성
TC-17  context 빌더: 포지션 있는 snapshot → has_position=True
TC-18  audit: 정상 응답 저장
TC-19  audit: JIT=None (타임아웃) 저장
TC-20  audit 저장 실패해도 예외 전파 안 함
TC-21  GO confidence 메타 전달
TC-22  ADJUST stop_loss 전달
TC-23  ADJUST take_profit 전달
TC-24  to_prompt() 핵심 필드 포함 확인
TC-25  add_position 액션도 JIT 호출
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from core.judge.jit_advisory.models import JITAdvisoryRequest, JITAdvisoryResponse
from core.judge.jit_advisory.client import JITAdvisoryClient
from core.judge.jit_advisory.context import build_jit_request
from core.judge.jit_advisory.audit import save_jit_audit
from core.judge.jit_advisory.gate import JITAdvisoryGate
from core.shared.data.dto import Decision, PositionDTO, SignalSnapshot

# ──────────────────────────────────────────────────────────────
# 공통 헬퍼
# ──────────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def _snapshot(
    signal: str = "long_setup",
    position: PositionDTO | None = None,
    params: dict | None = None,
) -> SignalSnapshot:
    return SignalSnapshot(
        pair="btc_jpy",
        exchange="gmo_coin",
        timestamp=_NOW,
        signal=signal,
        current_price=15_000_000.0,
        exit_signal={"action": "hold", "reason": ""},
        position=position,
        params=params if params is not None else {
            "position_size_pct": 50.0,
            "trading_style": "trend_following",
        },
    )


def _rule_decision(
    action: str = "entry_long",
    confidence: float = 0.75,
    size_pct: float = 0.5,
    stop_loss: float | None = 14_700_000.0,
) -> Decision:
    return Decision(
        action=action,
        pair="btc_jpy",
        exchange="gmo_coin",
        confidence=confidence,
        size_pct=size_pct,
        stop_loss=stop_loss,
        take_profit=None,
        reasoning="EMA 상향 + BB폭 충분",
        risk_factors=(),
        source="rule_based_v1",
        trigger="regular_4h",
        raw_signal="long_setup",
        meta={},
    )


def _jit_request() -> JITAdvisoryRequest:
    return JITAdvisoryRequest(
        request_id="test-req-001",
        pair="btc_jpy",
        exchange="gmo_coin",
        trading_style="trend_following",
        proposed_action="entry_long",
        rule_signal="long_setup",
        rule_confidence=0.75,
        rule_size_pct=0.5,
        rule_reasoning="EMA 상향",
    )


def _jit_response(
    decision: str = "GO",
    confidence: float = 0.82,
    adjusted_size_pct: float | None = None,
    adjusted_action: str | None = None,
    adjusted_stop_loss: float | None = None,
    adjusted_take_profit: float | None = None,
    reasoning: str = "추세가 명확하고 RSI 중립권. 진입 적합.",
) -> JITAdvisoryResponse:
    return JITAdvisoryResponse(
        request_id="test-req-001",
        decision=decision,
        confidence=confidence,
        adjusted_size_pct=adjusted_size_pct,
        adjusted_action=adjusted_action,
        adjusted_stop_loss=adjusted_stop_loss,
        adjusted_take_profit=adjusted_take_profit,
        reasoning=reasoning,
        risk_factors=["단기 과열 가능성"],
        latency_ms=450,
        model="openclaw/rachel",
    )


def _make_gate(
    mock_client: JITAdvisoryClient | None = None,
    rule_action: str = "entry_long",
    rule_confidence: float = 0.75,
) -> tuple[JITAdvisoryGate, AsyncMock]:
    """JITAdvisoryGate + session_factory mock 반환."""
    mock_session_factory = MagicMock()
    mock_session = AsyncMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    gate = JITAdvisoryGate(
        session_factory=mock_session_factory,
        jit_client=mock_client,
    )

    # 룰엔진 결과를 mock
    rule_dec = _rule_decision(action=rule_action, confidence=rule_confidence)
    gate._rule = MagicMock()
    gate._rule.decide = AsyncMock(return_value=rule_dec)

    return gate, mock_session_factory


# ──────────────────────────────────────────────────────────────
# TC-01: GO → entry_long 통과
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc01_go_passes_entry_long():
    """JIT=GO → 룰엔진 entry_long 결정이 그대로 통과한다."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=_jit_response(decision="GO"))

    gate, _ = _make_gate(mock_client=mock_client)
    dec = await gate.decide(_snapshot())

    assert dec.action == "entry_long"
    assert "[JIT GO]" in dec.reasoning
    assert dec.meta.get("jit_decision") == "GO"
    mock_client.request.assert_awaited_once()


# ──────────────────────────────────────────────────────────────
# TC-02: NO_GO → hold
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc02_nogo_returns_hold():
    """JIT=NO_GO → hold로 변경된다."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=_jit_response(
        decision="NO_GO",
        reasoning="매크로 리스크 높고 BB폭 부족. 진입 부적합.",
    ))

    gate, _ = _make_gate(mock_client=mock_client)
    dec = await gate.decide(_snapshot())

    assert dec.action == "hold"
    assert "[JIT NO_GO]" in dec.reasoning
    assert dec.meta.get("jit_decision") == "NO_GO"


# ──────────────────────────────────────────────────────────────
# TC-03: ADJUST size_pct 반영
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc03_adjust_size_pct():
    """JIT=ADJUST → adjusted_size_pct이 최종 decision에 반영된다."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=_jit_response(
        decision="ADJUST",
        adjusted_size_pct=0.3,
        reasoning="변동성이 크므로 사이즈 축소 권장.",
    ))

    gate, _ = _make_gate(mock_client=mock_client)
    dec = await gate.decide(_snapshot())

    assert dec.action == "entry_long"   # action 유지
    assert dec.size_pct == pytest.approx(0.3)
    assert "[JIT ADJUST]" in dec.reasoning
    assert dec.meta.get("jit_decision") == "ADJUST"
    assert dec.meta.get("jit_original_size") == pytest.approx(0.5)


# ──────────────────────────────────────────────────────────────
# TC-04: ADJUST action 변경
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc04_adjust_action_change():
    """JIT=ADJUST + adjusted_action → action이 변경된다."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=_jit_response(
        decision="ADJUST",
        adjusted_size_pct=0.4,
        adjusted_action="entry_short",
        reasoning="신호가 뒤집혔으므로 숏으로 전환 권장.",
    ))

    gate, _ = _make_gate(mock_client=mock_client)
    dec = await gate.decide(_snapshot())

    assert dec.action == "entry_short"
    assert dec.size_pct == pytest.approx(0.4)


# ──────────────────────────────────────────────────────────────
# TC-05: 타임아웃 fail-soft → hold
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc05_timeout_failsoft_go():
    """JIT 클라이언트가 None 반환(타임아웃) → fail-soft GO (size * 0.7)."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=None)

    gate, _ = _make_gate(mock_client=mock_client)
    dec = await gate.decide(_snapshot())

    assert dec.action == "entry_long"  # hold이 아닌 진입
    assert dec.size_pct == pytest.approx(0.5 * 0.7, rel=1e-3)  # 70% 축소
    assert "[JIT timeout-GO]" in dec.reasoning


# ──────────────────────────────────────────────────────────────
# TC-06: exit / tighten_stop / hold → JIT 호출 없이 즉시 통과
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("rule_action", ["exit", "tighten_stop", "hold"])
async def test_tc06_non_entry_passthrough(rule_action: str):
    """exit/tighten_stop/hold → JIT 호출 안 함, 룰 결과 그대로."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    gate, _ = _make_gate(mock_client=mock_client, rule_action=rule_action)

    dec = await gate.decide(_snapshot())

    assert dec.action == rule_action
    mock_client.request.assert_not_awaited()


# ──────────────────────────────────────────────────────────────
# TC-07: 감사 로그 저장 확인 (GO)
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc07_audit_saved_on_go():
    """JIT=GO → _log_audit 호출되어 DB 저장 시도."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=_jit_response(decision="GO"))

    gate, mock_sf = _make_gate(mock_client=mock_client)

    with patch("core.judge.jit_advisory.gate.save_jit_audit", new_callable=AsyncMock) as mock_save:
        dec = await gate.decide(_snapshot())

    mock_save.assert_awaited_once()
    call_kwargs = mock_save.call_args
    assert call_kwargs[1]["final_action"] == "entry_long"


# ──────────────────────────────────────────────────────────────
# TC-08: token 미설정 → JIT 스킵 → fail-soft GO (size * 0.7)
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc08_no_token_skip():
    """JIT_ADVISORY_TOKEN 미설정 → client.request가 None 반환 → fail-soft GO (size * 0.7)."""
    real_client = JITAdvisoryClient(url="http://invalid", token="", timeout_sec=5)
    gate, _ = _make_gate(mock_client=real_client)

    dec = await gate.decide(_snapshot())

    assert dec.action == "entry_long"  # hold이 아닌 진입
    assert "timeout-go" in dec.reasoning.lower()


# ──────────────────────────────────────────────────────────────
# TC-09: 마크다운 fence 제거 후 JSON 파싱
# ──────────────────────────────────────────────────────────────

def test_tc09_markdown_fence_stripped():
    """agent가 ```json ... ``` 형태로 응답해도 올바르게 파싱된다."""
    client = JITAdvisoryClient(url="http://invalid", token="test-token")
    md_text = (
        "```json\n"
        '{"decision": "GO", "confidence": 0.80, "reasoning": "추세 강하고 RSI 중립권 진입 적합.", "risk_factors": []}\n'
        "```"
    )
    raw = {
        "model": "openclaw/rachel",
        "output": [{"content": [{"type": "output_text", "text": md_text}]}],
    }

    resp = client._parse_response("req-001", raw, 300)

    assert resp is not None
    assert resp.decision == "GO"
    assert resp.confidence == pytest.approx(0.80)


# ──────────────────────────────────────────────────────────────
# TC-10: 잘못된 JSON → None
# ──────────────────────────────────────────────────────────────

def test_tc10_invalid_json_returns_none():
    """agent가 유효하지 않은 JSON을 반환하면 None."""
    client = JITAdvisoryClient(url="http://invalid", token="test-token")
    raw = {
        "output": [{"content": [{"type": "output_text", "text": "이것은 JSON이 아닙니다."}]}]
    }

    resp = client._parse_response("req-001", raw, 300)
    assert resp is None


# ──────────────────────────────────────────────────────────────
# TC-11: decision 값 이상 → None
# ──────────────────────────────────────────────────────────────

def test_tc11_invalid_decision_returns_none():
    """decision이 GO/NO_GO/ADJUST 외 값이면 None."""
    client = JITAdvisoryClient(url="http://invalid", token="test-token")
    raw = {
        "output": [{"content": [{"type": "output_text", "text": json.dumps({
            "decision": "UNKNOWN",
            "confidence": 0.5,
            "reasoning": "잘못된 결정값 테스트",
            "risk_factors": [],
        })}]}]
    }

    resp = client._parse_response("req-001", raw, 300)
    assert resp is None


# ──────────────────────────────────────────────────────────────
# TC-12: adjusted_size_pct 범위 오류 → None
# ──────────────────────────────────────────────────────────────

def test_tc12_adjusted_size_out_of_range():
    """adjusted_size_pct > 1.0 → None."""
    client = JITAdvisoryClient(url="http://invalid", token="test-token")
    raw = {
        "output": [{"content": [{"type": "output_text", "text": json.dumps({
            "decision": "ADJUST",
            "confidence": 0.7,
            "reasoning": "사이즈 조정. 변동성 높아 축소 권장함. 상세 확인 필요.",
            "adjusted_size_pct": 1.5,   # 범위 초과
            "risk_factors": [],
        })}]}]
    }

    resp = client._parse_response("req-001", raw, 300)
    assert resp is None


# ──────────────────────────────────────────────────────────────
# TC-13: adjusted_action 이상 → None으로 대체
# ──────────────────────────────────────────────────────────────

def test_tc13_invalid_adjusted_action_ignored():
    """adjusted_action이 entry 액션 외의 값이면 None으로 대체 (응답 자체는 유효)."""
    client = JITAdvisoryClient(url="http://invalid", token="test-token")
    raw = {
        "output": [{"content": [{"type": "output_text", "text": json.dumps({
            "decision": "ADJUST",
            "confidence": 0.7,
            "reasoning": "방향 조정 필요. 조정값 잘못됨.",
            "adjusted_size_pct": 0.4,
            "adjusted_action": "close_all",  # 허용 안 됨
            "risk_factors": [],
        })}]}]
    }

    resp = client._parse_response("req-001", raw, 300)
    assert resp is not None
    assert resp.adjusted_action is None


# ──────────────────────────────────────────────────────────────
# TC-14: HTTP 4xx → fail-soft None
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc14_http_4xx_returns_none():
    """HTTP 4xx 에러 → client.request가 None 반환."""
    client = JITAdvisoryClient(url="http://invalid", token="test-token", timeout_sec=5)

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=mock_resp
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await client.request(_jit_request())

    assert result is None


# ──────────────────────────────────────────────────────────────
# TC-15: HTTP 5xx → fail-soft None (재시도 없음)
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc15_http_5xx_returns_none_no_retry():
    """HTTP 5xx → 재시도 없이 None."""
    client = JITAdvisoryClient(url="http://invalid", token="test-token", timeout_sec=5, retry_count=0)

    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.text = "Service Unavailable"
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "503", request=MagicMock(), response=mock_resp
    )

    call_count = 0

    async def fake_post(*a, **kw):
        nonlocal call_count
        call_count += 1
        return mock_resp

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake_post):
        result = await client.request(_jit_request())

    assert result is None
    assert call_count == 1  # 재시도 없이 1회만


# ──────────────────────────────────────────────────────────────
# TC-16: context 빌더 기본 동작
# ──────────────────────────────────────────────────────────────

def test_tc16_context_builder_basic():
    """build_jit_request → JITAdvisoryRequest 기본 필드 정상 설정."""
    snap = _snapshot()
    dec = _rule_decision()

    req = build_jit_request(snap, dec)

    assert req.pair == "btc_jpy"
    assert req.exchange == "gmo_coin"
    assert req.proposed_action == "entry_long"
    assert req.rule_signal == snap.signal
    assert req.rule_confidence == pytest.approx(dec.confidence)
    assert req.rule_size_pct == pytest.approx(dec.size_pct)
    assert req.current_price == pytest.approx(snap.current_price)
    assert req.request_id != ""


# ──────────────────────────────────────────────────────────────
# TC-17: context 빌더 포지션 있는 경우
# ──────────────────────────────────────────────────────────────

def test_tc17_context_builder_with_position():
    """포지션 있는 snapshot → has_position=True, 포지션 필드 채워짐."""
    pos = PositionDTO(
        pair="btc_jpy",
        entry_price=14_800_000.0,
        entry_amount=0.01,
        stop_loss_price=14_500_000.0,
        stop_tightened=False,
    )
    snap = _snapshot(position=pos)
    dec = _rule_decision(action="add_position")

    req = build_jit_request(snap, dec)

    assert req.has_position is True
    assert req.position_entry_price == pytest.approx(14_800_000.0)


# ──────────────────────────────────────────────────────────────
# TC-18: audit 정상 저장
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc18_audit_saves_normal():
    """save_jit_audit → JITAdvisory ORM insert 시도."""
    mock_session = AsyncMock()

    req = _jit_request()
    resp = _jit_response(decision="GO")

    with patch("core.judge.jit_advisory.audit.JITAdvisory") as MockModel:
        mock_instance = MagicMock()
        MockModel.return_value = mock_instance
        await save_jit_audit(
            session=mock_session,
            request=req,
            response=resp,
            final_action="entry_long",
            final_size_pct=0.5,
            error=None,
        )

    mock_session.add.assert_called_once_with(mock_instance)
    mock_session.commit.assert_awaited_once()


# ──────────────────────────────────────────────────────────────
# TC-19: audit JIT=None (타임아웃) 저장
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc19_audit_saves_on_timeout():
    """JIT 타임아웃(response=None, error=...) 시에도 audit 저장된다."""
    mock_session = AsyncMock()

    req = _jit_request()

    with patch("core.judge.jit_advisory.audit.JITAdvisory") as MockModel:
        mock_instance = MagicMock()
        MockModel.return_value = mock_instance
        await save_jit_audit(
            session=mock_session,
            request=req,
            response=None,
            final_action="hold",
            final_size_pct=None,
            error="JIT 타임아웃",
        )

    mock_session.add.assert_called_once_with(mock_instance)
    # error=... 인 경우 ORM에 전달됐는지 kwargs 확인
    ctor_kwargs = MockModel.call_args[1]
    assert ctor_kwargs.get("jit_error") == "JIT 타임아웃"


# ──────────────────────────────────────────────────────────────
# TC-20: audit 저장 실패해도 예외 전파 안 함
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc20_audit_failure_no_exception():
    """감사 로그 DB 저장 실패해도 gate.decide()가 정상 Decision을 반환한다."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=_jit_response(decision="GO"))

    gate, _ = _make_gate(mock_client=mock_client)

    # _log_audit에서 예외가 발생하도록 설정
    gate._log_audit = AsyncMock(side_effect=Exception("DB 연결 끊김"))

    dec = await gate.decide(_snapshot())

    # audit 실패에도 불구하고 GO 결정이 통과되어야 함
    assert dec.action == "entry_long"


# ──────────────────────────────────────────────────────────────
# TC-21: GO confidence 메타 전달
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc21_go_confidence_in_meta():
    """JIT=GO → meta에 jit_confidence 포함."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=_jit_response(
        decision="GO", confidence=0.88
    ))

    gate, _ = _make_gate(mock_client=mock_client)
    dec = await gate.decide(_snapshot())

    assert dec.meta.get("jit_confidence") == pytest.approx(0.88)


# ──────────────────────────────────────────────────────────────
# TC-22: ADJUST stop_loss 전달
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc22_adjust_stop_loss():
    """JIT=ADJUST + adjusted_stop_loss → Decision.stop_loss 업데이트."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=_jit_response(
        decision="ADJUST",
        adjusted_size_pct=0.5,
        adjusted_stop_loss=14_500_000.0,
        reasoning="SL을 더 넓게 조정. ATR 기준 1.8 배수로 재산정함.",
    ))

    gate, _ = _make_gate(mock_client=mock_client)
    dec = await gate.decide(_snapshot())

    assert dec.stop_loss == pytest.approx(14_500_000.0)


# ──────────────────────────────────────────────────────────────
# TC-23: ADJUST take_profit 전달
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc23_adjust_take_profit():
    """JIT=ADJUST + adjusted_take_profit → Decision.take_profit 업데이트."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=_jit_response(
        decision="ADJUST",
        adjusted_size_pct=0.5,
        adjusted_take_profit=16_000_000.0,
        reasoning="TP를 더 높게 설정. 추세 강도 감안하여 1.8R 목표.",
    ))

    gate, _ = _make_gate(mock_client=mock_client)
    dec = await gate.decide(_snapshot())

    assert dec.take_profit == pytest.approx(16_000_000.0)


# ──────────────────────────────────────────────────────────────
# TC-24: to_prompt() 핵심 필드 포함
# ──────────────────────────────────────────────────────────────

def test_tc24_to_prompt_contains_key_fields():
    """to_prompt() 결과에 pair, proposed_action, rule_reasoning이 포함된다."""
    req = _jit_request()
    req.macro_fng = 55
    req.macro_high_impact_event_in_6h = True

    prompt = req.to_prompt()

    assert "btc_jpy" in prompt
    assert "entry_long" in prompt
    assert "EMA 상향" in prompt
    # 매크로 섹션
    assert "55" in prompt
    assert "예" in prompt   # 고영향 이벤트 있음
    # JSON 지시 포함
    assert "GO" in prompt
    assert "NO_GO" in prompt
    assert "ADJUST" in prompt


# ──────────────────────────────────────────────────────────────
# TC-25: add_position 액션도 JIT 호출
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc25_add_position_triggers_jit():
    """add_position 액션도 JIT 자문 대상이다."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=_jit_response(
        decision="GO",
        reasoning="피라미딩 조건 충족. ATR 대비 계좌 리스크 허용 범위 내.",
    ))

    gate, _ = _make_gate(mock_client=mock_client, rule_action="add_position")

    pos = PositionDTO(
        pair="btc_jpy",
        entry_price=14_800_000.0,
        entry_amount=0.01,
        stop_loss_price=14_500_000.0,
        stop_tightened=False,
    )
    dec = await gate.decide(_snapshot(position=pos))

    assert dec.action == "add_position"
    mock_client.request.assert_awaited_once()


# ──────────────────────────────────────────────────────────────
# TC-26: 일별 상한 도달 → v1 폴백
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc26_daily_limit_fallback():
    """일별 호출 상한 도달 시 JIT를 호출하지 않고 룰엔진 결정을 그대로 반환한다."""
    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=_jit_response(decision="GO"))

    mock_session_factory = MagicMock()
    mock_session = AsyncMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    gate = JITAdvisoryGate(
        session_factory=mock_session_factory,
        jit_client=mock_client,
        daily_limit=3,
    )
    rule_dec = _rule_decision(action="entry_long")
    gate._rule = MagicMock()
    gate._rule.decide = AsyncMock(return_value=rule_dec)

    # 상한까지 정상 호출
    for _ in range(3):
        dec = await gate.decide(_snapshot())
        assert dec.action == "entry_long"
        assert "[JIT daily-limit fallback]" not in dec.reasoning

    # 상한 초과 → v1 폴백
    dec = await gate.decide(_snapshot())
    assert dec.action == "entry_long"
    assert "[JIT daily-limit fallback]" in dec.reasoning
    # 4번째는 JIT 호출 없어야 함
    assert mock_client.request.await_count == 3


# ──────────────────────────────────────────────────────────────
# TC-27: 일별 상한 — 날짜 바뀌면 카운터 리셋
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc27_daily_limit_resets_on_new_day():
    """자정이 지나 날짜가 바뀌면 카운터가 리셋된다."""
    from datetime import date
    from unittest.mock import patch as _patch

    mock_client = AsyncMock(spec=JITAdvisoryClient)
    mock_client.request = AsyncMock(return_value=_jit_response(decision="GO"))

    mock_session_factory = MagicMock()
    mock_session = AsyncMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    gate = JITAdvisoryGate(
        session_factory=mock_session_factory,
        jit_client=mock_client,
        daily_limit=1,
    )
    rule_dec = _rule_decision(action="entry_long")
    gate._rule = MagicMock()
    gate._rule.decide = AsyncMock(return_value=rule_dec)

    # 오늘 상한 채움
    await gate.decide(_snapshot())
    dec = await gate.decide(_snapshot())
    assert "[JIT daily-limit fallback]" in dec.reasoning

    # 날짜 바뀜 → 카운터 리셋 → 다시 JIT 호출
    tomorrow = date(gate._counter_date.year, gate._counter_date.month, gate._counter_date.day)
    from datetime import timedelta
    tomorrow = tomorrow + timedelta(days=1)
    with _patch("core.judge.jit_advisory.gate.date") as mock_date:
        mock_date.today.return_value = tomorrow
        dec = await gate.decide(_snapshot())
    assert dec.action == "entry_long"
    assert "[JIT daily-limit fallback]" not in dec.reasoning
    assert mock_client.request.await_count == 2
