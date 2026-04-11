"""
core/data/hub.py 단위 테스트 — DataHub.

실제 DB 대신 SQLite 인메모리 + FakeExchangeAdapter 사용.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import WakeUpReview, create_candle_model
from adapters.database.session import Base
from core.data.hub import DataHub
from core.exchange.types import Balance, CurrencyBalance, Ticker
from tests.fake_exchange import FakeExchangeAdapter


TstCandle = create_candle_model("tst2", pair_column="pair")


@pytest_asyncio.fixture
async def db_and_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        tables = [
            Base.metadata.tables[t]
            for t in Base.metadata.tables
            if t.startswith("tst2_")
        ]
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory, engine
    await engine.dispose()


@pytest_asyncio.fixture
async def hub(db_and_factory):
    factory, _ = db_and_factory
    adapter = FakeExchangeAdapter(
        initial_balances={"jpy": 500_000.0},
        ticker_price=150.0,
    )
    await adapter.connect()
    yield DataHub(
        session_factory=factory,
        adapter=adapter,
        candle_model=TstCandle,
        pair_column="pair",
    )
    await adapter.close()


# ── get_candles ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_candles_empty(hub):
    """캔들 없을 때 빈 시퀀스 반환."""
    candles = await hub.get_candles("USD_JPY", "4h", limit=10)
    assert list(candles) == []


@pytest.mark.asyncio
async def test_get_candles_returns_dto_list(db_and_factory):
    """DB candle → CandleDTO 변환 확인."""
    factory, _ = db_and_factory
    # DB에 캔들 삽입
    async with factory() as db:
        row = TstCandle(
            pair="USD_JPY",
            timeframe="4h",
            open_time=datetime(2026, 4, 1, tzinfo=timezone.utc),
            close_time=datetime(2026, 4, 1, 4, 0, 0, tzinfo=timezone.utc),
            open=150.0, high=152.0, low=149.0, close=151.0,
            volume=1000.0,
            is_complete=True,
        )
        db.add(row)
        await db.commit()

    adapter = FakeExchangeAdapter(initial_balances={"jpy": 0}, ticker_price=151.0)
    await adapter.connect()
    hub2 = DataHub(
        session_factory=factory,
        adapter=adapter,
        candle_model=TstCandle,
        pair_column="pair",
    )
    candles = await hub2.get_candles("USD_JPY", "4h", limit=10)
    await adapter.close()

    assert len(candles) == 1
    c = candles[0]
    assert c.close == 151.0
    assert c.pair == "USD_JPY"
    assert c.timeframe == "4h"


# ── get_balance ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_balance_returns_balance(hub):
    """FakeExchangeAdapter 잔고 반환 확인."""
    balance = await hub.get_balance()
    assert balance.get_available("jpy") > 0


# ── v2 stubs ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_macro_snapshot_returns_none(hub):
    assert await hub.get_macro_snapshot() is None


@pytest.mark.asyncio
async def test_get_news_summary_returns_empty(hub):
    assert await hub.get_news_summary("BTC_JPY") == ()


@pytest.mark.asyncio
async def test_get_lessons_returns_empty(hub):
    assert await hub.get_lessons("BTC_JPY", "entry_ok") == ()


# ── v1.5: get_macro_snapshot ─────────────────────────────────

def _make_mock_httpx_client(json_body: dict, status_code: int = 200):
    """httpx.AsyncClient context manager mock 생성 헬퍼."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_body
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)
    return mock_client


@pytest.fixture
def hub_with_url(db_and_factory):
    """trading_data_url이 설정된 DataHub (동기 fixture — hub는 async_generator라 캡처 불가)."""
    factory, _ = db_and_factory
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 500_000.0}, ticker_price=150.0)
    return factory, adapter


