"""
모니터링 리포트 서비스 — 사만다 15분 보고용 서버측 텍스트 생성.

서버가 시그널 계산 → 아이콘/상태/요약/entry_blockers → telegram_text + memory_block 조립.
사만다(GPT 4.1)는 단순 출력만 한다.

설계: solution-design/archive/MONITORING_REPORT_API.md
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


# ── 표시용 함수 ──────────────────────────────────────────────

def get_trend_icon(ema_slope_pct: Optional[float]) -> str:
    if ema_slope_pct is None:
        return "❓"
    if ema_slope_pct > 0.05:
        return "📈"
    elif ema_slope_pct < -0.05:
        return "📉"
    return "➡️"


def get_rsi_state(rsi: Optional[float]) -> str:
    if rsi is None:
        return "RSI 없음"
    if rsi < 30:
        return f"RSI 과매도({rsi:.1f})"
    elif rsi > 70:
        return f"RSI 과열({rsi:.1f})"
    return f"RSI 중립({rsi:.1f})"


def get_ema_state(current_price: float, ema: Optional[float], ema_slope_pct: Optional[float]) -> str:
    if ema is None or ema_slope_pct is None:
        return "EMA 데이터 부족"
    if current_price >= ema:
        arrow = "↑" if ema_slope_pct > 0 else "↓"
        return f"EMA 위 {ema_slope_pct:+.2f}% {arrow}"
    else:
        arrow = "↑" if ema_slope_pct > 0 else "↓"
        return f"EMA 아래 {ema_slope_pct:+.2f}% {arrow}"


def get_volatility_state(atr_pct: Optional[float]) -> str:
    if atr_pct is None:
        return "변동성 불명"
    if atr_pct >= 3.0:
        return "변동성 높음"
    elif atr_pct >= 1.5:
        return "변동성 보통"
    return "변동성 낮음"


def get_market_summary(ema_slope_pct: Optional[float], rsi: Optional[float], signal: str) -> str:
    """포지션 미보유 시 한줄 요약."""
    if ema_slope_pct is None or rsi is None:
        return "데이터 부족"
    if signal == "exit_warning":
        return "🔻 하락 전환·전략 유효성 점검"
    if ema_slope_pct > 0.1 and 40 <= rsi <= 65:
        return "✅ 진입 임박"
    if ema_slope_pct > 0 and (rsi < 40 or rsi > 65):
        return "⏳ 추세 유지·눌림목 대기"
    if -0.1 < ema_slope_pct <= 0:
        return "⚠️ 추세 약화·관망"
    if ema_slope_pct <= -0.1 and rsi < 30:
        return "🔻 급락·반등 대기"
    if ema_slope_pct <= -0.1:
        return "🔻 하락 전환·전략 유효성 점검"
    return "관망"


def get_position_summary(exit_signal: dict, rsi: Optional[float], unrealized_pnl_pct: float) -> str:
    """포지션 보유 시 한줄 요약."""
    action = exit_signal.get("action", "hold")
    if action == "full_exit":
        return "🚨 청산 시그널 발생"
    if action == "tighten_stop":
        return "⚠️ 스탑 타이트닝 중"
    if unrealized_pnl_pct > 2.0:
        return "📈 수익 확대 중·보유 유지"
    if unrealized_pnl_pct > 0:
        return "상승추세·보유 유지"
    return "추세 유지·손익 관찰"


def get_entry_blockers(
    signal: str,
    current_price: float,
    ema: Optional[float],
    ema_slope_pct: Optional[float],
    rsi: Optional[float],
    rsi_min: float = 40.0,
    rsi_max: float = 65.0,
) -> List[str]:
    """진입까지 남은 조건 목록. 비어있으면 진입 가능."""
    blockers: List[str] = []
    if ema_slope_pct is not None and ema_slope_pct < 0:
        blockers.append(f"EMA slope {ema_slope_pct:+.2f}% → 양수 전환 필요")
    if ema is not None and current_price < ema:
        gap_pct = (ema - current_price) / ema * 100
        blockers.append(f"가격 < EMA20 (¥{current_price:,.0f} vs ¥{ema:,.0f}, 갭 {gap_pct:.1f}%)")
    if rsi is not None and rsi < rsi_min:
        blockers.append(f"RSI {rsi:.1f} → {rsi_min:.0f} 이상 필요 (breakdown)")
    if rsi is not None and rsi > rsi_max:
        blockers.append(f"RSI {rsi:.1f} → {rsi_max:.0f} 이하 필요 (과열)")
    return blockers


# ── 텔레그램 텍스트 조립 ─────────────────────────────────────

def build_telegram_text(prefix: str, time_str: str, pair: str, data: dict) -> str:
    icon = data["trend_icon"]
    lines = [f"[{prefix}] {time_str} | {pair} {icon}추세추종"]

    if data["position"]:
        p = data["position"]
        lines.append(f"¥{data['current_price']:,.0f} → {data['position_summary']}")
        lines.append(f"{data['ema_state']} {data['rsi_state']} {data['volatility_state']}")
        lines.append(f"손절 ¥{p['stop_loss_price']:,.0f} (거리 ¥{p['trailing_stop_distance']:,.0f})")
        currency = pair.split("_")[0].upper()
        lines.append(
            f"보유 {p['entry_amount']}{currency} @ ¥{p['entry_price']:,.0f}"
            f" | 미실현 ¥{p['unrealized_pnl_jpy']:,.0f} ({p['unrealized_pnl_pct']:.2f}%)"
        )
    else:
        lines.append(f"¥{data['current_price']:,.0f} → {data['market_summary']}")
        lines.append(f"{data['ema_state']} {data['rsi_state']} {data['volatility_state']}")
        if data["entry_blockers"]:
            short_parts = []
            for b in data["entry_blockers"]:
                if "→" in b:
                    parts = b.split("→", 1)
                    short_parts.append(f"{parts[0].strip()}→{parts[1].strip()}")
                else:
                    short_parts.append(b)
            lines.append(f"🚫 {' | '.join(short_parts)}")
        else:
            lines.append("✅ 진입 조건 충족")
        lines.append(f"JPY ¥{data['jpy_available']:,.0f} | 대기중")

    return "\n".join(lines)


# ── 메모리 블록 조립 ─────────────────────────────────────────

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


# ── 핵심: 리포트 생성 ────────────────────────────────────────

async def generate_trend_report(
    pair: str,
    prefix: str,
    pair_column: str,
    strategy: Any,
    adapter: Any,
    trend_manager: Any,
    candle_model: Any,
    db: AsyncSession,
) -> dict:
    """
    trend_following 전략의 모니터링 리포트 생성.

    내부에서 캔들 조회 → compute_trend_signal → 포지션/잔고 조합 → 텍스트 조립.
    """
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

    # ATR %
    atr_pct = (atr / current_price * 100) if (atr and current_price > 0) else None

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
    ) if not position_data else []

    entry_conditions_met = len(entry_blockers) == 0 and not position_data

    market_summary = get_market_summary(ema_slope_pct, rsi, signal) if not position_data else None

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
        "entry_blockers": entry_blockers,
        "jpy_available": jpy_available,
        "coin_available": coin_available,
        "ema20": ema,
        "strategy_name": strategy.name,
        "strategy_id": strategy.id,
    }

    telegram_text = build_telegram_text(prefix.upper(), time_str, pair, report_data)
    memory_block = build_memory_block(prefix.upper(), time_str, pair, report_data)

    # 최신 캔들 open_time
    candle_open_time = candles[-1].open_time.isoformat() if candles else None

    return {
        "success": True,
        "generated_at": now_jst.isoformat(),
        "report": {
            "telegram_text": telegram_text,
            "memory_block": memory_block,
        },
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
            "candle_open_time": candle_open_time,
        },
    }
