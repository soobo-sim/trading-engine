"""IStrategy Protocol 준수 확인."""
from core.strategy.base import IStrategy


def test_trend_following_implements_istrategy():
    from core.strategy.gmo_coin_trend import GmoCoinTrendManager
    assert issubclass(GmoCoinTrendManager, IStrategy) or isinstance(
        GmoCoinTrendManager.__dict__.get("start"), type(lambda: None)
    )
    # runtime_checkable Protocol — 인스턴스 없이 메서드 존재 확인
    assert hasattr(GmoCoinTrendManager, "start")
    assert hasattr(GmoCoinTrendManager, "stop")
    assert hasattr(GmoCoinTrendManager, "is_running")
    assert hasattr(GmoCoinTrendManager, "running_pairs")


def test_margin_trend_implements_istrategy():
    from core.punisher.strategy.plugins.gmo_coin_trend.base import MarginTrendManager
    assert hasattr(MarginTrendManager, "start")
    assert hasattr(MarginTrendManager, "stop")
    assert hasattr(MarginTrendManager, "is_running")
    assert hasattr(MarginTrendManager, "running_pairs")
