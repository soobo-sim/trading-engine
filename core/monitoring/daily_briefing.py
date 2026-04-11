"""
DailyBriefing — 09:00 JST 일간 브리핑 알림.

매일 09:00 JST에 Telegram으로 일간 브리핑을 전송한다.
브리핑 내용: 전일 트레이드 요약 + 현재 포지션 + 잔고.

동작:
  1. 서비스 시작 시 다음 09:00 JST까지 sleep
  2. 브리핑 생성 + Telegram 전송
  3. 24시간 sleep → 반복

ENABLE_DAILY_BRIEFING=true 환경변수로 활성화.

설계서: trader-common/docs/specs/ai-native/05_OPERATIONS_GUIDE.md §3-2
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))
_BRIEFING_HOUR_JST = 9   # 09:00 JST
_BRIEFING_MINUTE_JST = 0


class DailyBriefing:
    """09:00 JST 일간 브리핑 발송 태스크.

    Args:
        session_factory:    AsyncSession 팩토리 (전일 트레이드 조회).
        trade_model:        ORM Trade 모델 (bf_trades / gmo_trades).
        pairs:              list[str] — 보고 대상 페어.
        bot_token:          Telegram Bot Token.
        chat_id:            Telegram Chat ID.
        adapter:            거래소 어댑터 (현재 잔고/포지션 조회용).
        send_message:       send_telegram_message 함수 (DI용, 기본=auto-import).
    """

    def __init__(
        self,
        session_factory: Any,
        trade_model: Any,
        pairs: list[str],
        bot_token: str,
        chat_id: str,
        adapter: Any | None = None,
        send_message: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._trade_model = trade_model
        self._pairs = pairs
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._adapter = adapter
        self._send_message = send_message

        self._task: Optional[asyncio.Task] = None

    # ─────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────

    async def start(self) -> None:
        """asyncio 태스크 시작."""
        if not self._bot_token or not self._chat_id:
            logger.warning("[DailyBriefing] TELEGRAM 설정 없음 — 시작 스킵")
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="daily_briefing")
        logger.debug("[DailyBriefing] 시작")

    async def stop(self) -> None:
        """태스크 취소."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.debug("[DailyBriefing] 종료")

    # ─────────────────────────────────────────────
    # 내부 루프
    # ─────────────────────────────────────────────

    async def _run(self) -> None:
        """다음 09:00 JST까지 sleep → 브리핑 → 24H sleep 반복."""
        while True:
            wait_sec = self._seconds_until_next_briefing()
            logger.debug(f"[DailyBriefing] 다음 브리핑까지 {wait_sec/3600:.1f}H 대기")
            try:
                await asyncio.sleep(wait_sec)
            except asyncio.CancelledError:
                raise

            try:
                await self._send_briefing()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[DailyBriefing] 브리핑 발송 오류 (재시도 내일): {e}")

    def _seconds_until_next_briefing(self) -> float:
        """현재 시각부터 다음 09:00 JST까지 남은 초."""
        now_jst = datetime.now(_JST)
        target = now_jst.replace(
            hour=_BRIEFING_HOUR_JST,
            minute=_BRIEFING_MINUTE_JST,
            second=0,
            microsecond=0,
        )
        if now_jst >= target:
            target += timedelta(days=1)
        delta = (target - now_jst).total_seconds()
        return max(delta, 1.0)

    async def _send_briefing(self) -> None:
        """브리핑 텍스트 생성 → Telegram 전송."""
        text = await self._build_briefing_text()
        _send = self._send_message
        if _send is None:
            from core.task.auto_reporter import send_telegram_message
            _send = send_telegram_message

        sent = await _send(
            bot_token=self._bot_token,
            chat_id=self._chat_id,
            text=text,
        )
        if sent:
            logger.info("[DailyBriefing] 브리핑 전송 완료")
        else:
            logger.warning("[DailyBriefing] 브리핑 전송 실패")

    async def _build_briefing_text(self) -> str:
        """브리핑 텍스트 조합."""
        from datetime import datetime as _dt
        now_jst = datetime.now(_JST).strftime("%Y-%m-%d %H:%M JST")

        sections = [f"📊 일간 브리핑 ({now_jst})"]

        # 전일 트레이드 요약
        trade_summary = await self._fetch_yesterday_summary()
        sections.append(trade_summary)

        # 현재 잔고/포지션
        balance_section = await self._fetch_balance_summary()
        if balance_section:
            sections.append(balance_section)

        return "\n\n".join(sections)

    async def _fetch_yesterday_summary(self) -> str:
        """전일 완료 트레이드 요약."""
        now_jst = datetime.now(_JST)
        yesterday_start_jst = (now_jst - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        yesterday_end_jst = yesterday_start_jst + timedelta(days=1)
        # UTC 변환
        yesterday_start_utc = yesterday_start_jst.astimezone(timezone.utc)
        yesterday_end_utc = yesterday_end_jst.astimezone(timezone.utc)

        try:
            from sqlalchemy import select, and_
            model = self._trade_model

            # 해당 모델이 created_at 컬럼을 가지고 있다고 가정
            date_column = getattr(model, "created_at", None)
            if date_column is None:
                return "전일 트레이드: 조회 불가"

            async with self._session_factory() as session:
                stmt = (
                    select(model)
                    .where(and_(
                        date_column >= yesterday_start_utc,
                        date_column < yesterday_end_utc,
                    ))
                )
                result = await session.execute(stmt)
                trades = result.scalars().all()

            if not trades:
                return "전일 트레이드: 없음"

            total = len(trades)
            # 손익 계산 — realized_pnl_jpy 컬럼이 있으면 합산
            wins = losses = 0
            total_pnl = 0.0
            for t in trades:
                pnl = getattr(t, "realized_pnl_jpy", None)
                if pnl is not None:
                    total_pnl += float(pnl)
                    if pnl >= 0:
                        wins += 1
                    else:
                        losses += 1

            return (
                f"📈 전일 트레이드\n"
                f"  총 {total}건 (승: {wins}, 패: {losses})\n"
                f"  실현 손익: ¥{total_pnl:+,.0f}"
            )
        except Exception as e:
            logger.warning(f"[DailyBriefing] 전일 트레이드 조회 실패 — {e}")
            return "전일 트레이드: 조회 실패"

    async def _fetch_balance_summary(self) -> str:
        """현재 잔고 요약."""
        if self._adapter is None:
            return ""
        try:
            balance = await self._adapter.get_balance()
            lines = ["💰 현재 잔고"]
            for currency, cb in balance.currencies.items():
                lines.append(f"  {currency.upper()}: {cb.amount:,.0f} (가용: {cb.available:,.0f})")
            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"[DailyBriefing] 잔고 조회 실패 — {e}")
            return ""
