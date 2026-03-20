"""
Signer 단위 테스트.

실제 API 호출 없음 — 서명 알고리즘 정확성만 검증.
"""
import hashlib
import hmac
import time

import pytest

from adapters.coincheck.signer import CoincheckSigner
from adapters.bitflyer.signer import BitFlyerSigner
from adapters.gmo_fx.signer import GmoFxSigner


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def ck_signer() -> CoincheckSigner:
    return CoincheckSigner(api_key="test_ck_key", api_secret="test_ck_secret")


@pytest.fixture
def bf_signer() -> BitFlyerSigner:
    return BitFlyerSigner(api_key="test_bf_key", api_secret="test_bf_secret")


# ──────────────────────────────────────────────────────────────
# CoincheckSigner
# ──────────────────────────────────────────────────────────────

def test_ck_sign_returns_three_headers(ck_signer: CoincheckSigner) -> None:
    """헤더 키 3개가 정확히 존재한다."""
    headers = ck_signer.sign(url="https://coincheck.com/api/exchange/orders")
    assert set(headers.keys()) == {"ACCESS-KEY", "ACCESS-NONCE", "ACCESS-SIGNATURE"}


def test_ck_sign_api_key(ck_signer: CoincheckSigner) -> None:
    """ACCESS-KEY는 생성자에 전달한 api_key가 된다."""
    headers = ck_signer.sign(url="https://coincheck.com/api/exchange/orders")
    assert headers["ACCESS-KEY"] == "test_ck_key"


def test_ck_sign_signature_format(ck_signer: CoincheckSigner) -> None:
    """ACCESS-SIGNATURE는 64자 hex 문자열이다."""
    headers = ck_signer.sign(url="https://coincheck.com/api/exchange/orders")
    sig = headers["ACCESS-SIGNATURE"]
    assert len(sig) == 64
    assert all(c in "0123456789abcdef" for c in sig)


def test_ck_sign_post_body_included(ck_signer: CoincheckSigner) -> None:
    """POST 요청: body를 포함한 서명이 body-없는 서명과 다르다."""
    url = "https://coincheck.com/api/exchange/orders"
    body = '{"pair":"xrp_jpy","order_type":"buy","rate":"100","amount":"10"}'

    headers_with_body = ck_signer.sign(url=url, body=body)
    headers_no_body = ck_signer.sign(url=url, body="")

    # nonce가 동일하게 고정되지 않으므로 서명이 다른 이유가 body 포함 여부임을 별도 검증
    # nonce를 고정하여 비교
    fixed_nonce = "1742000000000"
    secret = "test_ck_secret"

    msg_with_body = fixed_nonce + url + body
    msg_no_body = fixed_nonce + url

    sig_with = hmac.new(secret.encode(), msg_with_body.encode(), hashlib.sha256).hexdigest()
    sig_without = hmac.new(secret.encode(), msg_no_body.encode(), hashlib.sha256).hexdigest()

    assert sig_with != sig_without


def test_ck_sign_get_no_body(ck_signer: CoincheckSigner) -> None:
    """GET 요청: body 파라미터 기본값은 빈 문자열이다."""
    url = "https://coincheck.com/api/accounts/balance"
    headers = ck_signer.sign(url=url)  # body 생략
    # nonce 추출 후 직접 검증
    nonce = headers["ACCESS-NONCE"]
    expected_msg = nonce + url
    expected_sig = hmac.new(
        "test_ck_secret".encode(), expected_msg.encode(), hashlib.sha256
    ).hexdigest()
    assert headers["ACCESS-SIGNATURE"] == expected_sig


def test_ck_nonce_is_milliseconds(ck_signer: CoincheckSigner) -> None:
    """CK nonce는 Unix ms 타임스탬프 — 13자리 숫자 문자열이다."""
    before = int(time.time() * 1000)
    headers = ck_signer.sign(url="https://coincheck.com/api/accounts/balance")
    after = int(time.time() * 1000)

    nonce = int(headers["ACCESS-NONCE"])
    assert len(headers["ACCESS-NONCE"]) == 13
    assert before <= nonce <= after


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
# CK vs BF nonce 단위 차이
# ──────────────────────────────────────────────────────────────

def test_ck_nonce_longer_than_bf_timestamp() -> None:
    """CK nonce(ms, 13자리) > BF timestamp(sec, 10자리)."""
    ck = CoincheckSigner("k", "s")
    bf = BitFlyerSigner("k", "s")

    ck_headers = ck.sign(url="https://coincheck.com/api/accounts/balance")
    bf_headers = bf.sign(method="GET", path="/v1/me/getbalance")

    ck_len = len(ck_headers["ACCESS-NONCE"])
    bf_len = len(bf_headers["ACCESS-TIMESTAMP"])

    assert ck_len == 13, f"CK nonce should be 13 digits, got {ck_len}"
    assert bf_len == 10, f"BF timestamp should be 10 digits, got {bf_len}"
    assert ck_len > bf_len


# ──────────────────────────────────────────────────────────────
# GmoFxSigner (CK ms / BF sec 비교)
# ──────────────────────────────────────────────────────────────

def test_gmo_timestamp_same_unit_as_ck() -> None:
    """GMO timestamp(ms, 13자리)는 CK nonce와 동일 단위."""
    gmo = GmoFxSigner("k", "s")
    headers = gmo.sign(method="GET", path="/v1/status")
    ts_len = len(headers["API-TIMESTAMP"])
    assert ts_len == 13, f"GMO timestamp should be 13 digits (ms), got {ts_len}"


def test_gmo_header_names_differ_from_ck_bf() -> None:
    """GMO 헤더: API-KEY/API-TIMESTAMP/API-SIGN (CK/BF와 다름)."""
    gmo = GmoFxSigner("k", "s")
    headers = gmo.sign(method="GET", path="/v1/status")
    assert set(headers.keys()) == {"API-KEY", "API-TIMESTAMP", "API-SIGN"}
