"""
core/decision/llm_client.py 단위 테스트 — OpenAiLlmClient.

실제 OpenAI API를 호출하지 않고, httpx.AsyncClient를 mock으로 치환하여 검증한다.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.judge.decision.llm_client import LlmCallError, OpenAiLlmClient

_SCHEMA: dict = {"name": "test_schema", "strict": True, "schema": {"type": "object"}}
_SYSTEM = "system prompt"
_USER = "user prompt"


def _make_client() -> OpenAiLlmClient:
    return OpenAiLlmClient(api_key="test-key", default_model="gpt-4o-mini", timeout=5.0)


def _ok_response(data: dict) -> MagicMock:
    """200 정상 응답 mock."""
    content = json.dumps(data)
    body = {"choices": [{"message": {"content": content}}]}
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.text = content
    return resp


def _error_response(status_code: int, text: str = "error") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = {}
    return resp


# ── 정상 응답 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_returns_dict_on_200():
    """
    Given: OpenAI 200 정상 응답
    When:  client.chat() 호출
    Then:  schema dict 반환
    """
    client = _make_client()
    expected = {"action": "entry_long", "confidence": 0.72}
    mock_resp = _ok_response(expected)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_resp)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_http_client

        result = await client.chat(_SYSTEM, _USER, _SCHEMA)

    assert result == expected


@pytest.mark.asyncio
async def test_chat_uses_model_override():
    """
    Given: model="gpt-4o" 지정
    When:  client.chat(..., model="gpt-4o")
    Then:  POST payload에 model="gpt-4o" 포함
    """
    client = _make_client()
    expected = {"action": "hold"}
    mock_resp = _ok_response(expected)

    captured_payload = {}

    async def fake_post(url, json=None, headers=None):
        captured_payload.update(json or {})
        return mock_resp

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http_client = AsyncMock()
        mock_http_client.post = fake_post
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_http_client

        await client.chat(_SYSTEM, _USER, _SCHEMA, model="gpt-4o")

    assert captured_payload.get("model") == "gpt-4o"


# ── 에러 처리 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_500_raises_llm_call_error():
    """
    Given: OpenAI 500 에러
    When:  client.chat()
    Then:  LlmCallError raise
    """
    client = _make_client()
    mock_resp = _error_response(500, "internal server error")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_resp)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_http_client

        with pytest.raises(LlmCallError) as exc_info:
            await client.chat(_SYSTEM, _USER, _SCHEMA)

    assert "500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_timeout_raises_llm_call_error():
    """
    Given: httpx 타임아웃
    When:  client.chat()
    Then:  LlmCallError raise
    """
    client = _make_client()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(
            side_effect=httpx.TimeoutException("timeout")
        )
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_http_client

        with pytest.raises(LlmCallError) as exc_info:
            await client.chat(_SYSTEM, _USER, _SCHEMA)

    assert "타임아웃" in str(exc_info.value)


@pytest.mark.asyncio
async def test_429_retries_once_and_succeeds():
    """
    Given: 첫 호출 429, 두 번째 200
    When:  client.chat()
    Then:  재시도 후 dict 반환 (sleep ~1초)
    """
    client = _make_client()
    expected = {"action": "hold"}
    ok_resp = _ok_response(expected)
    rate_resp = _error_response(429, "Too Many Requests")

    call_count = 0

    async def fake_post(url, json=None, headers=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return rate_resp
        return ok_resp

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http_client = AsyncMock()
        mock_http_client.post = fake_post
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_http_client

        # sleep 패치해서 실제 대기 없이 테스트
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client.chat(_SYSTEM, _USER, _SCHEMA)

    assert result == expected
    assert call_count == 2


@pytest.mark.asyncio
async def test_invalid_content_json_raises_llm_call_error():
    """
    Given: content 필드가 유효하지 않은 JSON
    When:  client.chat()
    Then:  LlmCallError raise
    """
    client = _make_client()
    body = {"choices": [{"message": {"content": "not-json-{"}}]}
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.text = "not-json-{"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=resp)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_http_client

        with pytest.raises(LlmCallError):
            await client.chat(_SYSTEM, _USER, _SCHEMA)


# ── 추가 엣지케이스 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_choices_key_raises_llm_call_error():
    """
    Given: 응답 body에 'choices' 키 없음
    When:  client.chat()
    Then:  LlmCallError raise (구조 파싱 실패)
    """
    client = _make_client()
    body = {"error": "bad response"}
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = body
    resp.text = json.dumps(body)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=resp)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_http_client

        with pytest.raises(LlmCallError) as exc_info:
            await client.chat(_SYSTEM, _USER, _SCHEMA)

    assert "구조 오류" in str(exc_info.value)


@pytest.mark.asyncio
async def test_429_retry_also_fails_raises_llm_call_error():
    """
    Given: 첫 호출 429, 두 번째도 429
    When:  client.chat()
    Then:  LlmCallError raise (재시도 후 비-200 → 오류)
    """
    client = _make_client()
    rate_resp = _error_response(429, "Too Many Requests")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=rate_resp)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_http_client

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(LlmCallError) as exc_info:
                await client.chat(_SYSTEM, _USER, _SCHEMA)

    assert "429" in str(exc_info.value)


@pytest.mark.asyncio
async def test_400_client_error_raises_llm_call_error():
    """
    Given: OpenAI 400 Bad Request (스키마 오류 등)
    When:  client.chat()
    Then:  LlmCallError raise
    """
    client = _make_client()
    mock_resp = _error_response(400, "invalid schema")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_resp)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_http_client

        with pytest.raises(LlmCallError) as exc_info:
            await client.chat(_SYSTEM, _USER, _SCHEMA)

    assert "400" in str(exc_info.value)
