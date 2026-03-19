"""
BitFlyerSigner — BitFlyer REST API HMAC-SHA256 서명.

서명 대상: timestamp + METHOD + path + body
timestamp: Unix timestamp 초 (str)
헤더: ACCESS-KEY, ACCESS-TIMESTAMP, ACCESS-SIGN  (≠ ACCESS-SIGNATURE)
"""
import hashlib
import hmac
import time


class BitFlyerSigner:
    """BitFlyer API 요청 서명."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret

    def sign(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """
        인증 헤더 생성.

        Args:
            method: HTTP 메서드 대문자 (예: "GET", "POST")
            path:   경로 + 쿼리스트링 (host 제외, 예: /v1/me/getbalance)
            body:   JSON 바디 문자열 (GET 요청은 빈 문자열)

        Returns:
            {"ACCESS-KEY": ..., "ACCESS-TIMESTAMP": ..., "ACCESS-SIGN": ...}
        """
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path + body
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "ACCESS-KEY": self._api_key,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-SIGN": signature,
        }
