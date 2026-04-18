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

from core.punisher.monitoring.maintenance import is_maintenance_window

if TYPE_CHECKING:
    from .health import SafetyCheck

logger = logging.getLogger(__name__)


class SafetyChecksMixin:
    """SF-01 ~ SF-10 안전장치 체크 + Telegram 직접 경고."""

    # 포지션 type별 태스크명 매핑
    _TASK_MAP = {
        "trend": {"sf01": "trend_stoploss", "sf02": "trend_candle"},
        "box":   {"sf01": "box_entry",      "sf02": "box_monitor"},
    }

    def _check_sf01_sf02(
        self,
        task_health: dict[str, dict],
        positions: list[dict],
        has_positions: bool,
    ) -> list["SafetyCheck"]:
        """SF-01: 스탑로스/진입 감시 태스크, SF-02: 캔들/박스 모니터 태스크."""
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

        # pair+type 조합별로 체크 (같은 pair에 trend/box 동시 가능)
        seen: set[tuple[str, str]] = set()
        for pos in positions:
            pair = pos["pair"]
            pos_type = pos.get("type", "trend")
            key = (pair, pos_type)
            if key in seen:
                continue
            seen.add(key)

            task_names = self._TASK_MAP.get(pos_type, self._TASK_MAP["trend"])

            # SF-01
            sf01_task = f"{task_names['sf01']}:{pair}"
            sf01_info = task_health.get(sf01_task)
            if sf01_info is None:
                checks.append(SafetyCheck(
                    id="SF-01", name="스탑로스 감시", status="critical",
                    severity="critical", detail=f"{sf01_task} 미등록", pair=pair,
                ))
            elif not sf01_info.get("alive", False):
                restarts = sf01_info.get("restarts", 0)
                checks.append(SafetyCheck(
                    id="SF-01", name="스탑로스 감시", status="critical",
                    severity="critical",
                    detail=f"{sf01_task} 죽음, restarts={restarts}",
                    pair=pair,
                ))
            else:
                restarts = sf01_info.get("restarts", 0)
                checks.append(SafetyCheck(
                    id="SF-01", name="스탑로스 감시", status="ok",
                    severity="critical",
                    detail=f"alive, restarts={restarts}",
                    pair=pair,
                ))

            # SF-02
            sf02_task = f"{task_names['sf02']}:{pair}"
            sf02_info = task_health.get(sf02_task)
            if sf02_info is None:
                checks.append(SafetyCheck(
                    id="SF-02", name="캔들 모니터", status="critical",
                    severity="critical", detail=f"{sf02_task} 미등록", pair=pair,
                ))
            elif not sf02_info.get("alive", False):
                restarts = sf02_info.get("restarts", 0)
                checks.append(SafetyCheck(
                    id="SF-02", name="캔들 모니터", status="critical",
                    severity="critical",
                    detail=f"{sf02_task} 죽음, restarts={restarts}",
                    pair=pair,
                ))
            else:
                restarts = sf02_info.get("restarts", 0)
                checks.append(SafetyCheck(
                    id="SF-02", name="캔들 모니터", status="ok",
                    severity="critical",
                    detail=f"alive, restarts={restarts}",
                    pair=pair,
                ))

        return checks

    def _check_sf03(self, ws_connected: bool) -> "SafetyCheck":
        """SF-03: WebSocket 연결. API 키 미설정 또는 활성 전략 없으면 스킵."""
        from .health import SafetyCheck

        # 정기 메인터넌스 중 → n/a (WS 연결 불가는 정상)
        if is_maintenance_window(os.getenv("EXCHANGE", "")):
            return SafetyCheck(
                id="SF-03", name="WebSocket", status="n/a",
                severity="critical",
                detail="정기 메인터넌스 중 — WS 연결 불가 정상",
            )

        # API 키 미설정 시 스킵
        if hasattr(self._adapter, "has_credentials") and not self._adapter.has_credentials():
            return SafetyCheck(
                id="SF-03", name="WebSocket", status="n/a",
                severity="critical",
                detail="API key not configured — skip",
            )

        # 활성 전략 없으면 WS 불필요 → 스킵
        has_active = getattr(self, "_has_active_strategies", None)
        if has_active is not None and not has_active:
            return SafetyCheck(
                id="SF-03", name="WebSocket", status="n/a",
                severity="critical",
                detail="활성 전략 없음 — WS 불필요",
            )

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
        active_box_pairs: set[str] | None = None,
    ) -> list["SafetyCheck"]:
        """SF-04: 오픈 포지션에 스탑 가격 설정 여부.

        - trend 포지션: stop_loss_price 컬럼 존재 여부로 판정
        - box 포지션: active box 존재 여부로 판정 (박스 무효화 감시 중)
        """
        from .health import SafetyCheck

        if not has_positions:
            return [SafetyCheck(
                id="SF-04", name="스탑 가격 설정", status="n/a",
                severity="critical", detail="포지션 없음 (해당없음)",
            )]

        _active_box_pairs = active_box_pairs or set()
        checks: list[SafetyCheck] = []
        for pos in positions:
            pair = pos["pair"]
            pos_type = pos.get("type", "trend")

            if pos_type == "box":
                # box 포지션: active box 존재 = 박스 무효화 감시 중 → ok
                if pair in _active_box_pairs:
                    checks.append(SafetyCheck(
                        id="SF-04", name="스탑 가격 설정", status="ok",
                        severity="critical",
                        detail="박스 무효화 감시 중",
                        pair=pair,
                    ))
                else:
                    checks.append(SafetyCheck(
                        id="SF-04", name="스탑 가격 설정", status="critical",
                        severity="critical",
                        detail="박스 포지션 있으나 active box 없음",
                        pair=pair,
                    ))
            else:
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

        # 정기 메인터넌스 중 → n/a (API 응답 불가는 정상)
        if is_maintenance_window(os.getenv("EXCHANGE", "")):
            return SafetyCheck(
                id="SF-06", name="거래소 API", status="n/a",
                severity="critical",
                detail="정기 메인터넌스 중 — API 응답 불가 정상",
            )

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

    # 거래소 표시명 매핑
    _EXCHANGE_DISPLAY = {"BITFLYER": "BF", "GMOFX": "GMO FX"}

    def _get_exchange_display_name(self) -> str:
        """EXCHANGE 환경변수 → 표시명."""
        raw = os.getenv("EXCHANGE", "unknown").upper()
        return self._EXCHANGE_DISPLAY.get(raw, raw)

    async def _send_safety_telegram_alert(self, checks: list["SafetyCheck"]) -> None:
        """안전장치 이상 시 사람이 읽기 쉬운 형태로 Telegram 경고 전송."""
        from core.punisher.monitoring.maintenance import is_maintenance_window

        exchange_raw = os.getenv("EXCHANGE", "unknown")
        if is_maintenance_window(exchange_raw):
            logger.debug(f"[Safety] {exchange_raw} 정기 메인터넌스 중 — 경고 스킵")
            return

        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not bot_token or not chat_id:
            logger.warning("[Safety] TELEGRAM_BOT_TOKEN/CHAT_ID 미설정 — 직접 경고 불가")
            return

        now = time.time()
        last_sent = self._telegram_alert_cooldown.get("safety", 0)
        if now - last_sent < self.TELEGRAM_ALERT_COOLDOWN_SEC:
            logger.debug(f"[Safety] Telegram 경고 쿨다운 중 ({int(now - last_sent)}s ago)")
            return

        critical_checks = [c for c in checks if c.status == "critical"]
        warning_checks = [c for c in checks if c.status == "warning"]

        if not critical_checks and not warning_checks:
            return

        exchange = self._get_exchange_display_name()
        total_issues = len(critical_checks) + len(warning_checks)

        # 포지션 유무 확인 (긴급도 판단용)
        has_position = False
        try:
            positions = await self._get_open_positions()
            has_position = len(positions) > 0
        except Exception:
            pass

        # 헤더
        if critical_checks:
            header = f"🔴 [{exchange}] 시스템 경고 ({total_issues}건)"
        else:
            header = f"⚠️ [{exchange}] 시스템 주의 ({total_issues}건)"

        lines = [header, ""]

        all_failed = critical_checks + warning_checks
        for c in all_failed:
            emoji = "🔴" if c.status == "critical" else "⚠️"
            desc = self._human_readable_description(c, has_position)
            lines.append(f"{emoji} {desc}")

        # 종합 긴급도
        if has_position and critical_checks:
            lines.append("")
            lines.append("⚡ 포지션 보유 중 — 즉시 확인 필요")
        elif not has_position and not critical_checks:
            lines.append("")
            lines.append("ℹ️ 포지션 없음 — 즉시 위험 없음")

        # 조치 안내
        actions = self._build_human_actions(all_failed)
        if actions:
            lines.append("")
            for a in actions:
                lines.append(f"→ {a}")

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

    @staticmethod
    def _human_readable_description(check: "SafetyCheck", has_position: bool) -> str:
        """SF 코드 대신 사람이 이해할 수 있는 설명 생성."""
        pair_str = f" ({check.pair})" if check.pair else ""

        descriptions = {
            "SF-01": {
                "title": f"자동매매 감시 중단{pair_str}",
                "impact": "스탑로스/진입 감시가 멈춤" + (" → 포지션 보호 불가" if has_position else ""),
            },
            "SF-02": {
                "title": f"캔들/박스 모니터 중단{pair_str}",
                "impact": "새 캔들 감지·박스 갱신 불가",
            },
            "SF-03": {
                "title": "실시간 시세 수신 중단",
                "impact": "실시간 가격 피드 끊김 → 4H봉 기준으로만 동작" + (" → 스탑 지연 위험" if has_position else ""),
            },
            "SF-04": {
                "title": f"스탑로스 미설정{pair_str}",
                "impact": "손절 안전장치 없이 포지션 보유 중",
            },
            "SF-05": {
                "title": f"최대 포지션 한도 초과{pair_str}",
                "impact": "설정된 자본 비율 초과 진입",
            },
            "SF-06": {
                "title": "거래소 API 응답 없음",
                "impact": "주문·잔고 조회 불가",
            },
            "SF-07": {
                "title": f"잔고-포지션 불일치{pair_str}",
                "impact": "DB 기록과 실제 거래소 잔고가 다름",
            },
            "SF-08": {
                "title": f"Kill 조건 접근{pair_str}",
                "impact": "연패/손실 누적이 전략 중단 기준에 근접",
            },
            "SF-09": {
                "title": f"스탑로스 가격 이상{pair_str}",
                "impact": "설정된 스탑 가격이 현재가 대비 비정상",
            },
            "SF-10": {
                "title": f"주문 실패 반복{pair_str}",
                "impact": "거래소 주문이 연속 거부됨",
            },
        }

        info = descriptions.get(check.id, {"title": f"{check.name}{pair_str}", "impact": check.detail})
        detail_extra = f" ({check.detail})" if check.detail and check.detail not in info["impact"] else ""
        return f"{info['title']}\n   영향: {info['impact']}{detail_extra}"

    @staticmethod
    def _build_human_actions(failed_checks: list["SafetyCheck"]) -> list[str]:
        """실패 항목 → 구체적 조치 안내."""
        actions: list[str] = []
        ids = {c.id for c in failed_checks}

        if "SF-01" in ids or "SF-02" in ids:
            actions.append("자동 복구 시도 중. 복구 안 되면 서버 재시작 필요 (docker restart)")
        if "SF-03" in ids:
            actions.append("자동 재연결 시도 중. 5분 내 복구 안 되면 서버 재시작 필요")
        if "SF-04" in ids or "SF-09" in ids:
            actions.append("스탑로스 즉시 확인 — 대시보드 관제 페이지에서 포지션 상태 점검")
        if "SF-06" in ids:
            actions.append("거래소 사이트 접속 확인. API 키 만료 가능성 점검")
        if "SF-07" in ids:
            actions.append("거래소 사이트에서 실제 잔고 확인 후, 불일치 시 레이첼에게 점검 요청")

        return actions
