"""
Advisory Bypass — 일시적 advisory 체크 무력화 상태 (인메모리 싱글턴).

rate limit 등으로 Rachel이 advisory를 갱신할 수 없는 기간 동안
경고 없이 v1 폴백으로 조용히 동작하게 한다.

API:
  POST   /api/advisories/bypass   body: { start, end }  — bypass 창 설정
  GET    /api/advisories/bypass                          — 현재 상태 조회
  DELETE /api/advisories/bypass                          — bypass 해제
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BypassWindow:
    start: datetime
    end: datetime


class _AdvisoryBypassState:
    """인메모리 싱글턴 — bypass 창 저장."""

    def __init__(self) -> None:
        self._window: Optional[BypassWindow] = None

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
