"""
CoincheckSigner — Coincheck REST API HMAC-SHA256 서명.

서명 대상: nonce + full_url + body
nonce: Unix timestamp 밀리초 (str)
헤더: ACCESS-KEY, ACCESS-NONCE, ACCESS-SIGNATURE
"""
import hashlib
import hmac
import time


class CoincheckSigner:
    """Coincheck API 요청 서명."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret

    def sign(self, url: str, body: str = "") -> dict[str, str]:
        """
        인증 헤더 생성.

        Args:
            url:  전체 URL (예: https://coincheck.com/api/exchange/orders?pair=xrp_jpy)
            body: JSON 바디 문자열 (GET 요청은 빈 문자열)

        Returns:
            {"ACCESS-KEY": ..., "ACCESS-NONCE": ..., "ACCESS-SIGNATURE": ...}
        """
        nonce = str(int(time.time() * 1000))
        message = nonce + url + body
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "ACCESS-KEY": self._api_key,
            "ACCESS-NONCE": nonce,
            "ACCESS-SIGNATURE": signature,
        }