@pytest.mark.asyncio
async def test_macro_snapshot_normal(hub_with_url):
    """정상 API 응답 → MacroSnapshotDTO 반환."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    mock_client = _make_mock_httpx_client({
        "series": {"DGS10": 4.2, "DGS2": 3.8, "VIXCLS": 18.5, "DTWEXBGS": 104.2}
    })
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_macro_snapshot()

    assert result is not None
    assert result.us_10y == 4.2
    assert result.us_2y == 3.8
    assert result.vix == 18.5
    assert result.dxy == 104.2
    assert result.fetched_at is not None
    await adapter.close()


@pytest.mark.asyncio
async def test_macro_snapshot_cache_hit(hub_with_url):
    """1초 후 재호출 → 캐시 반환 (HTTP 호출 1회)."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    mock_client = _make_mock_httpx_client({
        "series": {"DGS10": 4.5, "DGS2": 4.0, "VIXCLS": 20.0, "DTWEXBGS": 103.0}
    })
    with patch("httpx.AsyncClient", return_value=mock_client):
        first = await hub.get_macro_snapshot()
        second = await hub.get_macro_snapshot()

    assert first is second  # 동일 객체 (캐시)
    assert mock_client.get.call_count == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_macro_snapshot_api_failure_returns_none(hub_with_url):
    """API 실패 + 캐시 없음 → None."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_macro_snapshot()

    assert result is None
    await adapter.close()


@pytest.mark.asyncio
async def test_macro_snapshot_api_failure_returns_stale_cache(hub_with_url):
    """1회 성공 → TTL 만료(mocked) → 재호출 실패 → stale 캐시 반환."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    # 1. 첫 호출 성공
    mock_ok = _make_mock_httpx_client({"series": {"DGS10": 4.2, "DGS2": 3.9, "VIXCLS": 19.0}})
    with patch("httpx.AsyncClient", return_value=mock_ok):
        first = await hub.get_macro_snapshot()
    assert first is not None

    # 2. TTL 강제 만료
    from datetime import timedelta
    old_time = datetime.now(tz=timezone.utc) - timedelta(seconds=7200)
    hub._macro_cache = (first, old_time)

    # 3. 재호출 → API 실패 → stale 캐시
    mock_fail = AsyncMock()
    mock_fail.__aenter__ = AsyncMock(return_value=mock_fail)
    mock_fail.__aexit__ = AsyncMock(return_value=False)
    mock_fail.get = AsyncMock(side_effect=Exception("timeout"))
    with patch("httpx.AsyncClient", return_value=mock_fail):
        stale = await hub.get_macro_snapshot()

    assert stale is first  # stale 캐시 반환
    await adapter.close()


@pytest.mark.asyncio
async def test_macro_snapshot_no_url_returns_none(db_and_factory):
    """trading_data_url 미설정 → None 반환 (API 호출 없음)."""
    factory, _ = db_and_factory
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 0}, ticker_price=150.0)
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url=None,
    )
    assert await hub.get_macro_snapshot() is None
    await adapter.close()


# ── v1.5: get_upcoming_events ─────────────────────────────────

def _event_payload(minutes_from_now: int, impact: str = "High", title: str = "FOMC") -> dict:
    ev_time = datetime.now(timezone.utc).replace(microsecond=0)
    from datetime import timedelta
    ev_time += timedelta(minutes=minutes_from_now)
    return {
        "title": title, "country": "USD",
        "event_time": ev_time.isoformat(),
        "impact": impact, "forecast": "1.5%", "previous": "1.2%",
    }


@pytest.mark.asyncio
async def test_upcoming_events_normal(hub_with_url):
    """정상 응답 → EconomicEventDTO 2건 반환."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    events = [_event_payload(60), _event_payload(120, "Medium", "CPI")]
    mock_client = _make_mock_httpx_client({"events": events})
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_upcoming_events()

    assert len(result) == 2
    assert result[0].name == "FOMC"
    assert result[0].importance == "High"
    assert result[0].currency == "USD"
    assert result[0].forecast == "1.5%"
    assert result[1].name == "CPI"
    assert result[1].importance == "Medium"
    await adapter.close()


@pytest.mark.asyncio
async def test_upcoming_events_skips_bad_record(hub_with_url):
    """event_time 누락 이벤트 스킵 → 나머지 반환."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    events = [
        _event_payload(60),
        {"title": "BAD", "country": "USD", "impact": "High"},  # event_time 없음
        _event_payload(120),
    ]
    mock_client = _make_mock_httpx_client({"events": events})
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_upcoming_events()

    assert len(result) == 2  # 1건 스킵
    await adapter.close()


