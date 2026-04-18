"""
api/routes/advisories.py 단위 테스트.

TestClient + SQLite in-memory로 실제 DB 왕복을 검증한다.

커버 항목:
  - POST /api/advisories → 201, DB 저장 확인
  - POST invalid action → 400 INVALID_ACTION
  - POST invalid regime → 400 INVALID_REGIME
  - POST reasoning 너무 짧음 → 422 (Pydantic validation)
  - POST confidence 범위 초과 → 422
  - POST size_pct 최대치 초과 → 422
  - GET /{pair}/latest → advisory 없음 404
  - GET /{pair}/latest → 미만료 advisory 반환, is_expired=False
  - GET /{pair}/latest → 만료 advisory + include_expired=false → 404
  - GET /{pair}/latest → 만료 advisory + include_expired=true → is_expired=True
  - GET exchange 격리 — 다른 exchange의 advisory는 반환 안 함
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import RachelAdvisory
from adapters.database.session import Base
from api.routes.advisories import router

# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_factory():
    """SQLite in-memory — rachel_advisories 테이블만 생성."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        target = [Base.metadata.tables["rachel_advisories"]]
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=target))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _build_client(db_factory, exchange: str = "bitflyer") -> TestClient:
    """TestClient 생성 — EXCHANGE 환경변수 + DB override."""
    from api.dependencies import get_db, get_state

    app = FastAPI()
    app.include_router(router)

    state = MagicMock()

    async def _override_state():
        return state

    async def _override_db():
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_state] = _override_state
    app.dependency_overrides[get_db] = _override_db

    # EXCHANGE 환경변수 설정 (저장 시 exchange 컬럼에 반영)
    os.environ["EXCHANGE"] = exchange
    client = TestClient(app, raise_server_exceptions=True)
    return client


_VALID_BODY = {
    "pair": "BTC_JPY",
    "action": "entry_long",
    "confidence": 0.70,
    "size_pct": 0.5,
    "stop_loss": 9700000.0,
    "regime": "trending",
    "reasoning": "EMA 상향 + RSI 52 + 매크로 금리 동결 기대로 롱 진입 유망",
    "ttl_hours": 5.0,
}


# ──────────────────────────────────────────────────────────────
# POST /api/advisories
# ──────────────────────────────────────────────────────────────


def test_post_advisory_success(db_factory):
    """
    Given: 유효한 요청 body
    When:  POST /api/advisories
    Then:  201 + advisory 필드 반환, is_expired=False
    """
    client = _build_client(db_factory)
    resp = client.post("/api/advisories", json=_VALID_BODY)

    assert resp.status_code == 201
    data = resp.json()
    assert data["action"] == "entry_long"
    assert data["exchange"] == "bitflyer"
    assert data["pair"] == "btc_jpy"  # normalize_pair: 저장 시 소문자 정규화
    assert data["confidence"] == pytest.approx(0.70)
    assert data["is_expired"] is False
    assert "id" in data


def test_post_advisory_invalid_action(db_factory):
    """
    Given: action이 허용 목록 외
    When:  POST /api/advisories
    Then:  400 INVALID_ACTION
    """
    client = _build_client(db_factory)
    body = {**_VALID_BODY, "action": "buy"}  # 유효하지 않은 action
    resp = client.post("/api/advisories", json=body)

    assert resp.status_code == 400
    assert resp.json()["detail"]["blocked_code"] == "INVALID_ACTION"


def test_post_advisory_invalid_regime(db_factory):
    """
    Given: regime이 허용 목록 외
    When:  POST /api/advisories
    Then:  400 INVALID_REGIME
    """
    client = _build_client(db_factory)
    body = {**_VALID_BODY, "regime": "volatile"}  # 유효하지 않은 regime
    resp = client.post("/api/advisories", json=body)

    assert resp.status_code == 400
    assert resp.json()["detail"]["blocked_code"] == "INVALID_REGIME"


def test_post_advisory_short_reasoning(db_factory):
    """
    Given: reasoning이 20자 미만 (Pydantic min_length 위반)
    When:  POST /api/advisories
    Then:  422 Unprocessable Entity
    """
    client = _build_client(db_factory)
    body = {**_VALID_BODY, "reasoning": "짧음"}
    resp = client.post("/api/advisories", json=body)

    assert resp.status_code == 422


def test_post_advisory_confidence_out_of_range(db_factory):
    """
    Given: confidence > 1.0
    When:  POST /api/advisories
    Then:  422 Unprocessable Entity
    """
    client = _build_client(db_factory)
    body = {**_VALID_BODY, "confidence": 1.5}
    resp = client.post("/api/advisories", json=body)

    assert resp.status_code == 422


