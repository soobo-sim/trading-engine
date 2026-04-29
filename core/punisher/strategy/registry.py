"""
StrategyRegistry — trading_style → IStrategy 매핑.

main.py의 if/elif 전략 디스패치를 제거하고,
trading_style 문자열로 전략 매니저를 검색할 수 있게 한다.

사용법:
    registry = StrategyRegistry()
    registry.register("trend_following", trend_manager)
    registry.register("box_mean_reversion", box_manager)

    # 전략 시작 (main.py에서)
    manager = registry.get("trend_following")
    await manager.start(pair, params)
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from core.strategy.base import IStrategy

logger = logging.getLogger(__name__)


class StrategyRegistry:
    """전략 매니저 레지스트리."""

    def __init__(self) -> None:
        self._managers: Dict[str, IStrategy] = {}

    def register(self, trading_style: str, manager: IStrategy) -> None:
        """전략 매니저 등록."""
        self._managers[trading_style] = manager
        logger.debug(f"[Registry] 전략 등록: {trading_style} → {type(manager).__name__}")

    def get(self, trading_style: str) -> Optional[IStrategy]:
        """trading_style에 해당하는 매니저 반환. 없으면 None."""
        return self._managers.get(trading_style)

    def styles(self) -> list[str]:
        """등록된 전략 스타일 목록."""
        return list(self._managers.keys())

    def all_managers(self) -> Dict[str, IStrategy]:
        """전체 매니저 딕셔너리 반환 (읽기 전용 목적)."""
        return dict(self._managers)

    async def start_strategy(
        self,
        trading_style: str,
        pair: str,
        params: Dict,
        *,
        initial_delay_sec: float = 0,
    ) -> bool:
        """trading_style로 전략을 찾아 start. 성공 여부 반환."""
        manager = self.get(trading_style)
        if manager is None:
            logger.warning(f"[Registry] 미등록 전략: {trading_style}")
            return False
        await manager.start(pair, params, initial_delay_sec=initial_delay_sec)
        logger.debug(f"[Registry] {type(manager).__name__} 기동: pair={pair} initial_delay={initial_delay_sec:.0f}s")
        return True

    async def stop_pair_all_managers(self, pair: str) -> None:
        """모든 매니저에서 해당 pair를 중단한다.

        activate 시 기존 전략 정리(paper 포함) + archive 시 중단에 공용.
        실행 중이 아닌 매니저는 skip.
        예외 발생 시 WARNING 로그 후 계속 진행.
        """
        for style, manager in self._managers.items():
            try:
                if manager.is_running(pair):
                    await manager.stop(pair)
                    logger.debug(f"[Registry] {style} {pair}: 중단 완료")
            except Exception as e:
                logger.warning(f"[Registry] {style} {pair}: 중단 중 에러 (계속 진행) — {e}")