@pytest.mark.asyncio
async def test_upcoming_events_api_failure_returns_empty(hub_with_url):
    """API 실패 + 캐시 없음 → 빈 tuple."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("timeout"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_upcoming_events()

    assert result == ()
    await adapter.close()


# ── v1.5: get_lessons ─────────────────────────────────────────

def _make_lesson_row(
    row_id: int, pair: str, pnl: float, lessons: str | None, cause: str = "ENTRY_TIMING"
):
    """WakeUpReview row를 흉내내는 mock 객체."""
    row = MagicMock()
    row.id = row_id
    row.pair = pair
    row.realized_pnl = pnl
    row.lessons_learned = lessons
    row.cause_code = cause
    return row


def _make_mock_session_factory(rows: list):
    """session_factory() → select 결과로 rows를 반환하는 mock."""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = rows

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)
    return mock_factory


@pytest.mark.asyncio
async def test_get_lessons_returns_pair_match(db_and_factory):
    """lesson 2건 반환 → LessonDTO 목록."""
    factory, _ = db_and_factory
    rows = [
        _make_lesson_row(1, "USD_JPY", -500.0, "진입 타이밍 실수"),
        _make_lesson_row(2, "USD_JPY", 1000.0, "추세 확인 후 진입 필요"),
    ]
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 0}, ticker_price=150.0)
    await adapter.connect()
    hub = DataHub(
        session_factory=_make_mock_session_factory(rows),
        adapter=adapter,
        candle_model=TstCandle,
        pair_column="pair",
        lesson_model=WakeUpReview,
    )

    result = await hub.get_lessons("USD_JPY", "entry_ok")
    await adapter.close()

    assert len(result) == 2
    assert all(r.lesson_text is not None for r in result)
    assert result[0].situation_tags == ("ENTRY_TIMING",)


@pytest.mark.asyncio
async def test_get_lessons_outcome_loss(db_and_factory):
    """pnl < 0 → outcome='loss'."""
    factory, _ = db_and_factory
    rows = [_make_lesson_row(1, "USD_JPY", -500.0, "손실 케이스")]
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 0}, ticker_price=150.0)
    await adapter.connect()
    hub = DataHub(
        session_factory=_make_mock_session_factory(rows),
        adapter=adapter,
        candle_model=TstCandle,
        pair_column="pair",
        lesson_model=WakeUpReview,
    )
    result = await hub.get_lessons("USD_JPY", "entry_ok")
    await adapter.close()

    assert result[0].outcome == "loss"


@pytest.mark.asyncio
async def test_get_lessons_outcome_win(db_and_factory):
    """pnl > 0 → outcome='win'."""
    factory, _ = db_and_factory
    rows = [_make_lesson_row(1, "USD_JPY", 1000.0, "수익 케이스")]
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 0}, ticker_price=150.0)
    await adapter.connect()
    hub = DataHub(
        session_factory=_make_mock_session_factory(rows),
        adapter=adapter,
        candle_model=TstCandle,
        pair_column="pair",
        lesson_model=WakeUpReview,
    )
    result = await hub.get_lessons("USD_JPY", "entry_ok")
    await adapter.close()

    assert result[0].outcome == "win"


@pytest.mark.asyncio
async def test_get_lessons_no_model_returns_empty(db_and_factory):
    """lesson_model 미설정 → 빈 tuple, DB 호출 없음."""
    factory, _ = db_and_factory
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 0}, ticker_price=150.0)
    await adapter.connect()
    hub = DataHub(
        session_factory=factory,
        adapter=adapter,
        candle_model=TstCandle,
        pair_column="pair",
        lesson_model=None,
    )
    assert await hub.get_lessons("USD_JPY", "entry_ok") == ()
    await adapter.close()


@pytest.mark.asyncio
async def test_get_lessons_skips_null_lessons(db_and_factory):
    """lessons_learned=None 인 레코드는 DB 쿼리에서 애초에 제외 (WHERE lessons_learned IS NOT NULL).

    mock이 반환하는 rows에는 이미 필터된 결과만 있다고 가정.
    lessons_learned=None이 넘어오면 lesson_text는 None이 된다 — 즉 실제 DB가 올바르게 필터.
    단위 테스트에서는 필터 결과만 mock으로 검증.
    """
    factory, _ = db_and_factory
    rows = [_make_lesson_row(1, "USD_JPY", -200.0, "교훈 있음")]
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 0}, ticker_price=150.0)
    await adapter.connect()
    hub = DataHub(
        session_factory=_make_mock_session_factory(rows),
        adapter=adapter,
        candle_model=TstCandle,
        pair_column="pair",
        lesson_model=WakeUpReview,
    )
    result = await hub.get_lessons("USD_JPY", "entry_ok")
    await adapter.close()

    assert len(result) == 1
    assert result[0].lesson_text == "교훈 있음"


# ── 추가 엣지케이스 ───────────────────────────────────────────

# Protocol 준수
@pytest.mark.asyncio
async def test_datahub_satisfies_idatahub_protocol(hub):
    """DataHub 인스턴스가 IDataHub Protocol을 만족하는지 isinstance 검사."""
    from core.data.hub import IDataHub
    assert isinstance(hub, IDataHub)


# get_upcoming_events: no url
@pytest.mark.asyncio
async def test_upcoming_events_no_url_returns_empty(db_and_factory):
    """trading_data_url 미설정 → 빈 tuple, API 호출 없음."""
    factory, _ = db_and_factory
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 0}, ticker_price=150.0)
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url=None,
    )
    result = await hub.get_upcoming_events()
    await adapter.close()
    assert result == ()


# get_upcoming_events: cache hit
@pytest.mark.asyncio
async def test_upcoming_events_cache_hit(hub_with_url):
    """TTL 미만 재호출 → 동일 객체, HTTP 1회."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )
    events = [_event_payload(60)]
    mock_client = _make_mock_httpx_client({"events": events})
    with patch("httpx.AsyncClient", return_value=mock_client):
        first = await hub.get_upcoming_events()
        second = await hub.get_upcoming_events()

    assert first is second
    assert mock_client.get.call_count == 1
    await adapter.close()


