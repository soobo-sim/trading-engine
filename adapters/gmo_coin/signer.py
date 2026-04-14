"""
GmoSigner — GMO API HMAC-SHA256 서명.

서명 대상: timestamp(ms) + METHOD + path + body
timestamp: Unix timestamp 밀리초 (str)
헤더: API-KEY, API-TIMESTAMP, API-SIGN

⚠️ 서명 경로 주의:
  실제 요청: POST https://api.coin.z.com/private/v1/order
  서명 대상 path: /v1/order  (private/ 제외)
"""
import hashlib
import hmac
import time


class GmoSigner:
    """GMO API 요청 서명."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret

    def sign(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """
        인증 헤더 생성.

        Args:
            method: HTTP 메서드 대문자 (예: "GET", "POST")
            path:   서명 경로 (예: /v1/account/assets). private/ 제외.
            body:   JSON 바디 문자열 (GET 요청은 빈 문자열)

        Returns:
            {"API-KEY": ..., "API-TIMESTAMP": ..., "API-SIGN": ...}
        """
        timestamp = str(int(time.time() * 1000))
        message = timestamp + method.upper() + path + body
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "API-KEY": self._api_key,
            "API-TIMESTAMP": timestamp,
            "API-SIGN": signature,
        }
