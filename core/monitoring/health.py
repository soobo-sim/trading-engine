"""
HealthChecker — 비즈니스 로직 수준의 헬스 체크.

기존 CK/BF system.py는 WS 연결 + 태스크 생존만 확인했다.
HealthChecker는 추가로:
  - 포지션-잔고 정합성 (DB vs 실잔고)
  - 활성 전략 상태
  - 전체 healthy 판정
을 수행한다.

FastAPI에 의존하지 않는다. api/routes/system.py가 이 클래스를 호출한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Type

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.exchange.base import ExchangeAdapter
from core.task.supervisor import TaskSupervisor

logger = logging.getLogger(__name__)


@dataclass
class HealthReport:
    """헬스 체크 결과."""
    healthy: bool
    checked_at: str
    issues: list[str]
    ws_connected: bool
    tasks: dict[str, dict]
    active_strategies: list[dict]
    position_balance: list[dict]


class HealthChecker:
    """
    비즈니스 로직 수준 헬스 체크.

    생성자에서 의존성을 주입받아 거래소-무관으로 동작한다.
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        supervisor: TaskSupervisor,
        session_factory: async_sessionmaker[AsyncSession],
        strategy_model: Type,
        trend_position_model: Type,
        box_position_model: Type,
        pair_column: str = "pair",
    ) -> None:
        self._adapter = adapter
        self._supervisor = supervisor
        self._session_factory = session_factory
        self._strategy_model = strategy_model
        self._trend_position_model = trend_position_model
        self._box_position_model = box_position_model
        self._pair_column = pair_column

    async def check(self) -> HealthReport:
        """전체 헬스 체크 수행."""
        issues: list[str] = []
        now = datetime.now(timezone.utc).isoformat()

        # 1. WS 연결
        ws_connected = self._adapter.is_ws_connected()
        if not ws_connected:
            issues.append("ws: 연결 끊김")

        # 2. 태스크 상태
        task_health = self._supervisor.get_health()
        for name, info in task_health.items():
            if not info.get("alive", False):
                last_err = info.get("last_error", "unknown")
                issues.append(f"task[{name}]: 죽음 ({last_err})")

        # 3. 활성 전략
        strategies = await self._get_active_strategies()

        # 4. 포지션-잔고 정합성
        discrepancies = await self._check_position_balance_consistency()
        for d in discrepancies:
            issues.append(
                f"balance_mismatch[{d['currency']}]: "
                f"expected={d['expected']:.8f}, actual={d['actual']:.8f}"
            )

        healthy = len(issues) == 0

        return HealthReport(
            healthy=healthy,
            checked_at=now,
            issues=issues,
            ws_connected=ws_connected,
            tasks=task_health,
            active_strategies=strategies,
            position_balance=discrepancies,
        )

    async def _get_active_strategies(self) -> list[dict]:
        """DB에서 활성 전략 목록 조회."""
        try:
            async with self._session_factory() as db:
                stmt = select(self._strategy_model).where(
                    self._strategy_model.status == "active"
                )
                result = await db.execute(stmt)
                rows = result.scalars().all()
                return [
                    {
                        "id": r.id,
                        "name": r.name,
                        "status": r.status,
                        "parameters": r.parameters,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"[HealthChecker] 전략 조회 실패: {e}")
            return []

    async def _check_position_balance_consistency(self) -> list[dict]:
        """
        DB 오픈 포지션 vs 거래소 실잔고 비교.

        1% 이상 차이가 있으면 discrepancy로 보고.
        BUG-006의 독립 검증 레이어.
        """
        try:
            open_positions = await self._get_open_positions()
            if not open_positions:
                return []

            balance = await self._adapter.get_balance()
            discrepancies: list[dict] = []

            # 통화별로 그룹핑하여 비교
            expected_by_currency: dict[str, float] = {}
            for pos in open_positions:
                currency = pos["pair"].split("_")[0].lower()
                expected_by_currency[currency] = (
                    expected_by_currency.get(currency, 0.0) + pos["amount"]
                )

            for currency, expected in expected_by_currency.items():
                actual = balance.get_available(currency)
                if expected > 0 and abs(expected - actual) / expected > 0.01:
                    discrepancies.append({
                        "currency": currency,
                        "expected": expected,
                        "actual": actual,
                        "diff": actual - expected,
                    })

            return discrepancies
        except Exception as e:
            logger.error(f"[HealthChecker] 잔고 정합성 체크 실패: {e}")
            return []

    async def _get_open_positions(self) -> list[dict]:
        """trend + box 오픈 포지션 모두 조회."""
        positions: list[dict] = []
        try:
            async with self._session_factory() as db:
                # Trend positions
                stmt = select(self._trend_position_model).where(
                    self._trend_position_model.status == "open"
                )
                result = await db.execute(stmt)
                for row in result.scalars().all():
                    positions.append({
                        "type": "trend",
                        "pair": row.pair,
                        "amount": float(row.entry_amount),
                    })

                # Box positions
                pair_col = getattr(self._box_position_model, self._pair_column)
                stmt = select(self._box_position_model).where(
                    self._box_position_model.status == "open"
                )
                result = await db.execute(stmt)
                for row in result.scalars().all():
                    pair_val = getattr(row, self._pair_column)
                    positions.append({
                        "type": "box",
                        "pair": pair_val,
                        "amount": float(row.entry_amount),
                    })
        except Exception as e:
            logger.error(f"[HealthChecker] 포지션 조회 실패: {e}")
        return positions
