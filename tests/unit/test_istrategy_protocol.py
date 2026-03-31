"""IStrategy Protocol 준수 확인."""
from core.strategy.base import IStrategy


def test_trend_following_implements_istrategy():
    from core.strategy.trend_following import TrendFollowingManager
    assert issubclass(TrendFollowingManager, IStrategy) or isinstance(
        TrendFollowingManager.__dict__.get("start"), type(lambda: None)
    )
    # runtime_checkable Protocol — 인스턴스 없이 메서드 존재 확인
    assert hasattr(TrendFollowingManager, "start")
    assert hasattr(TrendFollowingManager, "stop")
    assert hasattr(TrendFollowingManager, "is_running")
    assert hasattr(TrendFollowingManager, "running_pairs")


def test_box_mean_reversion_implements_istrategy():
    from core.strategy.box_mean_reversion import BoxMeanReversionManager
    assert hasattr(BoxMeanReversionManager, "start")
    assert hasattr(BoxMeanReversionManager, "stop")
    assert hasattr(BoxMeanReversionManager, "is_running")
    assert hasattr(BoxMeanReversionManager, "running_pairs")


def test_cfd_trend_following_implements_istrategy():
    from core.strategy.cfd_trend_following import CfdTrendFollowingManager
    assert hasattr(CfdTrendFollowingManager, "start")
    assert hasattr(CfdTrendFollowingManager, "stop")
    assert hasattr(CfdTrendFollowingManager, "is_running")
    assert hasattr(CfdTrendFollowingManager, "running_pairs")
