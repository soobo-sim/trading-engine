"""
guardrails.py — P6 Canary 자동 롤백 트리거 정의.

5개 트리거 중 하나라도 발동 → GuardrailViolation 반환.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from adapters.database.hypothesis_model import Hypothesis

JST = timezone(timedelta(hours=9))
logger = logging.getLogger("core.judge.evolution.guardrails")

# ── 임계값 기본값 ────────────────────────────────────────────

_DEFAULTS: dict[str, float] = {
    "canary.rollback_pnl_jpy": -3000.0,
    "canary.rollback_pct": -2.0,
    "canary.rollback_consec_loss": 3.0,
    "canary.rollback_max_dd_pct": 5.0,
    "canary.expire_days": 7.0,
    "canary.min_trades": 3.0,
}


def _tunable(key: str) -> float:
    """TunableCatalog에서 값 조회. 없으면 기본값."""
    try:
        from core.shared.tunable_catalog import TunableCatalog
        spec = TunableCatalog.get(key)
        if spec is not None and spec.default is not None:
            return float(spec.default)
    except Exception:
        pass
    return _DEFAULTS.get(key, 0.0)


# ── 도메인 객체 ──────────────────────────────────────────────

@dataclass
class GuardrailViolation:
    trigger: Literal["pnl_jpy", "pnl_pct", "consec_loss", "max_dd", "expired"]
    actual_value: float
    threshold: float
    detected_at: datetime
    description: str  # 텔레그램용 한국어 1줄

    def to_dict(self) -> dict:
        return asdict(self)


# ── 헬퍼 ────────────────────────────────────────────────────

def _count_trailing_losses(trades: list[dict]) -> int:
    """최근 연속 손실 건수 (PnL ≤ 0)."""
    count = 0
    for t in reversed(trades):
        pnl = t.get("realized_pnl", 0) or 0
        if pnl <= 0:
            count += 1
        else:
            break
    return count


def _calculate_max_drawdown(trades: list[dict], start_balance: float) -> float:
    """시작 잔고 기준 최대 낙폭 (%)."""
    if start_balance <= 0:
        return 0.0
    peak = start_balance
    max_dd = 0.0
    running = start_balance
    for t in trades:
        pnl = t.get("realized_pnl", 0) or 0
        running += pnl
        if running > peak:
            peak = running
        dd_pct = (peak - running) / peak * 100 if peak > 0 else 0.0
        if dd_pct > max_dd:
            max_dd = dd_pct
    return max_dd


async def _fetch_trades_since(
    db: AsyncSession, h: "Hypothesis", since: datetime
) -> list[dict]:
    """canary 시작 이후 거래 조회 (gmoc_trend_positions 기준)."""
    try:
        from sqlalchemy import text
        rows = (await db.execute(
            text(
                "SELECT realized_pnl FROM gmoc_trend_positions "
                "WHERE closed_at >= :since AND strategy_id = :sid "
                "ORDER BY closed_at ASC"
            ),
            {"since": since, "sid": 2},  # 기본 전략 id=2
        )).all()
        return [{"realized_pnl": float(r[0] or 0)} for r in rows]
    except Exception as exc:
        logger.debug("_fetch_trades_since failed: %s", exc)
        return []


async def _fetch_current_balance_jpy(db: AsyncSession) -> float:
    """최신 잔고 조회 (gmoc_balance_entries)."""
    try:
        from sqlalchemy import text
        row = (await db.execute(
            text(
                "SELECT balance_jpy FROM gmoc_balance_entries "
                "ORDER BY recorded_at DESC LIMIT 1"
            )
        )).first()
        return float(row[0]) if row else 0.0
    except Exception as exc:
        logger.debug("_fetch_current_balance_jpy failed: %s", exc)
        return 0.0


# ── 메인 함수 ────────────────────────────────────────────────

async def check_guardrails(
    db: AsyncSession,
    h: "Hypothesis",
    *,
    current_balance_jpy: float,
    canary_start_balance_jpy: float,
    canary_start_at: datetime,
) -> GuardrailViolation | None:
    """5개 트리거 검사. 위반 시 GuardrailViolation 반환, 정상이면 None."""
    if h.status != "canary":
        return None

    now = datetime.now(tz=JST)
    trades = await _fetch_trades_since(db, h, since=canary_start_at)

    # (1) 절대 PnL
    pnl_jpy = sum(t.get("realized_pnl", 0) for t in trades)
    threshold_pnl = _tunable("canary.rollback_pnl_jpy")
    if pnl_jpy <= threshold_pnl:
        return GuardrailViolation(
            trigger="pnl_jpy",
            actual_value=pnl_jpy,
            threshold=threshold_pnl,
            detected_at=now,
            description=f"누적 손실 ¥{pnl_jpy:,.0f} ≤ 임계 ¥{threshold_pnl:,.0f}",
        )

    # (2) 비율 PnL
    if canary_start_balance_jpy > 0:
        pct = (current_balance_jpy - canary_start_balance_jpy) / canary_start_balance_jpy * 100
        threshold_pct = _tunable("canary.rollback_pct")
        if pct <= threshold_pct:
            return GuardrailViolation(
                trigger="pnl_pct",
                actual_value=pct,
                threshold=threshold_pct,
                detected_at=now,
                description=f"손실률 {pct:.2f}% ≤ 임계 {threshold_pct:.2f}%",
            )

    # (3) 연속 손실
    consec = _count_trailing_losses(trades)
    threshold_consec = _tunable("canary.rollback_consec_loss")
    if consec >= threshold_consec:
        return GuardrailViolation(
            trigger="consec_loss",
            actual_value=float(consec),
            threshold=threshold_consec,
            detected_at=now,
            description=f"연속 손실 {consec}회 ≥ 임계 {threshold_consec:.0f}회",
        )

    # (4) 최대 DD
    if canary_start_balance_jpy > 0:
        max_dd = _calculate_max_drawdown(trades, canary_start_balance_jpy)
        threshold_dd = _tunable("canary.rollback_max_dd_pct")
        if max_dd >= threshold_dd:
            return GuardrailViolation(
                trigger="max_dd",
                actual_value=max_dd,
                threshold=threshold_dd,
                detected_at=now,
                description=f"최대 DD {max_dd:.2f}% ≥ 임계 {threshold_dd:.2f}%",
            )

    # (5) 만료 + 거래 부족
    expire_days = int(_tunable("canary.expire_days"))
    min_trades = int(_tunable("canary.min_trades"))
    elapsed_days = (now - canary_start_at.replace(tzinfo=JST) if canary_start_at.tzinfo is None else now - canary_start_at).days
    if elapsed_days >= expire_days and len(trades) < min_trades:
        return GuardrailViolation(
            trigger="expired",
            actual_value=float(len(trades)),
            threshold=float(min_trades),
            detected_at=now,
            description=f"{expire_days}일 경과 + 거래 {len(trades)}건 < {min_trades}건",
        )

    return None
