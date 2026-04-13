"""추세추종 모니터링 리포트 — 텍스트 조립 + 생성."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, select
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
    get_entry_blockers_short,
    get_wait_direction,
)
from .alerts import (
    _prev_raw_cache,
    _last_alert_time,
    _build_test_alert,
    evaluate_alert,
    _trigger_rachel_analysis,
)

logger = logging.getLogger(__name__)


def build_telegram_text(prefix: str, time_str: str, pair: str, data: dict) -> str:
    icon = data["trend_icon"]
    currency = pair.split("_")[0].upper()
    lines = []

    if data["position"]:
        p = data["position"]
        pnl_jpy = p["unrealized_pnl_jpy"]
        pnl_pct = p["unrealized_pnl_pct"]
        pnl_sign = "+" if pnl_jpy >= 0 else ""
        current_price = data["current_price"]

        p_side = p.get("side", "buy")
        side_label = "롱" if p_side == "buy" else "숏"
        lines.append(f"[{prefix}] {time_str} | {pair} {icon}추세추종 — {side_label} 보유")
        lines.append(f"📍 ¥{current_price:,.0f}")
        lines.append(f"💰 미실현 {pnl_sign}¥{pnl_jpy:,.0f} ({pnl_sign}{pnl_pct:.2f}%)")
        lines.append(f" · 진입 {p['entry_amount']}{currency} @ ¥{p['entry_price']:,.0f}")
        stop = p.get("stop_loss_price", 0)
        distance = p.get("trailing_stop_distance", 0)
        stop_pct = (stop - current_price) / current_price * 100 if stop and current_price > 0 else 0.0
        lines.append(f" · 손절 ¥{stop:,.0f} (현재가 {stop_pct:.2f}%, 거리 ¥{distance:,.0f})")

        situation = data.get("position_summary") or "보유 유지"
        lines.append(f"📊 지금: {situation}")

        exit_sig = data.get("exit_signal")
        if exit_sig:
            action = exit_sig.get("action", "hold")
            if action == "full_exit":
                outlook = "즉시 청산 실행 중"
            elif action == "tighten_stop":
                outlook = "추세 약화 — 스탑 조임 중. 추가 하락 시 자동 청산"
            elif pnl_pct > 5.0:
                outlook = "큰 수익 구간 — 트레일링 스탑이 수익 보호 중"
            elif pnl_pct < -1.0:
                outlook = "손절선 접근 중 — 반등 없으면 자동 청산"
            else:
                outlook = "추세 이어지면 트레일링 스탑 자동 상향"
            lines.append(f"⚡ 전망: {outlook}")

        lines.append(f"💰 ¥{data['jpy_available']:,.0f}")
    else:
        wait_dir = data.get("wait_direction")  # None = 현물 (롱 전용)
        if wait_dir == "short":
            lines.append(f"[{prefix}] {time_str} | {pair} {icon}추세추종 — 숏 대기중")
            lines.append(f"📍 ¥{data['current_price']:,.0f}")
            lines.append("💡 진입 조건: 가격이 EMA 아래에서 우하향할 때 매도")
        elif wait_dir == "neutral":
            lines.append(f"[{prefix}] {time_str} | {pair} {icon}추세추종 — 관망중")
            lines.append(f"📍 ¥{data['current_price']:,.0f}")
            lines.append("💡 롱: EMA 위 우상향 매수 / 숏: EMA 아래 우하향 매도")
        elif wait_dir == "long":
            lines.append(f"[{prefix}] {time_str} | {pair} {icon}추세추종 — 롱 대기중")
            lines.append(f"📍 ¥{data['current_price']:,.0f}")
            lines.append("💡 진입 조건: 가격이 EMA 위에서 우상향할 때 매수")
        else:
            # wait_dir is None (현물 / spot) — 기존 동작 유지
            lines.append(f"[{prefix}] {time_str} | {pair} {icon}추세추종 — 대기중")
            lines.append(f"📍 ¥{data['current_price']:,.0f}")
            lines.append("💡 진입 조건: 가격이 EMA 위에서 우상향할 때 매수")

        situation = data.get("market_summary") or "관망"
        lines.append(f"📊 지금: {situation}")

        met = data.get("conditions_met", 0)
        total = data.get("conditions_total", 5)
        blockers = data.get("entry_blockers", [])
        if blockers:
            lines.append(f"🚫 {met}/{total} 진입까지:")
            for b in blockers:
                lines.append(f" · {b}")
        else:
            lines.append(f"✅ {met}/{total} 진입 조건 충족")

        lines.append(f"💰 ¥{data['jpy_available']:,.0f}")

    return "\n".join(lines)


def build_memory_block(prefix: str, time_str: str, pair: str, data: dict) -> str:
    strategy_name = data.get("strategy_name", "unknown")
    strategy_id = data.get("strategy_id", "?")
    lines = [
        f"## [{time_str} JST] 🟢{prefix}: {pair} | 모니터링 | strategy: {strategy_name}(id={strategy_id})",
        "",
        "### 추세 상태",
        f"- signal: {data['signal']} | {data['ema_state']} | {data['rsi_state']} | {data['volatility_state']}",
        f"- 현재가: ¥{data['current_price']:,.2f} | EMA20: ¥{data['ema20']:,.2f}" if data.get("ema20") else f"- 현재가: ¥{data['current_price']:,.2f}",
        "",
        "### 포지션 상태",
    ]

    if data["position"]:
        p = data["position"]
        currency = pair.split("_")[0].upper()
        lines.append(f"- 보유 {p['entry_amount']}{currency} @ ¥{p['entry_price']:,.0f}")
        lines.append(f"- 손절: ¥{p['stop_loss_price']:,.0f} | 미실현: ¥{p['unrealized_pnl_jpy']:,.0f} ({p['unrealized_pnl_pct']:.2f}%)")
    else:
        lines.append("- 포지션 없음")
        if data["entry_blockers"]:
            lines.append(f"- 진입 차단: {', '.join(data['entry_blockers'])}")

    lines.extend([
        "",
        "### 자산 현황",
        f"- JPY: ¥{data['jpy_available']:,.0f} | {pair.split('_')[0]}: {data['coin_available']:.4f}개",
        "",
        "### 특이사항",
        "- 없음",
    ])

    return "\n".join(lines)


async def generate_trend_report(
    pair: str,
    prefix: str,
    pair_column: str,
    strategy: Any,
    adapter: Any,
    trend_manager: Any,
    candle_model: Any,
    db: AsyncSession,
    test_alert_level: str | None = None,
    reset_cooldown: bool = False,
) -> dict:
    """trend_following 전략의 모니터링 리포트 생성."""
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
    position_obj = trend_manager.get_position(pair)
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

    # 4H 변동률
    last_candle_close = float(candles[-1].close) if candles else None
    candle_change_pct = (
        (current_price - last_candle_close) / last_candle_close * 100
        if last_candle_close and last_candle_close > 0 else 0.0
    )

    # 1H 변動률
    result_1h = await db.execute(
        select(candle_model)
        .where(
            and_(
                pair_col == pair,
                candle_model.timeframe == "1h",
                candle_model.is_complete == True,
            )
        )
        .order_by(candle_model.open_time.desc())
        .limit(1)
    )
    candle_1h = result_1h.scalar_one_or_none()
    candle_1h_close = float(candle_1h.close) if candle_1h else None
    candle_1h_change_pct = (
        (current_price - candle_1h_close) / candle_1h_close * 100
        if candle_1h_close and candle_1h_close > 0 else 0.0
    )

    # 4. 잔고 조회
    balance = await adapter.get_balance()
    jpy_available = balance.get_available("jpy")
    coin_currency = pair.split("_")[0].lower()
    coin_available = balance.get_available(coin_currency)

    # 5. 표시 값 조립
    trend_icon = get_trend_icon(ema_slope_pct)
    rsi_state = get_rsi_state(rsi)
    ema_state = get_ema_state(current_price, ema, ema_slope_pct)
    volatility_state = get_volatility_state(atr_pct)

    # 6. 포지션 데이터 or entry_blockers
    position_data = None
    position_summary = None
    if position_obj and position_obj.entry_price:
        unrealized_pnl_jpy = (current_price - position_obj.entry_price) * position_obj.entry_amount
        unrealized_pnl_pct = (
            (current_price - position_obj.entry_price) / position_obj.entry_price * 100
            if position_obj.entry_price > 0 else 0.0
        )
        stop_price = position_obj.stop_loss_price or 0.0
        trailing_distance = current_price - stop_price if stop_price else 0.0

        position_data = {
            "entry_price": position_obj.entry_price,
            "entry_amount": position_obj.entry_amount,
            "current_price": current_price,
            "unrealized_pnl_jpy": round(unrealized_pnl_jpy, 0),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
            "stop_loss_price": round(stop_price, 0),
            "trailing_stop_distance": round(trailing_distance, 0),
        }
        position_summary = get_position_summary(exit_signal, rsi, unrealized_pnl_pct)

    entry_blockers = get_entry_blockers(
        signal, current_price, ema, ema_slope_pct, rsi,
        rsi_min=float(params.get("entry_rsi_min", 40.0)),
        rsi_max=float(params.get("entry_rsi_max", 65.0)),
        slope_min=float(params.get("ema_slope_entry_min", 0.0)),
    ) if not position_data else []

    entry_conditions_met = len(entry_blockers) == 0 and not position_data
    # 5 conditions: price>EMA, slope≥min, RSI range, regime, no_position
    conditions_total = 5
    conditions_met = conditions_total - len(entry_blockers) if not position_data else conditions_total
    market_summary = get_market_summary(ema_slope_pct, rsi, signal) if not position_data else None
    # wait_direction: _supports_short=True 매니저(GMO Coin 등)는 CFD 분기 적용
    supports_short = getattr(trend_manager, "_supports_short", False)
    if supports_short and not position_data:
        wait_direction = get_wait_direction(True, signal, current_price, ema, ema_slope_pct)
        if wait_direction == "short":
            entry_blockers = get_entry_blockers_short(
                signal, current_price, ema, ema_slope_pct, rsi,
                rsi_min=float(params.get("entry_rsi_min_short", 35.0)),
                rsi_max=float(params.get("entry_rsi_max_short", 60.0)),
                slope_threshold=float(params.get("ema_slope_short_threshold", -0.05)),
            )
            conditions_met = max(0, conditions_total - len(entry_blockers))
    else:
        wait_direction = None  # spot (BF 등) — 기존 동작 유지
    # 7. 텍스트 조립용 데이터
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
        "exit_signal": exit_signal if position_data else None,
        "entry_blockers": entry_blockers,
        "wait_direction": wait_direction,
        "conditions_met": conditions_met,
        "conditions_total": conditions_total,
        "jpy_available": jpy_available,
        "coin_available": coin_available,
        "ema20": ema,
        "strategy_name": strategy.name,
        "strategy_id": strategy.id,
    }

    telegram_text = build_telegram_text(prefix.upper(), time_str, pair, report_data)
    memory_block = build_memory_block(prefix.upper(), time_str, pair, report_data)

    candle_open_time = candles[-1].open_time.isoformat() if candles else None

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
            "trading_style": "trend_following",
            "strategy_name": strategy.name,
            "strategy_id": strategy.id,
            "current_price": round(current_price, 6),
            "signal": signal,
            "trend_icon": trend_icon,
            "market_summary": market_summary,
            "position_summary": position_summary,
            "ema20": round(ema, 6) if ema else None,
            "ema_slope_pct": round(ema_slope_pct, 4) if ema_slope_pct is not None else None,
            "ema_state": ema_state,
            "rsi14": round(rsi, 2) if rsi is not None else None,
            "rsi_state": rsi_state,
            "atr": round(atr, 6) if atr else None,
            "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
            "volatility_state": volatility_state,
            "position": position_data,
            "jpy_available": round(jpy_available, 0),
            "coin_available": round(coin_available, 6),
            "entry_conditions_met": entry_conditions_met,
            "entry_blockers": entry_blockers,
            "exit_signal": {
                "action": exit_signal["action"],
                "reason": exit_signal["reason"],
            } if position_data else None,
            "candle_change_pct": round(candle_change_pct, 2),
            "candle_1h_change_pct": round(candle_1h_change_pct, 2),
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
