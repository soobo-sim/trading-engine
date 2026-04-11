"""
core/monitoring/event_detector.py 단위 테스트 — EventDetector.

DataHub / httpx 호출을 Mock으로 제어하여 외부 의존 없이 검증한다.

테스트 케이스:
  - 가격 급변 감지 → advisory POST
  - 가격 변화 미충족 → advisory POST 없음
  - 센티먼트 급변 감지
  - S/A급 이벤트 임박 감지
  - 쿨다운 중 재감지 방지
  - advisory POST 실패 시 WARNING만 (태스크 계속)
  - pairs 없으면 start() 스킵
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.exchange.types import Ticker
from core.monitoring.event_detector import EventDetector, _DETECTION_COOLDOWN_SEC


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

_NOW = datetime(2026, 4, 11, 9, 0, 0, tzinfo=timezone.utc)


def _make_detector(
    pairs: list[str] | None = None,
    price_change_pct: float = 2.0,
    sentiment_delta_pct: float = 30.0,
    event_advance_min: int = 5,
) -> EventDetector:
    data_hub = MagicMock()
    # 기본적으로 None 반환 (감지 없음)
    data_hub.get_ticker = AsyncMock(return_value=None)
    data_hub.get_sentiment = AsyncMock(return_value=None)
    data_hub.get_upcoming_events = AsyncMock(return_value=[])

    detector = EventDetector(
        data_hub=data_hub,
        advisory_base_url="http://localhost:8001",
        exchange="bitflyer",
        pairs=pairs or ["BTC_JPY"],
        settings={
            "price_change_pct": price_change_pct,
            "sentiment_delta_pct": sentiment_delta_pct,
            "event_advance_min": event_advance_min,
        },
    )
    return detector


def _make_ticker(last_price: float):
    return SimpleNamespace(last=last_price)


def _make_sentiment(score: int, classification: str = "neutral"):
    return SimpleNamespace(score=score, classification=classification)


# ──────────────────────────────────────────────────────────────
# 가격 급변 테스트
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_price_spike_detected_above_threshold():
    """
    Given: 이전 가격 10,000,000, 현재 10,250,000 (+2.5% > 임계 2.0%)
    When:  _poll_once()
    Then:  price_spike 감지 → detection list에 1개
    """
    det = _make_detector()
    det._data_hub.get_ticker = AsyncMock(return_value=_make_ticker(10_000_000.0))
    # 초기화: 이전 가격 설정
    await det._poll_once()

    det._data_hub.get_ticker = AsyncMock(return_value=_make_ticker(10_250_000.0))
    # 쿨다운 초기화 (이전 _poll_once가 쿨다운 설정했을 수 있음)
    det._last_detected.clear()

    detections = await det._poll_once()
    price_spikes = [d for d in detections if d["type"] == "price_spike"]
    assert len(price_spikes) == 1
    assert price_spikes[0]["change_pct"] == pytest.approx(2.5, rel=0.01)


@pytest.mark.asyncio
async def test_price_spike_not_detected_under_threshold():
    """
    Given: 이전 가격 10,000,000, 현재 10,100,000 (+1.0% < 임계 2.0%)
    When:  _poll_once()
    Then:  price_spike 감지 없음
    """
    det = _make_detector()
    det._data_hub.get_ticker = AsyncMock(return_value=_make_ticker(10_000_000.0))
    await det._poll_once()  # 초기 가격 기록

    det._data_hub.get_ticker = AsyncMock(return_value=_make_ticker(10_100_000.0))
    det._last_detected.clear()

    detections = await det._poll_once()
    price_spikes = [d for d in detections if d["type"] == "price_spike"]
    assert len(price_spikes) == 0


@pytest.mark.asyncio
async def test_price_spike_cooldown_prevents_duplicate():
    """
    Given: 가격 급변 감지 후 쿨다운 적용됨
    When:  다시 _poll_once() (동일 pair)
    Then:  감지 없음 (쿨다운 중)
    """
    import time
    det = _make_detector()
    # 이전 가격 설정
    det._prev_prices["BTC_JPY"] = 10_000_000.0
    det._data_hub.get_ticker = AsyncMock(return_value=_make_ticker(10_250_000.0))

    # 첫 감지
    detections = await det._poll_once()
    assert any(d["type"] == "price_spike" for d in detections)

    # 쿨다운 중 재시도 (last_detected 그대로)
    det._data_hub.get_ticker = AsyncMock(return_value=_make_ticker(10_500_000.0))
    detections2 = await det._poll_once()
    assert not any(d["type"] == "price_spike" for d in detections2)


# ──────────────────────────────────────────────────────────────
# 센티먼트 급변 테스트
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sentiment_shift_detected():
    """
    Given: 이전 sentiment=40, 현재=80 → Δ=40pt > 임계 30pt
    When:  _poll_once()
    Then:  sentiment_shift 감지
    """
    det = _make_detector()
    det._prev_sentiment_score = 40  # 이전 값 설정
    det._data_hub.get_sentiment = AsyncMock(return_value=_make_sentiment(80, "bullish"))

    detections = await det._poll_once()
    sentiment_shifts = [d for d in detections if d["type"] == "sentiment_shift"]
    assert len(sentiment_shifts) == 1
    assert sentiment_shifts[0]["delta_abs"] == pytest.approx(40)


@pytest.mark.asyncio
async def test_sentiment_no_shift_when_under_threshold():
    """
    Given: 이전=50, 현재=65 → Δ=15pt < 임계 30pt
    When:  _poll_once()
    Then:  감지 없음
    """
    det = _make_detector()
    det._prev_sentiment_score = 50
    det._data_hub.get_sentiment = AsyncMock(return_value=_make_sentiment(65, "neutral"))

    detections = await det._poll_once()
    assert not any(d["type"] == "sentiment_shift" for d in detections)


# ──────────────────────────────────────────────────────────────
# 경제 이벤트 테스트
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_imminent_detected():
    """
    Given: High 이벤트가 3분 후 도래 (임계 5분 이내)
    When:  _poll_once()
    Then:  event_imminent 감지
    """
    det = _make_detector()
    # 실제 현재 시각 기준 +3분 (timezone-aware)
    event_time = datetime.now(timezone.utc) + timedelta(minutes=3)
    event = SimpleNamespace(
        importance="High",
        name="FOMC 금리 결정",
        datetime_jst=event_time,
        currency="USD",
    )
    det._data_hub.get_upcoming_events = AsyncMock(return_value=[event])

    detections = await det._poll_once()
    event_detections = [d for d in detections if d["type"] == "event_imminent"]
    assert len(event_detections) == 1
    assert event_detections[0]["event_name"] == "FOMC 금리 결정"


@pytest.mark.asyncio
async def test_low_priority_event_not_detected():
    """
    Given: Low 이벤트가 2분 후 도래
    When:  _poll_once()
    Then:  감지 없음 (High 이벤트만 감지)
    """
    det = _make_detector()
    event_time = datetime.now(timezone.utc) + timedelta(minutes=2)
    event = SimpleNamespace(
        importance="Low",
        name="소비자물가지수",
        datetime_jst=event_time,
        currency="JPY",
    )
    det._data_hub.get_upcoming_events = AsyncMock(return_value=[event])

    detections = await det._poll_once()
    assert not any(d["type"] == "event_imminent" for d in detections)


# ──────────────────────────────────────────────────────────────
# advisory POST 테스트
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_advisory_posted_on_detection():
    """
    Given: 가격 급변 감지
    When:  _handle_detections() 호출
    Then:  httpx POST /api/advisories 호출
    """
    det = _make_detector()
    detection = {
        "type": "price_spike",
        "pair": "BTC_JPY",
        "change_pct": 3.0,
        "direction": "상승",
        "current_price": 10_300_000.0,
        "prev_price": 10_000_000.0,
        "detail": "BTC_JPY 상승 3.0%",
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_client.post = AsyncMock(return_value=mock_resp)

        await det._handle_detections([detection])
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        # URL에 /api/advisories 포함 확인
        assert "/api/advisories" in call_kwargs[0][0]


@pytest.mark.asyncio
async def test_advisory_post_failure_does_not_raise():
    """
    Given: httpx.post가 예외 발생
    When:  _handle_detections()
    Then:  예외 전파 없음 (WARNING 로그만)
    """
    det = _make_detector()
    detection = {
        "type": "price_spike",
        "pair": "BTC_JPY",
        "change_pct": 3.0,
        "direction": "상승",
        "current_price": 10_300_000.0,
        "prev_price": 10_000_000.0,
        "detail": "BTC_JPY 상승 3.0%",
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post = AsyncMock(side_effect=Exception("network error"))

        # 예외 없이 정상 종료해야 한다
        await det._handle_detections([detection])


# ──────────────────────────────────────────────────────────────
# 시작/종료 테스트
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_skip_when_no_pairs():
    """
    Given: pairs=[]
    When:  start()
    Then:  태스크 생성 안 함 (skip)
    """
    det = EventDetector(
        data_hub=MagicMock(),
        advisory_base_url="http://localhost:8001",
        exchange="bitflyer",
        pairs=[],
    )
    await det.start()
    assert det._task is None


# ──────────────────────────────────────────────────────────────
# 엣지케이스 보강
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_price_spike_ticker_returns_none_no_detection():
    """
    Given: ticker.get_ticker() → None (데이터 없음)
    When:  _poll_once()
    Then:  price_spike 감지 없음 (graceful skip)
    """
    det = _make_detector()
    det._data_hub.get_ticker = AsyncMock(return_value=None)
    detections = await det._poll_once()
    assert not any(d["type"] == "price_spike" for d in detections)


@pytest.mark.asyncio
async def test_price_spike_first_call_no_prev_no_detection():
    """
    Given: 최초 호출 (이전 가격 없음) → prev_price = None
    When:  _poll_once()
    Then:  price_spike 감지 없음 (초기화만)
    """
    det = _make_detector()
    det._data_hub.get_ticker = AsyncMock(return_value=_make_ticker(10_000_000.0))
    # 쿨다운 없이 최초 호출
    detections = await det._poll_once()
    assert not any(d["type"] == "price_spike" for d in detections)
    # 이전 가격이 기록되었는지 확인
    assert det._prev_prices.get("BTC_JPY") == 10_000_000.0


@pytest.mark.asyncio
async def test_sentiment_get_raises_no_detection():
    """
    Given: get_sentiment()가 예외 발생
    When:  _poll_once()
    Then:  sentiment_shift 감지 없음 (graceful skip)
    """
    det = _make_detector()
    det._data_hub.get_sentiment = AsyncMock(side_effect=Exception("API error"))
    detections = await det._poll_once()
    assert not any(d["type"] == "sentiment_shift" for d in detections)


@pytest.mark.asyncio
async def test_event_in_past_not_detected():
    """
    Given: High 이벤트가 이미 지나간 시각 (과거)
    When:  _poll_once()
    Then:  event_imminent 감지 없음 (time_until < 0)
    """
    det = _make_detector()
    past_event_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    event = SimpleNamespace(
        importance="High",
        name="FOMC",
        datetime_jst=past_event_time,
        currency="USD",
    )
    det._data_hub.get_upcoming_events = AsyncMock(return_value=[event])
    detections = await det._poll_once()
    assert not any(d["type"] == "event_imminent" for d in detections)


@pytest.mark.asyncio
async def test_telegram_notifier_called_on_detection():
    """
    Given: 가격 급변 감지 + telegram_notifier 설정됨
    When:  _handle_detections()
    Then:  telegram_notifier 호출됨
    """
    notifier = AsyncMock()
    det = EventDetector(
        data_hub=MagicMock(),
        advisory_base_url="http://localhost:8001",
        exchange="bitflyer",
        pairs=["BTC_JPY"],
        telegram_notifier=notifier,
    )
    detection = {
        "type": "price_spike",
        "pair": "BTC_JPY",
        "change_pct": 3.0,
        "direction": "상승",
        "current_price": 10_300_000.0,
        "prev_price": 10_000_000.0,
        "detail": "BTC_JPY 상승 3.0%",
    }
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_client.post = AsyncMock(return_value=mock_resp)
        await det._handle_detections([detection])

    notifier.assert_called_once()
    call_msg = notifier.call_args[0][0]
    assert "EventDetector" in call_msg


@pytest.mark.asyncio
async def test_sentiment_extreme_fear_detected():
    """
    TC7: score=8 (극단 공포)
    → sentiment_extreme 타입 감지 반환.
    """
    det = _make_detector()
    det._prev_sentiment_score = 15  # delta=7 < threshold=30 (급변은 아님, 극단치만 해당)
    extreme_sentiment = SimpleNamespace(score=8, classification="extreme_fear")
    det._data_hub.get_sentiment = AsyncMock(return_value=extreme_sentiment)

    detections = await det._poll_once()

    extreme_detections = [d for d in detections if d["type"] == "sentiment_extreme"]
    assert len(extreme_detections) == 1
    assert extreme_detections[0]["current_score"] == 8
    assert extreme_detections[0]["classification"] == "extreme_fear"


@pytest.mark.asyncio
async def test_sentiment_extreme_greed_detected():
    """
    TC8: score=92 (극단 탐욕)
    → sentiment_extreme 타입 감지 반환.
    """
    det = _make_detector()
    det._prev_sentiment_score = 85  # delta=7 < threshold=30
    extreme_sentiment = SimpleNamespace(score=92, classification="extreme_greed")
    det._data_hub.get_sentiment = AsyncMock(return_value=extreme_sentiment)

    detections = await det._poll_once()

    extreme_detections = [d for d in detections if d["type"] == "sentiment_extreme"]
    assert len(extreme_detections) == 1
    assert extreme_detections[0]["current_score"] == 92
    assert extreme_detections[0]["classification"] == "extreme_greed"


@pytest.mark.asyncio
async def test_sentiment_extreme_boundary_score_10():
    """
    엣지: score=10 (경계값 ≤10)
    → sentiment_extreme 감지 (10은 포함).
    """
    det = _make_detector()
    det._prev_sentiment_score = 12
    det._data_hub.get_sentiment = AsyncMock(
        return_value=SimpleNamespace(score=10, classification="extreme_fear")
    )
    detections = await det._poll_once()
    assert any(d["type"] == "sentiment_extreme" for d in detections)


@pytest.mark.asyncio
async def test_sentiment_extreme_boundary_score_90():
    """
    엣지: score=90 (경계값 ≥90)
    → sentiment_extreme 감지 (90은 포함).
    """
    det = _make_detector()
    det._prev_sentiment_score = 88
    det._data_hub.get_sentiment = AsyncMock(
        return_value=SimpleNamespace(score=90, classification="extreme_greed")
    )
    detections = await det._poll_once()
    assert any(d["type"] == "sentiment_extreme" for d in detections)


@pytest.mark.asyncio
async def test_sentiment_not_extreme_at_11():
    """
    엣지: score=11 (경계값 초과) → sentiment_extreme 감지 없음.
    """
    det = _make_detector()
    det._prev_sentiment_score = 12
    det._data_hub.get_sentiment = AsyncMock(
        return_value=SimpleNamespace(score=11, classification="fear")
    )
    detections = await det._poll_once()
    assert not any(d["type"] == "sentiment_extreme" for d in detections)


@pytest.mark.asyncio
async def test_sentiment_extreme_cooldown_no_duplicate():
    """
    엣지: sentiment_extreme 쿨다운 중 재감지
    → 두 번째 poll에서 sentiment_extreme 없음 (중복 알림 방지).
    """
    import time

    det = _make_detector()
    det._prev_sentiment_score = 15
    det._data_hub.get_sentiment = AsyncMock(
        return_value=SimpleNamespace(score=8, classification="extreme_fear")
    )

    # 첫 번째 감지
    det._last_detected["sentiment_shift"] = time.monotonic()  # shift 쿨다운도 강제
    detections1 = await det._poll_once()
    assert any(d["type"] == "sentiment_extreme" for d in detections1)

    # 두 번째 — 쿨다운 중
    detections2 = await det._poll_once()
    assert not any(d["type"] == "sentiment_extreme" for d in detections2)


# ──────────────────────────────────────────────────────────────
# ticker.last 접근 경로 검증 (BUG-FIX 회귀 방지)
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_price_spike_with_real_ticker_dataclass():
    """
    실제 Ticker dataclass 객체로 ticker.last 접근 검증.
    (BUG: ticker.last_price 잘못 참조 → float(Ticker) TypeError 재발 방지)
    """
    from datetime import datetime, timezone
    det = _make_detector()
    real_ticker = Ticker(
        pair="BTC_JPY",
        last=10_000_000.0,
        bid=9_999_000.0,
        ask=10_001_000.0,
        high=10_100_000.0,
        low=9_900_000.0,
        volume=100.0,
        timestamp=datetime.now(timezone.utc),
    )
    det._data_hub.get_ticker = AsyncMock(return_value=real_ticker)
    # 최초 호출 — 이전 가격 기록
    await det._poll_once()
    assert det._prev_prices.get("BTC_JPY") == pytest.approx(10_000_000.0)

    # 급변
    real_ticker2 = Ticker(
        pair="BTC_JPY",
        last=10_300_000.0,  # +3% > threshold 2%
        bid=10_299_000.0,
        ask=10_301_000.0,
        high=10_400_000.0,
        low=10_000_000.0,
        volume=120.0,
        timestamp=datetime.now(timezone.utc),
    )
    det._data_hub.get_ticker = AsyncMock(return_value=real_ticker2)
    det._last_detected.clear()
    detections = await det._poll_once()
    assert any(d["type"] == "price_spike" for d in detections)


@pytest.mark.asyncio
async def test_price_spike_zero_prev_price_no_crash():
    """
    엣지: 이전 가격이 0.0인 경우 — ZeroDivisionError 없이 스킵.
    """
    det = _make_detector()
    det._prev_prices["BTC_JPY"] = 0.0
    det._data_hub.get_ticker = AsyncMock(return_value=_make_ticker(10_000_000.0))
    # 예외 없이 실행되어야 함
    detections = await det._poll_once()
    # 0으로 나누면 inf → detection 없음 or 감지 (구현에 따라 다름, crash 없음이 핵심)
    assert isinstance(detections, list)


@pytest.mark.asyncio
async def test_price_spike_multiple_pairs_only_one_spikes():
    """
    GMO FX 패턴: pairs=['GBP_JPY', 'USD_JPY']에서 USD_JPY만 급변.
    → detection list에 USD_JPY price_spike 1개만.
    """
    from unittest.mock import AsyncMock
    from types import SimpleNamespace

    det = EventDetector(
        data_hub=MagicMock(),
        advisory_base_url="http://localhost:8003",
        exchange="gmofx",
        pairs=["GBP_JPY", "USD_JPY"],
    )
    det._data_hub.get_sentiment = AsyncMock(return_value=None)
    det._data_hub.get_upcoming_events = AsyncMock(return_value=[])

    # 이전 가격 설정
    det._prev_prices["GBP_JPY"] = 190.0
    det._prev_prices["USD_JPY"] = 150.0

    async def mock_ticker(pair: str):
        if pair == "USD_JPY":
            return SimpleNamespace(last=154.0)   # +2.67% > 2% threshold
        return SimpleNamespace(last=190.5)        # GBP_JPY: +0.26%, no spike

    det._data_hub.get_ticker = mock_ticker

    detections = await det._poll_once()
    price_spikes = [d for d in detections if d["type"] == "price_spike"]
    assert len(price_spikes) == 1
    assert price_spikes[0]["pair"] == "USD_JPY"
