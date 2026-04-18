"""
EventDetector — 긴급 이벤트 감지기 (asyncio 태스크).

60초 주기로 룰 기반 숫자 비교만 수행. LLM 비용 = 0.
이상 감지 시 POST /api/advisories (urgent 알림 + risk_notes)를 저장하거나
Telegram으로 즉시 알린다.

감지 규칙:
  (a) 가격 급변: 직전 대비 ±2% (설정 가능)
  (b) 센티먼트 급변: 직전 대비 ±30% (설정 가능)
  (c) S/A급 경제 이벤트: N분 이내 도래 (설정 가능, 기본 5분)

설계서: trader-common/docs/specs/ai-native/01_DATA_PIPELINE.md §3
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from core.data.hub import IDataHub

logger = logging.getLogger("core.judge.monitoring.event_detector")  # 구 경로 유지

_DEFAULT_PRICE_CHANGE_PCT = 2.0          # ±2% 가격 급변
_DEFAULT_SENTIMENT_DELTA_PCT = 30.0      # ±30% 센티먼트 변화
_DEFAULT_EVENT_ADVANCE_MIN = 5           # S/A급 이벤트 N분 전 알림
_DEFAULT_POLL_INTERVAL_SEC = 60          # 60초 주기
_DETECTION_COOLDOWN_SEC = 300            # 동일 타입 감지 후 5분 쿨다운 (중복 알림 방지)


class EventDetector:
    """60초 주기 룰 기반 이벤트 감지기.

    Args:
        data_hub:           IDataHub — 캔들/뉴스/이벤트 조회용.
        advisory_base_url:  str — POST /api/advisories 대상 URL.
                            예: "http://localhost:8001" (Docker: "http://bitflyer-trader:8001")
        exchange:           str — "bitflyer" | "gmofx".
        pairs:              list[str] — 감시 대상 페어 목록.
        telegram_notifier:  Optional callable(str) — 긴급 Telegram 알림용 (없으면 로그만).
        settings:           dict — 임계값 override.
                            keys: price_change_pct, sentiment_delta_pct, event_advance_min,
                                  poll_interval_sec
    """

    def __init__(
        self,
        data_hub: IDataHub,
        advisory_base_url: str,
        exchange: str,
        pairs: list[str],
        telegram_notifier: Any | None = None,
        settings: dict | None = None,
    ) -> None:
        self._data_hub = data_hub
        self._advisory_base_url = advisory_base_url.rstrip("/")
        self._exchange = exchange
        self._pairs = pairs
        self._telegram_notifier = telegram_notifier
        self._settings = settings or {}

        # 임계값
        self._price_change_pct: float = float(
            self._settings.get("price_change_pct", _DEFAULT_PRICE_CHANGE_PCT)
        )
        self._sentiment_delta_pct: float = float(
            self._settings.get("sentiment_delta_pct", _DEFAULT_SENTIMENT_DELTA_PCT)
        )
        self._event_advance_min: int = int(
            self._settings.get("event_advance_min", _DEFAULT_EVENT_ADVANCE_MIN)
        )
        self._poll_interval_sec: float = float(
            self._settings.get("poll_interval_sec", _DEFAULT_POLL_INTERVAL_SEC)
        )

        # 내부 상태
        self._prev_prices: dict[str, float] = {}
        self._prev_sentiment_score: Optional[int] = None
        # 쿨다운: detection_type → 마지막 감지 epoch (중복 알림 방지)
        self._last_detected: dict[str, float] = {}

        self._task: Optional[asyncio.Task] = None

    # ─────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────

    async def start(self) -> None:
        """asyncio 태스크로 무한 폴링 시작."""
        if self._task is not None:
            logger.warning("[EventDetector] 이미 실행 중")
            return
        if not self._pairs:
            logger.debug("[EventDetector] 감시 대상 pair 없음 — 시작 스킵")
            return
        self._task = asyncio.create_task(self._run(), name="event_detector")
        logger.debug(
            f"[EventDetector] 시작 — pairs={self._pairs} "
            f"poll={self._poll_interval_sec}s "
            f"price_thresh={self._price_change_pct}%"
        )

    async def stop(self) -> None:
        """태스크 취소 + 정리."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.debug("[EventDetector] 종료")

    # ─────────────────────────────────────────────
    # 내부 루프
    # ─────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._poll_interval_sec)
                detections = await self._poll_once()
                if detections:
                    await self._handle_detections(detections)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[EventDetector] 폴링 오류 (무시, 재시도): {e}")

    async def _poll_once(self) -> list[dict]:
        """1회 폴링 → 감지된 이벤트 목록 반환."""
        detections: list[dict] = []
        now = datetime.now(timezone.utc)

        # (a) 가격 급변 체크 — pair별
        for pair in self._pairs:
            detection = await self._check_price_spike(pair, now)
            if detection:
                detections.append(detection)

        # (b) 센티먼트 급변 체크
        detection = await self._check_sentiment_shift(now)
        if detection:
            detections.append(detection)

        # (b2) 센티먼트 극단치 체크 (score ≤10 or ≥90)
        extreme_detection = await self._check_sentiment_extreme(now)
        if extreme_detection:
            detections.append(extreme_detection)

        # (c) 경제 이벤트 도래 체크
        event_detections = await self._check_upcoming_events(now)
        detections.extend(event_detections)

        return detections

    async def _check_price_spike(self, pair: str, now: datetime) -> dict | None:
        """가격 급변 감지."""
        detection_key = f"price_spike:{pair}"
        if self._is_in_cooldown(detection_key):
            return None

        try:
            ticker = await self._data_hub.get_ticker(pair)
        except Exception as e:
            logger.debug(f"[EventDetector] {pair} ticker 조회 실패 (스킵): {e}")
            return None

        if ticker is None:
            return None

        current_price = float(ticker.last)
        prev_price = self._prev_prices.get(pair)
        self._prev_prices[pair] = current_price

        if prev_price is None or prev_price == 0:
            return None

        change_pct = abs((current_price - prev_price) / prev_price * 100.0)
        if change_pct >= self._price_change_pct:
            direction = "상승" if current_price > prev_price else "하락"
            logger.warning(
                f"[EventDetector] {pair} 가격 급변 감지: "
                f"{prev_price:.0f} → {current_price:.0f} ({change_pct:.1f}% {direction})"
            )
            logger.info(
                f"[EventDetector] {pair}: 60초 내 {change_pct:.1f}% {direction} → "
                f"advisory(hold) 등록. 다음 4H 사이클에서 진입 억제 효과"
            )
            self._mark_detected(detection_key)
            return {
                "type": "price_spike",
                "pair": pair,
                "change_pct": change_pct,
                "direction": direction,
                "current_price": current_price,
                "prev_price": prev_price,
                "detail": f"{pair} {direction} {change_pct:.1f}% (¥{prev_price:.0f} → ¥{current_price:.0f})",
            }
        return None

    async def _check_sentiment_shift(self, now: datetime) -> dict | None:
        """센티먼트 급변 감지."""
        detection_key = "sentiment_shift"
        if self._is_in_cooldown(detection_key):
            return None

        try:
            sentiment = await self._data_hub.get_sentiment()
        except Exception as e:
            logger.debug(f"[EventDetector] sentiment 조회 실패 (스킵): {e}")
            return None

        if sentiment is None:
            return None

        current_score = sentiment.score  # 0~100
        prev_score = self._prev_sentiment_score
        self._prev_sentiment_score = current_score

        if prev_score is None:
            return None

        if prev_score == 0:
            return None

        # 절대값 변화량 (±N포인트)으로 감지
        delta_abs = abs(current_score - prev_score)
        # sentiment_delta_pct를 포인트 차이로 해석 (0~100 스케일에서 30% = 30포인트)
        threshold_pts = self._sentiment_delta_pct

        if delta_abs >= threshold_pts:
            direction = "상승" if current_score > prev_score else "하락"
            logger.warning(
                f"[EventDetector] 센티먼트 급변 감지: "
                f"{prev_score} → {current_score} (Δ{delta_abs:.0f}pt {direction})"
            )
            logger.info(
                f"[EventDetector] 센티먼트 {delta_abs:.0f}pt {direction} ({prev_score} → {current_score}) → "
                f"advisory(hold) 등록. AI 판단에서 시장 심리 변화 반영"
            )
            self._mark_detected(detection_key)
            return {
                "type": "sentiment_shift",
                "pair": None,
                "delta_abs": delta_abs,
                "direction": direction,
                "current_score": current_score,
                "prev_score": prev_score,
                "classification": sentiment.classification,
                "detail": (
                    f"센티먼트 {direction} {delta_abs:.0f}pt "
                    f"({prev_score} → {current_score}, {sentiment.classification})"
                ),
            }
        return None

    async def _check_sentiment_extreme(self, now: datetime) -> dict | None:
        """센티먼트 절대값 극단치 감지 (score ≤ 10 or ≥ 90)."""
        detection_key = "sentiment_extreme"
        if self._is_in_cooldown(detection_key):
            return None

        try:
            sentiment = await self._data_hub.get_sentiment()
        except Exception as e:
            logger.debug(f"[EventDetector] sentiment 조회 실패 (스킵): {e}")
            return None

        if sentiment is None:
            return None

        current_score = sentiment.score
        if current_score > 10 and current_score < 90:
            return None

        direction = "극단 공포" if current_score <= 10 else "극단 탐욕"
        score_meaning = (
            "시장 공포 극심 — 역발상적 매수 기회 모니터링"
            if current_score <= 10
            else "과열 탐욕 — 리스크 경계 필요"
        )
        logger.warning(
            f"[EventDetector] 센티먼트 극단치 감지: score={current_score} ({direction})"
        )
        logger.info(
            f"[EventDetector] 센티먼트 score={current_score} ({direction}): {score_meaning}. "
            f"advisory(hold) 등록 → 다음 사이클 AI 판단에 반영"
        )
        self._mark_detected(detection_key)
        return {
            "type": "sentiment_extreme",
            "pair": None,
            "current_score": current_score,
            "classification": sentiment.classification,
            "direction": direction,
            "detail": (
                f"센티먼트 극단치: score={current_score} "
                f"({direction}, {sentiment.classification})"
            ),
        }

    async def _check_upcoming_events(self, now: datetime) -> list[dict]:
        """S/A급(High) 경제 이벤트 도래 감지."""
        try:
            events = await self._data_hub.get_upcoming_events()
        except Exception as e:
            logger.debug(f"[EventDetector] events 조회 실패 (스킵): {e}")
            return []

        if not events:
            return []

        detections = []
        advance_delta = timedelta(minutes=self._event_advance_min)

        for event in events:
            if event.importance != "High":
                continue

            event_time = event.datetime_jst
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)

            time_until = event_time - now
            if timedelta(0) <= time_until <= advance_delta:
                detection_key = f"event:{event.name}:{event_time.isoformat()}"
                if self._is_in_cooldown(detection_key):
                    continue
                logger.warning(
                    f"[EventDetector] 경제 이벤트 임박: {event.name} "
                    f"({time_until.total_seconds()/60:.0f}분 후, {event.currency})"
                )
                logger.info(
                    f"[EventDetector] {event.name}: {time_until.total_seconds()/60:.0f}분 후 발표 ({event.currency}). "
                    f"advisory(hold) 등록 → 발표 전후 변동성 대비 진입 억제"
                )
                self._mark_detected(detection_key)
                detections.append({
                    "type": "event_imminent",
                    "pair": None,
                    "event_name": event.name,
                    "event_time": event_time.isoformat(),
                    "minutes_until": time_until.total_seconds() / 60,
                    "currency": event.currency,
                    "detail": (
                        f"S/A급 이벤트 임박: {event.name} "
                        f"({time_until.total_seconds()/60:.0f}분 후, {event.currency})"
                    ),
                })
        return detections

    # ─────────────────────────────────────────────
    # 감지 결과 처리
    # ─────────────────────────────────────────────

    async def _handle_detections(self, detections: list[dict]) -> None:
        """감지 결과 처리 — advisory POST + Telegram 알림."""
        telegram_status = "전송" if self._telegram_notifier else "미설정"
        logger.info(
            f"[EventDetector] 폴링 완료: {len(detections)}건 감지 → "
            f"advisory {len(detections)}건 등록, "
            f"Telegram {telegram_status}"
        )
        for detection in detections:
            detail = detection.get("detail", str(detection))
            pair = detection.get("pair") or (self._pairs[0] if self._pairs else "UNKNOWN")

            # advisory로 저장 (TRADING_MODE=rachel일 때 엔진이 읽음)
            await self._post_advisory(pair=pair, detail=detail)

            # Telegram 알림
            if self._telegram_notifier is not None:
                try:
                    msg = f"⚠️ [EventDetector] {detail}"
                    await self._telegram_notifier(msg)
                except Exception as e:
                    logger.warning(f"[EventDetector] Telegram 알림 실패: {e}")

    async def _post_advisory(self, pair: str, detail: str) -> None:
        """POST /api/advisories — 긴급 이벤트 알림 advisory 저장.

        action=hold (진입/청산 판단 없음), risk_notes에 감지 내용 기록.
        실패해도 WARNING만 — 다음 폴링 계속.
        """
        payload = {
            "pair": pair,
            "action": "hold",
            "confidence": 1.0,
            "reasoning": f"EventDetector 긴급 감지: {detail[:200]}",
            "risk_notes": detail,
            "ttl_hours": 1.0,  # 1시간 TTL (다음 4H 캔들 전 만료)
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                url = f"{self._advisory_base_url}/api/advisories"
                resp = await client.post(url, json=payload)
                if resp.status_code not in (200, 201):
                    logger.warning(
                        f"[EventDetector] advisory POST 실패: {resp.status_code} {resp.text[:200]}"
                    )
                else:
                    logger.info(f"[EventDetector] advisory 저장 완료: {detail[:100]}")
        except Exception as e:
            logger.warning(f"[EventDetector] advisory POST 오류 (무시): {e}")

    # ─────────────────────────────────────────────
    # 쿨다운 관리
    # ─────────────────────────────────────────────

    def _is_in_cooldown(self, detection_key: str) -> bool:
        """동일 타입 이벤트가 최근 쿨다운 시간 내에 감지됐으면 True."""
        last_ts = self._last_detected.get(detection_key)
        if last_ts is None:
            return False
        return time.monotonic() - last_ts < _DETECTION_COOLDOWN_SEC

    def _mark_detected(self, detection_key: str) -> None:
        """감지 시각 기록."""
        self._last_detected[detection_key] = time.monotonic()
