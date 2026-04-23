"""
canary_monitor.py — P6 CanaryMonitor 백그라운드 작업.

매 60초마다 canary 가설을 점검하고, 가드레일 위반 시 자동 롤백.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

JST = timezone(timedelta(hours=9))
logger = logging.getLogger("core.judge.evolution.canary_monitor")


class CanaryMonitor:
    """canary 가설 실시간 추적 + 자동 롤백."""

    INTERVAL_SEC = 60

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        # hyp_id → 시작 잔고 캐시
        self._canary_start_balances: dict[str, float] = {}
        self._session_factory = None  # main.py에서 set

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("CanaryMonitor 시작 — 가드레일 점검 주기 %ds", self.INTERVAL_SEC)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("CanaryMonitor 정지")

    @property
    def is_running(self) -> bool:
        return self._running

    async def _loop(self) -> None:
        while self._running:
            try:
                if self._session_factory is None:
                    logger.debug("CanaryMonitor: session_factory 미설정, 루프 스킵")
                else:
                    async with self._session_factory() as db:
                        await self._check_all_canaries(db)
            except Exception as exc:
                logger.error("CanaryMonitor loop error: %s", exc, exc_info=True)
            await asyncio.sleep(self.INTERVAL_SEC)

    async def _check_all_canaries(self, db: AsyncSession) -> None:
        from adapters.database.hypothesis_model import Hypothesis
        from core.judge.evolution.guardrails import (
            check_guardrails,
            _fetch_current_balance_jpy,
        )

        stmt = select(Hypothesis).where(Hypothesis.status == "canary")
        canaries = (await db.execute(stmt)).scalars().all()

        if not canaries:
            return

        current_balance = await _fetch_current_balance_jpy(db)

        for h in canaries:
            start_balance = self._canary_start_balances.get(h.id)
            if start_balance is None:
                start_balance = await self._resolve_start_balance(db, h)
                self._canary_start_balances[h.id] = start_balance

            canary_start_at = h.approved_at or h.created_at
            violation = await check_guardrails(
                db,
                h,
                current_balance_jpy=current_balance,
                canary_start_balance_jpy=start_balance,
                canary_start_at=canary_start_at,
            )
            if violation:
                await self._trigger_rollback(db, h, violation)

    async def _trigger_rollback(self, db, h, violation) -> None:
        from api.services.hypotheses_service import HypothesesService

        logger.warning(
            "🛑 자동 롤백 가설 %s — %s", h.id, violation.description
        )
        svc = HypothesesService(db)
        try:
            await svc.transition(
                h.id,
                "rolled_back",
                actor="canary_monitor",
                payload={"reason": violation.description, "violation": violation.to_dict()},
            )
            logger.info("자동 롤백 완료: %s", h.id)
        except Exception as exc:
            logger.critical(
                "⚠️ 롤백 실패 가설 %s: %s — 수동 개입 필요", h.id, exc
            )

        # 캐시 정리
        self._canary_start_balances.pop(h.id, None)

    async def _resolve_start_balance(self, db: AsyncSession, h) -> float:
        """시작 잔고 복원 — canary_result > DB 조회 > 현재잔고 순."""
        from core.judge.evolution.guardrails import _fetch_current_balance_jpy
        from sqlalchemy import text

        if h.canary_result and "start_balance_jpy" in h.canary_result:
            return float(h.canary_result["start_balance_jpy"])

        # approved_at 직전 잔고 조회
        if h.approved_at:
            try:
                row = (await db.execute(
                    text(
                        "SELECT balance_jpy FROM gmoc_balance_entries "
                        "WHERE recorded_at <= :ts "
                        "ORDER BY recorded_at DESC LIMIT 1"
                    ),
                    {"ts": h.approved_at},
                )).first()
                if row:
                    bal = float(row[0])
                    # canary_result에 기록 (재시작 안전성)
                    h.canary_result = {**(h.canary_result or {}), "start_balance_jpy": bal}
                    await db.commit()
                    return bal
            except Exception as exc:
                logger.debug("_resolve_start_balance DB 조회 실패: %s", exc)

        # 폴백: 현재 잔고
        return await _fetch_current_balance_jpy(db)


# ── 싱글턴 ──────────────────────────────────────────────────

_instance: CanaryMonitor | None = None


def get_canary_monitor() -> CanaryMonitor:
    global _instance
    if _instance is None:
        _instance = CanaryMonitor()
    return _instance