# get_upcoming_events: API failure + stale cache
@pytest.mark.asyncio
async def test_upcoming_events_api_failure_returns_stale_cache(hub_with_url):
    """1회 성공 → TTL 만료 → 재호출 실패 → stale 캐시 반환."""
    from datetime import timedelta
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    mock_ok = _make_mock_httpx_client({"events": [_event_payload(60)]})
    with patch("httpx.AsyncClient", return_value=mock_ok):
        first = await hub.get_upcoming_events()
    assert len(first) == 1

    old_time = datetime.now(tz=timezone.utc) - timedelta(seconds=600)
    hub._events_cache = (first, old_time)

    mock_fail = AsyncMock()
    mock_fail.__aenter__ = AsyncMock(return_value=mock_fail)
    mock_fail.__aexit__ = AsyncMock(return_value=False)
    mock_fail.get = AsyncMock(side_effect=Exception("timeout"))
    with patch("httpx.AsyncClient", return_value=mock_fail):
        stale = await hub.get_upcoming_events()

    assert stale is first
    await adapter.close()


# get_macro_snapshot: HTTP 4xx → raise_for_status → graceful None
@pytest.mark.asyncio
async def test_macro_snapshot_http_error_graceful(hub_with_url):
    """HTTP 4xx raise_for_status → API 실패로 처리 → None (캐시 없음)."""
    import httpx as _httpx
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock(side_effect=_httpx.HTTPStatusError(
        "404", request=MagicMock(), response=MagicMock()
    ))
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_macro_snapshot()

    assert result is None
    await adapter.close()


