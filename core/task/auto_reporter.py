"""
AutoReporter — 15분 주기 자동 모니터링 보고 (Telegram 전송).

사만다 대신 서버에서 직접 /api/monitoring/report 로직을 호출하고
결과의 telegram_text를 Telegram Bot API로 전송한다.

환경변수:
    AUTO_REPORT_ENABLED       — true/false (기본 false)
    AUTO_REPORT_INTERVAL_MIN  — 분 단위 주기 (기본 15)
    AUTO_REPORT_BOT_TOKEN     — Telegram Bot API 토큰
    AUTO_REPORT_CHAT_ID       — 수신 chat_id
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


async def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    client: httpx.AsyncClient | None = None,
    max_retries: int = 2,
    backoff_base: float = 1.0,
) -> bool:
    """Telegram Bot API로 메시지 전송. 최대 max_retries회 재시도 (exponential backoff)."""
    url = TELEGRAM_API.format(token=bot_token)
    payload = {"chat_id": chat_id, "text": text}
    owns_client = client is None

    for attempt in range(max_retries + 1):
        try:
            if owns_client:
                client = httpx.AsyncClient(timeout=10)
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    return True
                # 4xx (429 제외)는 재시도 무의미
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    logger.error(
                        f"Telegram 전송 실패 (재시도 불가): status={resp.status_code} body={resp.text}"
                    )
                    return False
                logger.warning(
                    f"Telegram 전송 실패 (attempt {attempt+1}/{max_retries+1}): "
                    f"status={resp.status_code}"
                )
            finally:
                if owns_client:
                    await client.aclose()
                    client = None
        except Exception as e:
            logger.warning(
                f"Telegram 전송 예외 (attempt {attempt+1}/{max_retries+1}): {e}"
            )

        if attempt < max_retries:
            delay = backoff_base * (2 ** attempt)
            await asyncio.sleep(delay)

    logger.error(f"Telegram 전송 최종 실패: {max_retries+1}회 시도 후 포기")
    return False


class AutoReporter:
    """asyncio 태스크 기반 자동 보고기."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        state: "AppState",  # noqa: F821 — forward ref
        bot_token: str,
        chat_id: str,
        interval_min: int = 15,
    ):
        self._session_factory = session_factory
        self._state = state
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._interval_sec = interval_min * 60
        self._task: asyncio.Task | None = None
        self._http_client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            logger.warning("AutoReporter 이미 실행 중")
            return
        self._http_client = httpx.AsyncClient(timeout=10)
        self._task = asyncio.create_task(self._loop(), name="auto_reporter")
        logger.info(
            f"AutoReporter 시작: interval={self._interval_sec}s, "
            f"chat_id={self._chat_id}"
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("AutoReporter 종료")

    async def _loop(self) -> None:
        """주기적으로 보고 생성 + 전송."""
        # 첫 보고는 interval 후
        await asyncio.sleep(self._interval_sec)
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"AutoReporter 보고 실패 (다음 주기 정상 실행): {e}")
            await asyncio.sleep(self._interval_sec)

    async def _run_once(self) -> None:
        """활성 전략 전체에 대해 보고 생성 → Telegram 전송 + 손실 포지션 감지."""
        state = self._state
        async with self._session_factory() as db:
            # 손실 포지션 감지 (WAKE_UP_REVIEW_AUTO)
            try:
                from core.task.loss_detector import detect_and_notify_losses
                trend_model = getattr(state.models, "trend_position", None)
                if trend_model is not None:
                    sent = await detect_and_notify_losses(
                        db, trend_model,
                        prefix=state.prefix,
                        http_client=self._http_client,
                    )
                    if sent:
                        logger.info(f"Loss detector: {sent}건 webhook 전송")
            except Exception as e:
                logger.error(f"Loss detector 실패 (보고는 계속): {e}")

            # Kill 조건 자동 체크
            try:
                from core.monitoring.kill_checker import run_kill_checks
                trend_model = getattr(state.models, "trend_position", None)
                if trend_model is not None:
                    killed = await run_kill_checks(
                        db, trend_model, http_client=self._http_client
                    )
                    if killed:
                        logger.info(f"[KillChecker] {killed}건 Kill 발동")
                    else:
                        logger.debug("[KillChecker] Kill 조건 미충족")
            except Exception as e:
                logger.error(f"Kill checker 실패 (보고는 계속): {e}")

            # 활성 전략 조회
            StrategyModel = state.models.strategy
            result = await db.execute(
                select(StrategyModel).where(StrategyModel.status == "active")
            )
            active_strategies = result.scalars().all()

            for strategy in active_strategies:
                params = strategy.parameters or {}
                pair = params.get("pair") or params.get("product_code")
                style = params.get("trading_style")
                if not pair or not style:
                    continue

                try:
                    report = await self._generate_report(
                        style, pair, strategy, state, db
                    )
                except Exception as e:
                    logger.error(f"보고 생성 실패 [{pair}]: {e}")
                    continue

                if not report or not report.get("success"):
                    logger.warning(f"보고 실패 [{pair}]: {report}")
                    continue

                telegram_text = report.get("report", {}).get("telegram_text")
                if not telegram_text:
                    continue

                # 안전장치 요약 추가
                safety = report.get("safety", {})
                safety_summary = safety.get("summary")
                if safety_summary:
                    telegram_text += f"\n{safety_summary}"

                await send_telegram_message(
                    self._bot_token, self._chat_id, telegram_text,
                    client=self._http_client,
                )
                logger.info(f"자동 보고 전송 완료: {pair}")

    async def _generate_report(
        self,
        style: str,
        pair: str,
        strategy,
        state: "AppState",
        db: AsyncSession,
    ) -> dict | None:
        from api.services.monitoring import (
            generate_trend_report,
            generate_box_report,
            generate_cfd_report,
        )
        from dataclasses import asdict

        kwargs = dict(
            pair=pair,
            prefix=state.prefix,
            pair_column=state.pair_column,
            strategy=strategy,
            adapter=state.adapter,
            candle_model=state.models.candle,
            db=db,
        )

        if style == "trend_following":
            report = await generate_trend_report(
                trend_manager=state.trend_manager, **kwargs
            )
        elif style == "box_mean_reversion":
            report = await generate_box_report(
                health_checker=state.health_checker,
                box_model=state.models.box,
                box_position_model=state.models.box_position,
                **kwargs,
            )
        elif style == "cfd_trend_following":
            report = await generate_cfd_report(
                cfd_manager=state.cfd_manager, **kwargs
            )
        else:
            return None

        # 안전장치 요약 추가
        if report and report.get("success"):
            try:
                safety_report = await state.health_checker.check_safety_only()
                from core.monitoring.health import format_safety_summary
                summary = format_safety_summary(safety_report)
                report["safety"] = {
                    "status": safety_report.status,
                    "summary": summary,
                    "checks": [asdict(c) for c in safety_report.checks],
                }
            except Exception as e:
                logger.error(f"안전장치 체크 실패: {e}")
                report["safety"] = {
                    "status": "unknown",
                    "summary": "🛡️ 안전장치: ❓ 체크 실패",
                }

        return report


def create_auto_reporter(
    session_factory: async_sessionmaker,
    state: "AppState",
) -> AutoReporter | None:
    """환경변수 기반으로 AutoReporter 생성. 비활성이면 None 반환."""
    enabled = os.environ.get("AUTO_REPORT_ENABLED", "false").lower() == "true"
    if not enabled:
        logger.info("AutoReporter 비활성 (AUTO_REPORT_ENABLED=false)")
        return None

    bot_token = os.environ.get("AUTO_REPORT_BOT_TOKEN", "")
    chat_id = os.environ.get("AUTO_REPORT_CHAT_ID", "")
    interval_min = int(os.environ.get("AUTO_REPORT_INTERVAL_MIN", "15"))

    if not bot_token or not chat_id:
        logger.error(
            "AutoReporter: AUTO_REPORT_BOT_TOKEN 또는 AUTO_REPORT_CHAT_ID 미설정"
        )
        return None

    return AutoReporter(
        session_factory=session_factory,
        state=state,
        bot_token=bot_token,
        chat_id=chat_id,
        interval_min=interval_min,
    )
