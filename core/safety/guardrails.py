"""
Safety Layer — IGuardrail Protocol + AiGuardrails 구현체.

역할 분리:
  - 이 모듈은 **실행 전 사전 검증** (Pre-execution Guardrails, GR-01~GR-04).
  - 기존 SafetyChecksMixin (SF-01~SF-10)은 **사후 모니터링**이며 별개.
    monitoring/safety_checks.py를 수정하지 않는다.

진입(entry_long / entry_short) 시만 체크. 청산·hold는 무조건 통과.

체크 목록:
  GR-01: 일일 최대 거래 횟수 (max_trades_per_day, 기본 3)
  GR-02: 일일 최대 손실률 (max_daily_loss_pct, 기본 5.0%)
  GR-03: 포지션 사이즈 조정 (0.0 < size ≤ 0.8) — 거부가 아닌 축소
  GR-04: 포트폴리오 낙폭 (max_portfolio_dd_pct, 기본 15.0%) — balance_model 제공 시 활성화
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Protocol, Type, runtime_checkable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.data.dto import Decision, GuardrailResult, SignalSnapshot, modify_decision

logger = logging.getLogger(__name__)

# 진입 액션만 검사
_ENTRY_ACTIONS = {"entry_long", "entry_short"}

# GR-03: 포지션 사이즈 상한
_MAX_SIZE_PCT = 0.8
_MIN_SIZE_PCT = 0.1


@runtime_checkable
class IGuardrail(Protocol):
    """사전 검증 인터페이스."""

    async def check(
        self,
        decision: Decision,
        snapshot: SignalSnapshot,
    ) -> GuardrailResult:
        """Decision을 검증하여 GuardrailResult를 반환한다.

        진입 액션이 아닌 경우(exit / tighten_stop / hold) 무조건 approved=True.
        """
        ...


class AiGuardrails:
    """v1 안전장치 구현체. IGuardrail Protocol 준수.

    Args:
        session_factory: SQLAlchemy async session factory.
        trade_model:     거래소별 Trade ORM 모델 클래스 (BfTrade, GmoFxTrade).
        balance_model:   거래소별 BalanceEntry ORM 모델 클래스 (BfBalanceEntry 등).
                         None이면 GR-04 비활성화 (항상 통과).
        settings:        안전장치 파라미터 override용 dict.
                         없으면 환경변수 · 기본값 사용.
    """

    _DEFAULT_MAX_TRADES_PER_DAY = 3
    _DEFAULT_MAX_DAILY_LOSS_PCT = 5.0
    _DEFAULT_MAX_PORTFOLIO_DD_PCT = 15.0

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        trade_model: Type,
        balance_model: Type | None = None,
        settings: dict | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._trade_model = trade_model
        self._balance_model = balance_model
        self._settings = settings or {}

    async def check(
        self,
        decision: Decision,
        snapshot: SignalSnapshot,
    ) -> GuardrailResult:
        """진입 액션만 검사. 나머지는 무조건 통과."""
        if decision.action not in _ENTRY_ACTIONS:
            return GuardrailResult(
                approved=True,
                final_decision=decision,
                rejection_reason=None,
                violations=(),
            )

        violations: list[str] = []
        final_decision = decision

        # GR-01: 일일 최대 거래 횟수
        gr01 = await self._check_gr01()
        if gr01 is not None:
            violations.append(gr01)

        # GR-02: 일일 최대 손실률
        gr02 = await self._check_gr02()
        if gr02 is not None:
            violations.append(gr02)

        # GR-03: 포지션 사이즈 조정
        final_decision = self._check_gr03(final_decision)

        # GR-04: 포트폴리오 낙폭
        gr04 = await self._check_gr04()
        if gr04 is not None:
            violations.append(gr04)

        # GR-05: 포트폴리오 DD (ATH 기준)
        gr05 = await self._check_gr05()
        if gr05 is not None:
            violations.append(gr05)

        is_blocked = bool(violations)

        if is_blocked:
            reason = " | ".join(violations)
            final_decision = modify_decision(
                decision,
                action="blocked",
                reasoning=f"Guardrail 거부: {reason}",
            )
            logger.warning(
                f"[Guardrail] {decision.pair} 진입 거부 — {reason}"
            )
        else:
            logger.debug(
                f"[Guardrail] {decision.pair} 진입 승인 "
                f"(size={final_decision.size_pct:.0%})"
            )

        return GuardrailResult(
            approved=not is_blocked,
            final_decision=final_decision,
            rejection_reason=" | ".join(violations) if violations else None,
            violations=tuple(violations),
        )

    # ── GR-01 ────────────────────────────────────────────────────

    async def _check_gr01(self) -> str | None:
        """당일 완료 거래 횟수 초과 여부."""
        max_trades = int(
            self._settings.get("max_trades_per_day", self._DEFAULT_MAX_TRADES_PER_DAY)
        )
        count = await self._count_today_trades()
        if count >= max_trades:
            return f"GR-01: 당일 거래 {count}/{max_trades}회 초과"
        return None

    async def _count_today_trades(self) -> int:
        """당일 UTC 00:00 이후 completed 거래 수 (pending/open 제외).

        BF status: lowercase "completed" / CK status: uppercase "COMPLETED"
        → func.lower()로 대소문자 무관하게 비교.
        """
        Trade = self._trade_model
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        async with self._session_factory() as db:
            result = await db.execute(
                select(func.count(Trade.id)).where(
                    Trade.created_at >= today_start,
                    func.lower(Trade.status) == "completed",
                )
            )
            total = result.scalar() or 0
        return int(total)

    # ── GR-02 ────────────────────────────────────────────────────

    async def _check_gr02(self) -> str | None:
        """당일 누적 손실률이 임계값 초과 여부."""
        max_loss_pct = float(
            self._settings.get("max_daily_loss_pct", self._DEFAULT_MAX_DAILY_LOSS_PCT)
        )
        loss_pct = await self._calc_today_loss_pct()
        if loss_pct is not None and loss_pct <= -max_loss_pct:
            return f"GR-02: 당일 손실 {loss_pct:.2f}% (임계 -{max_loss_pct}%)"
        return None

    async def _calc_today_loss_pct(self) -> float | None:
        """당일 closed_at 기준 완료 거래의 profit_loss_percentage 합계."""
        Trade = self._trade_model
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        try:
            async with self._session_factory() as db:
                result = await db.execute(
                    select(func.sum(Trade.profit_loss_percentage)).where(
                        Trade.closed_at >= today_start,
                        Trade.profit_loss_percentage.isnot(None),
                    )
                )
                total = result.scalar()
                return float(total) if total is not None else None
        except Exception as e:
            logger.warning(f"[Guardrail] GR-02 계산 실패 (무시): {e}")
            return None

    # ── GR-03 ────────────────────────────────────────────────────

    def _check_gr03(self, decision: Decision) -> Decision:
        """포지션 사이즈를 안전 범위 [MIN, MAX]로 클램핑.

        거부(blocked)가 아닌 축소 처리. 원본 Decision은 불변.
        """
        size = decision.size_pct
        if size <= 0.0:
            return decision  # hold/tighten/exit 등 — 변경 불필요

        clamped = max(_MIN_SIZE_PCT, min(size, _MAX_SIZE_PCT))
        if clamped != size:
            logger.info(
                f"[Guardrail] GR-03 사이즈 조정 {size:.0%} → {clamped:.0%}"
            )
            return modify_decision(decision, size_pct=clamped)
        return decision

    # ── GR-04 ────────────────────────────────────────────────────

    async def _check_gr04(self) -> str | None:
        """포트폴리오 낙폭(drawdown) 검증.

        balance_model 미제공 시 항상 통과 (GR-04 비활성화).

        알고리즘:
          peak_jpy = MAX(available) WHERE currency='jpy'
          current_jpy = 가장 최근 available WHERE currency='jpy'
          dd_pct = (peak_jpy - current_jpy) / peak_jpy * 100

        데이터 부족·계산 오류 시 → None (통과). 안전장치가 데이터 부족으로
        거래를 막으면 안됨.
        """
        if self._balance_model is None:
            return None

        max_dd_pct = float(
            self._settings.get("max_portfolio_dd_pct", self._DEFAULT_MAX_PORTFOLIO_DD_PCT)
        )
        try:
            peak_jpy, current_jpy = await self._fetch_peak_and_current_jpy()
        except Exception as e:
            logger.warning(f"[Guardrail] GR-04 조회 실패 (무시): {e}")
            return None

        if peak_jpy is None or current_jpy is None:
            return None  # 데이터 없음 — 통과
        if peak_jpy <= 0.0:
            return None  # 제로 디비전 방어

        dd_pct = (peak_jpy - current_jpy) / peak_jpy * 100.0
        if dd_pct >= max_dd_pct:
            return (
                f"GR-04: 포트폴리오 낙폭 {dd_pct:.1f}% (임계 {max_dd_pct:.0f}%)"
            )
        return None

    async def _fetch_peak_and_current_jpy(
        self,
    ) -> tuple[float | None, float | None]:
        """balance_entries 테이블에서 피크 JPY와 최근 JPY를 조회.

        Returns:
            (peak_available_jpy, latest_available_jpy)
            데이터 없으면 (None, None)
        """
        BalanceEntry = self._balance_model
        async with self._session_factory() as db:
            # 피크: 전 기간 JPY available 최대값
            peak_result = await db.execute(
                select(func.max(BalanceEntry.available)).where(
                    func.lower(BalanceEntry.currency) == "jpy",
                )
            )
            peak_jpy: float | None = peak_result.scalar()

            # 현재: 가장 최근 JPY available
            current_result = await db.execute(
                select(BalanceEntry.available)
                .where(func.lower(BalanceEntry.currency) == "jpy")
                .order_by(BalanceEntry.created_at.desc())
                .limit(1)
            )
            row = current_result.fetchone()
            current_jpy: float | None = row[0] if row is not None else None

        return peak_jpy, current_jpy

    # ── GR-05 ────────────────────────────────────────────────────

    async def _check_gr05(self) -> str | None:
        """포트폴리오 DD = (ATH - 현재잔고) / ATH × 100.

        ATH(All-Time High): balance_entries 테이블의 역대 최대 total_jpy.
        현재잔고: 가장 최근 balance_entry의 total_jpy.
        total_jpy 컬럼이 없는 모델은 available(JPY) × 1로 단순화.

        balance_model 미제공 시 항상 통과 (GR-05 비활성화).
        데이터 부족·계산 오류 시 통과 (안전장치가 데이터 부족으로 거래를 막지 않도록).

        Note: BTC 보유 자산은 미포함 (JPY only 단순화).
              BTC+JPY 합산 포트폴리오 가치 계산은 미결 항목 (03_EXECUTION_MODEL §6).
        """
        if self._balance_model is None:
            return None

        max_dd_pct = float(
            self._settings.get("max_portfolio_dd_pct", self._DEFAULT_MAX_PORTFOLIO_DD_PCT)
        )
        try:
            peak_jpy, current_jpy = await self._fetch_ath_and_current_jpy()
        except Exception as e:
            logger.warning(f"[Guardrail] GR-05 조회 실패 (무시): {e}")
            return None

        if peak_jpy is None or current_jpy is None:
            return None
        if peak_jpy <= 0.0:
            return None

        dd_pct = (peak_jpy - current_jpy) / peak_jpy * 100.0
        if dd_pct < 0.0:
            # 현재 잔고 > ATH → DD 없음 (ATH 갱신 직후 비정상값 방어)
            return None
        if dd_pct >= max_dd_pct:
            return (
                f"GR-05: 포트폴리오 DD {dd_pct:.1f}% (임계 {max_dd_pct:.0f}%) — 수보오빠 수동 해제 필요"
            )
        return None

    async def _fetch_ath_and_current_jpy(
        self,
    ) -> tuple[float | None, float | None]:
        """balance_entries 테이블에서 ATH(전역 최대) JPY와 최근 JPY를 조회.

        GR-04(_fetch_peak_and_current_jpy)와 동일 로직이지만 의미 차이:
          GR-04: 낙폭(최근 피크 대비) — 시간 제한 없음
          GR-05: ATH 기준 DD — 전체 기간 최대치 기준 (동일 쿼리)
        """
        BalanceEntry = self._balance_model
        async with self._session_factory() as db:
            ath_result = await db.execute(
                select(func.max(BalanceEntry.available)).where(
                    func.lower(BalanceEntry.currency) == "jpy",
                )
            )
            ath_jpy: float | None = ath_result.scalar()

            current_result = await db.execute(
                select(BalanceEntry.available)
                .where(func.lower(BalanceEntry.currency) == "jpy")
                .order_by(BalanceEntry.created_at.desc())
                .limit(1)
            )
            row = current_result.fetchone()
            current_jpy: float | None = row[0] if row is not None else None

        return ath_jpy, current_jpy
