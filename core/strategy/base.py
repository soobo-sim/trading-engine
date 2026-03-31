"""
IStrategy Protocol — 전략 매니저의 공통 인터페이스.

main.py 전략 디스패치와 StrategyRegistry가 이 Protocol을 기준으로 동작한다.
모든 전략 매니저 (TrendFollowing, BoxMeanReversion, CfdTrendFollowing)는
이 Protocol을 만족해야 한다.
"""
from __future__ import annotations

from typing import Dict, Protocol, runtime_checkable


@runtime_checkable
class IStrategy(Protocol):
    """전략 매니저가 구현해야 하는 최소 인터페이스."""

    async def start(self, pair: str, params: Dict) -> None:
        """pair에 대한 전략 태스크 시작."""
        ...

    async def stop(self, pair: str) -> None:
        """pair에 대한 전략 태스크 종료."""
        ...

    def is_running(self, pair: str) -> bool:
        """pair에 대한 전략 태스크가 실행 중인지 확인."""
        ...

    def running_pairs(self) -> list[str]:
        """현재 실행 중인 pair 목록 반환."""
        ...
