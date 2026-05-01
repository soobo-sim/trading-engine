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


def _format_situation_with_basis(
    situation: str,
    ema_slope_pct: float | None,
    rsi: float | None,
) -> str:
    """📊 지금: 판단 근거(EMA slope + RSI)를 괄호로 append."""
    if ema_slope_pct is not None and rsi is not None:
        if ema_slope_pct > 0:
            arrow = "↑"
        elif ema_slope_pct < 0:
            arrow = "↓"
        else:
            arrow = "→"
        return f"{situation} (EMA{arrow}{ema_slope_pct:+.2f}%, RSI {rsi:.0f})"
    return situation


def _format_balance_line(data: dict) -> str:
    """💼 증거금 (레버리지) or 💰 잔고 (현물) 한 줄."""
    collateral = data.get("collateral")
    if collateral:
        total = collateral["collateral"]
        required = collateral["require_collateral"]
        available = max(total - required, 0)
        return f"💼 증거금 ¥{total:,.0f} | 필요 ¥{required:,.0f} | 여력 ¥{available:,.0f}"
    return f"💰 ¥{data['jpy_available']:,.0f}"


def _build_entry_mode_lines(data: dict) -> list[str]:
    """entry_mode / entry_timeframe / armed 상태 줄 목록을 반환한다.

    ws_cross 모드 또는 1h timeframe이면 '진입 모드' 줄과
    armed 상태 줄(ws_cross 전용)을 반환한다. 기본 설정이면 빈 리스트.
    """
    import time as _time

    lines: list[str] = []
    entry_mode = data.get("entry_mode", "market")
    entry_tf = data.get("entry_timeframe")
    tf_is_1h = bool(entry_tf and str(entry_tf).lower() in ("1h", "1"))

    if entry_mode == "ws_cross":
        _tf_part = " + 1H slope/RSI" if tf_is_1h else ""
        lines.append(f"진입 모드: ⚡ WS 돌파{_tf_part} (EMA 실시간 감시)")
    elif tf_is_1h:
        lines.append("진입 모드: 📊 1H slope/RSI + 4H 체제")

    if entry_mode == "ws_cross":
        armed_dir = data.get("armed_direction")
        armed_ema = data.get("armed_ema")
        armed_expire = data.get("armed_expire_at", 0.0) or 0.0
        if armed_dir is not None and armed_ema is not None:
            remain = max(0.0, armed_expire - _time.time())
            h_ = int(remain // 3600)
            m_ = int((remain % 3600) // 60)
            dir_kr = "숏" if armed_dir == "short" else "롱"
            lines.append(f"⚡ WS 대기: {dir_kr} armed @ ¥{armed_ema:,.0f}  (만료까지 {h_}h {m_:02d}m)")
        else:
            lines.append("⏳ WS 대기: armed 조건 미충족")

    return lines


_STRATEGY_LABEL = {
    "trend_following": "추세추종",
    "box_mean_reversion": "박스역추세",
}
_REGIME_LABEL = {
    "trending": "추세장",
    "ranging": "횡보장",
    "unclear": "불명확",
}


def build_telegram_text(prefix: str, time_str: str, pair: str, data: dict) -> str:
    icon = data["trend_icon"]
    currency = pair.split("_")[0].upper()
    ema_slope_pct = data.get("ema_slope_pct")
    rsi = data.get("rsi")
    regime = data.get("regime")
    active_strategy = data.get("active_strategy")
    lines = []

    if data["position"]:
        p = data["position"]
        pnl_jpy = p["unrealized_pnl_jpy"]
        pnl_pct = p["unrealized_pnl_pct"]
        current_price = data["current_price"]

        p_side = p.get("side", "buy")
        side_label = "롱" if p_side == "buy" else "숏"
        lines.append(f"[{prefix}] {time_str} | {pair} {icon}추세추종 — {side_label} 보유")

        # 현시세 + 진입가 대비 차이 (부호를 ¥ 앞에)
        price_diff = p.get("price_diff", 0)
        if price_diff >= 0:
            diff_str = f"+¥{price_diff:,.0f}"
        else:
            diff_str = f"-¥{abs(price_diff):,.0f}"
        lines.append(f"📍 ¥{current_price:,.0f} (진입가 대비 {diff_str})")

        # 미실현 P&L — 부호를 ¥ 앞에 표시
        if pnl_jpy >= 0:
            lines.append(f"💰 미실현 +¥{pnl_jpy:,.0f} (+{pnl_pct:.2f}%)")
        else:
            lines.append(f"💰 미실현 -¥{abs(pnl_jpy):,.0f} ({pnl_pct:.2f}%)")

        lines.append(f" · 진입 {p['entry_amount']}{currency} @ ¥{p['entry_price']:,.0f}")
        stop = p.get("stop_loss_price", 0)
        distance = p.get("trailing_stop_distance", 0)
        pnl_at_stop = p.get("pnl_at_stop", 0)
        breakeven_target = p.get("breakeven_trigger_price")
        current_price = data["current_price"]
        side_val = p.get("side", "buy")

        # SL 섹션: 명확한 수치 기반 표시
        distance_pct = abs(current_price - stop) / current_price * 100 if current_price > 0 and stop else 0
        lines.append(f"🛑 SL: ¥{stop:,.0f}")
        lines.append(f"   · 현재가까지 거리: ¥{distance:,.0f} ({distance_pct:.1f}%)")
        if pnl_at_stop > 0:
            lines.append(f"   · 발동 시: +¥{pnl_at_stop:,.0f} 이익 확정 (손익보호 중)")
        elif pnl_at_stop == 0:
            lines.append(f"   · 발동 시: ¥0 (손익분기)")
        else:
            lines.append(f"   · 발동 시: -¥{abs(pnl_at_stop):,.0f} 손절")
        # 손익분기 전환 기준가 표시
        if breakeven_target and pnl_at_stop < 0:
            if side_val == "sell":
                remaining = current_price - breakeven_target
            else:
                remaining = breakeven_target - current_price
            if remaining > 0:
                lines.append(f"   · ¥{breakeven_target:,.0f} 도달 시 → 발동해도 손해 없음 (¥{remaining:,.0f} 남음)")

        situation = data.get("position_summary") or "보유 유지"
        lines.append(f"📊 지금: {_format_situation_with_basis(situation, ema_slope_pct, rsi)}")

        exit_sig = data.get("exit_signal")
        if exit_sig:
            action = exit_sig.get("action", "hold")
            reason = exit_sig.get("reason", "")
            if action == "full_exit":
                outlook = "즉시 청산 실행 중"
            elif action == "tighten_stop":
                outlook = "추세 약화 — 스탑 조임 중. 추가 하락 시 자동 청산"
            elif pnl_at_stop > 0:
                outlook = f"트레일링 스탑이 이익 보호 중 (최소 +¥{pnl_at_stop:,.0f} 확정)"
            elif pnl_jpy > 0 and pnl_at_stop <= 0:
                outlook = "이익 중이나 스탑은 진입가 아래 — 추가 상승 시 이익보호로 전환"
            elif pnl_pct < -1.0:
                outlook = "손절선 접근 중 — 반등 없으면 자동 청산"
            else:
                outlook = "추세 이어지면 트레일링 스탑 자동 상향"
            lines.append(f"⚡ 전망: {outlook}")
            _ACTION_KR = {
                "hold": "보유 유지",
                "full_exit": "즉시 청산",
                "tighten_stop": "스탑 조임",
            }
            action_kr = _ACTION_KR.get(action, action)
            reason_str = f", reason={reason}" if reason else ""
            lines.append(f"판단 도메인 → {action_kr} (action={action}{reason_str})")

        _rg = data.get("regime_gate_info")
        if _rg is not None:
            _last = _rg.get("last_regime")
            _cnt = _rg.get("consecutive_count", 0)
            _active = _rg.get("active_strategy")
            _rl = _REGIME_LABEL.get(_last, _last or "-")
            if _active is not None:
                if data.get("jit_bypass_gate"):
                    lines.append(f"⚙️ 체제: {_rl}(×{_cnt}) | JIT bypass")
                else:
                    lines.append(f"⚙️ 체제: {_rl}(×{_cnt}) | 활성: {_STRATEGY_LABEL.get(_active, _active)}")
            else:
                lines.append(f"⚙️ 체제: {_rl}(×{_cnt}) | 진입 차단 중")
        elif regime or active_strategy:
            regime_label = _REGIME_LABEL.get(regime, regime or "-")
            strategy_label = _STRATEGY_LABEL.get(active_strategy, active_strategy or "-")
            lines.append(f"⚙️ 체제: {regime_label} | 활성전략: {strategy_label}")
        lines.extend(_build_entry_mode_lines(data))
        lines.append(_format_balance_line(data))
    else:
        wait_dir = data.get("wait_direction")  # None = 현물 (롱 전용)
        if wait_dir == "short":
            lines.append(f"[{prefix}] {time_str} | {pair} {icon}추세추종 — 숏 대기중")
            lines.append(f"📍 ¥{data['current_price']:,.0f}")
        elif wait_dir == "neutral":
            lines.append(f"[{prefix}] {time_str} | {pair} {icon}추세추종 — 관망중")
            lines.append(f"📍 ¥{data['current_price']:,.0f}")
        elif wait_dir == "long":
            lines.append(f"[{prefix}] {time_str} | {pair} {icon}추세추종 — 롱 대기중")
            lines.append(f"📍 ¥{data['current_price']:,.0f}")
        else:
            # wait_dir is None (현물 / spot) — 기존 동작 유지
            lines.append(f"[{prefix}] {time_str} | {pair} {icon}추세추종 — 대기중")
            lines.append(f"📍 ¥{data['current_price']:,.0f}")

        situation = data.get("market_summary") or "관망"
        lines.append(f"📊 지금: {_format_situation_with_basis(situation, ema_slope_pct, rsi)}")

        # ── 판단 도메인 결론 표시 ────────────────────────────────────
        _SIGNAL_KR = {
            'long_setup':      '🟢 롱 진입 신호 — 조건 충족',
            'short_setup':     '🔴 숏 진입 신호 — 조건 충족',
            'hold':            '⏸ 조건 미충족 — 대기',
            'wait_regime':     '⏳ RegimeGate 차단 — 체제 미충족',
            'long_caution':    '⚠️ 롱 추세 이탈 경고 — 진입 보류',
            'long_overheated': '🌡 롱 RSI 과열 — 진입 보류',
            'short_caution':   '⚠️ 숏 추세 이탈 경고 — 진입 보류',
            'short_oversold':  '🌡 숏 RSI 과매도 — 진입 보류',
        }
        signal_val = data.get("signal", "")
        signal_display = _SIGNAL_KR.get(signal_val, f"신호: {signal_val}")
        signal_suffix = f" (signal={signal_val})" if signal_val else ""
        lines.append(f"판단 도메인 → {signal_display}{signal_suffix}")

        _rg = data.get("regime_gate_info")
        if _rg is not None:
            _last = _rg.get("last_regime")
            _cnt = _rg.get("consecutive_count", 0)
            _active = _rg.get("active_strategy")
            _rl = _REGIME_LABEL.get(_last, _last or "-")
            if _active is not None:
                if data.get("jit_bypass_gate"):
                    lines.append(f"⚙️ 체제: {_rl}(×{_cnt}) | JIT bypass")
                else:
                    lines.append(f"⚙️ 체제: {_rl}(×{_cnt}) | 활성: {_STRATEGY_LABEL.get(_active, _active)}")
            else:
                lines.append(f"⚙️ 체제: {_rl}(×{_cnt}) | 진입 차단 중")
        elif regime or active_strategy:
            regime_label = _REGIME_LABEL.get(regime, regime or "-")
            strategy_label = _STRATEGY_LABEL.get(active_strategy, active_strategy or "-")
            lines.append(f"⚙️ 체제: {regime_label} | 활성전략: {strategy_label}")
        lines.extend(_build_entry_mode_lines(data))
        lines.append(_format_balance_line(data))

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

    # 4. 잔고 조회 + 증거금 (레버리지 어댑터)
    balance = await adapter.get_balance()
    jpy_available = balance.get_available("jpy")
    coin_currency = pair.split("_")[0].lower()
    coin_available = balance.get_available(coin_currency)

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
            logger.warning(f"[TrendReport] {pair}: 증거금 조회 실패: {e}")

    # 5. 표시 값 조립
    trend_icon = get_trend_icon(ema_slope_pct)
    rsi_state = get_rsi_state(rsi)
    ema_state = get_ema_state(current_price, ema, ema_slope_pct)
    volatility_state = get_volatility_state(atr_pct)

    # 6. 포지션 데이터 or entry_blockers
    position_data = None
    position_summary = None
    if position_obj and position_obj.entry_price:
        side = (position_obj.extra or {}).get("side", "buy")
        # 롱: (현재가 - 진입가) × 수량 / 숏: (진입가 - 현재가) × 수량
        if side == "sell":
            unrealized_pnl_jpy = (position_obj.entry_price - current_price) * position_obj.entry_amount
        else:
            unrealized_pnl_jpy = (current_price - position_obj.entry_price) * position_obj.entry_amount
        unrealized_pnl_pct = (
            unrealized_pnl_jpy / (position_obj.entry_price * position_obj.entry_amount) * 100
            if position_obj.entry_price > 0 else 0.0
        )
        stop_price = position_obj.stop_loss_price or 0.0
        trailing_distance = abs(current_price - stop_price) if stop_price else 0.0
        price_diff = current_price - position_obj.entry_price  # 부호 있는 가격차

        # 스탑 발동 시 예상 P&L: 롱 (stop-entry)*amount, 숏 (entry-stop)*amount
        if side == "sell":
            pnl_at_stop = round((position_obj.entry_price - stop_price) * position_obj.entry_amount, 0)
        else:
            pnl_at_stop = round((stop_price - position_obj.entry_price) * position_obj.entry_amount, 0)

        position_data = {
            "side": side,
            "entry_price": position_obj.entry_price,
            "entry_amount": position_obj.entry_amount,
            "current_price": current_price,
            "unrealized_pnl_jpy": round(unrealized_pnl_jpy, 0),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
            "stop_loss_price": round(stop_price, 0),
            "trailing_stop_distance": round(trailing_distance, 0),
            "price_diff": round(price_diff, 0),
            "pnl_at_stop": int(pnl_at_stop),
        }
        # 손익분기 전환 기준가: 이 가격 도달 시 SL이 진입가 이상 → 스탑 발동해도 손해 없음
        if atr:
            breakeven_trigger_atr = float(params.get("breakeven_trigger_atr", 1.0))
            if side == "sell":
                position_data["breakeven_trigger_price"] = round(
                    position_obj.entry_price - atr * breakeven_trigger_atr, 0
                )
            else:
                position_data["breakeven_trigger_price"] = round(
                    position_obj.entry_price + atr * breakeven_trigger_atr, 0
                )
        position_summary = get_position_summary(exit_signal, rsi, unrealized_pnl_pct)

    entry_blockers = get_entry_blockers(
        signal, current_price, ema, ema_slope_pct, rsi,
        rsi_min=float(params.get("entry_rsi_min", 40.0)),
        rsi_max=float(params.get("entry_rsi_max", 65.0)),
        slope_min=float(params.get("ema_slope_entry_min", 0.0)),
    ) if not position_data else []

    entry_conditions_met = len(entry_blockers) == 0 and not position_data
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

    # ── 판단 도메인 최종 시그널 (실행 도메인에 전달된 값) ──────────
    # entry_condition_lines는 제거 — 실행 도메인은 판단 도메인 결론만 수행
    entry_condition_lines: list[str] = []

    # 7. 텍스트 조립용 데이터
    regime = sig.get("regime")  # "trending" | "ranging" | "unclear"
    _regime_gate = getattr(trend_manager, "_regime_gate", None)
    active_strategy = _regime_gate.active_strategy if _regime_gate is not None else None
    regime_gate_info = None
    if _regime_gate is not None:
        _hist = _regime_gate.regime_history
        regime_gate_info = {
            "last_regime": _hist[-1] if _hist else None,
            "consecutive_count": _regime_gate.consecutive_count,
            "active_strategy": _regime_gate.active_strategy,
        }
    _jit_bypass_gate = getattr(trend_manager, "_jit_bypass_gate", False)

    # ── entry_mode / entry_timeframe / armed 상태 (trend_manager 인메모리) ──
    _entry_mode = str(params.get("entry_mode", "market"))
    _entry_timeframe = params.get("entry_timeframe")  # '1h' | None
    _armed_ema_val = getattr(trend_manager, "_armed_entry_ema", {}).get(pair)
    _armed_dir = getattr(trend_manager, "_armed_direction", {}).get(pair)
    _armed_expire = getattr(trend_manager, "_armed_expire_at", {}).get(pair, 0.0)

    report_data = {
        "current_price": current_price,
        "signal": signal,
        "trend_icon": trend_icon,
        "ema_slope_pct": ema_slope_pct,
        "rsi": rsi,
        "atr": atr,
        "ema_state": ema_state,
        "rsi_state": rsi_state,
        "volatility_state": volatility_state,
        "market_summary": market_summary,
        "position_summary": position_summary,
        "position": position_data,
        "exit_signal": exit_signal if position_data else None,
        "entry_blockers": entry_blockers,
        "entry_condition_lines": entry_condition_lines,
        "wait_direction": wait_direction,
        "conditions_met": conditions_met,
        "conditions_total": conditions_total,
        "jpy_available": jpy_available,
        "coin_available": coin_available,
        "collateral": collateral_data,
        "ema20": ema,
        "strategy_name": strategy.name,
        "strategy_id": strategy.id,
        "regime": regime,
        "active_strategy": active_strategy,
        "regime_gate_info": regime_gate_info,
        "jit_bypass_gate": _jit_bypass_gate,
        # 신규: 진입 모드 + armed 상태
        "entry_mode": _entry_mode,
        "entry_timeframe": _entry_timeframe,
        "armed_direction": _armed_dir,
        "armed_ema": _armed_ema_val,
        "armed_expire_at": _armed_expire,
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
