"""
Signer 단위 테스트.

실제 API 호출 없음 — 서명 알고리즘 정확성만 검증.
"""
import hashlib
import hmac
import time

import pytest

from adapters.bitflyer.signer import BitFlyerSigner
from adapters.gmo_fx.signer import GmoFxSigner


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def bf_signer() -> BitFlyerSigner:
    return BitFlyerSigner(api_key="test_bf_key", api_secret="test_bf_secret")


# ──────────────────────────────────────────────────────────────
# BitFlyerSigner
# ──────────────────────────────────────────────────────────────

def test_bf_sign_returns_three_headers(bf_signer: BitFlyerSigner) -> None:
    """헤더 키 3개가 정확히 존재한다 (ACCESS-SIGN, SIGNATURE 아님)."""
    headers = bf_signer.sign(method="GET", path="/v1/me/getbalance")
    assert set(headers.keys()) == {"ACCESS-KEY", "ACCESS-TIMESTAMP", "ACCESS-SIGN"}


def test_bf_sign_no_access_signature_key(bf_signer: BitFlyerSigner) -> None:
    """BF 서명 헤더는 ACCESS-SIGNATURE가 아닌 ACCESS-SIGN이다."""
    headers = bf_signer.sign(method="GET", path="/v1/me/getbalance")
    assert "ACCESS-SIGNATURE" not in headers


def test_bf_sign_post_body(bf_signer: BitFlyerSigner) -> None:
    """POST 요청: body 포함 서명을 직접 재현할 수 있다."""
    path = "/v1/me/sendchildorder"
    body = '{"product_code":"XRP_JPY","child_order_type":"LIMIT","side":"BUY","size":10,"price":100}'
    headers = bf_signer.sign(method="POST", path=path, body=body)

    # 동일 timestamp로 서명 재현
    ts = headers["ACCESS-TIMESTAMP"]
    expected_msg = ts + "POST" + path + body
    expected_sig = hmac.new(
        "test_bf_secret".encode(), expected_msg.encode(), hashlib.sha256
    ).hexdigest()
    assert headers["ACCESS-SIGN"] == expected_sig


def test_bf_sign_get_with_query_string(bf_signer: BitFlyerSigner) -> None:
    """GET 쿼리스트링 포함 path가 서명 message에 반영된다."""
    path = "/v1/me/getchildorders?product_code=XRP_JPY&child_order_state=ACTIVE"
    headers = bf_signer.sign(method="GET", path=path)

    ts = headers["ACCESS-TIMESTAMP"]
    expected_msg = ts + "GET" + path
    expected_sig = hmac.new(
        "test_bf_secret".encode(), expected_msg.encode(), hashlib.sha256
    ).hexdigest()
    assert headers["ACCESS-SIGN"] == expected_sig


def test_bf_nonce_is_seconds(bf_signer: BitFlyerSigner) -> None:
    """BF timestamp는 Unix 초 — 10자리 숫자 문자열이다 (CK ms 13자리와 다름)."""
    before = int(time.time())
    headers = bf_signer.sign(method="GET", path="/v1/me/getbalance")
    after = int(time.time())

    ts = int(headers["ACCESS-TIMESTAMP"])
    assert len(headers["ACCESS-TIMESTAMP"]) == 10
    assert before <= ts <= after


# ──────────────────────────────────────────────────────────────
# GmoFxSigner
# ──────────────────────────────────────────────────────────────

def test_gmo_timestamp_same_unit_as_bf_ms() -> None:
    """GMO timestamp(ms, 13자리)은 BF sec(10자리)보다 길다."""
    gmo = GmoFxSigner("k", "s")
    headers = gmo.sign(method="GET", path="/v1/status")
    ts_len = len(headers["API-TIMESTAMP"])
    assert ts_len == 13, f"GMO timestamp should be 13 digits (ms), got {ts_len}"


def test_gmo_header_names_differ_from_bf() -> None:
    """GMO 헤더: API-KEY/API-TIMESTAMP/API-SIGN (BF와 다름)."""
    gmo = GmoFxSigner("k", "s")
    headers = gmo.sign(method="GET", path="/v1/status")
    assert set(headers.keys()) == {"API-KEY", "API-TIMESTAMP", "API-SIGN"}
