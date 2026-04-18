"""RegimeGate 상태 DB 영속화 헬퍼.

RegimeGate 자체는 DB 의존 없이 순수 상태머신으로 유지.
이 모듈이 gmoc_regime_gate_state 테이블에 대한 save/load를 담당한다.

save: base_trend.py candle_monitor — 새 4H 캔들 처리 후 호출
load: main.py lifespan — RegimeGate 생성 직후 호출

실패 시 WARNING 로그만 기록 (DB 장애가 거래를 막으면 안 됨).
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from .regime_gate import RegimeGate

logger = logging.getLogger(__name__)
_LOG_PREFIX = "⚙️ [RegimeGate:Persist]"

_TABLE = "gmoc_regime_gate_state"


async def save_regime_gate_state(
    session_factory: async_sessionmaker[AsyncSession],
    gate: RegimeGate,
) -> None:
    """RegimeGate 상태를 DB에 UPSERT.

    INSERT ... ON CONFLICT (pair) DO UPDATE.
    실패 시 WARNING 로그만 — 거래 흐름에 영향 없음.

    Args:
        session_factory: AsyncSession 팩토리 (base_trend._session_factory).
        gate: 저장할 RegimeGate 인스턴스.
    """
    state = gate.to_dict()
    pair = state["pair"]

    sql = text(
        f"""
        INSERT INTO {_TABLE}
            (pair, active_strategy, regime_history, last_switch_at,
             switch_count, consecutive_count, consecutive_regime,
             last_candle_key, updated_at)
        VALUES
            (:pair, :active_strategy, :regime_history, :last_switch_at,
             :switch_count, :consecutive_count, :consecutive_regime,
             :last_candle_key, NOW())
        ON CONFLICT (pair) DO UPDATE SET
            active_strategy   = EXCLUDED.active_strategy,
            regime_history    = EXCLUDED.regime_history,
            last_switch_at    = EXCLUDED.last_switch_at,
            switch_count      = EXCLUDED.switch_count,
            consecutive_count = EXCLUDED.consecutive_count,
            consecutive_regime= EXCLUDED.consecutive_regime,
            last_candle_key   = EXCLUDED.last_candle_key,
            updated_at        = NOW()
        """
    )

    try:
        async with session_factory() as session:
            await session.execute(
                sql,
                {
                    "pair": pair,
                    "active_strategy": state["active_strategy"],
                    "regime_history": json.dumps(state["regime_history"]),
                    "last_switch_at": state["last_switch_at"],
                    "switch_count": state["switch_count"],
                    "consecutive_count": state["consecutive_count"],
                    "consecutive_regime": state["consecutive_regime"],
                    "last_candle_key": state["last_candle_key"],
                },
            )
            await session.commit()
        logger.debug(
            f"{_LOG_PREFIX} {pair}: 상태 저장 완료 "
            f"(active={state['active_strategy']}, key={state['last_candle_key']})"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"{_LOG_PREFIX} {pair}: 상태 저장 실패 (무시) — {exc}"
        )


async def load_regime_gate_state(
    session_factory: async_sessionmaker[AsyncSession],
    gate: RegimeGate,
) -> bool:
    """DB에서 상태를 읽어 RegimeGate에 복원.

    행이 없으면 False 반환 (초기 기동, warm-up 시작).
    실패 시 WARNING 로그 + False 반환 (warm-up으로 자연 폴백).

    Args:
        session_factory: AsyncSession 팩토리.
        gate: 복원 대상 RegimeGate 인스턴스.

    Returns:
        복원 성공 여부.
    """
    pair = gate.to_dict()["pair"]

    sql = text(
        f"""
        SELECT active_strategy, regime_history, last_switch_at,
               switch_count, consecutive_count, consecutive_regime,
               last_candle_key
        FROM {_TABLE}
        WHERE pair = :pair
        LIMIT 1
        """
    )

    try:
        async with session_factory() as session:
            result = await session.execute(sql, {"pair": pair})
            row = result.mappings().one_or_none()

        if row is None:
            logger.info(f"{_LOG_PREFIX} {pair}: DB에 저장된 상태 없음 — warm-up 시작")
            return False

        # JSONB는 asyncpg/psycopg2가 자동으로 list로 파싱함
        regime_history = row["regime_history"]
        if isinstance(regime_history, str):
            regime_history = json.loads(regime_history)

        state = {
            "active_strategy": row["active_strategy"],
            "regime_history": regime_history,
            "last_switch_at": row["last_switch_at"],
            "switch_count": row["switch_count"],
            "consecutive_count": row["consecutive_count"],
            "consecutive_regime": row["consecutive_regime"],
            "last_candle_key": row["last_candle_key"],
        }
        gate.restore(state)
        return True

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"{_LOG_PREFIX} {pair}: 상태 로드 실패 (warm-up으로 폴백) — {exc}"
        )
        return False
