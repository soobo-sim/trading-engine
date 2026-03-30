"""CFD 추세추종 모니터링 리포트 — 생성."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.strategy.signals import compute_trend_signal

from .display import (
    JST,
    get_trend_icon,
    get_rsi_state,
    get_ema_state,
    get_volatility_state,
    get_market_summary,
    get_position_summary,
    get_entry_blockers,
)
from .alerts import (
    _prev_raw_cache,
    _last_alert_time,
    _build_test_alert,
    evaluate_alert,
    _trigger_rachel_analysis,
)
from .trend_report import build_telegram_text, build_memory_block

logger = logging.getLogger(__name__)


async def generate_cfd_report(
    pair: str,
    prefix: str,
    pair_column: str,
    strategy: Any,
    adapter: Any,
    cfd_manager: Any,
    candle_model: Any,
    db: AsyncSession,
    test_alert_level: str | None = None,
    reset_cooldown: bool = False,
) -> dict:
    """cfd_trend_following 전략의 모니터링 리포트 생성."""
    pair = pair.lower()  # DB candle pair는 소문자 — 정규화
    params = strategy.parameters or {}
    now_jst = datetime.now(JST)
    time_str = now_jst.strftime("%H:%M")

    # 1. 캔들 조회 (4H 완성 캔들)
    pair_col = getattr(candle_model, pair_column)
    candle_limit = 60
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
        .limit(candle_limit)
    )
    candles = list(reversed(result.scalars().all()))

    if len(candles) < 21:
        return {
            "success": False,
            "error": f"캔들 데이터 부족 ({len(candles)}개 / 최소 21개 필요)",
        }

    # 2. 포지션 확인 (인메모리)
    position_obj = cfd_manager.get_position(pair)
    entry_price = position_obj.entry_price if position_obj else None

    # 3. 시그널 계산
    sig = compute_trend_signal(candles, params, entry_price)

    current_price = sig["current_price"]
    ema = sig["ema"]
    ema_slope_pct = sig["ema_slope_pct"]
    atr = sig["atr"]
    rsi = sig["rsi"]
    signal = sig["signal"]
    exit_signal = sig["exit_signal"]

    atr_pct = (atr / current_price * 100) if (atr and current_price > 0) else None

    # 4. 증거금 + keep_rate
    collateral_data = None
    if hasattr(adapter, "get_collateral"):
        try:
            c = await adapter.get_collateral()
            collateral_data = {
                "collateral": c.collateral,
                "open_position_pnl": c.open_position_pnl,
                "require_collateral": c.require_collateral,
                "keep_rate": c.keep_rate,
            }
        except Exception as e:
            logger.warning(f"[CfdReport] 증거금 조회 실패: {e}")

    # 5. 포지션 데이터
    position_data = None
    position_summary = None
    if position_obj and position_obj.entry_price:
        side = (position_obj.extra or {}).get("side", "unknown")
        # CFD: side에 따라 P&L 방향이 다름
        if side == "sell":
            unrealized_pnl_jpy = (position_obj.entry_price - current_price) * position_obj.entry_amount
        else:
            unrealized_pnl_jpy = (current_price - position_obj.entry_price) * position_obj.entry_amount
        unrealized_pnl_pct = (
            unrealized_pnl_jpy / (position_obj.entry_price * position_obj.entry_amount) * 100
            if position_obj.entry_price > 0 else 0.0
        )
        stop_price = position_obj.stop_loss_price or 0.0

        position_data = {
            "side": side,
            "entry_price": position_obj.entry_price,
            "entry_amount": position_obj.entry_amount,
            "current_price": current_price,
            "unrealized_pnl_jpy": round(unrealized_pnl_jpy, 0),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
            "stop_loss_price": round(stop_price, 0),
        }
        position_summary = get_position_summary(exit_signal, rsi, unrealized_pnl_pct)

    entry_blockers = get_entry_blockers(
        signal, current_price, ema, ema_slope_pct, rsi,
        slope_min=float(params.get("ema_slope_entry_min", 0.0)),
    ) if not position_data else []

    # keep_rate 블로커 추가
    keep_rate_warn = params.get("keep_rate_warn", 250)
    if collateral_data and collateral_data["keep_rate"] < keep_rate_warn:
        entry_blockers.append(f"keep_rate {collateral_data['keep_rate']:.0f}% < {keep_rate_warn}%")

    entry_conditions_met = len(entry_blockers) == 0 and not position_data
    market_summary = get_market_summary(ema_slope_pct, rsi, signal) if not position_data else None

    # 6. 아이콘/상태 조립
    trend_icon = get_trend_icon(ema_slope_pct)
    rsi_state = get_rsi_state(rsi)
    ema_state = get_ema_state(current_price, ema, ema_slope_pct)
    volatility_state = get_volatility_state(atr_pct)

    # 7. 텍스트 조립
    report_data = {
        "current_price": current_price,
        "signal": signal,
        "trend_icon": trend_icon,
        "ema_state": ema_state,
        "rsi_state": rsi_state,
        "volatility_state": volatility_state,
        "market_summary": market_summary,
        "position_summary": position_summary,
        "position": position_data,
        "entry_blockers": entry_blockers,
        "jpy_available": collateral_data["collateral"] if collateral_data else 0,
        "coin_available": position_data["entry_amount"] if position_data else 0,
        "ema20": ema,
        "strategy_name": strategy.name,
        "strategy_id": strategy.id,
    }

    telegram_text = build_telegram_text(prefix.upper(), time_str, pair, report_data)
    # CFD 추가 라인
    if collateral_data:
        kr = collateral_data["keep_rate"]
        kr_icon = "🟢" if kr >= keep_rate_warn else "🟡" if kr >= params.get("keep_rate_critical", 120) else "🔴"
        cfd_line = f"\n💰 증거금: ¥{collateral_data['collateral']:,.0f} | {kr_icon} keep_rate: {kr:.0f}%"
        telegram_text += cfd_line
    if position_data:
        side_icon = "📈" if position_data["side"] == "buy" else "📉"
        telegram_text += f"\n{side_icon} CFD {position_data['side'].upper()} {position_data['entry_amount']:.4f} BTC"

    memory_block = build_memory_block(prefix.upper(), time_str, pair, report_data)

    candle_open_time = candles[-1].open_time.isoformat() if candles else None

    # 4H 변동률
    last_candle_close = float(candles[-1].close) if candles else None
    candle_change_pct = (
        (current_price - last_candle_close) / last_candle_close * 100
        if last_candle_close and last_candle_close > 0 else 0.0
    )

    result_dict = {
        "success": True,
        "generated_at": now_jst.isoformat(),
        "report": {
            "telegram_text": telegram_text,
            "memory_block": memory_block,
        },
        "alert": None,
        "raw": {
            "pair": pair,
            "trading_style": "cfd_trend_following",
            "strategy_name": strategy.name,
            "strategy_id": strategy.id,
            "current_price": round(current_price, 0),
            "signal": signal,
            "trend_icon": trend_icon,
            "market_summary": market_summary,
            "position_summary": position_summary,
            "ema20": round(ema, 0) if ema else None,
            "ema_slope_pct": round(ema_slope_pct, 4) if ema_slope_pct is not None else None,
            "ema_state": ema_state,
            "rsi14": round(rsi, 2) if rsi is not None else None,
            "rsi_state": rsi_state,
            "atr": round(atr, 0) if atr else None,
            "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
            "volatility_state": volatility_state,
            "position": position_data,
            "collateral": collateral_data,
            "entry_conditions_met": entry_conditions_met,
            "entry_blockers": entry_blockers,
            "exit_signal": {
                "action": exit_signal["action"],
                "reason": exit_signal["reason"],
            } if position_data else None,
            "candle_change_pct": round(candle_change_pct, 2),
            "candle_open_time": candle_open_time,
        },
    }

    # Alert 평가
    if test_alert_level:
        alert = _build_test_alert(result_dict["raw"], test_alert_level)
    else:
        prev_raw = _prev_raw_cache.get(pair)
        alert = evaluate_alert(result_dict["raw"], prev_raw)
    _prev_raw_cache[pair] = result_dict["raw"]
    result_dict["alert"] = alert

    if reset_cooldown:
        _last_alert_time.pop(pair, None)

    if alert and alert["level"] == "critical":
        is_test = test_alert_level is not None
        has_position = bool(result_dict.get("raw", {}).get("position"))
        current_regime = result_dict.get("raw", {}).get("signal", "")
        await _trigger_rachel_analysis(
            pair, alert, test=is_test,
            has_position=has_position,
            current_regime=current_regime,
        )

    return result_dict
