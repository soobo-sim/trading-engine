"""
Data Layer — IDataHub Protocol + DataHub 구현체.

역할:
  - 캔들, 잔고, 포지션 등 데이터 접근을 단일 인터페이스로 추상화.
  - 매크로/뉴스/센티먼트는 v2에서 구현 예정 (v1은 None 반환).
  - BaseTrendManager가 직접 DB 쿼리하지 않고 DataHub를 통해 접근하도록
    점진적으로 전환한다. v1에서는 기존 _compute_signal() 경로를 유지한다.

IDataHub Protocol:
  - 구조적 서브타이핑 (runtime_checkable).
  - 테스트에서 MockDataHub로 치환 가능.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Protocol, Sequence, runtime_checkable

import httpx
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

_MACRO_CACHE_TTL = 3600   # 1시간 — FRED 일배치 데이터
_EVENTS_CACHE_TTL = 300   # 5분 — 경제 캘린더
_SENTIMENT_CACHE_TTL = 3600  # 1시간 — Fear & Greed Index 갱신 주기

from core.data.dto import (
    CandleDTO,
    EconomicEventDTO,
    LessonDTO,
    MacroSnapshotDTO,
    NewsDTO,
    SentimentDTO,
)
from core.exchange.base import ExchangeAdapter
from core.exchange.types import Balance, Position, Ticker


@runtime_checkable
class IDataHub(Protocol):
    """데이터 접근 추상 인터페이스.

    Decision Layer는 이 Protocol을 통해서만 데이터를 가져온다.
    거래소·DB 구조에 독립적.
    """

    async def get_candles(
        self,
        pair: str,
        timeframe: str,
        limit: int,
        exchange: Optional[str] = None,
    ) -> Sequence[CandleDTO]:
        """완료된 캔들 목록을 오래된 것부터 반환."""
        ...

    async def get_ticker(self, pair: str) -> Optional[Ticker]:
        """거래소 현재가."""
        ...

    async def get_balance(self) -> Balance:
        """거래소 잔고."""
        ...

    async def get_position(self, pair: str) -> Optional[Position]:
        """인메모리 포지션 상태 (없으면 None)."""
        ...

    async def get_macro_snapshot(self) -> Optional[MacroSnapshotDTO]:
        """매크로 스냅샷. v1에서는 None."""
        ...

    async def get_news_summary(self, pair: str) -> tuple[NewsDTO, ...]:
        """최근 관련 뉴스. v1에서는 빈 tuple."""
        ...

    async def get_sentiment(self) -> Optional[SentimentDTO]:
        """센티먼트 지수. v1에서는 None."""
        ...

    async def get_upcoming_events(self) -> tuple[EconomicEventDTO, ...]:
        """향후 24시간 경제 이벤트. v1에서는 빈 tuple."""
        ...

    async def get_lessons(self, pair: str, signal: str) -> tuple[LessonDTO, ...]:
        """유사 상황 교훈. v1에서는 빈 tuple."""
        ...


class DataHub:
    """IDataHub 구현체 — v1 (캔들·잔고·포지션만 지원).

    v2에서 매크로/뉴스/센티먼트 구현 시 이 클래스를 확장하거나
    IDataHub를 구현한 별도 클래스로 교체한다.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        adapter: ExchangeAdapter,
        candle_model: type,
        pair_column: str = "pair",
        positions: Optional[dict[str, Optional[Position]]] = None,
        trading_data_url: Optional[str] = None,
        lesson_model: Optional[type] = None,
    ) -> None:
        """
        Args:
            session_factory: SQLAlchemy async session factory.
            adapter: 거래소 어댑터 (잔고·현재가 조회용).
            candle_model: ORM 캔들 모델 클래스 (bf_candles, gmo_candles 등).
            pair_column: 캔들 모델에서 페어를 나타내는 컬럼명 (기본 "pair").
            positions: BaseTrendManager._position dict (참조 공유).
                       None이면 get_position() 항상 None 반환.
            trading_data_url: trading-data 서비스 베이스 URL.
                            None이면 get_macro_snapshot/get_upcoming_events가 None/빈 tuple 반환.
            lesson_model: WakeUpReview ORM 모델 클래스.
                          None이면 get_lessons()가 빈 tuple 반환.
        """
        self._session_factory = session_factory
        self._adapter = adapter
        self._candle_model = candle_model
        self._pair_column = pair_column
        self._positions = positions or {}
        self._trading_data_url = trading_data_url.rstrip("/") if trading_data_url else None
        self._lesson_model = lesson_model
        # v1.5 캐시
        self._macro_cache: Optional[tuple[MacroSnapshotDTO, datetime]] = None
        self._events_cache: Optional[tuple[tuple[EconomicEventDTO, ...], datetime]] = None
        # v2 뉴스 캐시 (category별)
        self._news_cache: dict[str, tuple[tuple[NewsDTO, ...], datetime]] = {}
        # v2 센티먼트 캐시 (FNG 1시간 TTL)
        self._sentiment_cache: Optional[tuple[SentimentDTO, datetime]] = None

    # ── v1 구현: 캔들 ──────────────────────────────

    async def get_candles(
        self,
        pair: str,
        timeframe: str,
        limit: int,
        exchange: Optional[str] = None,
    ) -> Sequence[CandleDTO]:
        """DB에서 완료된 캔들을 오래된 것부터 반환."""
        CandleModel = self._candle_model
        pair_col = getattr(CandleModel, self._pair_column)

        async with self._session_factory() as db:
            result = await db.execute(
                select(CandleModel)
                .where(
                    and_(
                        pair_col == pair,
                        CandleModel.timeframe == timeframe,
                        CandleModel.is_complete == True,  # noqa: E712
                    )
                )
                .order_by(CandleModel.open_time.desc())
                .limit(limit)
            )
            rows = list(reversed(result.scalars().all()))

        return [self._to_candle_dto(row, pair) for row in rows]

    def _to_candle_dto(self, row: object, pair: str) -> CandleDTO:
        """ORM 캔들 row → CandleDTO."""
        return CandleDTO(
            open_time=row.open_time,       # type: ignore[attr-defined]
            open=float(row.open),          # type: ignore[attr-defined]
            high=float(row.high),          # type: ignore[attr-defined]
            low=float(row.low),            # type: ignore[attr-defined]
            close=float(row.close),        # type: ignore[attr-defined]
            volume=float(getattr(row, "volume", 0.0)),
            pair=pair,
            timeframe=getattr(row, "timeframe", ""),
        )

    # ── v1 구현: 잔고·현재가·포지션 ───────────────

    async def get_ticker(self, pair: str) -> Optional[Ticker]:
        """거래소 현재가. 실패 시 None."""
        try:
            return await self._adapter.get_ticker(pair)
        except Exception:
            return None

    async def get_balance(self) -> Balance:
        """거래소 잔고. 실패 시 빈 Balance."""
        try:
            return await self._adapter.get_balance()
        except Exception:
            return Balance()

    async def get_position(self, pair: str) -> Optional[Position]:
        """인메모리 포지션. 없으면 None."""
        return self._positions.get(pair)

    # ── v2 stub: 매크로·뉴스·센티먼트·이벤트·교훈 ──

    async def get_macro_snapshot(self) -> Optional[MacroSnapshotDTO]:
        """trading-data /api/intermarket/latest에서 매크로 스냅샷 조회.

        캐시 TTL 1시간. API 실패 시 stale 캐시 반환, 캐시 없으면 None.
        trading_data_url 미설정 시 None 반환.
        """
        if self._trading_data_url is None:
            return None

        now = datetime.now(tz=timezone.utc)
        if self._macro_cache is not None:
            cached, fetched_at = self._macro_cache
            if (now - fetched_at).total_seconds() < _MACRO_CACHE_TTL:
                return cached

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._trading_data_url}/api/intermarket/latest")
                resp.raise_for_status()
                series = resp.json().get("series", {})
        except Exception as e:
            logger.warning(f"[DataHub] 매크로 스냅샷 조회 실패 (graceful): {e}")
            return self._macro_cache[0] if self._macro_cache else None

        dto = MacroSnapshotDTO(
            us_10y=series.get("DGS10"),
            us_2y=series.get("DGS2"),
            vix=series.get("VIXCLS"),
            dxy=series.get("DTWEXBGS"),
            fetched_at=now,
        )
        self._macro_cache = (dto, now)
        return dto

    async def get_news_summary(self, pair: str) -> tuple[NewsDTO, ...]:
        """trading-data /api/news/latest에서 뉴스 조회.

        pair → category 매핑:
          BTC_JPY, FX_BTC_JPY → "crypto"
          USD_JPY, GBP_JPY, EUR_JPY 등 → "forex"
          기타 → category 필터 없이 전체

        캐시 TTL 5분 (이벤트 캐시와 동일). API 실패 시 빈 tuple.
        trading_data_url 미설정 시 빈 tuple.
        """
        if self._trading_data_url is None:
            return ()

        # pair → category 매핑
        pair_upper = pair.upper()
        if "BTC" in pair_upper:
            category: Optional[str] = "crypto"
        elif any(fx in pair_upper for fx in ("USD", "GBP", "EUR", "AUD", "CAD")):
            category = "forex"
        else:
            category = None

        cache_key = category or "all"
        now = datetime.now(tz=timezone.utc)
        if cache_key in self._news_cache:
            cached, fetched_at = self._news_cache[cache_key]
            if (now - fetched_at).total_seconds() < _EVENTS_CACHE_TTL:
                return cached

        try:
            params: dict = {"hours": 24, "limit": 10}
            if category:
                params["category"] = category
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._trading_data_url}/api/news/latest",
                    params=params,
                )
                resp.raise_for_status()
                articles = resp.json().get("articles", [])
        except Exception as e:
            logger.warning(f"[DataHub] 뉴스 조회 실패 (graceful): {e}")
            return ()

        dtos: tuple[NewsDTO, ...] = tuple(
            NewsDTO(
                title=a.get("title", ""),
                source=a.get("source", ""),
                published_at=datetime.fromisoformat(
                    a["published_at"].replace("Z", "+00:00")
                ),
                category=a.get("category", "general"),
                sentiment_score=a.get("sentiment_score"),
            )
            for a in articles
            if a.get("published_at")
        )

        self._news_cache[cache_key] = (dtos, now)
        return dtos

    async def get_sentiment(self) -> Optional[SentimentDTO]:
        """센티먼트 지수 조회. Fear & Greed Index 우선, 실패 시 뉴스 평균 폴백.

        1차: trading-data /api/sentiment/latest (source=alternative_me_fng)
        2차 폴백: trading-data /api/news/latest avg_sentiment → 0~100 변환

        캐시 TTL: 1시간 (FNG 갱신 주기). 캐시 HIT 시 API 호출 없음.
        API 실패 및 데이터 없으면 None. trading_data_url 미설정 시 None.
        """
        if self._trading_data_url is None:
            return None

        now = datetime.now(tz=timezone.utc)
        if self._sentiment_cache is not None:
            cached, fetched_at = self._sentiment_cache
            if (now - fetched_at).total_seconds() < _SENTIMENT_CACHE_TTL:
                return cached

        # 1차: Fear & Greed Index (trading-data /api/sentiment/latest)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._trading_data_url}/api/sentiment/latest",
                    params={"source": "alternative_me_fng", "limit": 1},
                )
                resp.raise_for_status()
                scores = resp.json().get("scores", [])

            if scores:
                entry = scores[0]
                dto = SentimentDTO(
                    source="alternative_me_fng",
                    score=int(entry["score"]),
                    classification=entry["classification"],
                    timestamp=datetime.fromisoformat(
                        entry["fetched_at"].replace("Z", "+00:00")
                    ),
                )
                self._sentiment_cache = (dto, now)
                return dto
        except Exception as e:
            logger.warning(f"[DataHub] Fear & Greed 조회 실패 (뉴스로 폴백): {e}")

        # 2차 폴백: 뉴스 sentiment 평균 (기존 로직)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._trading_data_url}/api/news/latest",
                    params={"hours": 24, "limit": 50},
                )
                resp.raise_for_status()
                body = resp.json()
        except Exception as e:
            logger.warning(f"[DataHub] 센티먼트 조회 실패 (graceful): {e}")
            return None

        avg = body.get("avg_sentiment")
        if avg is None:
            return None

        # avg_sentiment (-1.0 ~ 1.0) → 0~100 스케일 변환
        score_100 = int(round((float(avg) + 1.0) * 50))
        score_100 = max(0, min(100, score_100))

        if score_100 <= 10:
            classification = "extreme_fear"
        elif score_100 <= 30:
            classification = "fear"
        elif score_100 <= 70:
            classification = "neutral"
        elif score_100 <= 90:
            classification = "greed"
        else:
            classification = "extreme_greed"

        return SentimentDTO(
            source="marketaux_news_avg",
            score=score_100,
            classification=classification,
            timestamp=datetime.now(timezone.utc),
        )

    async def get_upcoming_events(self) -> tuple[EconomicEventDTO, ...]:
        """trading-data /api/economic-calendar/upcoming에서 이벤트 조회.

        캐시 TTL 5분. API 실패 시 stale 캐시 반환, 캐시 없으면 빈 tuple.
        trading_data_url 미설정 시 빈 tuple 반환.
        """
        if self._trading_data_url is None:
            return ()

        now = datetime.now(tz=timezone.utc)
        if self._events_cache is not None:
            cached, fetched_at = self._events_cache
            if (now - fetched_at).total_seconds() < _EVENTS_CACHE_TTL:
                return cached

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._trading_data_url}/api/economic-calendar/upcoming",
                    params={"hours": 24},
                )
                resp.raise_for_status()
                events_raw = resp.json().get("events", [])
        except Exception as e:
            logger.warning(f"[DataHub] 경제 이벤트 조회 실패 (graceful): {e}")
            return self._events_cache[0] if self._events_cache else ()

        dtos = []
        for ev in events_raw:
            try:
                dtos.append(EconomicEventDTO(
                    name=ev.get("title", ""),
                    datetime_jst=datetime.fromisoformat(ev["event_time"]),
                    importance=ev.get("impact", "Medium"),
                    currency=ev.get("country", ""),
                    forecast=ev.get("forecast"),
                    previous=ev.get("previous"),
                ))
            except (KeyError, ValueError) as parse_err:
                logger.debug(f"[DataHub] 이벤트 파싱 스킵: {parse_err}")
                continue

        result: tuple[EconomicEventDTO, ...] = tuple(dtos)
        self._events_cache = (result, now)
        return result

    async def get_lessons(self, pair: str, signal: str) -> tuple[LessonDTO, ...]:
        """WakeUpReview DB에서 유사 상황 교훈 조회.

        pair 일치 + lessons_learned 존재 최근 5건.
        lesson_model 미설정 시 빈 tuple 반환.
        signal 파라미터는 v2 매칭 필터용 (v1.5: 미사용).
        """
        if self._lesson_model is None:
            return ()

        try:
            LessonModel = self._lesson_model
            async with self._session_factory() as db:
                result = await db.execute(
                    select(LessonModel)
                    .where(
                        and_(
                            LessonModel.pair == pair,
                            LessonModel.lessons_learned.isnot(None),
                        )
                    )
                    .order_by(LessonModel.created_at.desc())
                    .limit(5)
                )
                rows = result.scalars().all()

            return tuple(
                LessonDTO(
                    lesson_id=row.id,
                    situation_tags=(row.cause_code,),
                    lesson_text=row.lessons_learned,
                    outcome="loss" if float(row.realized_pnl) < 0 else "win",
                )
                for row in rows
            )
        except Exception as e:
            logger.warning(f"[DataHub] 교훈 조회 실패 (graceful): {e}")
            return ()
