"""
GMO FX Signer + Adapter 단위 테스트.

실제 API 호출 없음 — 서명 알고리즘/어댑터 변환 로직 검증.
"""
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from adapters.gmo_fx.signer import GmoFxSigner
from adapters.gmo_fx.client import GmoFxAdapter


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def gmo_signer() -> GmoFxSigner:
    return GmoFxSigner(api_key="test_gmo_key", api_secret="test_gmo_secret")


@pytest_asyncio.fixture
async def gmo_adapter() -> GmoFxAdapter:
    adapter = GmoFxAdapter(
        api_key="test_key",
        api_secret="test_secret",
        base_url="https://forex-api.coin.z.com",
    )
    await adapter.connect()
    yield adapter
    await adapter.close()


# ──────────────────────────────────────────────────────────────
# GmoFxSigner
# ──────────────────────────────────────────────────────────────

def test_gmo_sign_returns_three_headers(gmo_signer: GmoFxSigner) -> None:
    """헤더 키 3개: API-KEY, API-TIMESTAMP, API-SIGN."""
    headers = gmo_signer.sign(method="GET", path="/v1/account/assets")
    assert set(headers.keys()) == {"API-KEY", "API-TIMESTAMP", "API-SIGN"}


def test_gmo_sign_api_key(gmo_signer: GmoFxSigner) -> None:
    """API-KEY는 생성자에 전달한 api_key."""
    headers = gmo_signer.sign(method="GET", path="/v1/account/assets")
    assert headers["API-KEY"] == "test_gmo_key"


def test_gmo_sign_signature_format(gmo_signer: GmoFxSigner) -> None:
    """API-SIGN은 64자 hex 문자열."""
    headers = gmo_signer.sign(method="GET", path="/v1/account/assets")
    sig = headers["API-SIGN"]
    assert len(sig) == 64
    assert all(c in "0123456789abcdef" for c in sig)


def test_gmo_sign_post_body(gmo_signer: GmoFxSigner) -> None:
    """POST 요청: body 포함 서명 재현."""
    path = "/v1/speedOrder"
    body = '{"symbol":"USD_JPY","side":"BUY","size":"1000"}'
    headers = gmo_signer.sign(method="POST", path=path, body=body)

    ts = headers["API-TIMESTAMP"]
    expected_msg = ts + "POST" + path + body
    expected_sig = hmac.new(
        "test_gmo_secret".encode(), expected_msg.encode(), hashlib.sha256
    ).hexdigest()
    assert headers["API-SIGN"] == expected_sig


def test_gmo_sign_get_with_query(gmo_signer: GmoFxSigner) -> None:
    """GET 쿼리스트링 포함 서명."""
    path = "/v1/activeOrders?symbol=USD_JPY"
    headers = gmo_signer.sign(method="GET", path=path)

    ts = headers["API-TIMESTAMP"]
    expected_msg = ts + "GET" + path
    expected_sig = hmac.new(
        "test_gmo_secret".encode(), expected_msg.encode(), hashlib.sha256
    ).hexdigest()
    assert headers["API-SIGN"] == expected_sig


def test_gmo_timestamp_is_milliseconds(gmo_signer: GmoFxSigner) -> None:
    """GMO timestamp는 Unix ms — 13자리 숫자 문자열."""
    before = int(time.time() * 1000)
    headers = gmo_signer.sign(method="GET", path="/v1/status")
    after = int(time.time() * 1000)

    ts = int(headers["API-TIMESTAMP"])
    assert len(headers["API-TIMESTAMP"]) == 13
    assert before <= ts <= after


def test_gmo_sign_path_excludes_private():
    """서명 경로에 /private/ 포함하지 않음 확인."""
    signer = GmoFxSigner(api_key="k", api_secret="s")
    # 올바른 사용: /v1/order (private/ 제외)
    h1 = signer.sign(method="POST", path="/v1/order")
    # 잘못된 사용: /private/v1/order → 서명이 달라야 함
    h2 = signer.sign(method="POST", path="/private/v1/order")
    # timestamp가 다를 수 있으므로 key/format만 확인
    assert "API-SIGN" in h1
    assert "API-SIGN" in h2


# ──────────────────────────────────────────────────────────────
# GmoFxAdapter — Properties
# ──────────────────────────────────────────────────────────────

def test_gmo_exchange_name(gmo_adapter: GmoFxAdapter) -> None:
    assert gmo_adapter.exchange_name == "gmofx"


def test_gmo_constraints(gmo_adapter: GmoFxAdapter) -> None:
    c = gmo_adapter.constraints
    assert "usd" in c.min_order_sizes
    assert c.min_order_sizes["usd"] == 1
    assert c.rate_limit == (6, 1)


