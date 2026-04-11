"""
core/analysis/event_filter.py

경제 이벤트 기반 진입 차단 + 스탑 타이트닝 (F-01 알파 팩터).

coinmarket-data(:8002)의 /api/economic-calendar/upcoming 엔드포인트를
5분 TTL 인메모리 캐시로 조회하여:
  - 이벤트 N시간 이내 → 진입 차단
  - 이벤트 임박 → 스탑 타이트닝 계수 반환

tick 루프(_entry_monitor) 안에서 호출되므로 캐시 필수.
API 호출 실패 = graceful degradation (진입 허용, 경고 로그).

통화 → 페어 매핑:
  USD_JPY: USD, JPY 이벤트 영향
  GBP_JPY: GBP, JPY 이벤트 영향
  EUR_JPY: EUR, JPY 이벤트 영향
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# 페어별 영향 통화 매핑
EVENT_CURRENCY_MAP: dict[str, list[str]] = {
    "usd_jpy": ["USD", "JPY"],
    "gbp_jpy": ["GBP", "JPY"],
    "eur_jpy": ["EUR", "JPY"],
    "aud_jpy": ["AUD", "JPY"],
    "cad_jpy": ["CAD", "JPY"],
    "chf_jpy": ["CHF", "JPY"],
}

_CACHE_TTL_SEC = 300  # 5분


class EventFilter:
    """
    경제 이벤트 기반 진입 차단 + 스탑 타이트닝.

    사용:
        filter = EventFilter(trading_data_url="http://trading-data:8002")
        blocked, reason = await filter.is_event_blackout("usd_jpy", params)
        factor = await filter.get_tighten_factor("usd_jpy", params)
    """

    def __init__(self, trading_data_url: str) -> None:
        self._base_url = trading_data_url.rstrip("/")
        # pair → (events_list, fetched_at)
        self._cache: dict[str, tuple[list[dict], datetime]] = {}

    # ── Public API ────────────────────────────────────────────

    async def is_event_blackout(
        self, pair: str, params: dict
    ) -> tuple[bool, str]:
        """
        진입 차단 여부 판단.

        Returns:
            (blocked: bool, reason: str)
            blocked=True → 진입 차단. reason은 로그용 설명.
        """
        now = datetime.now(timezone.utc)
        events = await self._get_events(pair)

        high_hours = float(params.get("event_blackout_hours", 4))
        medium_hours = float(params.get("event_blackout_medium_hours", 2))
        post_minutes = float(params.get("event_post_blackout_minutes", 30))

        for ev in events:
            try:
                ev_time = datetime.fromisoformat(ev["event_time"])
            except (ValueError, KeyError):
                continue

            impact = ev.get("impact", "")
            title = ev.get("title", "")

            # 이벤트 후 블랙아웃: 발표 직후 N분
            minutes_since = (now - ev_time).total_seconds() / 60
            if 0 <= minutes_since < post_minutes:
                return True, f"발표 직후 대기 중: {title} ({minutes_since:.0f}분 경과)"

            # 이벤트 전 블랙아웃
            minutes_before = (ev_time - now).total_seconds() / 60
            if minutes_before < 0:
                continue  # 이미 지난 이벤트

            if impact == "High" and minutes_before <= high_hours * 60:
                return True, f"High 이벤트 {minutes_before:.0f}분 전: {title}"

            if impact == "Medium" and minutes_before <= medium_hours * 60:
                return True, f"Medium 이벤트 {minutes_before:.0f}분 전: {title}"

        return False, ""

    async def get_tighten_factor(
        self, pair: str, params: dict
    ) -> float:
        """
        이벤트 임박 시 스탑 타이트닝 계수 반환.

        Returns:
            1.0 = 변경 없음
            < 1.0 = 스탑 축소 (예: 0.7 = 기존 스탑 거리 × 0.7)
        """
        if not params.get("event_tighten_stop", True):
            return 1.0

        blocked, _ = await self.is_event_blackout(pair, params)
        if blocked:
            return float(params.get("event_tighten_factor", 0.7))
        return 1.0

    # ── 내부 ──────────────────────────────────────────────────

    async def _get_events(self, pair: str) -> list[dict]:
        """캐시 조회 or API 호출. 실패 시 [] 반환 (graceful)."""
        now = datetime.now(timezone.utc)
        cached = self._cache.get(pair)
        if cached:
            events, fetched_at = cached
            if (now - fetched_at).total_seconds() < _CACHE_TTL_SEC:
                return events

        # 캐시 miss → API 조회
        currencies = EVENT_CURRENCY_MAP.get(pair.lower(), [])
        if not currencies:
            return []

        try:
            events = await self._fetch_events(currencies)
            self._cache[pair] = (events, now)
            return events
        except Exception as e:
            logger.warning(
                f"[EventFilter] {pair}: trading-data API 조회 실패 ({e}) "
                "— 이벤트 필터 일시 비활성 (진입 허용)"
            )
            # 실패 시 기존 캐시가 있으면 연장 사용, 없으면 빈 리스트
            if cached:
                events, _ = cached
                self._cache[pair] = (events, now)  # TTL 리셋
                return events
            return []

    async def _fetch_events(self, currencies: list[str]) -> list[dict]:
        """coinmarket-data REST API에서 이벤트 조회."""
        country_param = ",".join(currencies)
        url = f"{self._base_url}/api/economic-calendar/upcoming"
        params = {"hours": 24, "country": country_param}

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        return data.get("events", [])


def create_event_filter() -> Optional[EventFilter]:
    """
    환경변수 TRADING_DATA_URL에서 URL 읽어 EventFilter 생성.
    URL이 설정되지 않으면 None 반환 (event_filter 비활성).
    """
    url = os.environ.get(
        "TRADING_DATA_URL",
        "http://trading-data:8002",  # Docker 기본값
    )
    if not url:
        return None
    return EventFilter(trading_data_url=url)
