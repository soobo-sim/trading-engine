"""
Execution Layer — IApprovalGate Protocol + 구현체.

Phase A: TelegramApprovalGate — 인라인 키보드 1클릭 승인 (수동)
Phase B: AutoApprovalGate     — 조건부 자동 승인 + Telegram 사후 보고

사용:
  TELEGRAM_APPROVAL=true  → TelegramApprovalGate (Phase A: 항상 수동)
  APPROVAL_MODE=auto      → AutoApprovalGate     (Phase B: 조건 충족 시 자동)
  미설정                  → approval_gate=None   → 무조건 통과 (현재 기본값)

Long Polling 방식 사용.
  - trading-engine은 로컬 Docker → public webhook URL 없음
  - 승인 대기 분 단위, 2초 polling으로 충분
  - 향후 fly.io 배포 시 webhook 구현체로 전환 가능 (IApprovalGate Protocol)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Protocol, runtime_checkable

import httpx

from core.data.dto import Decision

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

_TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"


# ──────────────────────────────────────────────────────────────
# IApprovalGate Protocol
# ──────────────────────────────────────────────────────────────


@runtime_checkable
class IApprovalGate(Protocol):
    """실행 전 수보오빠 승인 인터페이스."""

    async def request_approval(self, decision: Decision) -> bool:
        """Decision에 대한 승인을 요청한다.

        True  → 승인 (실행 진행)
        False → 거부 또는 타임아웃 (실행 중단)
        """
        ...


# ──────────────────────────────────────────────────────────────
# TelegramApprovalGate (Phase A — 항상 수동)
# ──────────────────────────────────────────────────────────────


class TelegramApprovalGate:
    """Telegram 인라인 키보드 기반 1클릭 승인. IApprovalGate Protocol 준수.

    동작:
      1. sendMessage (inline_keyboard: ✅승인 / ❌거부 / ⏸보류)
         callback_data = "approve:{uuid}" / "reject:{uuid}" / "hold:{uuid}"
      2. getUpdates long polling으로 callback_query 대기
      3. asyncio.wait_for → timeout_sec 초과 시 자동 거부
      4. 결과 후 editMessageText로 메시지 상태 업데이트

    Args:
        bot_token:     TELEGRAM_BOT_TOKEN
        chat_id:       TELEGRAM_CHAT_ID
        timeout_sec:   승인 대기 시간 (기본 300초 = 5분)
        poll_interval: getUpdates 간격 (기본 2초)
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout_sec: int = 300,
        poll_interval: float = 2.0,
    ) -> None:
        if not bot_token:
            raise ValueError("TelegramApprovalGate: bot_token이 비어있습니다")
        if not chat_id:
            raise ValueError("TelegramApprovalGate: chat_id가 비어있습니다")
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout_sec = timeout_sec
        self._poll_interval = poll_interval
        self._last_update_id: int = 0

    async def request_approval(self, decision: Decision) -> bool:
        """Telegram 승인 요청.

        1. _format_proposal(decision) → 텍스트 + inline_keyboard
        2. sendMessage → message_id
        3. _poll_for_response(approval_id, timeout) → "approve" | "reject" | "hold" | None
        4. "approve" → True, 그 외 → False
        5. editMessageText로 결과 반영

        에러 발생 시 → WARNING 로그 + False (안전 방향)
        """
        approval_id = uuid.uuid4().hex[:12]
        try:
            text, reply_markup = self._format_proposal(decision, approval_id)
            message_id = await self._send_message(text, reply_markup)
        except Exception as e:
            logger.warning(f"[ApprovalGate] 승인 메시지 전송 실패 — {e}. 진입 차단.")
            return False

        try:
            response = await asyncio.wait_for(
                self._poll_for_response(approval_id),
                timeout=self._timeout_sec,
            )
        except asyncio.TimeoutError:
            response = None

        approved = response == "approve"

        # 결과 반영 (실패해도 무시)
        status_map = {
            "approve": "✅ 승인됨",
            "reject": "❌ 거부됨",
            "hold": "⏸ 보류됨",
            None: "⏰ 타임아웃",
        }
        try:
            await self._edit_message(
                message_id,
                text + f"\n\n{status_map.get(response, '❓ 알 수 없음')}",
            )
        except Exception as e:
            logger.debug(f"[ApprovalGate] 메시지 수정 실패 (무시): {e}")

        action = response or "timeout"
        logger.info(
            f"[ApprovalGate] {decision.pair} 승인 요청 결과: {action} "
            f"(id={approval_id})"
        )
        return approved

    def _format_proposal(
        self, decision: Decision, approval_id: str
    ) -> tuple[str, dict]:
        """Decision → Telegram 메시지 텍스트 + reply_markup dict."""
        action_label = {
            "entry_long": "롱 진입",
            "entry_short": "숏 진입",
        }.get(decision.action, decision.action)

        pair_display = decision.pair.replace("_", "/")
        confidence_pct = f"{decision.confidence:.0%}"
        size_pct = f"{decision.size_pct:.0%}"

        now_jst = datetime.now(JST).strftime("%H:%M")

        sl_str = f"¥{decision.stop_loss:,.0f}" if decision.stop_loss else "없음"
        tp_str = f"¥{decision.take_profit:,.0f}" if decision.take_profit else "없음"
        source_label = "AI v2" if "ai" in decision.source else "Rule v1"
        reasoning_short = (decision.reasoning or "")[:80]

        text = (
            f"📊 거래 제안 #{approval_id} ({now_jst})\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 {pair_display} {action_label}\n"
            f"확신도: {confidence_pct} | 크기: {size_pct}\n"
            f"SL: {sl_str} | TP: {tp_str}\n"
            f"근거: {reasoning_short}\n"
            f"(판단: {source_label})"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ 승인", "callback_data": f"approve:{approval_id}"},
                    {"text": "❌ 거부", "callback_data": f"reject:{approval_id}"},
                    {"text": "⏸ 보류", "callback_data": f"hold:{approval_id}"},
                ]
            ]
        }
        return text, reply_markup

    async def _send_message(self, text: str, reply_markup: dict) -> int:
        """sendMessage API 호출 → message_id 반환."""
        url = f"{_TELEGRAM_API_BASE.format(token=self._bot_token)}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "reply_markup": json.dumps(reply_markup),
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram sendMessage 실패: {data}")
            return data["result"]["message_id"]

    async def _edit_message(self, message_id: int, text: str) -> None:
        """editMessageText API 호출 (reply_markup 제거)."""
        url = (
            f"{_TELEGRAM_API_BASE.format(token=self._bot_token)}/editMessageText"
        )
        payload = {
            "chat_id": self._chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": json.dumps({"inline_keyboard": []}),
        }
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload)

    async def _poll_for_response(self, approval_id: str) -> str:
        """getUpdates long polling으로 callback_query 응답 대기.

        매칭되면 "approve" / "reject" / "hold" 반환.
        타임아웃은 asyncio.wait_for가 처리 — 이 메서드는 무한 루프.
        answerCallbackQuery 호출로 Telegram UI 로딩 해제.
        """
        url = f"{_TELEGRAM_API_BASE.format(token=self._bot_token)}/getUpdates"

        async with httpx.AsyncClient(timeout=self._poll_interval + 5) as client:
            while True:
                try:
                    resp = await client.get(
                        url,
                        params={
                            "offset": self._last_update_id + 1,
                            "timeout": int(self._poll_interval),
                        },
                    )
                    data = resp.json()
                    if not data.get("ok"):
                        await asyncio.sleep(self._poll_interval)
                        continue

                    for update in data.get("result", []):
                        update_id: int = update["update_id"]
                        if update_id > self._last_update_id:
                            self._last_update_id = update_id

                        cb = update.get("callback_query")
                        if cb is None:
                            continue

                        callback_data: str = cb.get("data", "")
                        parts = callback_data.split(":", 1)
                        if len(parts) != 2 or parts[1] != approval_id:
                            continue  # 다른 approval_id — 무시

                        action = parts[0]  # "approve" / "reject" / "hold"

                        # Telegram UI 로딩 해제
                        try:
                            await client.post(
                                f"{_TELEGRAM_API_BASE.format(token=self._bot_token)}"
                                "/answerCallbackQuery",
                                json={"callback_query_id": cb["id"]},
                            )
                        except Exception:
                            pass

                        return action

                except (httpx.ReadTimeout, httpx.ConnectError):
                    await asyncio.sleep(self._poll_interval)
                except Exception as e:
                    logger.warning(f"[ApprovalGate] polling 오류: {e}")
                    await asyncio.sleep(self._poll_interval)


