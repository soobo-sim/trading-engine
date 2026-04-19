"""
Advisory Bypass — 일시적 advisory 체크 무력화 상태 (인메모리 싱글턴).

rate limit 등으로 Rachel이 advisory를 갱신할 수 없는 기간 동안
경고 없이 v1 폴백으로 조용히 동작하게 한다.

환경변수 (컨테이너 재시작 후에도 유지):
  ADVISORY_BYPASS_UNTIL=2026-04-20T09:00:00+09:00
    → 컨테이너 기동 시 자동으로 현재~해당 시각까지 bypass 설정

API (런타임 덮어쓰기):
  POST   /api/advisories/bypass   body: { start, end }  — bypass 창 설정
  GET    /api/advisories/bypass                          — 현재 상태 조회
  DELETE /api/advisories/bypass                          — bypass 해제
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BypassWindow:
    start: datetime
    end: datetime


class _AdvisoryBypassState:
    """인메모리 싱글턴 — bypass 창 저장.

    기동 시 ADVISORY_BYPASS_UNTIL 환경변수를 읽어 자동 초기화.
    """

    def __init__(self) -> None:
        self._window: Optional[BypassWindow] = None
        self._init_from_env()

    def _init_from_env(self) -> None:
        """ADVISORY_BYPASS_UNTIL 환경변수로 기동 시 자동 bypass 설정."""
        until_str = os.environ.get("ADVISORY_BYPASS_UNTIL", "").strip()
        if not until_str:
            return
        try:
            end = datetime.fromisoformat(until_str)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if end <= now:
                logger.info(
                    f"[AdvisoryBypass] ADVISORY_BYPASS_UNTIL={until_str} 이미 만료 — 무시"
                )
                return
            self._window = BypassWindow(start=now, end=end)
            logger.info(
                f"[AdvisoryBypass] 환경변수로 bypass 자동 설정 — "
                f"~{end.isoformat()}"
            )
        except ValueError:
            logger.error(
                f"[AdvisoryBypass] ADVISORY_BYPASS_UNTIL 파싱 실패: {until_str!r} "
                f"(형식: ISO 8601, 예: 2026-04-20T09:00:00+09:00)"
            )

    def set(self, start: datetime, end: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end <= start:
            raise ValueError("end는 start보다 이후여야 합니다")
        self._window = BypassWindow(start=start, end=end)
        logger.info(
            f"[AdvisoryBypass] bypass 설정 — "
            f"start={start.isoformat()} end={end.isoformat()}"
        )

    def clear(self) -> None:
        if self._window is not None:
            logger.info("[AdvisoryBypass] bypass 해제")
        self._window = None

    def is_active(self) -> bool:
        if self._window is None:
            return False
        now = datetime.now(timezone.utc)
        return self._window.start <= now <= self._window.end

    def get_window(self) -> Optional[BypassWindow]:
        return self._window


# 모듈 전역 싱글턴
advisory_bypass = _AdvisoryBypassState()
