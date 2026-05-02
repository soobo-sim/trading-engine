"""후방 호환 shim — 실제 구현은 gmo_coin_trend.base로 이동됨."""
from __future__ import annotations

from core.punisher.strategy.plugins.gmo_coin_trend.base import MarginTrendManager  # noqa: F401

# 후방 호환 alias — DB에 cfd_trend_following style로 등록된 전략 + 기존 import 지원
CfdTrendFollowingManager = MarginTrendManager
