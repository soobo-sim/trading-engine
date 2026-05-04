"""
GmoCoinTrendManager — GMO Coin 레버리지 추세추종 매니저.

GmoCoinBaseManager 상속. 추세추종 시그널 타입만 선언.
GMO Coin 고유 주문 실행 로직(open/close/trailing/losscut 등)은
GmoCoinBaseManager에서 상속.

상속 체인:
    BaseTrendManager → MarginTrendManager → GmoCoinBaseManager → GmoCoinTrendManager

GmoCoinBoxManager와 형제 관계:
    GmoCoinBaseManager
        ├── GmoCoinTrendManager   (trend_following)
        └── GmoCoinBoxManager     (box_mean_reversion)
"""
from __future__ import annotations

from core.punisher.strategy.plugins.gmo_coin_base.manager import GmoCoinBaseManager


class GmoCoinTrendManager(GmoCoinBaseManager):
    """GMO Coin 레버리지 추세추종 매니저. 롱/숏 양방향."""

    _task_prefix = "gmoc_trend"
    _log_prefix = "[TrendMgr]"
    # _supports_short = True — GmoCoinBaseManager에서 상속

    def _get_strategy_type(self) -> str:
        return "trend_following"

