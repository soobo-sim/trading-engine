"""
BoxMeanReversionManager — 후방 호환 re-export.

실제 구현: core/strategy/plugins/box_mean_reversion/manager.py

Note: should_close_for_weekend도 re-export (기존 테스트 호환).
"""
from core.strategy.plugins.box_mean_reversion.manager import BoxMeanReversionManager
from core.exchange.session import should_close_for_weekend  # noqa: F401

__all__ = ["BoxMeanReversionManager", "should_close_for_weekend"]