def test_post_advisory_size_pct_exceeds_max(db_factory):
    """
    Given: size_pct = 0.90 (최대 0.80 초과)
    When:  POST /api/advisories
    Then:  422 Unprocessable Entity
    """
    client = _build_client(db_factory)
    body = {**_VALID_BODY, "size_pct": 0.90}
    resp = client.post("/api/advisories", json=body)

    assert resp.status_code == 422


def test_post_advisory_ttl_hours_exceeds_max(db_factory):
    """
    Given: ttl_hours = 72.0 (최대 48H 초과)
    When:  POST /api/advisories
    Then:  422 Unprocessable Entity
    """
    client = _build_client(db_factory)
    body = {**_VALID_BODY, "ttl_hours": 72.0}
    resp = client.post("/api/advisories", json=body)

    assert resp.status_code == 422


def test_post_advisory_hold_without_size_pct(db_factory):
    """
    Given: action=hold, size_pct 없음 (선택 필드)
    When:  POST /api/advisories
    Then:  201, size_pct=None
    """
    client = _build_client(db_factory)
    body = {
        "pair": "BTC_JPY",
        "action": "hold",
        "confidence": 0.45,
        "reasoning": "레인징 구간 — 뚜렷한 방향성 없음, 진입 보류",
    }
    resp = client.post("/api/advisories", json=body)

    assert resp.status_code == 201
    assert resp.json()["size_pct"] is None
    assert resp.json()["action"] == "hold"


# ──────────────────────────────────────────────────────────────
# GET /api/advisories/{pair}/latest
# ──────────────────────────────────────────────────────────────


def test_get_latest_no_advisory_returns_404(db_factory):
    """
    Given: DB에 advisory 없음
    When:  GET /api/advisories/BTC_JPY/latest
    Then:  404
    """
    client = _build_client(db_factory)
    resp = client.get("/api/advisories/BTC_JPY/latest")

    assert resp.status_code == 404


def test_get_latest_returns_unexpired_advisory(db_factory):
    """
    Given: 미만료 advisory가 DB에 있음
    When:  GET /api/advisories/BTC_JPY/latest
    Then:  200, is_expired=False, action 일치
    """
    client = _build_client(db_factory)
    # 먼저 advisory 저장
    post_resp = client.post("/api/advisories", json=_VALID_BODY)
    assert post_resp.status_code == 201

    get_resp = client.get("/api/advisories/BTC_JPY/latest")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["action"] == "entry_long"
    assert data["is_expired"] is False


