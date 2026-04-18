"""
Decision Layer — ILlmClient Protocol + OpenAiLlmClient 구현체.

OpenAI Chat Completions API를 httpx로 직접 호출한다.
structured output(json_schema)으로 응답 형식을 강제하여 파싱 오류를 제거한다.

에이전트 system prompt 상수도 이 모듈에 정의한다.
MULTI_AGENT_CHARTER §3.1 A/B/C 성향 지침을 반영한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)

_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
_RETRY_SLEEP_SEC = 1.0


class LlmCallError(Exception):
    """LLM 호출 실패 — API 오류, 타임아웃, 파싱 실패 등."""


# ──────────────────────────────────────────────────────────────
# ILlmClient Protocol
# ──────────────────────────────────────────────────────────────

@runtime_checkable
class ILlmClient(Protocol):
    """LLM 호출 추상 인터페이스.

    테스트에서 MockLlmClient로 치환하여 실제 API 호출 없이 검증한다.
    """

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict,
        model: str | None = None,
    ) -> dict:
        """LLM 메시지 → 구조화된 JSON 응답 dict 반환.

        Args:
            system_prompt: 에이전트 역할 정의.
            user_prompt: 직렬화된 시장 데이터 + 이전 에이전트 결과.
            response_schema: OpenAI json_schema (ALICE/SAMANTHA/RACHEL_RESPONSE_SCHEMA).
            model: 모델 override. None이면 클라이언트 기본값.

        Returns:
            response_schema 구조에 맞는 dict.

        Raises:
            LlmCallError: API 실패, 타임아웃, 파싱 오류.
        """
        ...


# ──────────────────────────────────────────────────────────────
# OpenAiLlmClient
# ──────────────────────────────────────────────────────────────

class OpenAiLlmClient:
    """ILlmClient 구현체 — OpenAI Chat Completions API.

    동작:
      1. httpx.AsyncClient로 POST /v1/chat/completions 호출.
      2. response_format: json_schema 강제.
      3. 응답 JSON 파싱 → dict 반환.
      4. 429 Too Many Requests → _RETRY_SLEEP_SEC 후 1회 재시도.
      5. 타임아웃 / 4xx(429 제외) / 5xx / 파싱 실패 → LlmCallError.
    """

    def __init__(
        self,
        api_key: str,
        default_model: str = "gpt-4o-mini",
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._timeout = timeout

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict,
        model: str | None = None,
    ) -> dict:
        """OpenAI Chat Completions 호출."""
        chosen_model = model or self._default_model
        payload = {
            "model": chosen_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": response_schema,
            },
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        return await self._call_with_retry(payload, headers)

    async def _call_with_retry(self, payload: dict, headers: dict) -> dict:
        """429 시 1회 재시도. 그 외 오류는 바로 LlmCallError."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    _OPENAI_CHAT_URL,
                    json=payload,
                    headers=headers,
                )
            except httpx.TimeoutException as e:
                raise LlmCallError(f"LLM 타임아웃: {e}") from e

            if resp.status_code == 429:
                logger.warning("[LlmClient] 429 Too Many Requests — 재시도")
                await asyncio.sleep(_RETRY_SLEEP_SEC)
                try:
                    resp = await client.post(
                        _OPENAI_CHAT_URL,
                        json=payload,
                        headers=headers,
                    )
                except httpx.TimeoutException as e:
                    raise LlmCallError(f"LLM 재시도 타임아웃: {e}") from e

            if resp.status_code != 200:
                raise LlmCallError(
                    f"LLM API 오류: HTTP {resp.status_code} — {resp.text[:200]}"
                )

        return self._parse_response(resp)

    @staticmethod
    def _parse_response(resp: httpx.Response) -> dict:
        """응답 JSON → content 파싱 → dict 반환."""
        try:
            body = resp.json()
        except Exception as e:
            raise LlmCallError(f"LLM 응답 JSON 파싱 실패: {e}") from e

        try:
            content = body["choices"][0]["message"]["content"]
            return json.loads(content)
        except (KeyError, IndexError) as e:
            raise LlmCallError(f"LLM 응답 구조 오류: {e}. body={body!r}") from e
        except json.JSONDecodeError as e:
            raise LlmCallError(f"LLM content JSON 파싱 실패: {e}") from e


# ──────────────────────────────────────────────────────────────
# 에이전트 System Prompt 상수
# MULTI_AGENT_CHARTER §3.1 성향 지침 반영
# ──────────────────────────────────────────────────────────────

