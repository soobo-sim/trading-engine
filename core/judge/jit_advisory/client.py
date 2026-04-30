"""
JIT Advisory HTTP 클라이언트.

openclaw gateway(:18793) POST /v1/responses 동기 API를 호출한다.
패턴 A: stream=false → LLM 결과가 HTTP 응답 본문에 직접 포함.

통신 흐름:
    엔진 → POST /v1/responses (model=openclaw/rachel, stream=false)
         → openclaw이 Rachel agent 1턴 실행 (격리 세션)
         → HTTP 응답 body에 agent 출력 JSON 포함
    엔진이 output[0].content[0].text 를 파싱 → JITAdvisoryResponse

fail-soft: 타임아웃/오류 시 None 반환. 호출자(JITAdvisoryGate)가 NO_GO 처리.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Optional

import httpx

from .models import JITAdvisoryRequest, JITAdvisoryResponse

logger = logging.getLogger(__name__)

# ── 환경변수 ──────────────────────────────────────────────────────────────────
JIT_ADVISORY_URL = os.getenv(
    "JIT_ADVISORY_URL",
    "http://host.docker.internal:18793/v1/responses",
)
JIT_ADVISORY_TOKEN = os.getenv("JIT_ADVISORY_TOKEN", "")
JIT_TIMEOUT_SEC = float(os.getenv("JIT_TIMEOUT_SEC", "20"))
JIT_RETRY_COUNT = int(os.getenv("JIT_RETRY_COUNT", "1"))   # 기본 1회 재시도

_ENTRY_ACTIONS = frozenset({"entry_long", "entry_short", "add_position"})


class JITAdvisoryClient:
    """openclaw /v1/responses 동기 호출 클라이언트."""

    def __init__(
        self,
        url: str = JIT_ADVISORY_URL,
        token: str = JIT_ADVISORY_TOKEN,
        timeout_sec: float = JIT_TIMEOUT_SEC,
        retry_count: int = JIT_RETRY_COUNT,
    ) -> None:
        self._url = url
        self._token = token
        self._timeout_sec = timeout_sec
        self._retry_count = retry_count

    async def request(self, req: JITAdvisoryRequest) -> Optional[JITAdvisoryResponse]:
        """JIT 자문 요청.

        Returns:
            JITAdvisoryResponse — 정상 응답
            None — 타임아웃/실패/파싱 오류 (fail-soft: 호출자가 NO_GO 처리)
        """
        if not self._token:
            logger.warning("[JIT] JIT_ADVISORY_TOKEN 미설정 — JIT 자문 스킵")
            return None

        payload = {
            "model": "openclaw/rachel",
            "input": req.to_prompt(),
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
            # 격리 세션 사용 — main 채팅 컨텍스트 오염 방지
            "x-openclaw-session-key": f"jit:{req.request_id}",
        }

        start = time.monotonic()
        last_err: Exception | None = None

        for attempt in range(self._retry_count + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_sec) as client:
                    http_resp = await client.post(
                        self._url, json=payload, headers=headers
                    )
                    http_resp.raise_for_status()
                    raw = http_resp.json()
                    latency_ms = int((time.monotonic() - start) * 1000)
                    return self._parse_response(req.request_id, raw, latency_ms)

            except httpx.TimeoutException as e:
                last_err = e
                logger.warning(
                    f"[JIT] 타임아웃 (attempt={attempt + 1}/{self._retry_count + 1}, "
                    f"timeout={self._timeout_sec}s, pair={req.pair})"
                )
            except httpx.HTTPStatusError as e:
                last_err = e
                logger.error(
                    f"[JIT] HTTP 오류 {e.response.status_code} — {e.response.text[:200]}"
                )
                break   # 서버 오류는 재시도 무의미
            except Exception as e:
                last_err = e
                logger.error(f"[JIT] 예상치 못한 오류 (attempt={attempt + 1}): {e}")

        logger.error(f"[JIT] 자문 실패 — fail-soft NO_GO 적용. 마지막 오류: {last_err}")
        return None

    def _parse_response(
        self,
        request_id: str,
        raw: dict,
        latency_ms: int,
    ) -> Optional[JITAdvisoryResponse]:
        """/v1/responses 응답에서 agent 출력 텍스트를 추출 후 JSON 파싱."""
        try:
            # OpenResponses 응답 구조:
            # {"output": [{"content": [{"type": "output_text", "text": "..."}]}]}
            text = raw["output"][0]["content"][0]["text"]
            model = raw.get("model", "")
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"[JIT] 응답 구조 오류: {e} | raw={str(raw)[:200]}")
            return None

        # agent가 JSON 앞에 마크다운 fence를 붙였을 경우 제거
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(
                l for l in lines if not l.startswith("```")
            ).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"[JIT] JSON 파싱 실패: {e} | text={text[:300]}")
            return None

        decision = data.get("decision", "")
        if decision not in ("GO", "NO_GO", "ADJUST"):
            logger.error(f"[JIT] 잘못된 decision 값: {decision!r}")
            return None

        reasoning = data.get("reasoning", "")
        if len(reasoning) < 10:
            logger.warning(f"[JIT] reasoning이 짧음: {reasoning!r}")

        confidence = float(data.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))

        adjusted_size = data.get("adjusted_size_pct")
        if adjusted_size is not None:
            adjusted_size = float(adjusted_size)
            if not (0.0 < adjusted_size <= 1.0):
                logger.error(f"[JIT] adjusted_size_pct 범위 오류: {adjusted_size}")
                return None

        adjusted_action = data.get("adjusted_action")
        if adjusted_action is not None and adjusted_action not in _ENTRY_ACTIONS:
            logger.error(f"[JIT] 잘못된 adjusted_action: {adjusted_action!r}")
            adjusted_action = None

        resp = JITAdvisoryResponse(
            request_id=request_id,
            decision=decision,
            adjusted_size_pct=adjusted_size,
            adjusted_stop_loss=data.get("adjusted_stop_loss"),
            adjusted_take_profit=data.get("adjusted_take_profit"),
            adjusted_action=adjusted_action,
            confidence=confidence,
            reasoning=reasoning,
            risk_factors=list(data.get("risk_factors", [])),
            latency_ms=latency_ms,
            model=model,
        )

        logger.info(
            f"[JIT] 파싱 완료 → decision={resp.decision}, "
            f"confidence={resp.confidence:.2f}, latency={latency_ms}ms"
        )
        return resp