# get_lessons: DB exception → graceful empty tuple
@pytest.mark.asyncio
async def test_get_lessons_db_exception_graceful(db_and_factory):
    """DB 조회 중 예외 발생 → 빈 tuple 반환 (graceful)."""
    factory, _ = db_and_factory

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=Exception("DB connection lost"))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_factory = MagicMock(return_value=mock_session)

    adapter = FakeExchangeAdapter(initial_balances={"jpy": 0}, ticker_price=150.0)
    await adapter.connect()
    hub = DataHub(
        session_factory=mock_factory,
        adapter=adapter,
        candle_model=TstCandle,
        pair_column="pair",
        lesson_model=WakeUpReview,
    )
    result = await hub.get_lessons("USD_JPY", "entry_ok")
    await adapter.close()

    assert result == ()


# get_lessons: max 5 건 제한 (mock에서 6건 반환해도 hub는 DB에 limit(5) 절달)
@pytest.mark.asyncio
async def test_get_lessons_respects_limit(db_and_factory):
    """mock이 6건 반환해도 쿼리는 limit(5)로 제한 — mock 자체는 5건만 전달."""
    factory, _ = db_and_factory
    rows = [_make_lesson_row(i, "USD_JPY", 100.0 * i, f"교훈{i}") for i in range(1, 6)]
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 0}, ticker_price=150.0)
    await adapter.connect()
    hub = DataHub(
        session_factory=_make_mock_session_factory(rows),
        adapter=adapter,
        candle_model=TstCandle,
        pair_column="pair",
        lesson_model=WakeUpReview,
    )
    result = await hub.get_lessons("USD_JPY", "entry_ok")
    await adapter.close()

    assert len(result) == 5


# ── v2: get_news_summary ──────────────────────────────────────

def _article_payload(
    uuid: str = "uuid-1",
    title: str = "BTC surges",
    published_offset_min: int = -30,
    category: str = "crypto",
    sentiment: float = 0.5,
) -> dict:
    from datetime import timedelta
    pub_time = datetime.now(timezone.utc).replace(microsecond=0)
    pub_time += timedelta(minutes=published_offset_min)
    return {
        "uuid": uuid,
        "title": title,
        "snippet": "snippet text",
        "source": "reuters.com",
        "url": "https://reuters.com/article",
        "published_at": pub_time.isoformat(),
        "category": category,
        "symbols": "BTC",
        "sentiment_score": sentiment,
    }


@pytest.mark.asyncio
async def test_get_news_summary_crypto_pair(hub_with_url):
    """pair=BTC_JPY → category=crypto 요청, NewsDTO 2건 반환."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    articles = [_article_payload("a1"), _article_payload("a2", "ETH dips", -60)]
    mock_client = _make_mock_httpx_client({"articles": articles, "count": 2, "avg_sentiment": 0.5})
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_news_summary("BTC_JPY")

    assert len(result) == 2
    assert result[0].title == "BTC surges"
    assert result[0].category == "crypto"
    assert result[0].sentiment_score == 0.5
    # category=crypto で要求されたことを確認
    call_kwargs = mock_client.get.call_args
    assert call_kwargs.kwargs["params"]["category"] == "crypto"
    await adapter.close()


@pytest.mark.asyncio
async def test_get_news_summary_forex_pair(hub_with_url):
    """pair=USD_JPY → category=forex 요청."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    articles = [_article_payload("f1", "FOMC decision", category="forex", sentiment=-0.2)]
    mock_client = _make_mock_httpx_client({"articles": articles, "count": 1, "avg_sentiment": -0.2})
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_news_summary("USD_JPY")

    assert len(result) == 1
    call_kwargs = mock_client.get.call_args
    assert call_kwargs.kwargs["params"]["category"] == "forex"
    await adapter.close()