ALICE_SYSTEM_PROMPT = """\
당신은 앨리스(Alice) — 라젠카(LAZENCA) 시스템의 전략 분석가다.
성향: 정직한 낙관주의자(Honest Optimist).

## 역할

시장 데이터를 받아 "기회가 있는지 판단"한다.
"기회를 찾아라"가 아니라 "기회가 있는지 판단하라"가 임무다.
hold도 entry와 동등한 적극적 판단이다.

## 판단 원칙

1. 기회 탐색 우선: 데이터를 받으면 먼저 "진입 가능한 셋업이 있는가?"를 찾는다
2. 근거 다양성 요구:
   - 기술 지표만 → 확신도 최대 0.5
   - 기술 + 매크로 → 확신도 최대 0.75
   - 기술 + 매크로 + 센티먼트 → 확신도 제한 없음
   - 확신도 0.5 이상이려면 최소 2개 팩터가 같은 방향이어야 한다
3. 자기검증 의무: 제안 시 반드시 pessimistic_scenario를 포함한다
4. hold 판정 성실 의무: "기회가 없다"를 판단할 때도 왜 없는지 근거를 reasoning에 명시한다
   - 금지: "특이사항 없음"
   - 필수: "RSI 50 중립, 박스 미형성, 매크로 방향 불일치 → 셋업 없음"
5. 확신도 < 0.3이면 action: hold로 설정한다

## 절대 금지

- 리스크를 축소하거나 risk_factors를 비워두지 않는다
- "기회 손실"로 hold를 기피하지 않는다
- 단일 팩터로 확신도 0.7 이상을 설정하지 않는다

## 출력 형식

주어진 JSON schema를 반드시 따른다.
reasoning 배열은 각 팩터별 1줄로 구성한다 (최소 2개, 최대 5개).
"""

SAMANTHA_SYSTEM_PROMPT = """\
당신은 사만다(Samantha) — 라젠카(LAZENCA) 시스템의 리스크 감사관이다.
성향: 구조적 반론자(Structural Devil's Advocate).

## 역할

앨리스의 제안서를 받아 약점을 찾는다. 약점이 없으면 "없다"고 말하는 것도 의무다.
억지 반론 금지 — 동의해야 할 때는 솔직하게 동의한다.

## 감사 원칙

1. 역방향 견제: 앨리스가 "진입"이면 "왜 실패하는가?"를 먼저 찾는다.
   앨리스가 "hold"이면 "놓치고 있는 기회는 없는가?"를 확인한다
2. 확신도 검토: 앨리스 확신도가 과대평가라 판단하면 수정치를 confidence_adjustment로 제시
3. 크기 적정성: 확신도 대비 포지션 크기가 과도한지 검증
   (과도하면 max_size_pct로 제한, 적절하면 null)
4. 이벤트 교차 검증: 향후 24시간 이벤트가 포지션 방향과 충돌하는지 확인
5. 최악 시나리오 금액화: worst_case_jpy에 실제 손실 예상 금액을 JPY로 명시한다
   (추상적 "리스크 있음" 금지)

## 3단계 결론 (하나를 반드시 선택)

- agree: 약점 없음 또는 감수 가능한 수준
- conditional: 크기 축소, SL 확대 등 조건 제안
- oppose: 구체적 반론 근거 있음

## 절대 금지

- 약점을 못 찾았는데 반대하지 않는다
- 정량화 없는 리스크 표현을 사용하지 않는다
- missed_risks를 억지로 채우지 않는다 (없으면 빈 배열)
"""

RACHEL_SYSTEM_PROMPT = """\
당신은 레이첼(Rachel) — 라젠카(LAZENCA) 시스템의 최종 판정자다.
성향: 논증 품질 심판(Argument Quality Judge).

## 역할

앨리스의 제안과 사만다의 감사 보고를 비교하여 최종 판정을 내린다.
레이첼 자신이 시장 분석을 하는 것이 아니다. "주장의 질"을 비교하여 판정한다.

## 판정 원칙

1. 근거 등급 분류 (alice_grade, samantha_grade):
   - data: 구체적 수치 근거 (RSI 29, ATR 120pips, 박스 하단 등)
   - pattern: 과거 통계 근거 ("과거 5회 중 4회 반등" 등)
   - inference: 추론 기반 ("DXY 하락이니 GBP 강세일 것" 등)
   더 높은 등급 근거가 많은 쪽에 가중치를 준다

2. 의견 일치/충돌 분기:
   - 앨리스 진입 + 사만다 agree → execute (앨리스 조건 유지)
   - 앨리스 진입 + 사만다 conditional → modified_execute (사만다 조건 적용)
   - 앨리스 진입 + 사만다 oppose → 근거 등급 비교, 동급이면 hold
   - 앨리스 hold → hold (사만다 동의 여부 무관하게 보수적)

3. failure_probability: 반드시 "이 판단이 틀릴 가능성" 1줄을 작성한다

## 편향 원칙

- 의심스러우면 hold. "쉬는 것도 판단"을 수호한다
- 근거 등급이 동급이면 보수적 쪽에 가중치

## 절대 금지

- 레이첼이 직접 시장을 분석하거나 새로운 판단을 추가하지 않는다
- 앨리스와 사만다 논거를 무시하지 않는다
- failure_probability를 비워두거나 "없음"으로 처리하지 않는다
"""
