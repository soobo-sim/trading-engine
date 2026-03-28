"""
안전장치 체크 믹스인 — SF-01 ~ SF-10 + Telegram 경고.

HealthChecker 클래스에 믹스인으로 합성되며,
self._adapter, self._supervisor, self._trend_manager 등을 사용한다.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .health import SafetyCheck

logger = logging.getLogger(__name__)


class SafetyChecksMixin:
    """SF-01 ~ SF-10 안전장치 체크 + Telegram 직접 경고."""

    def _check_sf01_sf02(
        self,
        task_health: dict[str, dict],
        positions: list[dict],
        has_positions: bool,
    ) -> list["SafetyCheck"]:
        """SF-01: 스탑로스 태스크, SF-02: 캔들 모니터 태스크."""
        from .health import SafetyCheck

        checks: list[SafetyCheck] = []

        if not has_positions:
            checks.append(SafetyCheck(
                id="SF-01", name="스탑로스 감시", status="n/a",
                severity="critical", detail="포지션 없음 (해당없음)",
            ))
            checks.append(SafetyCheck(
                id="SF-02", name="캔들 모니터", status="n/a",
                severity="critical", detail="포지션 없음 (해당없음)",
            ))
            return checks

        pairs_seen: set[str] = set()
        for pos in positions:
            pair = pos["pair"]
            if pair in pairs_seen:
                continue
            pairs_seen.add(pair)

            # SF-01: trend_stoploss:{pair}
            sl_task_name = f"trend_stoploss:{pair}"
            sl_info = task_health.get(sl_task_name)
            if sl_info is None:
                checks.append(SafetyCheck(
                    id="SF-01", name="스탑로스 감시", status="critical",
                    severity="critical", detail=f"{sl_task_name} 미등록", pair=pair,
                ))
            elif not sl_info.get("alive", False):
                restarts = sl_info.get("restarts", 0)
                checks.append(SafetyCheck(
                    id="SF-01", name="스탑로스 감시", status="critical",
                    severity="critical",
                    detail=f"{sl_task_name} 죽음, restarts={restarts}",
                    pair=pair,
                ))
            else:
                restarts = sl_info.get("restarts", 0)
                checks.append(SafetyCheck(
                    id="SF-01", name="스탑로스 감시", status="ok",
                    severity="critical",
                    detail=f"alive, restarts={restarts}",
                    pair=pair,
                ))

            # SF-02: trend_candle:{pair}
            candle_task_name = f"trend_candle:{pair}"
            candle_info = task_health.get(candle_task_name)
            if candle_info is None:
                checks.append(SafetyCheck(
                    id="SF-02", name="캔들 모니터", status="critical",
                    severity="critical", detail=f"{candle_task_name} 미등록", pair=pair,
                ))
            elif not candle_info.get("alive", False):
                restarts = candle_info.get("restarts", 0)
                checks.append(SafetyCheck(
                    id="SF-02", name="캔들 모니터", status="critical",
                    severity="critical",
                    detail=f"{candle_task_name} 죽음, restarts={restarts}",
                    pair=pair,
                ))
            else:
                restarts = candle_info.get("restarts", 0)
                checks.append(SafetyCheck(
                    id="SF-02", name="캔들 모니터", status="ok",
                    severity="critical",
                    detail=f"alive, restarts={restarts}",
                    pair=pair,
                ))

        return checks

    def _check_sf03(self, ws_connected: bool) -> "SafetyCheck":
        """SF-03: WebSocket 연결."""
        from .health import SafetyCheck

        if ws_connected:
            return SafetyCheck(
                id="SF-03", name="WebSocket", status="ok",
                severity="critical", detail="connected",
            )
        return SafetyCheck(
            id="SF-03", name="WebSocket", status="critical",
            severity="critical", detail="연결 끊김",
        )

    def _check_sf04(
        self, positions: list[dict], has_positions: bool,
    ) -> list["SafetyCheck"]:
        """SF-04: 오픈 포지션에 스탑 가격 설정 여부."""
        from .health import SafetyCheck

        if not has_positions:
            return [SafetyCheck(
                id="SF-04", name="스탑 가격 설정", status="n/a",
                severity="critical", detail="포지션 없음 (해당없음)",
            )]

        checks: list[SafetyCheck] = []
        for pos in positions:
            pair = pos["pair"]
            stop = pos.get("stop_loss_price")
            if stop and stop > 0:
                checks.append(SafetyCheck(
                    id="SF-04", name="스탑 가격 설정", status="ok",
                    severity="critical",
                    detail=f"stop=¥{stop:,.0f}",
                    pair=pair,
                ))
            else:
                checks.append(SafetyCheck(
                    id="SF-04", name="스탑 가격 설정", status="critical",
                    severity="critical",
                    detail="스탑 가격 미설정",
                    pair=pair,
                ))
        return checks

    async def _check_sf05(self) -> "SafetyCheck":
        """SF-05: 레이첼 webhook 파이프라인 — 환경변수 존재 + health ping."""
        from .health import SafetyCheck

        webhook_url = os.getenv("RACHEL_WEBHOOK_URL", "")
        webhook_token = os.getenv("RACHEL_WEBHOOK_TOKEN", "")

        if not webhook_url or not webhook_token:
            return SafetyCheck(
                id="SF-05", name="레이첼 webhook", status="warning",
                severity="high",
                detail="RACHEL_WEBHOOK_URL 또는 TOKEN 미설정",
            )

        try:
            from urllib.parse import urlparse
            parsed = urlparse(webhook_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            return SafetyCheck(
                id="SF-05", name="레이첼 webhook", status="warning",
                severity="high",
                detail=f"URL 파싱 실패: {webhook_url[:50]}",
            )

        try:
            import httpx
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                resp = await client.get(f"{base_url}/health")
                if resp.status_code < 500:
                    return SafetyCheck(
                        id="SF-05", name="레이첼 webhook", status="ok",
                        severity="high",
                        detail=f"설정됨 + ping 정상 ({base_url})",
                    )
                return SafetyCheck(
                    id="SF-05", name="레이첼 webhook", status="warning",
                    severity="high",
                    detail=f"ping 실패: HTTP {resp.status_code}",
                )
        except Exception as e:
            return SafetyCheck(
                id="SF-05", name="레이첼 webhook", status="warning",
                severity="high",
                detail=f"ping 실패: {str(e)[:80]}",
            )

    async def _check_sf06(self) -> "SafetyCheck":
        """SF-06: 거래소 API 응답 가능 여부. API 키 미설정 시 스킵."""
        from .health import SafetyCheck

        # API 키 미설정 시 스킵
        if hasattr(self._adapter, "has_credentials") and not self._adapter.has_credentials():
            return SafetyCheck(
                id="SF-06", name="거래소 API", status="n/a",
                severity="critical",
                detail="API key not configured — skip",
            )

        try:
            await self._adapter.get_balance()
            return SafetyCheck(
                id="SF-06", name="거래소 API", status="ok",
                severity="critical", detail="응답 정상",
            )
        except Exception as e:
            return SafetyCheck(
                id="SF-06", name="거래소 API", status="critical",
                severity="critical", detail=f"응답 실패: {e}",
            )

    def _check_sf07(self, discrepancies: list[dict]) -> "SafetyCheck":
        """SF-07: 잔고 정합성."""
        from .health import SafetyCheck

        if not discrepancies:
            return SafetyCheck(
                id="SF-07", name="잔고 정합성", status="ok",
                severity="high", detail="정합",
            )
        currencies = ", ".join(d["currency"] for d in discrepancies)
        return SafetyCheck(
            id="SF-07", name="잔고 정합성", status="warning",
            severity="high", detail=f"불일치: {currencies}",
        )

    def _check_sf08(self) -> "SafetyCheck":
        """SF-08: 사만사 15분 보고 주기."""
        from .health import SafetyCheck, _last_report_time

        last_ts = _last_report_time.get("last")
        if last_ts is None:
            return SafetyCheck(
                id="SF-08", name="사만사 15분 보고", status="ok",
                severity="high",
                detail="아직 보고 기록 없음 (서버 기동 직후)",
            )

        elapsed = time.time() - last_ts
        jst = timezone(timedelta(hours=9))
        last_dt = datetime.fromtimestamp(last_ts, tz=jst)
        last_str = last_dt.strftime("%H:%M")
        elapsed_min = int(elapsed / 60)

        if elapsed < 1200:  # 20분 (15분 주기 + 5분 여유)
            return SafetyCheck(
                id="SF-08", name="사만사 15분 보고", status="ok",
                severity="high",
                detail=f"last_report={last_str} JST ({elapsed_min}분 전)",
            )
        return SafetyCheck(
            id="SF-08", name="사만사 15분 보고", status="warning",
            severity="high",
            detail=f"마지막 보고 {elapsed_min}분 전 ({last_str} JST) — 20분 초과",
        )

    def _check_sf09(
        self, positions: list[dict], has_positions: bool,
    ) -> list["SafetyCheck"]:
        """SF-09: 트레일링 스탑 갱신 (이익 잠금 확인)."""
        from .health import SafetyCheck

        trend_positions = [p for p in positions if p["type"] == "trend"]
        if not trend_positions:
            return [SafetyCheck(
                id="SF-09", name="트레일링 스탑 갱신", status="n/a",
                severity="critical", detail="포지션 없음 (해당없음)",
            )]

        checks: list[SafetyCheck] = []
        for pos in trend_positions:
            pair = pos["pair"]
            stop = pos.get("stop_loss_price")
            entry = pos.get("entry_price")

            if not stop or stop <= 0:
                checks.append(SafetyCheck(
                    id="SF-09", name="트레일링 스탑 갱신", status="critical",
                    severity="critical",
                    detail="스탑 미설정 — 트레일링 미작동",
                    pair=pair,
                ))
                continue

            if entry and entry > 0:
                gap_pct = (stop - entry) / entry * 100
                if stop >= entry:
                    detail = f"stop=¥{stop:,.0f} ≥ entry=¥{entry:,.0f} (+{gap_pct:.1f}%) — 이익 잠금"
                else:
                    detail = f"stop=¥{stop:,.0f}, entry=¥{entry:,.0f} ({gap_pct:.1f}%)"
            else:
                detail = f"stop=¥{stop:,.0f}"

            checks.append(SafetyCheck(
                id="SF-09", name="트레일링 스탑 갱신", status="ok",
                severity="critical", detail=detail, pair=pair,
            ))
        return checks

    def _check_sf10(
        self, positions: list[dict], has_positions: bool,
    ) -> list["SafetyCheck"]:
        """SF-10: RSI > 75 시 스탑 타이트닝 확인."""
        from .health import SafetyCheck

        if self._trend_manager is None:
            return [SafetyCheck(
                id="SF-10", name="스탑 타이트닝", status="n/a",
                severity="high", detail="trend_manager 미연결",
            )]

        trend_positions = [p for p in positions if p["type"] == "trend"]
        if not trend_positions:
            return [SafetyCheck(
                id="SF-10", name="스탑 타이트닝", status="n/a",
                severity="high", detail="포지션 없음 (해당없음)",
            )]

        checks: list[SafetyCheck] = []
        for pos in trend_positions:
            pair = pos["pair"]
            rsi = self._trend_manager._last_rsi.get(pair)
            mem_pos = self._trend_manager.get_position(pair)

            if rsi is None:
                checks.append(SafetyCheck(
                    id="SF-10", name="스탑 타이트닝", status="n/a",
                    severity="high", detail="RSI 미산출", pair=pair,
                ))
                continue

            if rsi < 75:
                checks.append(SafetyCheck(
                    id="SF-10", name="스탑 타이트닝", status="n/a",
                    severity="high",
                    detail=f"RSI {rsi:.1f} < 75 — 타이트닝 조건 미충족",
                    pair=pair,
                ))
                continue

            if mem_pos is not None and mem_pos.stop_tightened:
                checks.append(SafetyCheck(
                    id="SF-10", name="스탑 타이트닝", status="ok",
                    severity="high",
                    detail=f"RSI {rsi:.1f} ≥ 75, tightened=True ✓",
                    pair=pair,
                ))
            else:
                checks.append(SafetyCheck(
                    id="SF-10", name="스탑 타이트닝", status="warning",
                    severity="high",
                    detail=f"RSI {rsi:.1f} ≥ 75이나 tightened=False — 확인 필요",
                    pair=pair,
                ))
        return checks

    # ── Telegram 직접 경고 ────────────────────────────────────

    _telegram_alert_cooldown: dict[str, float] = {}
    TELEGRAM_ALERT_COOLDOWN_SEC = 900  # 15분

    async def _send_safety_telegram_alert(self, checks: list["SafetyCheck"]) -> None:
        """안전장치 critical 시 서버가 직접 Telegram으로 경고 전송."""
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not bot_token or not chat_id:
            logger.warning("[Safety] TELEGRAM_BOT_TOKEN/CHAT_ID 미설정 — 직접 경고 불가")
            return

        now = time.time()
        last_sent = self._telegram_alert_cooldown.get("safety", 0)
        if now - last_sent < self.TELEGRAM_ALERT_COOLDOWN_SEC:
            logger.info(f"[Safety] Telegram 경고 쿨다운 중 ({int(now - last_sent)}s ago)")
            return

        critical_checks = [c for c in checks if c.status == "critical"]
        if not critical_checks:
            return

        lines = ["🔴🔴🔴 [시스템 경고] 안전장치 장애!"]
        for c in critical_checks:
            pair_str = f" ({c.pair})" if c.pair else ""
            lines.append(f"{c.id} {c.name}{pair_str} — {c.detail}")
        lines.append("즉시 확인 필요!")

        text = "\n".join(lines)

        try:
            import httpx
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json={
                    "chat_id": chat_id,
                    "text": text,
                })
                if resp.status_code == 200:
                    self._telegram_alert_cooldown["safety"] = now
                    logger.info("[Safety] Telegram 직접 경고 전송 완료")
                else:
                    logger.error(f"[Safety] Telegram 전송 실패: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"[Safety] Telegram 전송 오류: {e}")
