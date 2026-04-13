"""
Account API 단위 테스트.

P-01: GET /api/accounts/positions — 포지션 2건 반환
P-02: GET /api/accounts/positions — 포지션 없으면 빈 목록
P-03: GET /api/accounts/positions — get_positions 미구현 → 501
S-01: GET /api/accounts/positions/summary — 요약 반환
S-02: GET /api/accounts/positions/summary — 미구현 → 501
C-01: GET /api/accounts/collateral — 증거금 필드 반환
C-02: GET /api/accounts/collateral — 미구현 → 501
EX-01: GET /api/accounts/executions — 체결 이력 반환
EX-02: GET /api/accounts/executions — 미구현 → 501
EX-03: GmoCoinAdapter.get_latest_executions — sign_path 확인
B-01: GET /api/accounts/balance — 정상 반환 (기존 엔드포인트 회귀)
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.account import router


# ── helpers ──────────────────────────────────────────────────

def _make_state(adapter):
    state = MagicMock()
    state.adapter = adapter
    return state


def _build_client(adapter) -> TestClient:
    from api.dependencies import get_state

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_state] = lambda: _make_state(adapter)
    return TestClient(app)


def _fx_position(position_id=1, side="BUY", price=11000000.0, size=0.004, pnl=50.0):
    p = MagicMock()
    p.position_id = position_id
    p.side = side
    p.price = price
    p.size = size
    p.pnl = pnl
    p.leverage = 2.0
    p.open_date = datetime(2026, 4, 13, 0, 10, 0, tzinfo=timezone.utc)
    return p


def _collateral(collateral=100000.0, pnl=200.0, require=67000.0, keep=148.0):
    c = MagicMock()
    c.collateral = collateral
    c.open_position_pnl = pnl
    c.require_collateral = require
    c.keep_rate = keep
    return c


# ── P: positions ─────────────────────────────────────────────

def test_p01_positions_two_rows():
    """P-01: 포지션 2건 반환."""
    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.get_positions = AsyncMock(return_value=[
        _fx_position(283097336, "BUY", 11283341.0, 0.004, 76.0),
        _fx_position(283097340, "BUY", 11287524.0, 0.004, 59.0),
    ])

    resp = _build_client(adapter).get("/api/accounts/positions?symbol=BTC_JPY")
    assert resp.status_code == 200
    body = resp.json()
    assert body["exchange"] == "gmo_coin"
    assert body["symbol"] == "BTC_JPY"
    assert body["count"] == 2
    assert body["positions"][0]["position_id"] == 283097336
    assert body["positions"][0]["side"] == "BUY"
    assert body["positions"][1]["size"] == 0.004


def test_p02_positions_empty():
    """P-02: 포지션 없으면 count=0, positions=[]."""
    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.get_positions = AsyncMock(return_value=[])

    resp = _build_client(adapter).get("/api/accounts/positions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["positions"] == []


def test_p03_positions_not_supported():
    """P-03: get_positions 미구현 → 501."""
    adapter = MagicMock(spec=[])  # spec=[] → hasattr 전부 False
    adapter.exchange_name = "coincheck"

    resp = _build_client(adapter).get("/api/accounts/positions")
    assert resp.status_code == 501
    assert "지원하지 않습니다" in resp.json()["detail"]["error"]


# ── S: positions/summary ──────────────────────────────────────

def test_s01_position_summary_returned():
    """S-01: 포지션 요약 반환."""
    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.get_position_summary = AsyncMock(return_value={
        "list": [{"side": "BUY", "sumPositionQuantity": "0.012", "averagePositionRate": "11286678"}]
    })

    resp = _build_client(adapter).get("/api/accounts/positions/summary?symbol=BTC_JPY")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "BTC_JPY"
    assert "list" in body["summary"]


def test_s02_position_summary_not_supported():
    """S-02: get_position_summary 미구현 → 501."""
    adapter = MagicMock(spec=[])
    adapter.exchange_name = "coincheck"

    resp = _build_client(adapter).get("/api/accounts/positions/summary")
    assert resp.status_code == 501


# ── C: collateral ─────────────────────────────────────────────

def test_c01_collateral_fields():
    """C-01: 증거금 4개 필드 반환."""
    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.get_collateral = AsyncMock(return_value=_collateral(100184.0, 184.0, 67721.0, 147.9))

    resp = _build_client(adapter).get("/api/accounts/collateral")
    assert resp.status_code == 200
    body = resp.json()
    assert body["collateral"] == 100184.0
    assert body["open_position_pnl"] == 184.0
    assert body["require_collateral"] == 67721.0
    assert body["keep_rate"] == 147.9
    assert body["exchange"] == "gmo_coin"


def test_c02_collateral_not_supported():
    """C-02: get_collateral 미구현 → 501."""
    adapter = MagicMock(spec=[])
    adapter.exchange_name = "coincheck"

    resp = _build_client(adapter).get("/api/accounts/collateral")
    assert resp.status_code == 501


# ── EX: executions ────────────────────────────────────────────

def test_ex01_executions_returned():
    """EX-01: 약정 이력 반환."""
    raw = [
        {"executionId": "9001", "orderId": "8336459025", "side": "SELL",
         "settleType": "CLOSE", "size": "0.004", "price": "11301225", "lossGain": "71"},
        {"executionId": "9002", "orderId": "8336459025", "side": "SELL",
         "settleType": "CLOSE", "size": "0.004", "price": "11301225", "lossGain": "54"},
    ]
    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.get_latest_executions = AsyncMock(return_value=raw)

    resp = _build_client(adapter).get("/api/accounts/executions?symbol=BTC_JPY&count=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["executions"][0]["lossGain"] == "71"
    adapter.get_latest_executions.assert_awaited_once_with("BTC_JPY", 10)


def test_ex02_executions_not_supported():
    """EX-02: get_latest_executions 미구현 → 501."""
    adapter = MagicMock(spec=[])
    adapter.exchange_name = "bitflyer"

    resp = _build_client(adapter).get("/api/accounts/executions")
    assert resp.status_code == 501


def test_ex03_gmoc_get_latest_executions_sign_path():
    """EX-03: GmoCoinAdapter.get_latest_executions — sign_path 검증."""
    import importlib
    import sys
    # 실제 클라이언트를 import해서 메서드 존재 + sign_path 소스 확인
    import inspect
    import adapters.gmo_coin.client as m
    src = inspect.getsource(m.GmoCoinAdapter.get_latest_executions)
    assert "/v1/latestExecutions" in src
    assert "symbol" in src
    assert "count" in src


# ── B: balance regression ─────────────────────────────────────

def test_b01_balance_regression():
    """B-01: 기존 /api/accounts/balance 회귀."""
    cb = MagicMock()
    cb.currency = "jpy"
    cb.amount = 100000.0
    cb.available = 100000.0

    balance = MagicMock()
    balance.currencies = {"jpy": cb}

    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.get_balance = AsyncMock(return_value=balance)

    resp = _build_client(adapter).get("/api/accounts/balance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currencies"]["jpy"]["amount"] == 100000.0
