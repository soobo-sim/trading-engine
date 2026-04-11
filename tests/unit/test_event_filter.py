"""
EventFilter 유닛 테스트 (F-01 알파 팩터).
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from core.analysis.event_filter import EventFilter


_UTC = timezone.utc


def _make_event(
    title: str = "FOMC",
    country: str = "USD",
    impact: str = "High",
    offset_minutes: int = 120,  # 양수 = 미래, 음수 = 과거
) -> dict:
    """테스트용 이벤트 딕셔너리."""
    ev_time = datetime.now(_UTC) + timedelta(minutes=offset_minutes)
    return {
        "title": title,
        "country": country,
        "impact": impact,
        "event_time": ev_time.isoformat(),
        "forecast": None,
        "previous": None,
        "actual": None,
    }


@pytest.fixture
def event_filter() -> EventFilter:
    return EventFilter(trading_data_url="http://mock-coinmarket:8002")


# ── 이벤트 차단 판정 ────────────────────────────────────────

class TestIsEventBlackout:
    @pytest.mark.asyncio
    async def test_no_events_not_blocked(self, event_filter):
        """이벤트 없음 → 차단 안 함."""
        event_filter._cache["usd_jpy"] = ([], datetime.now(_UTC))
        blocked, reason = await event_filter.is_event_blackout("usd_jpy", {})
        assert blocked is False
        assert reason == ""

    @pytest.mark.asyncio
    async def test_high_event_within_blackout_blocks(self, event_filter):
        """High 이벤트 120분 전, blackout_hours=3 → 차단."""
        ev = _make_event(offset_minutes=120)  # 2시간 후
        event_filter._cache["usd_jpy"] = ([ev], datetime.now(_UTC))
        params = {"event_blackout_hours": 3}
        blocked, reason = await event_filter.is_event_blackout("usd_jpy", params)
        assert blocked is True
        assert "FOMC" in reason

    @pytest.mark.asyncio
    async def test_high_event_outside_blackout_allowed(self, event_filter):
        """High 이벤트 300분(5시간) 후, blackout_hours=4 → 허용."""
        ev = _make_event(offset_minutes=300)  # 5시간 후
        event_filter._cache["usd_jpy"] = ([ev], datetime.now(_UTC))
        params = {"event_blackout_hours": 4}
        blocked, reason = await event_filter.is_event_blackout("usd_jpy", params)
        assert blocked is False

    @pytest.mark.asyncio
    async def test_medium_event_within_medium_blackout(self, event_filter):
        """Medium 이벤트 60분 전, blackout_medium_hours=2 → 차단."""
        ev = _make_event(impact="Medium", offset_minutes=60)
        event_filter._cache["usd_jpy"] = ([ev], datetime.now(_UTC))
        params = {"event_blackout_medium_hours": 2}
        blocked, reason = await event_filter.is_event_blackout("usd_jpy", params)
        assert blocked is True

    @pytest.mark.asyncio
    async def test_low_event_never_blocks(self, event_filter):
        """Low 이벤트 10분 전 → 차단 없음."""
        ev = _make_event(impact="Low", offset_minutes=10)
        event_filter._cache["usd_jpy"] = ([ev], datetime.now(_UTC))
        blocked, _ = await event_filter.is_event_blackout("usd_jpy", {})
        assert blocked is False

    @pytest.mark.asyncio
    async def test_post_blackout_within_30min(self, event_filter):
        """발표 10분 후, post_blackout_minutes=30 → 차단."""
        ev = _make_event(offset_minutes=-10)  # 10분 전에 발표됨
        event_filter._cache["usd_jpy"] = ([ev], datetime.now(_UTC))
        params = {"event_post_blackout_minutes": 30}
        blocked, reason = await event_filter.is_event_blackout("usd_jpy", params)
        assert blocked is True
        assert "발표 직후" in reason

    @pytest.mark.asyncio
    async def test_post_blackout_expired(self, event_filter):
        """발표 60분 후, post_blackout_minutes=30 → 허용."""
        ev = _make_event(offset_minutes=-60)  # 60분 전에 발표됨
        event_filter._cache["usd_jpy"] = ([ev], datetime.now(_UTC))
        params = {"event_post_blackout_minutes": 30}
        blocked, _ = await event_filter.is_event_blackout("usd_jpy", params)
        assert blocked is False


# ── 캐시 동작 ──────────────────────────────────────────────

class TestCacheBehavior:
    @pytest.mark.asyncio
    async def test_cache_returns_fresh_data(self, event_filter):
        """캐시 유효 시간 내 → API 재호출 없음."""
        ev = _make_event()
        event_filter._cache["usd_jpy"] = ([ev], datetime.now(_UTC))
        # _fetch_events가 호출되지 않아야 함
        with patch.object(event_filter, "_fetch_events", new_callable=AsyncMock) as mock_fetch:
            events = await event_filter._get_events("usd_jpy")
            mock_fetch.assert_not_called()
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_cache_miss_fetches_api(self, event_filter):
        """캐시 없음 → API 호출."""
        ev = _make_event()
        with patch.object(event_filter, "_fetch_events", new_callable=AsyncMock, return_value=[ev]) as mock_fetch:
            events = await event_filter._get_events("usd_jpy")
            mock_fetch.assert_called_once()
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_api_failure_returns_empty_graceful(self, event_filter):
        """API 호출 실패 → 빈 리스트 반환 (캐시 없음), 예외 미전파."""
        with patch.object(event_filter, "_fetch_events", new_callable=AsyncMock, side_effect=Exception("connection error")):
            events = await event_filter._get_events("usd_jpy")
        assert events == []

    @pytest.mark.asyncio
    async def test_api_failure_uses_stale_cache(self, event_filter):
        """API 실패 + 만료 캐시 있음 → 만료 캐시 사용 (graceful degradation)."""
        from datetime import timedelta
        ev = _make_event()
        stale_time = datetime.now(_UTC) - timedelta(seconds=400)  # TTL(300) 초과
        event_filter._cache["usd_jpy"] = ([ev], stale_time)

        with patch.object(event_filter, "_fetch_events", new_callable=AsyncMock, side_effect=Exception("net err")):
            events = await event_filter._get_events("usd_jpy")
        # 만료됐지만 graceful → 기존 캐시 사용
        assert len(events) == 1


# ── 스탑 타이트닝 계수 ─────────────────────────────────────

class TestGetTightenFactor:
    @pytest.mark.asyncio
    async def test_tighten_disabled_returns_1(self, event_filter):
        """event_tighten_stop=False → 항상 1.0."""
        event_filter._cache["usd_jpy"] = ([], datetime.now(_UTC))
        factor = await event_filter.get_tighten_factor("usd_jpy", {"event_tighten_stop": False})
        assert factor == 1.0

    @pytest.mark.asyncio
    async def test_tighten_when_blocked(self, event_filter):
        """이벤트 차단 중 → factor 반환."""
        ev = _make_event(offset_minutes=60)
        event_filter._cache["usd_jpy"] = ([ev], datetime.now(_UTC))
        params = {
            "event_blackout_hours": 3,
            "event_tighten_stop": True,
            "event_tighten_factor": 0.7,
        }
        factor = await event_filter.get_tighten_factor("usd_jpy", params)
        assert factor == 0.7

    @pytest.mark.asyncio
    async def test_default_tighten_factor(self, event_filter):
        """기본 factor=0.7."""
        ev = _make_event(offset_minutes=60)
        event_filter._cache["usd_jpy"] = ([ev], datetime.now(_UTC))
        params = {"event_blackout_hours": 3}
        factor = await event_filter.get_tighten_factor("usd_jpy", params)
        assert factor == 0.7

    @pytest.mark.asyncio
    async def test_no_event_returns_1(self, event_filter):
        """이벤트 없으면 → 1.0."""
        event_filter._cache["usd_jpy"] = ([], datetime.now(_UTC))
        factor = await event_filter.get_tighten_factor("usd_jpy", {})
        assert factor == 1.0