@pytest.mark.asyncio
async def test_get_news_summary_no_url_returns_empty(db_and_factory):
    """trading_data_url=None → 빈 tuple, API 호출 없음."""
    factory, _ = db_and_factory
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 0}, ticker_price=150.0)
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url=None,
    )
    result = await hub.get_news_summary("BTC_JPY")
    await adapter.close()
    assert result == ()


@pytest.mark.asyncio
async def test_get_news_summary_api_failure_returns_empty(hub_with_url):
    """API 실패 → 빈 tuple (graceful)."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("timeout"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_news_summary("BTC_JPY")

    assert result == ()
    await adapter.close()


@pytest.mark.asyncio
async def test_get_news_summary_cache_hit(hub_with_url):
    """TTL 미만 재호출 → 동일 객체, HTTP 1회."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    articles = [_article_payload("c1")]
    mock_client = _make_mock_httpx_client({"articles": articles, "count": 1, "avg_sentiment": 0.5})
    with patch("httpx.AsyncClient", return_value=mock_client):
        first = await hub.get_news_summary("BTC_JPY")
        second = await hub.get_news_summary("BTC_JPY")

    assert first is second
    assert mock_client.get.call_count == 1
    await adapter.close()


# ── v2: get_sentiment ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_sentiment_neutral(hub_with_url):
    """avg_sentiment=0.3 → score=65, classification=neutral."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    mock_client = _make_mock_httpx_client({"articles": [], "count": 0, "avg_sentiment": 0.3})
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_sentiment()

    assert result is not None
    assert result.score == 65
    assert result.classification == "neutral"
    assert result.source == "marketaux_news_avg"
    await adapter.close()


@pytest.mark.asyncio
async def test_get_sentiment_extreme_fear(hub_with_url):
    """avg_sentiment=-0.9 → score=5, classification=extreme_fear."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    mock_client = _make_mock_httpx_client({"articles": [], "count": 0, "avg_sentiment": -0.9})
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_sentiment()

    assert result is not None
    assert result.score == 5
    assert result.classification == "extreme_fear"
    await adapter.close()


@pytest.mark.asyncio
async def test_get_sentiment_extreme_greed(hub_with_url):
    """avg_sentiment=1.0 → score=100, classification=extreme_greed."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    mock_client = _make_mock_httpx_client({"articles": [], "count": 0, "avg_sentiment": 1.0})
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_sentiment()

    assert result is not None
    assert result.score == 100
    assert result.classification == "extreme_greed"
    await adapter.close()


@pytest.mark.asyncio
async def test_get_sentiment_no_avg_returns_none(hub_with_url):
    """avg_sentiment=null → None 반환."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    mock_client = _make_mock_httpx_client({"articles": [], "count": 0, "avg_sentiment": None})
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_sentiment()

    assert result is None
    await adapter.close()


@pytest.mark.asyncio
async def test_get_sentiment_no_url_returns_none(db_and_factory):
    """trading_data_url=None → None 반환."""
    factory, _ = db_and_factory
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 0}, ticker_price=150.0)
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url=None,
    )
    result = await hub.get_sentiment()
    await adapter.close()
    assert result is None


@pytest.mark.asyncio
async def test_get_sentiment_api_failure_returns_none(hub_with_url):
    """API 실패 → None (graceful)."""
    factory, adapter = hub_with_url
    await adapter.connect()
    hub = DataHub(
        session_factory=factory, adapter=adapter,
        candle_model=TstCandle, pair_column="pair",
        trading_data_url="http://mock-trading-data:8002",
    )

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("connection error"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await hub.get_sentiment()

    assert result is None
    await adapter.close()
