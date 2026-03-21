"""
BUG-008 소급 패치 — 수정 이전 청산건의 PnL 복원.

대상:
  - BF position 1: exit_price 존재 → PnL 직접 계산
  - CK positions 1~2: closed_at 시점 coinmarket-data 1min candle close로 exit_price 역산
  - CK position 3: 복원 불가 → unknown 유지 (스킵)

사용법:
  # dry-run (기본값: 변경 없이 결과만 출력)
  python scripts/backfill_pnl.py

  # 실제 적용
  python scripts/backfill_pnl.py --apply
"""
import argparse
import asyncio
import logging
import os
import sys

from decimal import Decimal
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")
COINMARKET_URL = os.getenv("COINMARKET_URL", "http://localhost:8002")


async def fetch_candle_close_from_db(session: AsyncSession, pair: str, closed_at) -> float | None:
    """DB에서 closed_at 시점 직전 1h candle close 조회 (coinmarket-data ck_candles 테이블 직접)."""
    result = await session.execute(
        text(
            "SELECT close FROM ck_candles "
            "WHERE pair = :pair AND timeframe = '1h' AND is_complete = true "
            "AND open_time <= :closed_at "
            "ORDER BY open_time DESC LIMIT 1"
        ),
        {"pair": pair, "closed_at": closed_at},
    )
    row = result.first()
    if not row:
        logger.warning("해당 시점 캔들 없음: %s", closed_at)
        return None
    return float(row[0])


async def backfill_bf_position_1(session: AsyncSession, apply: bool) -> None:
    """BF position 1: exit_price 있으므로 PnL 직접 계산."""
    row = await session.execute(
        text("SELECT id, entry_price, exit_price, entry_amount FROM bf_trend_positions WHERE id = 1")
    )
    pos = row.mappings().first()
    if not pos:
        logger.info("BF position 1 없음 — 스킵")
        return

    entry = float(pos["entry_price"])
    exit_p = float(pos["exit_price"]) if pos["exit_price"] else None
    amount = float(pos["entry_amount"])

    if exit_p is None:
        logger.warning("BF position 1: exit_price 없음 — 스킵")
        return

    pnl_jpy = round((exit_p - entry) * amount, 2)
    pnl_pct = round((exit_p - entry) / entry * 100, 4) if entry else 0

    logger.info(
        "BF position 1: entry=%.2f exit=%.2f amount=%.6f → pnl_jpy=%.2f pnl_pct=%.4f%%",
        entry, exit_p, amount, pnl_jpy, pnl_pct,
    )

    if apply:
        await session.execute(
            text(
                "UPDATE bf_trend_positions "
                "SET realized_pnl_jpy = :pnl_jpy, realized_pnl_pct = :pnl_pct "
                "WHERE id = 1"
            ),
            {"pnl_jpy": pnl_jpy, "pnl_pct": pnl_pct},
        )
        logger.info("  → 적용 완료")
    else:
        logger.info("  → dry-run (--apply로 실행 시 적용됨)")


async def backfill_ck_positions(session: AsyncSession, apply: bool) -> None:
    """CK positions 1~2: closed_at 시점 1min candle close로 exit_price 역산."""
    rows = await session.execute(
        text(
            "SELECT id, entry_price, entry_amount, closed_at "
            "FROM ck_trend_positions WHERE id IN (1, 2) ORDER BY id"
        )
    )
    positions = rows.mappings().all()

    for pos in positions:
        pos_id = pos["id"]
        entry = float(pos["entry_price"])
        amount = float(pos["entry_amount"])
        closed_at = pos["closed_at"]

        if closed_at is None:
            logger.warning("CK position %d: closed_at 없음 — 스킵", pos_id)
            continue

        exit_p = await fetch_candle_close_from_db(session, "xrp_jpy", closed_at)

        if exit_p is None:
            logger.warning("CK position %d: 캔들 조회 실패 — 스킵", pos_id)
            continue

        pnl_jpy = round((exit_p - entry) * amount, 2)
        pnl_pct = round((exit_p - entry) / entry * 100, 4) if entry else 0

        logger.info(
            "CK position %d: entry=%.4f exit=%.4f amount=%.4f → pnl_jpy=%.2f pnl_pct=%.4f%%",
            pos_id, entry, exit_p, amount, pnl_jpy, pnl_pct,
        )

        if apply:
            await session.execute(
                text(
                    "UPDATE ck_trend_positions "
                    "SET exit_price = :exit_price, realized_pnl_jpy = :pnl_jpy, realized_pnl_pct = :pnl_pct "
                    "WHERE id = :id"
                ),
                {"exit_price": exit_p, "pnl_jpy": pnl_jpy, "pnl_pct": pnl_pct, "id": pos_id},
            )
            logger.info("  → 적용 완료")
        else:
            logger.info("  → dry-run (--apply로 실행 시 적용됨)")


async def main():
    parser = argparse.ArgumentParser(description="BUG-008 PnL 소급 패치")
    parser.add_argument("--apply", action="store_true", help="실제로 DB에 적용 (기본: dry-run)")
    args = parser.parse_args()

    if not DATABASE_URL:
        logger.error("DATABASE_URL 환경변수 필요")
        sys.exit(1)

    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        logger.info("=== BF position 1 패치 ===")
        await backfill_bf_position_1(session, args.apply)

        logger.info("=== CK positions 1~2 패치 ===")
        await backfill_ck_positions(session, args.apply)

        logger.info("=== CK position 3: 복원 불가 — unknown 유지 ===")

        if args.apply:
            await session.commit()
            logger.info("커밋 완료")
        else:
            logger.info("dry-run 완료. --apply로 실행하면 실제 적용됩니다.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
