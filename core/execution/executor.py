"""Executor — 주문 실행 추상화 계층.

RealExecutor  : 실거래소 주문. 기존 adapter.place_order() 위임.
PaperExecutor : 주문 스킵 + paper_trades DB 기록. proposed 전략용.

설계 원칙:
  - 박스 감지·시그널 로직은 100% 공유, 실행부만 교체 가능.
  - IExecutor는 순수 Runtime Protocol — ABCMeta 없음, duck-typing 허용.
  - 진입/청산 기록은 비동기 DB 세션 통해 삽입. 실패 시 로그만.

참고: trader-common/solution-design/ALPHA_FACTORS_PROPOSAL.md §15.2
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapters.database.models import PaperTrade
from core.exchange.types import Order, OrderSide, OrderStatus, OrderType

logger = logging.getLogger(__name__)


@runtime_checkable
class IExecutor(Protocol):
    """주문 실행 인터페이스."""

    async def place_order(
        self,
        adapter: Any,
        order_type: OrderType,
        pair: str,
        amount: float,
    ) -> Order:
        """실제 또는 가상 주문을 실행하고 Order를 반환한다."""
        ...

    async def record_paper_entry(
        self,
        strategy_id: int,
        pair: str,
        direction: str,
        entry_price: float,
    ) -> Optional[int]:
        """페이퍼 진입 기록. paper_trade row id 반환. RealExecutor는 None 반환."""
        ...

    async def record_paper_exit(
        self,
        paper_trade_id: int,
        exit_price: float,
        exit_reason: str,
        entry_price: float,
        invest_jpy: float,
        direction: str,
    ) -> None:
        """페이퍼 청산 기록. RealExecutor는 no-op."""
        ...


class RealExecutor:
    """실거래소 주문 실행기. 기존 adapter 위임, paper 기록 없음."""

    async def place_order(
        self,
        adapter: Any,
        order_type: OrderType,
        pair: str,
        amount: float,
    ) -> Order:
        return await adapter.place_order(order_type, pair, amount)

    async def record_paper_entry(
        self,
        strategy_id: int,
        pair: str,
        direction: str,
        entry_price: float,
    ) -> Optional[int]:
        return None

    async def record_paper_exit(
        self,
        paper_trade_id: int,
        exit_price: float,
        exit_reason: str,
        entry_price: float,
        invest_jpy: float,
        direction: str,
    ) -> None:
        return


class PaperExecutor:
    """가상 주문 실행기. 주문 없이 paper_trades 테이블에 기록.

    proposed 상태 전략 전용. 현재가를 진입가/청산가로 사용.
    슬리피지 미반영 — Paper 결과에 -0.5~1% 보정 필요.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        strategy_id: int,
    ) -> None:
        self._session_factory = session_factory
        self._strategy_id = strategy_id

    async def place_order(
        self,
        adapter: Any,
        order_type: OrderType,
        pair: str,
        amount: float,
    ) -> Order:
        """주문 없이 현재가 기반 가상 결과 반환."""
        try:
            ticker = await adapter.get_ticker(pair)
            price = ticker.last
        except Exception:
            price = 0.0

        paper_id = f"paper-{self._strategy_id}-{int(datetime.now(timezone.utc).timestamp())}"
        logger.info(
            f"[PaperExec] strategy_id={self._strategy_id} {pair}: "
            f"가상 주문 order_type={order_type.value} price={price} amount={amount}"
        )
        side = OrderSide.BUY if order_type in (OrderType.MARKET_BUY, OrderType.BUY) else OrderSide.SELL
        return Order(
            order_id=paper_id,
            pair=pair,
            order_type=order_type,
            side=side,
            price=price,
            amount=amount,
            status=OrderStatus.COMPLETED,
        )

    async def record_paper_entry(
        self,
        strategy_id: int,
        pair: str,
        direction: str,
        entry_price: float,
    ) -> Optional[int]:
        """paper_trades에 진입 행 삽입. id 반환."""
        try:
            now = datetime.now(timezone.utc)
            async with self._session_factory() as db:
                row = PaperTrade(
                    strategy_id=strategy_id,
                    pair=pair,
                    direction=direction,
                    entry_price=entry_price,
                    entry_time=now,
                )
                db.add(row)
                await db.flush()
                record_id = row.id
                await db.commit()
            logger.info(
                f"[PaperExec] paper_trade 진입 기록 id={record_id} "
                f"pair={pair} direction={direction} entry_price={entry_price}"
            )
            return record_id
        except Exception as e:
            logger.error(f"[PaperExec] paper_trade 진입 기록 실패 — {e}", exc_info=True)
            return None

    async def record_paper_exit(
        self,
        paper_trade_id: int,
        exit_price: float,
        exit_reason: str,
        entry_price: float,
        invest_jpy: float,
        direction: str,
    ) -> None:
        """paper_trades 청산 컬럼 업데이트."""
        try:
            now = datetime.now(timezone.utc)
            pnl_pct = _calc_pnl_pct(entry_price, exit_price, direction)
            pnl_jpy = invest_jpy * pnl_pct / 100

            async with self._session_factory() as db:
                result = await db.execute(
                    select(PaperTrade).where(PaperTrade.id == paper_trade_id)
                )
                row = result.scalars().first()
                if row is None:
                    logger.warning(f"[PaperExec] paper_trade id={paper_trade_id} 없음 — 청산 갱신 스킵")
                    return
                row.exit_price = exit_price
                row.exit_time = now
                row.exit_reason = exit_reason
                row.paper_pnl_pct = pnl_pct
                row.paper_pnl_jpy = pnl_jpy
                await db.commit()

            logger.info(
                f"[PaperExec] paper_trade 청산 기록 id={paper_trade_id} "
                f"exit_price={exit_price} reason={exit_reason} pnl={pnl_pct:.2f}%"
            )
        except Exception as e:
            logger.error(f"[PaperExec] paper_trade 청산 기록 실패 — {e}", exc_info=True)


def _calc_pnl_pct(entry_price: float, exit_price: float, direction: str) -> float:
    """손익률(%) 계산. direction: 'long'|'short'."""
    if entry_price <= 0:
        return 0.0
    raw = (exit_price - entry_price) / entry_price * 100
    return raw if direction == "long" else -raw


def create_executor(
    session_factory: async_sessionmaker[AsyncSession],
    strategy_id: int,
    is_proposed: bool,
) -> "IExecutor":
    """전략 상태에 따라 RealExecutor 또는 PaperExecutor 반환."""
    if is_proposed:
        return PaperExecutor(session_factory, strategy_id)
    return RealExecutor()