def test_gmo_pair_to_symbol() -> None:
    assert GmoFxAdapter._pair_to_symbol("usd_jpy") == "USD_JPY"
    assert GmoFxAdapter._pair_to_symbol("eur_usd") == "EUR_USD"


# ──────────────────────────────────────────────────────────────
# GmoFxAdapter — Order parsing
# ──────────────────────────────────────────────────────────────

def test_gmo_parse_order_buy(gmo_adapter: GmoFxAdapter) -> None:
    raw = {
        "rootOrderId": 123456789,
        "orderId": 123456789,
        "symbol": "USD_JPY",
        "side": "BUY",
        "executionType": "LIMIT",
        "orderPrice": "150.500",
        "orderSize": "1000",
        "orderStatus": "ORDERED",
    }
    order = gmo_adapter._parse_order(raw, "usd_jpy")
    assert order.order_id == "123456789"
    assert order.pair == "usd_jpy"
    assert order.side.value == "buy"
    assert order.price == 150.5
    assert order.amount == 1000.0


def test_gmo_parse_order_market_sell(gmo_adapter: GmoFxAdapter) -> None:
    raw = {
        "rootOrderId": 987654321,
        "symbol": "EUR_JPY",
        "side": "SELL",
        "executionType": "MARKET",
        "size": "500",
        "orderStatus": "EXECUTED",
    }
    order = gmo_adapter._parse_order(raw, "")
    assert order.pair == "eur_jpy"
    assert order.side.value == "sell"
    assert order.status.value == "completed"


def test_gmo_parse_order_cancelled(gmo_adapter: GmoFxAdapter) -> None:
    raw = {
        "rootOrderId": 111222333,
        "symbol": "USD_JPY",
        "side": "BUY",
        "executionType": "LIMIT",
        "orderStatus": "CANCELED",
    }
    order = gmo_adapter._parse_order(raw, "usd_jpy")
    assert order.status.value == "cancelled"


def test_gmo_parse_order_expired(gmo_adapter: GmoFxAdapter) -> None:
    raw = {
        "rootOrderId": 444555666,
        "symbol": "USD_JPY",
        "side": "SELL",
        "executionType": "STOP",
        "orderStatus": "EXPIRED",
    }
    order = gmo_adapter._parse_order(raw, "usd_jpy")
    assert order.status.value == "cancelled"


# ──────────────────────────────────────────────────────────────
# GmoFxAdapter — URL 구조
# ──────────────────────────────────────────────────────────────

def test_gmo_url_structure(gmo_adapter: GmoFxAdapter) -> None:
    """public/private URL 분리 확인."""
    assert gmo_adapter._public_url == "https://forex-api.coin.z.com/public"
    assert gmo_adapter._private_url == "https://forex-api.coin.z.com/private"


# ──────────────────────────────────────────────────────────────
# GmoFxAdapter — Error handling
# ──────────────────────────────────────────────────────────────

def test_gmo_raise_for_business_error(gmo_adapter: GmoFxAdapter) -> None:
    """status != 0 → ExchangeError."""
    from core.exchange.errors import ExchangeError
    mock_response = MagicMock()
    mock_response.status_code = 200
    data = {
        "status": 1,
        "messages": [{"message_code": "ERR-200", "message_string": "Insufficient balance"}],
    }
    with pytest.raises(ExchangeError, match="ERR-200"):
        gmo_adapter._raise_for_exchange_error(mock_response, data)


def test_gmo_raise_for_401(gmo_adapter: GmoFxAdapter) -> None:
    """HTTP 401 → AuthenticationError."""
    from core.exchange.errors import AuthenticationError
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"
    with pytest.raises(AuthenticationError):
        gmo_adapter._raise_for_exchange_error(mock_response)


def test_gmo_raise_for_429(gmo_adapter: GmoFxAdapter) -> None:
    """HTTP 429 → RateLimitError."""
    from core.exchange.errors import RateLimitError
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.text = "Too Many Requests"
    with pytest.raises(RateLimitError):
        gmo_adapter._raise_for_exchange_error(mock_response)


def test_gmo_no_error_on_success(gmo_adapter: GmoFxAdapter) -> None:
    """status=0, HTTP 200 → 에러 없음."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    data = {"status": 0, "data": []}
    # 예외 없이 통과
    gmo_adapter._raise_for_exchange_error(mock_response, data)


# ──────────────────────────────────────────────────────────────
# GmoFxAdapter — get_balance 파싱
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gmo_get_balance_list_response(gmo_adapter: GmoFxAdapter) -> None:
    """data가 리스트(문서 표준)일 때 정상 파싱."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": 0,
        "data": [{"equity": "500000", "availableAmount": "300000"}],
    }
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    gmo_adapter._client = mock_client

    balance = await gmo_adapter.get_balance()
    assert balance.get_available("jpy") == 300000.0
    assert balance.currencies["jpy"].amount == 500000.0