@pytest.mark.asyncio
async def test_get_latest_expired_excluded_by_default(db_factory):
    """
    Given: advisory가 이미 만료됨 (expires_at을 과거로 직접 삽입)
    When:  GET /api/advisories/BTC_JPY/latest (include_expired=false, 기본값)
    Then:  404 (만료된 것은 제외)
    """
    # 만료된 advisory 직접 삽입
    async with db_factory() as session:
        row = RachelAdvisory(
            pair="btc_jpy",  # normalize_pair 표준: 소문자
            exchange="bitflyer",
            action="hold",
            confidence=0.5,
            reasoning="만료 테스트용 advisory 직접 삽입 (20자)",
            expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),  # 과거
        )
        session.add(row)
        await session.commit()

    client = _build_client(db_factory)
    resp = client.get("/api/advisories/BTC_JPY/latest")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_latest_include_expired_returns_expired(db_factory):
    """
    Given: 만료된 advisory가 DB에 있음
    When:  GET /api/advisories/BTC_JPY/latest?include_expired=true
    Then:  200, is_expired=True
    """
    async with db_factory() as session:
        row = RachelAdvisory(
            pair="btc_jpy",  # normalize_pair 표준: 소문자
            exchange="bitflyer",
            action="entry_long",
            confidence=0.7,
            reasoning="만료 포함 조회 테스트용 advisory 직접 삽입 (20자)",
            expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        session.add(row)
        await session.commit()

    client = _build_client(db_factory)
    resp = client.get("/api/advisories/BTC_JPY/latest?include_expired=true")

    assert resp.status_code == 200
    assert resp.json()["is_expired"] is True


def test_post_advisory_log_expires_in_jst(db_factory, caplog):
    """
    Given: 유효한 advisory POST 요청
    When:  POST /api/advisories
    Then:  로그 메시지의 expires 값이 JST (+09:00) 형식으로 출력되어야 함
    """
    import logging

    client = _build_client(db_factory)
    with caplog.at_level(logging.INFO, logger="api.routes.advisories"):
        resp = client.post("/api/advisories", json=_VALID_BODY)

    assert resp.status_code == 201
    log_msgs = [r.message for r in caplog.records if "저장 완료" in r.message]
    assert len(log_msgs) == 1, "저장 완료 로그가 1건이어야 함"
    assert "+09:00" in log_msgs[0], f"JST 시간대가 아님: {log_msgs[0]}"
    assert "+00:00" not in log_msgs[0], f"UTC 시간대 출력됨: {log_msgs[0]}"


@pytest.mark.asyncio
async def test_get_latest_exchange_isolation(db_factory):
    """
    Given: gmofx exchange로 advisory 저장됨
    When:  EXCHANGE=bitflyer 서버에서 GET /api/advisories/BTC_JPY/latest
    Then:  404 (다른 exchange advisory는 조회 불가)
    """
    # gmofx advisory 직접 삽입
    async with db_factory() as session:
        row = RachelAdvisory(
            pair="btc_jpy",  # normalize_pair 표준: 소문자
            exchange="gmofx",  # 다른 exchange
            action="entry_long",
            confidence=0.7,
            reasoning="GMO FX exchange 격리 테스트용 advisory (20자 이상)",
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
        session.add(row)
        await session.commit()

    # EXCHANGE=bitflyer 서버에서 조회
    client = _build_client(db_factory, exchange="bitflyer")
    resp = client.get("/api/advisories/BTC_JPY/latest")

    assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────
# hold_override_policy 엣지 케이스 — EC-01 ~ EC-04 (BUG-037)
# ──────────────────────────────────────────────────────────────


def test_ec01_post_hold_advisory_with_signal_entry_ok_policy(db_factory):
    """
    EC-01: action=hold, hold_override_policy=signal_entry_ok
    → 201 + 응답에 hold_override_policy="signal_entry_ok" 반영
    """
    client = _build_client(db_factory)
    body = {
        "pair": "btc_jpy",
        "action": "hold",
        "confidence": 0.65,
        "reasoning": "RSI 65 과매수로 홀드, 시그널 해소 시 진입 허용",
        "hold_override_policy": "signal_entry_ok",
        "ttl_hours": 5.0,
    }
    resp = client.post("/api/advisories", json=body)

    assert resp.status_code == 201
    data = resp.json()
    assert data["action"] == "hold"
    assert data["hold_override_policy"] == "signal_entry_ok"


def test_ec02_post_entry_long_advisory_override_policy_forced_none(db_factory):
    """
    EC-02: action=entry_long, hold_override_policy=signal_entry_ok 지정
    → hold 아닌 advisory이므로 DB에 "none" 강제 저장
    → 응답 hold_override_policy="none"
    """
    client = _build_client(db_factory)
    body = {
        **_VALID_BODY,
        "action": "entry_long",
        "hold_override_policy": "signal_entry_ok",  # 무의미, 강제 none
    }
    resp = client.post("/api/advisories", json=body)

    assert resp.status_code == 201
    data = resp.json()
    assert data["hold_override_policy"] == "none", (
        "entry_long advisory에서 hold_override_policy는 none으로 강제되어야 함"
    )


def test_ec03_post_advisory_invalid_hold_override_policy(db_factory):
    """
    EC-03: hold_override_policy에 허용 외 값
    → 400 INVALID_HOLD_OVERRIDE
    """
    client = _build_client(db_factory)
    body = {
        "pair": "btc_jpy",
        "action": "hold",
        "confidence": 0.5,
        "reasoning": "유효하지 않은 hold_override_policy 테스트 (20자 이상)",
        "hold_override_policy": "always_enter",  # 허용 외
    }
    resp = client.post("/api/advisories", json=body)

    assert resp.status_code == 400
    assert resp.json()["detail"]["blocked_code"] == "INVALID_HOLD_OVERRIDE"


def test_ec04_post_advisory_default_hold_override_policy_is_none(db_factory):
    """
    EC-04: hold_override_policy 미지정 (기본값)
    → 응답에 hold_override_policy="none" 포함
    """
    client = _build_client(db_factory)
    resp = client.post("/api/advisories", json=_VALID_BODY)

    assert resp.status_code == 201
    data = resp.json()
    assert "hold_override_policy" in data
    assert data["hold_override_policy"] == "none"
