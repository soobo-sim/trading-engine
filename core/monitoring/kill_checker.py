"""
core/monitoring/kill_checker.py

strategy_changes.kill_conditions JSON에 정의된 Kill 조건을 자동 체크.
평가 결과: None(미충족) or KillResult(충족)

설계서: solution-design/REPORT_DB_STORAGE.md 섹션 4
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

RACHEL_WEBHOOK_URL = os.getenv(
    "RACHEL_WEBHOOK_URL", "http://localhost:18793/hooks/market-alert"
)
RACHEL_WEBHOOK_TOKEN = os.getenv("RACHEL_WEBHOOK_TOKEN", "")


# ──────────────────────────────────────────
# Result type
# ──────────────────────────────────────────

@dataclass
class KillResult:
    evaluator: str          # 발동 조건 이름
    detail: str             # 사람이 읽을 수 있는 설명


# ──────────────────────────────────────────
# Evaluators
# ──────────────────────────────────────────

async def eval_consecutive_losses(
    threshold: Any,
    strategy_change: Any,
    db: AsyncSession,
    pos_model: Any,
) -> Optional[KillResult]:
    """N연패 체크."""
    n = int(threshold)
    strategy_id = strategy_change.new_strategy_id

    stmt = (
        select(pos_model)
        .where(
            pos_model.strategy_id == strategy_id,
            pos_model.status == "closed",
        )
        .order_by(desc(pos_model.closed_at))
        .limit(n)
    )
    rows = (await db.execute(stmt)).scalars().all()

    if len(rows) < n:
        return None  # 거래 부족

    all_loss = all(
        (float(getattr(r, "realized_pnl_jpy", 0) or 0) < 0)
        for r in rows
    )
    if all_loss:
        return KillResult(
            evaluator="consecutive_losses",
            detail=f"최근 {n}거래 연속 손실",
        )
    return None


async def eval_no_trade_days(
    threshold: Any,
    strategy_change: Any,
    db: AsyncSession,
    pos_model: Any,
) -> Optional[KillResult]:
    """N일 무거래 체크."""
    days = int(threshold)
    strategy_id = strategy_change.new_strategy_id
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    stmt = (
        select(pos_model)
        .where(
            pos_model.strategy_id == strategy_id,
            pos_model.created_at >= cutoff,
        )
        .limit(1)
    )
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        # created_at 기준 거래 없음 → 추가로 전략 생성 이후 경과 시간 확인
        created_at = getattr(strategy_change, "created_at", None)
        if created_at is None:
            return None
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age = datetime.now(tz=timezone.utc) - created_at
        if age >= timedelta(days=days):
            return KillResult(
                evaluator="no_trade_days",
                detail=f"{days}일간 거래 없음",
            )
    return None


async def eval_first_n_win_rate(
    threshold: Any,
    strategy_change: Any,
    db: AsyncSession,
    pos_model: Any,
) -> Optional[KillResult]:
    """첫 N거래 승률 < min_rate% 체크."""
    if isinstance(threshold, dict):
        n = int(threshold.get("n", 5))
        min_rate = float(threshold.get("min_rate", 60))
    else:
        n = 5
        min_rate = float(threshold)

    strategy_id = strategy_change.new_strategy_id

    stmt = (
        select(pos_model)
        .where(
            pos_model.strategy_id == strategy_id,
            pos_model.status == "closed",
        )
        .order_by(pos_model.closed_at)
        .limit(n)
    )
    rows = (await db.execute(stmt)).scalars().all()

    if len(rows) < n:
        return None  # 아직 첫 N거래 미완료

    wins = sum(
        1 for r in rows
        if (float(getattr(r, "realized_pnl_jpy", 0) or 0)) > 0
    )
    rate = wins / n * 100
    if rate < min_rate:
        return KillResult(
            evaluator="first_n_trades_win_rate",
            detail=f"첫 {n}거래 승률 {rate:.1f}% < 기준 {min_rate}%",
        )
    return None


async def eval_max_drawdown(
    threshold: Any,
    strategy_change: Any,
    db: AsyncSession,
    pos_model: Any,
) -> Optional[KillResult]:
    """최대 드로우다운 % 초과 체크 (관찰 기간 내 누적 손익 기준)."""
    max_dd = float(threshold)
    strategy_id = strategy_change.new_strategy_id
    created_at = getattr(strategy_change, "created_at", None)

    conditions = [
        pos_model.strategy_id == strategy_id,
        pos_model.status == "closed",
    ]
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        conditions.append(pos_model.closed_at >= created_at)

    stmt = select(pos_model).where(*conditions)
    rows = (await db.execute(stmt)).scalars().all()

    cumulative = 0.0
    peak = 0.0
    for r in rows:
        pnl = float(getattr(r, "realized_pnl_jpy", 0) or 0)
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        if peak > 0:
            dd = (peak - cumulative) / abs(peak) * 100
            if dd > max_dd:
                return KillResult(
                    evaluator="max_drawdown_pct",
                    detail=f"드로우다운 {dd:.1f}% > 기준 {max_dd}%",
                )
    return None


async def eval_max_total_loss(
    threshold: Any,
    strategy_change: Any,
    db: AsyncSession,
    pos_model: Any,
) -> Optional[KillResult]:
    """관찰 기간 내 누적 손실 JPY 초과 체크."""
    max_loss = float(threshold)
    strategy_id = strategy_change.new_strategy_id
    created_at = getattr(strategy_change, "created_at", None)

    conditions = [
        pos_model.strategy_id == strategy_id,
        pos_model.status == "closed",
    ]
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        conditions.append(pos_model.closed_at >= created_at)

    stmt = select(pos_model).where(*conditions)
    rows = (await db.execute(stmt)).scalars().all()
    total = sum(float(getattr(r, "realized_pnl_jpy", 0) or 0) for r in rows)
    if total < -abs(max_loss):
        return KillResult(
            evaluator="max_total_loss_jpy",
            detail=f"누적 손실 ¥{total:,.0f} > 기준 ¥{-abs(max_loss):,.0f}",
        )
    return None


# ──────────────────────────────────────────
# Evaluator registry
# ──────────────────────────────────────────

EVALUATORS: dict[str, Callable] = {
    "consecutive_losses": eval_consecutive_losses,
    "no_trade_days": eval_no_trade_days,
    "first_n_trades_win_rate": eval_first_n_win_rate,
    "max_drawdown_pct": eval_max_drawdown,
    "max_total_loss_jpy": eval_max_total_loss,
}


# ──────────────────────────────────────────
# Main check function
# ──────────────────────────────────────────

async def check_kill_conditions(
    strategy_change: Any,
    db: AsyncSession,
    pos_model: Any,
) -> Optional[KillResult]:
    """
    kill_conditions JSON의 각 키를 EVALUATORS로 평가.
    하나라도 충족 → KillResult. 전부 미충족 → None.
    이미 killed/graduated → 스킵(None).
    """
    # 중복 방지
    if getattr(strategy_change, "kill_triggered_at", None) is not None:
        return None
    if getattr(strategy_change, "status", "active") != "active":
        return None

    kill_conditions: dict = getattr(strategy_change, "kill_conditions", None) or {}

    for key, threshold in kill_conditions.items():
        evaluator = EVALUATORS.get(key)
        if evaluator is None:
            logger.debug(f"[KillChecker] 알 수 없는 조건 '{key}' → skip")
            continue
        try:
            result = await evaluator(threshold, strategy_change, db, pos_model)
            if result is not None:
                return result
        except Exception as e:
            logger.error(f"[KillChecker] evaluator '{key}' 실패 → skip: {e}")
            # Telegram 경고는 호출자(auto_reporter)에서 처리
            raise RuntimeError(f"Kill 체크 실패: {key}") from e

    return None


# ──────────────────────────────────────────
# Webhook
# ──────────────────────────────────────────

async def send_kill_webhook(
    strategy_change: Any,
    kill_result: KillResult,
    http_client: Any,
) -> bool:
    """Kill 조건 발동 시 레이첼 webhook 전송."""
    if not RACHEL_WEBHOOK_TOKEN:
        logger.warning("RACHEL_WEBHOOK_TOKEN 미설정 — kill webhook 스킵")
        return False

    sc_id = getattr(strategy_change, "id", "?")
    pair = getattr(strategy_change, "pair", "?")
    new_sid = getattr(strategy_change, "new_strategy_id", "?")

    payload = {
        "name": "KillConditionTriggered",
        "metadata": {
            "type": "kill_condition_triggered",
            "strategy_change_id": sc_id,
            "new_strategy_id": new_sid,
            "pair": pair,
            "evaluator": kill_result.evaluator,
            "detail": kill_result.detail,
        },
        "message": (
            f"⚠️ Kill 조건 발동: {pair} strategy_change#{sc_id}\n"
            f"조건: {kill_result.evaluator}\n"
            f"상세: {kill_result.detail}\n"
            "자동 롤백 없음 — 레이첼 판정 필요"
        ),
        "timeoutSeconds": 60,
    }
    try:
        resp = await http_client.post(
            RACHEL_WEBHOOK_URL,
            json=payload,
            headers={"Authorization": f"Bearer {RACHEL_WEBHOOK_TOKEN}"},
        )
        if resp.status_code < 300:
            logger.info(f"[KillChecker] Kill webhook 전송 완료: sc#{sc_id}")
            return True
        logger.warning(f"[KillChecker] Kill webhook 응답 {resp.status_code}")
        return False
    except Exception as e:
        logger.error(f"[KillChecker] Kill webhook 전송 실패: {e}")
        return False


# ──────────────────────────────────────────
# High-level: DB PATCH + webhook
# ──────────────────────────────────────────

async def trigger_kill(
    strategy_change: Any,
    kill_result: KillResult,
    db: AsyncSession,
    http_client: Any,
) -> None:
    """
    Kill 도달 시:
    ① strategy_changes.status = killed, kill_triggered_at = now
    ② webhook 전송 (실패해도 DB는 유지)
    ③ 자동 롤백 없음
    """
    from adapters.database.models import StrategyChange

    now = datetime.now(tz=timezone.utc)
    strategy_change.status = "killed"
    strategy_change.kill_triggered_at = now
    await db.commit()
    logger.info(
        f"[KillChecker] sc#{strategy_change.id} killed: {kill_result.evaluator}"
    )

    if http_client:
        await send_kill_webhook(strategy_change, kill_result, http_client)


# ──────────────────────────────────────────
# Run all active strategy_changes (auto_reporter 통합용)
# ──────────────────────────────────────────

async def run_kill_checks(
    db: AsyncSession,
    pos_model: Any,
    http_client: Any = None,
) -> int:
    """
    status='active'인 모든 strategy_changes에 대해 Kill 체크 실행.
    반환: Kill 발동 건수.
    """
    from adapters.database.models import StrategyChange

    stmt = select(StrategyChange).where(StrategyChange.status == "active")
    rows = (await db.execute(stmt)).scalars().all()

    triggered = 0
    for sc in rows:
        if not sc.kill_conditions:
            continue
        try:
            result = await check_kill_conditions(sc, db, pos_model)
            if result:
                await trigger_kill(sc, result, db, http_client)
                triggered += 1
        except RuntimeError as e:
            logger.error(f"[KillChecker] {e}")
            # Telegram 경고 (best-effort)
            try:
                if http_client:
                    import httpx  # noqa
                    bot_token = os.getenv("AUTO_REPORT_BOT_TOKEN", "")
                    chat_id = os.getenv("AUTO_REPORT_CHAT_ID", "")
                    if bot_token and chat_id:
                        await http_client.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": f"⚠️ {e}"},
                        )
            except Exception:
                pass

    return triggered
