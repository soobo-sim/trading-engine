"""
API 의존성 — AppState를 통한 DI.

main.py에서 app.state에 AppState를 설정하면,
각 라우트가 Depends(get_state)로 접근한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Type

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.exchange.base import ExchangeAdapter
from core.monitoring.health import HealthChecker
from core.strategy.box_mean_reversion import BoxMeanReversionManager
from core.strategy.trend_following import TrendFollowingManager
from core.task.supervisor import TaskSupervisor


@dataclass
class ModelRegistry:
    """거래소별 ORM 모델을 한곳에 모은 레지스트리."""
    strategy: Type
    trade: Type
    balance_entry: Type
    insight: Type
    summary: Type
    candle: Type
    box: Type
    box_position: Type
    trend_position: Type
    technique: Type  # StrategyTechnique (공유)


@dataclass
class AppState:
    """FastAPI app.state에 저장되는 애플리케이션 상태."""
    adapter: ExchangeAdapter
    supervisor: TaskSupervisor
    session_factory: async_sessionmaker[AsyncSession]
    trend_manager: TrendFollowingManager
    box_manager: BoxMeanReversionManager
    health_checker: HealthChecker
    models: ModelRegistry
    prefix: str           # "ck" or "bf"
    pair_column: str       # "pair" or "product_code"


def get_state(request: Request) -> AppState:
    """라우트에서 AppState를 주입받는 의존성."""
    return request.app.state.app_state


async def get_db(request: Request) -> AsyncSession:
    """라우트에서 DB 세션을 주입받는 의존성. 요청 종료 시 자동 close."""
    state: AppState = request.app.state.app_state
    async with state.session_factory() as session:
        yield session
