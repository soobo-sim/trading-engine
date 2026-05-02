"""후방 호환 shim — 실제 구현은 gmo_coin_trend.base."""
from core.punisher.strategy.plugins.gmo_coin_trend.base import MarginTrendManager  # noqa: F401
from core.punisher.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager  # noqa: F401

__all__ = ["MarginTrendManager", "CfdTrendFollowingManager"]
