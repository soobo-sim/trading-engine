"""
모니터링 Status 서비스 — 대시보드용 구조화 상태 응답.

GET /api/monitoring/status → 추세추종/박스권 전략 상태를 단일 JSON으로 반환.
설계: trader-common/solution-design/DASHBOARD_MONITORING_API.md
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.strategy.signals import compute_trend_signal

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))


async def generate_trend_status(
    pair: str,
    prefix: str,
    pair_column: str,
    strategy: Any,
    adapter: Any,
    trend_manager: Any,
    candle_model: Any,
    trend_position_model: Any,
    db: AsyncSession,
) -> dict:
    """추세추종 전략의 구조화 상태 응답 생성 (S1: 진입대기, S2: 포지션보유)."""
    params = strategy.parameters or {}
    now_jst = datetime.now(JST)

    # 1. 캔들 조회 (4H 완성)
    pair_col = getattr(candle_model, pair_column)
    result = await db.execute(
        select(candle_model)
        .where(
            and_(
                pair_col == pair,
                candle_model.timeframe == "4h",
                candle_model.is_complete == True,
            )
        )
        .order_by(candle_model.open_time.desc())
        .limit(60)
    )
    candles = list(reversed(result.scalars().all()))

    if len(candles) < 21:
        return {"success": False, "error": f"캔들 데이터 부족 ({len(candles)}개)"}

    # 2. 인메모리 포지션 확인
    position_obj = trend_manager.get_position(pair)
    entry_price = position_obj.entry_price if position_obj else None

    # 3. 시그널 계산
    sig = compute_trend_signal(candles, params, entry_price)
    current_price = sig["current_price"]
    ema = sig["ema"]
    ema_slope_pct = sig["ema_slope_pct"]
    atr = sig["atr"]
    rsi = sig["rsi"]
    regime = sig["regime"]
    exit_signal = sig["exit_signal"]

    # EMA gap %
    ema_gap_pct = (
        round((current_price - ema) / ema * 100, 2)
        if ema and ema > 0 else None
    )

    # 4H 변동률
    last_candle_close = float(candles[-1].close) if candles else None
    candle_4h_change_pct = round(
        (current_price - last_candle_close) / last_candle_close * 100, 2
    ) if last_candle_close and last_candle_close > 0 else 0.0

    # regime_confidence (간이)
    regime_confidence = 0.7 if regime == "trending" else (0.3 if regime == "ranging" else 0.5)

    # ── 공통 market 블록 ──
    market = {
        "price": round(current_price, 6),
        "ema20": round(ema, 6) if ema else None,
        "ema_gap_pct": ema_gap_pct,
        "ema_slope_pct": round(ema_slope_pct, 4) if ema_slope_pct is not None else None,
        "rsi": round(rsi, 2) if rsi is not None else None,
        "atr": round(atr, 6) if atr else None,
        "regime": regime,
        "regime_confidence": regime_confidence,
        "candle_4h_change_pct": candle_4h_change_pct,
    }

    # ── 공통 strategy 블록 ──
    strategy_block = {
        "id": strategy.id,
        "name": strategy.name,
        "trading_style": params.get("trading_style", "trend_following"),
        "status": strategy.status,
        "parameters": {
            k: v for k, v in params.items()
            if k in (
                "entry_rsi_min", "entry_rsi_max", "ema_slope_entry_min",
                "trailing_stop_atr_initial", "trailing_stop_atr_mature",
                "tighten_stop_atr", "position_size_pct", "atr_multiplier_stop",
            )
        },
    }

    alerts: List[dict] = []

    # ── 포지션 보유 중 (S2) ──
    if position_obj and position_obj.entry_price:
        state = "in_position"
        unrealized_pnl_jpy = round(
            (current_price - position_obj.entry_price) * position_obj.entry_amount, 0
        )
        unrealized_pnl_pct = round(
            (current_price - position_obj.entry_price) / position_obj.entry_price * 100, 2
        ) if position_obj.entry_price > 0 else 0.0

        # DB에서 opened_at 조회
        holding_hours = None
        entry_at = None
        if position_obj.db_record_id:
            pos_result = await db.execute(
                select(trend_position_model).where(
                    trend_position_model.id == position_obj.db_record_id
                )
            )
            db_pos = pos_result.scalar_one_or_none()
            if db_pos and db_pos.created_at:
                entry_at = db_pos.created_at.isoformat()
                elapsed = datetime.now(timezone.utc) - db_pos.created_at.replace(tzinfo=timezone.utc) \
                    if db_pos.created_at.tzinfo is None else datetime.now(timezone.utc) - db_pos.created_at
                holding_hours = round(elapsed.total_seconds() / 3600, 1)

        stop_price = position_obj.stop_loss_price or 0.0
        distance_pct = round(
            (current_price - stop_price) / current_price * 100, 2
        ) if current_price > 0 and stop_price > 0 else None

        # 현재 ATR multiplier 추론
        from core.strategy.signals import compute_adaptive_trailing_mult
        current_mult = (
            float(params.get("tighten_stop_atr", 1.0))
            if position_obj.stop_tightened
            else compute_adaptive_trailing_mult(ema_slope_pct, rsi, params)
        )

        position_block = {
            "side": "buy",
            "entry_price": round(position_obj.entry_price, 6),
            "entry_at": entry_at,
            "amount": round(position_obj.entry_amount, 8),
            "current_price": round(current_price, 6),
            "unrealized_pnl_jpy": unrealized_pnl_jpy,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "holding_hours": holding_hours,
            "trailing_stop": {
                "current_stop_price": round(stop_price, 6) if stop_price else None,
                "distance_pct": distance_pct,
                "atr_multiplier": round(current_mult, 2),
                "tightened": position_obj.stop_tightened,
                "tighten_reason": exit_signal.get("reason") if position_obj.stop_tightened else None,
            },
            "exit_signals": {
                "ema_below": sig["signal"] == "exit_warning",
                "trailing_stop_hit": False,
                "action": exit_signal["action"],
                "reason": exit_signal["reason"],
                "triggers": exit_signal.get("triggers", {}),
            },
        }

        # 상태 요약 라인
        pnl_icon = "📈" if unrealized_pnl_pct >= 0 else "📉"
        status_line = (
            f"{pnl_icon} 보유 중: {unrealized_pnl_pct:+.2f}% "
            f"(¥{unrealized_pnl_jpy:,.0f}) | "
            f"스탑 ¥{stop_price:,.0f}"
        )
        if holding_hours is not None:
            status_line += f" | {holding_hours:.0f}H 보유"

        return {
            "success": True,
            "generated_at": now_jst.isoformat(),
            "pair": pair,
            "exchange": prefix,
            "strategy": strategy_block,
            "market": market,
            "state": state,
            "entry_conditions": None,
            "position": position_block,
            "alerts": alerts,
            "status_line": status_line,
        }

    # ── 진입 대기 (S1) ──
    state = "waiting"

    rsi_min = float(params.get("entry_rsi_min", 40.0))
    rsi_max = float(params.get("entry_rsi_max", 65.0))
    slope_min = float(params.get("ema_slope_entry_min", 0.0))

    conditions = []

    # EMA slope
    ema_slope_met = ema_slope_pct is not None and ema_slope_pct >= slope_min
    conditions.append({
        "name": "ema_slope",
        "met": ema_slope_met,
        "current": round(ema_slope_pct, 4) if ema_slope_pct is not None else None,
        "required": f">= {slope_min}",
        "label": (
            f"EMA slope {ema_slope_pct:+.2f}% ✓" if ema_slope_met
            else f"EMA slope {ema_slope_pct:+.2f}% → {slope_min}% 이상 필요"
            if ema_slope_pct is not None else "EMA slope 데이터 없음"
        ),
    })

    # Price above EMA
    price_above = ema is not None and current_price > ema
    conditions.append({
        "name": "price_above_ema",
        "met": price_above,
        "current": round(current_price, 6),
        "required": f"> {round(ema, 6)}" if ema else "N/A",
        "label": (
            f"가격 > EMA20 ✓" if price_above
            else f"가격 < EMA20 (갭 {abs(ema_gap_pct or 0):.1f}%)"
        ),
    })

    # RSI range
    rsi_in_range = rsi is not None and rsi_min <= rsi <= rsi_max
    conditions.append({
        "name": "rsi_range",
        "met": rsi_in_range,
        "current": round(rsi, 2) if rsi is not None else None,
        "required": f"{rsi_min}~{rsi_max}",
        "label": (
            f"RSI {rsi:.1f} ✓" if rsi_in_range
            else f"RSI {rsi:.1f} → {rsi_min:.0f}~{rsi_max:.0f} 범위 필요"
            if rsi is not None else "RSI 데이터 없음"
        ),
    })

    met_count = sum(1 for c in conditions if c["met"])
    entry_conditions = {
        "total": len(conditions),
        "met": met_count,
        "conditions": conditions,
    }

    # alerts
    if rsi is not None and rsi < 30:
        alerts.append({
            "code": "rsi_low",
            "severity": "warning",
            "label": f"RSI 과매도 ({rsi:.1f})",
            "description": "극단적 과매도 구간. 반등 가능성 있으나 추가 하락 주의.",
        })
    if rsi is not None and rsi > 75:
        alerts.append({
            "code": "rsi_high",
            "severity": "warning",
            "label": f"RSI 과매수 ({rsi:.1f})",
            "description": "과열 구간. 눌림목 대기.",
        })
    if ema_gap_pct is not None and abs(ema_gap_pct) > 3.0:
        alerts.append({
            "code": "large_ema_gap",
            "severity": "warning",
            "label": f"EMA 괴리 확대 ({abs(ema_gap_pct):.1f}%)",
            "description": f"가격이 EMA20 대비 {abs(ema_gap_pct):.1f}% {'하방' if ema_gap_pct < 0 else '상방'} 이탈.",
        })

    # status_line
    blockers = [c["label"] for c in conditions if not c["met"]]
    if met_count == len(conditions):
        status_line = "✅ 모든 진입 조건 충족 — 진입 대기"
    else:
        status_line = f"⏳ 진입 대기: {', '.join(blockers[:2])}"

    return {
        "success": True,
        "generated_at": now_jst.isoformat(),
        "pair": pair,
        "exchange": prefix,
        "strategy": strategy_block,
        "market": market,
        "state": state,
        "entry_conditions": entry_conditions,
        "position": None,
        "alerts": alerts,
        "status_line": status_line,
    }
