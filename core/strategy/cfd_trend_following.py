"""
MarginTrendManager — 후방 호환 re-export.

실제 구현: core/strategy/plugins/cfd_trend_following/manager.py
"""
from core.strategy.plugins.cfd_trend_following.manager import MarginTrendManager, CfdTrendFollowingManager

__all__ = ["MarginTrendManager", "CfdTrendFollowingManager"]
