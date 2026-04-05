"""
HealthChecker — 비즈니스 로직 수준의 헬스 체크.

기존 CK/BF system.py는 WS 연결 + 태스크 생존만 확인했다.
HealthChecker는 추가로:
  - 포지션-잔고 정합성 (DB vs 실잔고)
  - 활성 전략 상태
  - 안전장치 상태 (SF-01~SF-07)
  - 전체 healthy 판정
을 수행한다.

FastAPI에 의존하지 않는다. api/routes/system.py가 이 클래스를 호출한다.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Type

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.exchange.base import ExchangeAdapter
from core.monitoring.maintenance import is_maintenance_window
from core.task.supervisor import TaskSupervisor
from .safety_checks import SafetyChecksMixin

logger = logging.getLogger(__name__)


@dataclass
class SafetyCheck:
    """개별 안전장치 체크 결과."""
    id: str           # "SF-01" ~ "SF-07"
    name: str         # 한글 이름
    status: str       # "ok" | "critical" | "warning" | "n/a"
    severity: str     # "critical" | "high"
    detail: str       # 상세 설명
    pair: str | None = None  # 해당 페어 (있으면)


def compute_safety_status(checks: list[SafetyCheck]) -> str:
    """
    안전장치 종합 판정.

    - CRITICAL severity 중 하나라도 status != "ok" and != "n/a" → "critical"
    - HIGH severity 중 하나라도 status != "ok" and != "n/a" → "degraded"
    - 전부 ok or n/a → "all_ok"
    """
    for c in checks:
        if c.severity == "critical" and c.status not in ("ok", "n/a"):
            return "critical"
    for c in checks:
        if c.severity == "high" and c.status not in ("ok", "n/a"):
            return "degraded"
    return "all_ok"


def format_safety_summary(report: "SafetyReport") -> str:
    """SafetyReport → 한 줄 요약 텍스트. n/a 포함 전체 카운트 (대시보드 UI 일치)."""
    total = len(report.checks)
    ok_count = sum(1 for c in report.checks if c.status in ("ok", "n/a"))
    if report.status == "all_ok":
        return f"🛡️ 안전장치: ✅ 전체 정상 ({ok_count}/{total})"
    elif report.status == "critical":
        names = [c.name for c in report.checks if c.status == "critical"]
        return f"🛡️ 안전장치: 🔴 {', '.join(names)} ({ok_count}/{total}) — 즉시 확인 필요"
    else:
        names = [c.name for c in report.checks if c.status == "warning"]
        return f"🛡️ 안전장치: 🟡 {', '.join(names)} ({ok_count}/{total})"


@dataclass
class SafetyReport:
    """안전장치 종합 보고."""
    status: str                  # "all_ok" | "degraded" | "critical"
    checks: list[SafetyCheck]
    last_checked: str            # ISO8601


@dataclass
class HealthReport:
    """헬스 체크 결과."""
    healthy: bool
    checked_at: str
    issues: list[str]
    ws_connected: bool
    ws_status: str  # "connected" | "disconnected" | "n/a (no active strategy)"
    tasks: dict[str, dict]
    active_strategies: list[dict]
    position_balance: list[dict]
    safety: SafetyReport | None = None


class HealthChecker(SafetyChecksMixin):
    """
    비즈니스 로직 수준 헬스 체크.

    생성자에서 의존성을 주입받아 거래소-무관으로 동작한다.
    SF-01~SF-10 안전장치 체크는 SafetyChecksMixin에서 제공.
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
        trend_manager: Any = None,
        box_model: Optional[Type] = None,
    ) -> None:
        self._adapter = adapter
        self._supervisor = supervisor
        self._session_factory = session_factory
        self._strategy_model = strategy_model
        self._trend_position_model = trend_position_model
        self._box_position_model = box_position_model
        self._pair_column = pair_column
        self._trend_manager = trend_manager
        self._box_model = box_model

    async def check(self) -> HealthReport:
        """전체 헬스 체크 수행."""
        issues: list[str] = []
        now = datetime.now(timezone.utc).isoformat()

        # 1. 활성 전략 (WS 체크보다 먼저)
        strategies = await self._get_active_strategies()
        self._has_active_strategies = len(strategies) > 0

        # 2. WS 연결 — 활성 전략 없으면 n/a (연결 불필요)
        ws_connected = self._adapter.is_ws_connected()
        if not ws_connected:
            if self._has_active_strategies:
                issues.append("ws: 연결 끊김")
            # else: 활성 전략 없음 → WS 미연결은 정상, issues에 추가 안 함

        # 3. 태스크 상태
        task_health = self._supervisor.get_health()
        for name, info in task_health.items():
            if not info.get("alive", False):
                last_err = info.get("last_error", "unknown")
                issues.append(f"task[{name}]: 죽음 ({last_err})")

        # (활성 전략은 위에서 이미 조회)

        # 4. 포지션-잔고 정합성
        discrepancies = await self._check_position_balance_consistency()
        for d in discrepancies:
            issues.append(
                f"balance_mismatch[{d['currency']}]: "
                f"expected={d['expected']:.8f}, actual={d['actual']:.8f}"
            )

        # 5. 안전장치 체크
        safety = await self._check_safety(ws_connected, task_health, discrepancies)

        # safety critical → healthy=False
        healthy = len(issues) == 0 and safety.status != "critical"

        # WS 상태 문자열 결정
        if ws_connected:
            ws_status = "connected"
        elif not self._has_active_strategies:
            ws_status = "n/a (no active strategy)"
        else:
            ws_status = "disconnected"

        return HealthReport(
            healthy=healthy,
            checked_at=now,
            issues=issues,
            ws_connected=ws_connected,
            ws_status=ws_status,
            tasks=task_health,
            active_strategies=strategies,
            position_balance=discrepancies,
            safety=safety,
        )

    async def check_safety_only(self) -> SafetyReport:
        """안전장치만 체크 (모니터링 리포트용 경량 호출)."""
        ws_connected = self._adapter.is_ws_connected()
        task_health = self._supervisor.get_health()
        discrepancies = await self._check_position_balance_consistency()
        # SF-03에서 활성 전략 유무 참조
        if not hasattr(self, "_has_active_strategies"):
            strategies = await self._get_active_strategies()
            self._has_active_strategies = len(strategies) > 0
        return await self._check_safety(ws_connected, task_health, discrepancies)

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

    # BUG-016: dust 판정 임계값 (이 이하의 코인 수량은 정합성 체크 스킵)
    _DUST_COIN_THRESHOLD = 0.0001
    # BUG-016: 진입 직후 grace period (초)
    _BALANCE_GRACE_SECONDS = 60

    async def _check_position_balance_consistency(self) -> list[dict]:
        """
        DB 오픈 포지션 vs 거래소 실잔고 비교.

        1% 이상 차이가 있으면 discrepancy로 보고.
        BUG-006의 독립 검증 레이어.
        BUG-016: dust 무시 + 진입 직후 grace period.
        API 키 미설정 / 메인터넌스 중 시 스킵.
        """
        # 정기 메인터넌스 중 → get_balance 호출 시 timeout 대기 → 스킵
        if is_maintenance_window(os.getenv("EXCHANGE", "")):
            return []

        # API 키 미설정 시 잔고 조회 불가 → 스킵
        if hasattr(self._adapter, "has_credentials") and not self._adapter.has_credentials():
            return []

        # SF-07: FX 마진 거래는 현물 통화를 보유하지 않으므로 잔고 비교 불가 → 스킵
        if getattr(self._adapter, "is_margin_trading", False):
            return []

        try:
            open_positions = await self._get_open_positions()
            if not open_positions:
                return []

            balance = await self._adapter.get_balance()
            discrepancies: list[dict] = []
            now = datetime.now(timezone.utc)

            # 통화별로 그룹핑하여 비교
            expected_by_currency: dict[str, float] = {}
            grace_currencies: set[str] = set()
            for pos in open_positions:
                currency = pos["pair"].split("_")[0].lower()
                expected_by_currency[currency] = (
                    expected_by_currency.get(currency, 0.0) + pos["amount"]
                )
                # 진입 직후 포지션은 grace period 적용
                created = pos.get("created_at")
                if created:
                    # timezone-naive 대응
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if (now - created) < timedelta(seconds=self._BALANCE_GRACE_SECONDS):
                        grace_currencies.add(currency)

            for currency, expected in expected_by_currency.items():
                # BUG-016: 진입 직후 grace period
                if currency in grace_currencies:
                    continue
                # BUG-016: dust 수량은 체크 불필요
                if expected < self._DUST_COIN_THRESHOLD:
                    continue
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

    @staticmethod
    def _coin_amount(row) -> float:
        """entry_amount에서 코인 수량을 안전하게 추출.

        BUG-016: BF MARKET_BUY에서 entry_amount가 JPY 단위로 저장된 레코드 대응.
        entry_jpy / entry_price가 가장 신뢰할 수 있으므로 우선 사용.
        """
        entry_price = float(row.entry_price) if row.entry_price else 0
        entry_jpy = float(row.entry_jpy) if getattr(row, "entry_jpy", None) else 0

        # entry_jpy와 entry_price가 있으면 나눗셈으로 정확한 코인 수량 계산
        if entry_price > 0 and entry_jpy > 0:
            return entry_jpy / entry_price

        # fallback: entry_amount 그대로 사용
        return float(row.entry_amount)

    async def _get_active_box_pairs(self) -> set[str]:
        """DB에서 status=active인 박스의 pair 집합을 반환. box_model 미설정 시 빈 집합."""
        if self._box_model is None:
            return set()
        pairs: set[str] = set()
        try:
            async with self._session_factory() as db:
                pair_col = getattr(self._box_model, self._pair_column)
                stmt = select(pair_col).where(
                    self._box_model.status == "active",
                    self._box_model.strategy_id.is_(None),
                )
                result = await db.execute(stmt)
                for (pair,) in result.all():
                    pairs.add(pair)
        except Exception as e:
            logger.error(f"[HealthChecker] 활성 박스 조회 실패: {e}")
        return pairs

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
                        "amount": self._coin_amount(row),
                        "entry_price": float(row.entry_price) if row.entry_price else None,
                        "stop_loss_price": float(row.stop_loss_price) if row.stop_loss_price else None,
                        "created_at": row.created_at,
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
                        "amount": self._coin_amount(row),
                        "stop_loss_price": float(row.stop_loss_price) if getattr(row, "stop_loss_price", None) else None,
                        "created_at": getattr(row, "created_at", None),
                    })
        except Exception as e:
            logger.error(f"[HealthChecker] 포지션 조회 실패: {e}")
        return positions

    # ══════════════════════════════════════════════════════════
    #  안전장치 (Safety) 체크 — SF-01 ~ SF-10
    # ══════════════════════════════════════════════════════════

    async def _check_safety(
        self,
        ws_connected: bool,
        task_health: dict[str, dict],
        discrepancies: list[dict],
    ) -> SafetyReport:
        """전체 안전장치 체크 수행."""
        checks: list[SafetyCheck] = []
        now = datetime.now(timezone.utc)

        # 오픈 포지션 조회 (이미 _check_position_balance_consistency에서 조회했지만,
        # stop_loss_price도 필요하므로 _get_open_positions 재사용)
        positions = await self._get_open_positions()
        has_positions = len(positions) > 0

        # SF-01, SF-02: 태스크 체크 (포지션별)
        checks.extend(self._check_sf01_sf02(task_health, positions, has_positions))

        # SF-03: WebSocket
        checks.append(self._check_sf03(ws_connected))

        # SF-04: 스탑 가격 설정 (box 포지션은 active box 존재 여부로 대체)
        active_box_pairs = await self._get_active_box_pairs()
        checks.extend(self._check_sf04(positions, has_positions, active_box_pairs))

        # SF-05: 레이첼 webhook 파이프라인
        checks.append(await self._check_sf05())

        # SF-06: 거래소 API 응답
        checks.append(await self._check_sf06())

        # SF-07: 잔고 정합성
        checks.append(self._check_sf07(discrepancies))

        # SF-08: 사만다 15분 보고
        checks.append(self._check_sf08())

        # SF-09: 트레일링 스탑 갱신
        checks.extend(self._check_sf09(positions, has_positions))

        # SF-10: 스탑 타이트닝
        checks.extend(self._check_sf10(positions, has_positions))

        status = compute_safety_status(checks)

        safety_report = SafetyReport(
            status=status,
            checks=checks,
            last_checked=now.isoformat(),
        )

        # critical 시 Telegram 직접 경고
        if status == "critical":
            await self._send_safety_telegram_alert(checks)

        return safety_report


# ── 모듈 수준 사만다 보고 시간 추적 (SF-08) ────────────────────
# monitoring.py에서 보고 생성 성공 시 이 dict를 갱신한다.
_last_report_time: dict[str, float] = {}
