"""
TaskSupervisor — 중앙집중 asyncio 태스크 생명주기 관리.

기존 TrendFollowingManager에서 발생한 문제:
  - start() 동시 호출 시 태스크 중복 생성 (race condition, 2026-03-16)
  - 태스크 예외 시 조용히 죽음 (헬스체크가 alive만 체크)
  - 태스크 재시작 로직 없음

TaskSupervisor가 이를 해결:
  1. Lock 기반 등록으로 동일 이름 태스크 중복 방지
  2. 래퍼가 예외를 감지 → 자동 재시작 (exponential backoff)
  3. 구조화된 헬스 리포트 (alive, restarts, last_error)
  4. graceful shutdown (전체 취소 + await)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)


@dataclass
class TaskInfo:
    """태스크 메타데이터."""
    name: str
    task: asyncio.Task[None]
    restart_count: int = 0
    max_restarts: int = 5
    last_error: Optional[str] = None
    last_error_at: Optional[datetime] = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    auto_restart: bool = True

    @property
    def alive(self) -> bool:
        return not self.task.done()


class TaskSupervisor:
    """
    asyncio 태스크 등록 · 감시 · 재시작 · 종료.

    사용법:
        supervisor = TaskSupervisor()

        await supervisor.register(
            "trend_candle:xrp_jpy",
            candle_monitor_coro,
            max_restarts=5,
        )

        health = supervisor.get_health()
        await supervisor.stop("trend_candle:xrp_jpy")
        await supervisor.stop_all()
    """

    def __init__(self) -> None:
        self._tasks: dict[str, TaskInfo] = {}
        self._lock = asyncio.Lock()

    # ── 등록 ────────────────────────────────

    async def register(
        self,
        name: str,
        coro_factory: Callable[[], Coroutine[Any, Any, None]],
        *,
        max_restarts: int = 5,
        auto_restart: bool = True,
    ) -> None:
        """
        태스크 등록 및 시작.

        이미 동일 이름이 실행 중이면 먼저 중지 후 재등록.
        coro_factory는 인자 없는 callable로, 호출할 때마다 새 coroutine을 반환해야 한다.
        (재시작 시 새 coroutine이 필요하므로 async def 자체가 아닌 lambda/partial 사용)

        Args:
            name:          고유 태스크 이름 (예: "trend_candle:xrp_jpy")
            coro_factory:  coroutine 팩토리 — 호출 시마다 새 coroutine 반환
            max_restarts:  자동 재시작 최대 횟수 (초과 시 포기)
            auto_restart:  True면 예외 시 자동 재시작
        """
        async with self._lock:
            if name in self._tasks and self._tasks[name].alive:
                logger.debug(f"[Supervisor] {name}: 이미 실행 중 → 교체")
                await self._cancel_task(self._tasks[name])

            info = TaskInfo(
                name=name,
                task=asyncio.create_task(
                    self._supervised_run(name, coro_factory, max_restarts, auto_restart),
                    name=name,
                ),
                max_restarts=max_restarts,
                auto_restart=auto_restart,
            )
            self._tasks[name] = info
            logger.debug(f"[Supervisor] {name}: 등록 완료")

    # ── 중지 ────────────────────────────────

    async def stop(self, name: str) -> None:
        """단일 태스크 중지."""
        async with self._lock:
            info = self._tasks.pop(name, None)
            if info:
                await self._cancel_task(info)
                logger.debug(f"[Supervisor] {name}: 중지 완료")

    async def stop_group(self, prefix: str) -> None:
        """prefix로 시작하는 모든 태스크 중지. (예: "trend_candle:xrp_jpy" → prefix="xrp_jpy")"""
        async with self._lock:
            to_remove = [n for n in self._tasks if n.endswith(f":{prefix}") or n == prefix]
            for name in to_remove:
                info = self._tasks.pop(name)
                await self._cancel_task(info)
            if to_remove:
                logger.debug(f"[Supervisor] 그룹 중지: {to_remove}")

    async def stop_all(self) -> None:
        """모든 태스크 graceful shutdown."""
        async with self._lock:
            names = list(self._tasks.keys())
            for name in names:
                info = self._tasks.pop(name)
                await self._cancel_task(info)
            logger.debug(f"[Supervisor] 전체 종료: {len(names)}개 태스크")

    # ── 조회 ────────────────────────────────

    def is_running(self, name: str) -> bool:
        info = self._tasks.get(name)
        return info is not None and info.alive

    def running_names(self) -> list[str]:
        return [n for n, info in self._tasks.items() if info.alive]

    def get_health(self) -> dict[str, dict]:
        """
        전체 태스크 헬스 리포트.

        Returns:
            {name: {alive, restarts, max_restarts, last_error, started_at}}
        """
        result = {}
        for name, info in self._tasks.items():
            entry: dict[str, Any] = {
                "alive": info.alive,
                "restarts": info.restart_count,
                "max_restarts": info.max_restarts,
                "started_at": info.started_at.isoformat(),
            }
            if info.last_error:
                entry["last_error"] = info.last_error
                entry["last_error_at"] = info.last_error_at.isoformat() if info.last_error_at else None
            if not info.alive and not info.task.cancelled():
                exc = info.task.exception() if not info.task.cancelled() else None
                if exc:
                    entry["final_exception"] = repr(exc)
            result[name] = entry
        return result

    def get_health_for(self, pair: str) -> dict[str, dict]:
        """특정 pair 관련 태스크 헬스만 반환."""
        return {n: v for n, v in self.get_health().items() if pair in n}

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    @property
    def alive_count(self) -> int:
        return sum(1 for info in self._tasks.values() if info.alive)

    # ── 내부 ────────────────────────────────

    async def _supervised_run(
        self,
        name: str,
        coro_factory: Callable[[], Coroutine[Any, Any, None]],
        max_restarts: int,
        auto_restart: bool,
    ) -> None:
        """래퍼: 예외 감지 → 로깅 → backoff 재시작."""
        restarts = 0
        backoff = 1.0  # 초기 backoff (초)

        while True:
            try:
                await coro_factory()
                # coroutine이 정상 종료 — 재시작 안 함
                logger.debug(f"[Supervisor] {name}: 정상 종료")
                return
            except asyncio.CancelledError:
                logger.debug(f"[Supervisor] {name}: 취소됨")
                return
            except Exception as exc:
                restarts += 1
                error_msg = f"{type(exc).__name__}: {exc}"
                logger.error(f"[Supervisor] {name}: 예외 발생 ({restarts}/{max_restarts}) — {error_msg}")

                # TaskInfo 업데이트 (메인 dict에서 참조)
                info = self._tasks.get(name)
                if info:
                    info.restart_count = restarts
                    info.last_error = error_msg
                    info.last_error_at = datetime.now(timezone.utc)

                if not auto_restart or restarts >= max_restarts:
                    logger.error(f"[Supervisor] {name}: 재시작 한도 초과 — 포기")
                    return

                wait = min(backoff * (2 ** (restarts - 1)), 60.0)  # 최대 60초
                logger.debug(f"[Supervisor] {name}: {wait:.1f}초 후 재시작")
                await asyncio.sleep(wait)

    @staticmethod
    async def _cancel_task(info: TaskInfo) -> None:
        """태스크 취소 + 완료 대기."""
        if not info.task.done():
            info.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(info.task), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