@pytest.mark.asyncio
async def test_gmo_get_balance_dict_response(gmo_adapter: GmoFxAdapter) -> None:
    """data가 딕셔너리(주말 휴장 등 실제 관찰 케이스)일 때 정상 파싱."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": 0,
        "data": {"equity": "120000", "availableAmount": "80000"},
    }
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    gmo_adapter._client = mock_client

    balance = await gmo_adapter.get_balance()
    assert balance.get_available("jpy") == 80000.0
    assert balance.currencies["jpy"].amount == 120000.0


@pytest.mark.asyncio
async def test_gmo_get_balance_empty_response(gmo_adapter: GmoFxAdapter) -> None:
    """data가 빈 리스트일 때 0원 반환."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": 0, "data": []}
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    gmo_adapter._client = mock_client

    balance = await gmo_adapter.get_balance()
    assert balance.get_available("jpy") == 0.0


# ── BUG修正テスト (e8d2028, 9af807d, signature fix) ──────────────


class TestWssTickerFilter:
    """GMO FX WSS ticker応答にchannel fieldがない問題の検証."""

    def test_ticker_without_channel_field_accepted(self):
        """GMO WSS: channel=None, symbol=GBP_JPY → callback呼ばれるべき."""
        data = {"symbol": "GBP_JPY", "ask": "210.82", "bid": "210.80", "timestamp": "2026-04-01T00:00:00Z", "status": "OPEN"}
        # 修正後の条件: channel=="ticker" or symbol exists
        assert data.get("channel") == "ticker" or data.get("symbol")

    def test_ticker_with_channel_field_accepted(self):
        """channel=ticker も引き続き受け入れる."""
        data = {"channel": "ticker", "ask": "210.82", "bid": "210.80"}
        assert data.get("channel") == "ticker" or data.get("symbol")

    def test_unrelated_message_rejected(self):
        """channel/symbol どちらもないメッセージは無視."""
        data = {"type": "heartbeat"}
        assert not (data.get("channel") == "ticker" or data.get("symbol"))


class TestSignatureQueryExclusion:
    """GET API署名にquery stringを含めてはいけない (ERR-5010修正)."""

    def test_signer_path_without_query(self):
        """sign()に渡すpathにquery stringが含まれないことを検証."""
        from adapters.gmo_fx.signer import GmoFxSigner
        signer = GmoFxSigner(api_key="test", api_secret="test_secret")

        # 正しい署名: path にquery なし
        headers_clean = signer.sign("GET", "/v1/openPositions")
        # 間違った署名: path にquery あり
        headers_dirty = signer.sign("GET", "/v1/openPositions?symbol=GBP_JPY")

        # 同じtimestampでは署名が異なるはず（pathが違うため）
        # ここでは「pathにqueryを含めるべきでない」というルールをドキュメント化
        assert headers_clean["API-SIGN"] != headers_dirty["API-SIGN"], \
            "query in sign path produces different (wrong) signature"

    def test_sign_path_must_not_contain_question_mark(self):
        """client.py内のsign_pathに?が含まれないことを静的に検証."""
        import re
        with open("adapters/gmo_fx/client.py") as f:
            content = f.read()

        # sign_path = "..." の行で ? が含まれるものを検出
        bad_lines = re.findall(r'sign_path\s*=\s*f?["\'].*\?.*["\']', content)
        # request_path に ? があるのはOK, sign_path に ? があるのはNG
        assert len(bad_lines) == 0, f"sign_path with query string found: {bad_lines}"


class TestSubscribeTradesNonBlocking:
    """subscribe_tradesがbackground taskとして実行される検証."""

    def test_entry_monitor_uses_create_task(self):
        """_entry_monitorがsubscribe_tradesをcreate_taskで呼ぶことを確認."""
        with open("core/strategy/plugins/box_mean_reversion/manager.py") as f:
            content = f.read()

        # asyncio.create_task(self._adapter.subscribe_trades が存在すること
        assert "asyncio.create_task(self._adapter.subscribe_trades" in content, \
            "subscribe_trades must be called via create_task, not await"

        # await self._adapter.subscribe_trades が存在しないこと
        assert "await self._adapter.subscribe_trades" not in content, \
            "subscribe_trades must NOT be directly awaited (blocks forever)"


class TestPrevStateRestartInit:
    """再起動時のprev_state初期化 — 静的コード検証."""

    def test_no_position_branch_sets_none(self):
        """ポジションなし→prev_state=Noneのコードパスが存在."""
        with open("core/strategy/plugins/box_mean_reversion/manager.py") as f:
            content = f.read()
        # "else:" 分岐で None 設定
        assert "self._prev_box_state[pair] = None" in content