# ──────────────────────────────────────────────────────────────
# AutoApprovalGate (Phase B — 조건부 자동 승인)
# ──────────────────────────────────────────────────────────────


class AutoApprovalGate:
    """Phase B — 조건부 자동 승인 + Telegram 사후 보고. IApprovalGate Protocol 준수.

    자동 승인 조건 (모두 충족 시):
      1. confidence >= min_confidence (기본 0.65)
      2. samantha verdict != "oppose" (decision.meta에서 추출)
      3. size_pct <= max_auto_size (기본 0.4 = 40%)

    미충족 → telegram_gate 폴백 (수동 승인 요청).
    자동 승인 시 → Telegram 사후 보고 메시지 전송 (fire-and-forget).

    Args:
        telegram_gate:   TelegramApprovalGate (폴백 + 사후 보고용)
        min_confidence:  자동 승인 최소 확신도 (기본 0.65)
        max_auto_size:   자동 승인 최대 사이즈 비율 (기본 0.4)
    """

    def __init__(
        self,
        telegram_gate: TelegramApprovalGate,
        min_confidence: float = 0.65,
        max_auto_size: float = 0.40,
    ) -> None:
        self._telegram_gate = telegram_gate
        self._min_confidence = min_confidence
        self._max_auto_size = max_auto_size

    async def request_approval(self, decision: Decision) -> bool:
        """자동 승인 조건 평가.

        조건 충족 → Telegram 사후 보고 (fire-and-forget) + True
        조건 미충족 → telegram_gate.request_approval() 폴백
        """
        if self._should_auto_approve(decision):
            logger.info(
                f"[AutoApprovalGate] {decision.pair} 자동 승인 "
                f"(confidence={decision.confidence:.2f}, size={decision.size_pct:.0%})"
            )
            asyncio.create_task(self._send_post_report(decision))
            return True

        logger.info(
            f"[AutoApprovalGate] {decision.pair} 자동 승인 조건 미충족 → 수동 요청"
        )
        return await self._telegram_gate.request_approval(decision)

    def _should_auto_approve(self, decision: Decision) -> bool:
        """자동 승인 조건 판별."""
        if decision.confidence < self._min_confidence:
            return False
        if decision.size_pct > self._max_auto_size:
            return False

        # meta에서 samantha_verdict 추출 (ai_v2만 존재, v1은 비어있음 → 통과)
        meta: dict = getattr(decision, "meta", {}) or {}
        samantha_verdict = meta.get("samantha_verdict", "")
        if samantha_verdict == "oppose":
            return False

        return True

    async def _send_post_report(self, decision: Decision) -> None:
        """Telegram 사후 보고 (火-and-forget). 실패해도 WARNING만."""
        try:
            pair_display = decision.pair.replace("_", "/")
            action_label = {
                "entry_long": "롱 진입",
                "entry_short": "숏 진입",
            }.get(decision.action, decision.action)
            now_jst = datetime.now(JST).strftime("%H:%M")
            sl_str = f"¥{decision.stop_loss:,.0f}" if decision.stop_loss else "없음"
            tp_str = f"¥{decision.take_profit:,.0f}" if decision.take_profit else "없음"

            text = (
                f"📊 거래 실행 완료 (자동) {now_jst}\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 {pair_display} {action_label}\n"
                f"확신도: {decision.confidence:.0%} | 크기: {decision.size_pct:.0%}\n"
                f"SL: {sl_str} | TP: {tp_str}\n"
                f"근거: {(decision.reasoning or '')[:80]}"
            )
            await self._telegram_gate._send_message(text, {"inline_keyboard": []})
        except Exception as e:
            logger.warning(f"[AutoApprovalGate] 사후 보고 실패 (무시): {e}")
