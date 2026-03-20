"""
모니터링 리포트 서비스 — 사만다 15분 보고용 서버측 텍스트 생성.

서버가 시그널 계산 → 아이콘/상태/요약/entry_blockers → telegram_text + memory_block 조립.
사만다(GPT 4.1)는 단순 출력만 한다.

설계:
  - 추세추종: solution-design/archive/MONITORING_REPORT_API.md
  - 박스: solution-design/MONITORING_REPORT_BOX.md
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.strategy.signals import compute_trend_signal

logger = logging.getLogger(__name__)

RACHEL_WEBHOOK_URL = os.getenv("RACHEL_WEBHOOK_URL", "http://localhost:18793/hooks/market-alert")
RACHEL_WEBHOOK_TOKEN = os.getenv("RACHEL_WEBHOOK_TOKEN", "")
ALERT_COOLDOWN_SEC = 900  # 15분
_last_alert_time: Dict[str, float] = {}


async def _trigger_rachel_analysis(pair: str, alert: dict, test: bool = False) -> None:
    """critical alert 시 레이첼에게 긴급 분석 요청."""
    if not RACHEL_WEBHOOK_TOKEN:
        logger.warning("RACHEL_WEBHOOK_TOKEN 미설정 — webhook 스킵")
        return

    now = time.time()
    last = _last_alert_time.get(pair, 0)
    if now - last < ALERT_COOLDOWN_SEC:
        logger.info(f"레이첼 webhook 쿨다운 중: {pair} ({int(now - last)}s ago)")
        return

    _last_alert_time[pair] = now

    if test:
        action_instruction = (
            "⚠️ 테스트 모드: 전략 전환은 실행하지 말 것. "
            "전환이 필요하다고 판단하면 '이렇게 전환했을 것이다'를 "
            "Telegram으로 [Rachel 긴급 테스트] prefix로 보고하라."
        )
    else:
        action_instruction = (
            "전략 전환 필요 여부를 반드시 판단하고, "
            "전환이 필요하면 수보오빠 승인 없이 즉시 실행하라 (긴급 자율 전환 권한). "
            "결과를 Telegram으로 [Rachel 긴급] prefix로 보고하라."
        )

    message = (
        f"긴급 분석 요청 (자동 트리거)\n"
        f"페어: {pair}\n"
        f"트리거: {', '.join(alert['triggers'])}\n"
        f"상세: {alert['text']}\n\n"
        f"즉시 WORKFLOW_AUTO.md 단기 분석 STEP 1~7을 실행하라. "
        f"{action_instruction}"
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                RACHEL_WEBHOOK_URL,
                headers={
                    "Authorization": f"Bearer {RACHEL_WEBHOOK_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "message": message,
                    "name": "MarketAlert",
                    "deliver": True,
                    "channel": "telegram",
                    "timeoutSeconds": 480,
                },
            )
            if resp.status_code == 200:
                logger.info(f"레이첼 긴급 분석 트리거 성공: {pair}")
            else:
                logger.error(f"레이첼 webhook 실패: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"레이첼 webhook 오류: {e}")

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
    test_alert_level: str | None = None,
    reset_cooldown: bool = False,
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

    # 4H 변동률 (마지막 완성 캔들 종가 → 현재가)
    last_candle_close = float(candles[-1].close) if candles else None
    candle_change_pct = (
        (current_price - last_candle_close) / last_candle_close * 100
        if last_candle_close and last_candle_close > 0 else 0.0
    )

    # 1H 변動률 (최신 완성 1H 캔들 종가 → 현재가)
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

    # Critical alert → 레이철 긴급 분석 트리거
    if alert and alert["level"] == "critical":
        is_test = test_alert_level is not None
        await _trigger_rachel_analysis(pair, alert, test=is_test)

    return result_dict


# ══════════════════════════════════════════════════════════════
#  알림(Alert) 평가
# ══════════════════════════════════════════════════════════════

# 이전 사이클 데이터 캐시 (서버 메모리, 재시작 시 리셋 — 허용)
_prev_raw_cache: Dict[str, dict] = {}


def _build_test_alert(raw: dict, level: str) -> dict:
    """테스트용 alert 생성 — 실제 수치 기반으로 만들되 level만 override."""
    pair = raw.get("pair", "unknown")
    price = raw.get("current_price", 0)
    rsi = raw.get("rsi14")

    prefix = "CK" if pair.islower() else "BF"

    if level == "critical":
        return {
            "level": "critical",
            "triggers": ["test_forced_critical"],
            "text": (
                f"\U0001f6a8\U0001f6a8\U0001f6a8 [{prefix} 긴급 테스트] {pair}\n"
                f"\xa5{price:,.0f}"
                + (f" | RSI {rsi:.1f}" if rsi is not None else "")
                + "\n\u26a0\ufe0f 테스트 강제 트리거"
            ),
        }
    else:  # warning
        return {
            "level": "warning",
            "triggers": ["test_forced_warning"],
            "text": (
                f"\u26a0\ufe0f [{prefix} 주의 테스트] {pair}"
                + (f" \u2014 RSI {rsi:.1f}" if rsi is not None else "")
                + " (테스트 강제)"
            ),
        }


def evaluate_alert(raw: dict, prev_raw: Optional[dict] = None) -> Optional[dict]:
    """급변 시장 상황 평가. alert가 없으면 None 반환."""
    triggers: List[Tuple[str, str, str]] = []

    rsi = raw.get("rsi14")
    current_price = raw.get("current_price", 0)
    ema = raw.get("ema20")

    # 15분 변동률 (직전 보고 대비)
    if prev_raw:
        prev_price = prev_raw.get("current_price", current_price)
        change_15m_pct = (
            (current_price - prev_price) / prev_price * 100
            if prev_price and prev_price > 0 else 0.0
        )
    else:
        change_15m_pct = 0.0

    # 1H 변동률 (현재 1H 캔들 시가 대비)
    candle_1h_change_pct = raw.get("candle_1h_change_pct", 0)

    # --- Critical triggers ---
    if rsi is not None:
        if rsi < 20:
            triggers.append(("critical", "rsi_extreme_low", f"RSI {rsi:.1f} 극단 과매도"))
        elif rsi > 85:
            triggers.append(("critical", "rsi_extreme_high", f"RSI {rsi:.1f} 극단 과열"))

    # 15분 급변 (직전 보고 대비)
    if change_15m_pct < -3:
        triggers.append(("critical", "price_crash_15m", f"15분 {change_15m_pct:.1f}% 초급락"))
    elif change_15m_pct > 3:
        triggers.append(("critical", "price_surge_15m", f"15분 +{change_15m_pct:.1f}% 초급등"))

    # 1H 급변
    if candle_1h_change_pct < -5:
        triggers.append(("critical", "price_crash_1h", f"1H {candle_1h_change_pct:.1f}% 급락"))
    elif candle_1h_change_pct > 5:
        triggers.append(("critical", "price_surge_1h", f"1H +{candle_1h_change_pct:.1f}% 급등"))

    # 포지션 위험
    pos = raw.get("position")
    if pos and pos.get("unrealized_pnl_pct", 0) < -3:
        triggers.append(("critical", "position_at_risk",
            f"보유 포지션 {pos['unrealized_pnl_pct']:.1f}% 손실"))

    # 체제 전환 (이전 사이클 비교)
    if prev_raw:
        prev_signal = prev_raw.get("signal", "")
        curr_signal = raw.get("signal", "")
        if prev_signal and curr_signal and _is_regime_shift(prev_signal, curr_signal):
            triggers.append(("critical", "regime_shift",
                f"시그널 전환: {prev_signal} → {curr_signal}"))

    # --- Warning triggers ---
    if rsi is not None:
        if rsi < 25 and not any(t[1] == "rsi_extreme_low" for t in triggers):
            triggers.append(("warning", "rsi_low", f"RSI {rsi:.1f} 과매도"))
        elif rsi > 80 and not any(t[1] == "rsi_extreme_high" for t in triggers):
            triggers.append(("warning", "rsi_high", f"RSI {rsi:.1f} 과열"))

    # 15분 변동성 (warning 범위)
    if abs(change_15m_pct) > 1.5 and abs(change_15m_pct) <= 3:
        triggers.append(("warning", "high_volatility_15m", f"15분 {change_15m_pct:+.1f}%"))

    # 1H 변동성 (warning 범위)
    if abs(candle_1h_change_pct) > 3 and abs(candle_1h_change_pct) <= 5:
        triggers.append(("warning", "high_volatility_1h", f"1H {candle_1h_change_pct:+.1f}%"))

    if ema is not None and current_price > 0:
        ema_gap_pct = abs((current_price - ema) / ema * 100)
        if ema_gap_pct > 3:
            triggers.append(("warning", "large_ema_gap", f"EMA 갭 {ema_gap_pct:.1f}%"))

    # slope 부호 전환
    if prev_raw:
        prev_slope = prev_raw.get("ema_slope_pct")
        curr_slope = raw.get("ema_slope_pct")
        if prev_slope is not None and curr_slope is not None:
            if (prev_slope > 0 and curr_slope < 0) or (prev_slope < 0 and curr_slope > 0):
                triggers.append(("warning", "slope_reversal",
                    f"EMA slope 전환 {prev_slope:+.2f}%→{curr_slope:+.2f}%"))

    if not triggers:
        return None

    max_level = "critical" if any(t[0] == "critical" for t in triggers) else "warning"

    return {
        "level": max_level,
        "triggers": [t[1] for t in triggers],
        "text": build_alert_text(raw, triggers, max_level),
    }


def _is_regime_shift(prev: str, curr: str) -> bool:
    """방향이 반대로 바뀌는 경우만."""
    bullish = {"entry_ok", "wait_dip"}
    bearish = {"exit_warning"}
    return (prev in bullish and curr in bearish) or \
           (prev in bearish and curr in bullish)


def build_alert_text(raw: dict, triggers: List[Tuple[str, str, str]], level: str) -> str:
    """알림 텍스트 생성."""
    pair = raw.get("pair", "unknown")
    price = raw.get("current_price", 0)

    prefix = "CK" if pair.islower() else "BF"
    trigger_details = " | ".join(t[2] for t in triggers)

    if level == "critical":
        return (
            f"🚨🚨🚨 [{prefix} 긴급] {pair}\n"
            f"¥{price:,.0f}\n"
            f"{trigger_details}\n"
            f"\n"
            f"⚡ 레이첼 심층분석 권장\n"
            f'→ "레이첼 심층분석" 전송'
        )
    else:  # warning
        return f"⚠️ [{prefix} 주의] {pair} — {trigger_details}"


# ══════════════════════════════════════════════════════════════
#  박스 전략 리포트
# ══════════════════════════════════════════════════════════════


def build_bar_chart(price: float, lower: float, upper: float) -> str:
    """10칸 바 차트로 현재가의 박스 내 위치 시각화."""
    if price < lower:
        return "●[━━━━━━━━━━]"
    if price > upper:
        return "[━━━━━━━━━━]●"
    bar_pos = round((price - lower) / (upper - lower) * 10)
    bar_pos = max(0, min(10, bar_pos))
    return "[" + "━" * bar_pos + "●" + "━" * (10 - bar_pos) + "]"


def build_health_line(health_report: Any) -> str:
    """HealthReport → 한줄 상태 요약."""
    ws = "✅" if health_report.ws_connected else "🔴"

    tasks = health_report.tasks
    alive = sum(1 for t in tasks.values() if t.get("alive"))
    total = len(tasks)
    restarts = sum(t.get("restarts", 0) for t in tasks.values())
    task_icon = "✅" if alive == total else "⚠️"
    restart_text = f"(재시작{restarts})" if restarts > 0 else ""

    balance_issues = health_report.position_balance
    balance_icon = "✅" if not balance_issues else "⚠️"

    prefix = "🟢" if health_report.healthy and not balance_issues else "🚨"

    return f"{prefix} WS{ws} 태스크{alive}/{total}{task_icon}{restart_text} 잔고{balance_icon}"


def get_box_position_label(price: float, lower: float, upper: float, tolerance_pct: float) -> str:
    """현재가의 박스 내 위치 라벨."""
    tol = tolerance_pct / 100.0
    box_range = upper - lower
    if box_range <= 0:
        return "middle"

    if price < lower * (1 - tol) or price > upper * (1 + tol):
        return "outside"
    if abs(price - lower) <= box_range * 0.2:
        return "near_lower"
    if abs(price - upper) <= box_range * 0.2:
        return "near_upper"
    return "middle"


# ── 박스 텔레그램 텍스트 ──────────────────────────────────────

def build_box_telegram_text(prefix: str, time_str: str, pair: str, data: dict) -> str:
    lines = [f"[{prefix}] {time_str} | {pair} 📦박스"]
    lines.append(data["health_line"])

    box = data.get("box")
    if box:
        lines.append(f"¥{data['current_price']:,.2f} {data['position_label']} (폭 {box['box_width_pct']:.1f}%)")
        lines.append(f"하단¥{box['lower_bound']:,.2f} {box['bar_chart']} 상단¥{box['upper_bound']:,.2f}")
    else:
        lines.append(f"¥{data['current_price']:,.2f} 📭박스 미형성")

    lines.append(f"{data['basis_timeframe']}봉 시작: {data['candle_open_time_jst']} JST")

    currency = pair.split("_")[0]
    pos = data.get("position")
    if pos:
        lines.append(
            f"JPY ¥{data['jpy_available']:,.0f} {currency} {data['coin_available']:.2f}개 | "
            f"보유 {pos['entry_amount']}{currency} @ ¥{pos['entry_price']:,.2f} | "
            f"미실현 ¥{pos['unrealized_pnl_jpy']:,.0f} ({pos['unrealized_pnl_pct']:.2f}%)"
        )
    else:
        lines.append(f"JPY ¥{data['jpy_available']:,.0f} {currency} {data['coin_available']:.2f}개 | 포지션 미보유")

    return "\n".join(lines)


# ── 박스 메모리 블록 ──────────────────────────────────────────

def build_box_memory_block(prefix: str, time_str: str, pair: str, data: dict) -> str:
    strategy_name = data.get("strategy_name", "unknown")
    strategy_id = data.get("strategy_id", "?")
    lines = [
        f"## [{time_str} JST] {prefix}: {pair} | 모니터링 | strategy: {strategy_name}(id={strategy_id})",
        "",
        "### 시스템 상태",
        f"- {data['health_line']}",
        "",
        "### 박스 상태",
    ]

    box = data.get("box")
    if box:
        lines.append(
            f"- box_id: {box['id']} | upper: ¥{box['upper_bound']:,.2f}"
            f" | lower: ¥{box['lower_bound']:,.2f} | width: {box['box_width_pct']:.1f}%"
        )
        lines.append(f"- 현재가: ¥{data['current_price']:,.2f} | 위치: {data['position_label']}")
    else:
        lines.append(f"- 박스 미형성 | 현재가: ¥{data['current_price']:,.2f}")

    lines.extend(["", "### 포지션 상태"])
    pos = data.get("position")
    currency = pair.split("_")[0]
    if pos:
        lines.append(
            f"- 보유: {currency} {pos['entry_amount']}개"
            f" @ ¥{pos['entry_price']:,.2f}"
            f" | 미실현 ¥{pos['unrealized_pnl_jpy']:,.0f} ({pos['unrealized_pnl_pct']:.2f}%)"
        )
    else:
        lines.append("- 포지션 없음")

    lines.extend([
        "",
        "### 자산 현황",
        f"- JPY: ¥{data['jpy_available']:,.0f} | {currency}: {data['coin_available']:.4f}개",
        "",
        "### 특이사항",
        "- 없음",
    ])

    return "\n".join(lines)


# ── 핵심: 박스 리포트 생성 ────────────────────────────────────

async def generate_box_report(
    pair: str,
    prefix: str,
    pair_column: str,
    strategy: Any,
    adapter: Any,
    health_checker: Any,
    box_model: Any,
    box_position_model: Any,
    candle_model: Any,
    db: AsyncSession,
    test_alert_level: str | None = None,
    reset_cooldown: bool = False,
) -> dict:
    """
    box_mean_reversion 전략의 모니터링 리포트 생성.

    내부에서 헬스체크 → 박스 조회 → 포지션/잔고 → 텍스트 조립.
    """
    params = strategy.parameters or {}
    now_jst = datetime.now(JST)
    time_str = now_jst.strftime("%H:%M")
    basis_tf = params.get("basis_timeframe", "4h")

    # 1. 헬스체크
    try:
        health_report = await health_checker.check()
        health_line = build_health_line(health_report)
    except Exception as e:
        logger.warning(f"[BoxReport] 헬스체크 실패: {e}")
        health_line = "⚠️ 헬스 미확인"

    # 2. 현재가 조회
    ticker = await adapter.get_ticker(pair)
    current_price = ticker.last

    # 3. 활성 박스 조회
    pair_col = getattr(box_model, pair_column)
    result = await db.execute(
        select(box_model)
        .where(and_(pair_col == pair, box_model.status == "active"))
        .order_by(desc(box_model.created_at))
        .limit(1)
    )
    box_row = result.scalar_one_or_none()

    box_data = None
    position_label = "no_box"
    if box_row:
        upper = float(box_row.upper_bound)
        lower = float(box_row.lower_bound)
        tolerance_pct = float(box_row.tolerance_pct)
        box_width_pct = (upper - lower) / lower * 100 if lower > 0 else 0.0
        position_label = get_box_position_label(current_price, lower, upper, tolerance_pct)
        bar_chart = build_bar_chart(current_price, lower, upper)

        box_data = {
            "id": box_row.id,
            "upper_bound": upper,
            "lower_bound": lower,
            "box_width_pct": round(box_width_pct, 1),
            "status": box_row.status,
            "bar_chart": bar_chart,
        }

    # 4. 오픈 포지션 조회 (DB)
    pos_pair_col = getattr(box_position_model, pair_column)
    result = await db.execute(
        select(box_position_model)
        .where(and_(pos_pair_col == pair, box_position_model.status == "open"))
        .order_by(desc(box_position_model.created_at))
        .limit(1)
    )
    pos_row = result.scalar_one_or_none()

    position_data = None
    if pos_row and pos_row.entry_price:
        entry_price = float(pos_row.entry_price)
        entry_amount = float(pos_row.entry_amount)
        unrealized_pnl_jpy = (current_price - entry_price) * entry_amount
        unrealized_pnl_pct = (
            (current_price - entry_price) / entry_price * 100
            if entry_price > 0 else 0.0
        )
        position_data = {
            "entry_price": entry_price,
            "entry_amount": entry_amount,
            "current_price": current_price,
            "unrealized_pnl_jpy": round(unrealized_pnl_jpy, 0),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
            "stop_loss_price": float(pos_row.exit_price) if pos_row.exit_price else None,
        }

    # 5. 잔고 조회
    balance = await adapter.get_balance()
    jpy_available = balance.get_available("jpy")
    coin_currency = pair.split("_")[0].lower()
    coin_available = balance.get_available(coin_currency)

    # 6. 최신 캔들 open_time + close (4H 변동률용)
    candle_pair_col = getattr(candle_model, pair_column)
    result = await db.execute(
        select(candle_model)
        .where(
            and_(
                candle_pair_col == pair,
                candle_model.timeframe == basis_tf,
                candle_model.is_complete == True,
            )
        )
        .order_by(candle_model.open_time.desc())
        .limit(1)
    )
    latest_candle = result.scalar_one_or_none()
    latest_open_time = latest_candle.open_time if latest_candle else None
    candle_open_time_jst = (
        latest_open_time.astimezone(JST).strftime("%H:%M")
        if latest_open_time else "불명"
    )

    # 4H 변동률
    last_candle_close = float(latest_candle.close) if latest_candle else None
    candle_change_pct = (
        (current_price - last_candle_close) / last_candle_close * 100
        if last_candle_close and last_candle_close > 0 else 0.0
    )

    # 1H 변동률 (최신 완성 1H 캔들 종가 → 현재가)
    result_1h = await db.execute(
        select(candle_model)
        .where(
            and_(
                candle_pair_col == pair,
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

    # 7. 텍스트 조립
    report_data = {
        "current_price": current_price,
        "health_line": health_line,
        "box": box_data,
        "position_label": position_label,
        "position": position_data,
        "jpy_available": jpy_available,
        "coin_available": coin_available,
        "basis_timeframe": basis_tf,
        "candle_open_time_jst": candle_open_time_jst,
        "strategy_name": strategy.name,
        "strategy_id": strategy.id,
    }

    telegram_text = build_box_telegram_text(prefix.upper(), time_str, pair, report_data)
    memory_block = build_box_memory_block(prefix.upper(), time_str, pair, report_data)

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
            "trading_style": "box_mean_reversion",
            "strategy_name": strategy.name,
            "strategy_id": strategy.id,
            "current_price": round(current_price, 6),
            "health_line": health_line,
            "box": box_data,
            "position_label": position_label,
            "position": position_data,
            "jpy_available": round(jpy_available, 0),
            "coin_available": round(coin_available, 6),
            "candle_change_pct": round(candle_change_pct, 2),
            "candle_1h_change_pct": round(candle_1h_change_pct, 2),
            "candle_open_time": latest_open_time.isoformat() if latest_open_time else None,
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

    # Critical alert → 레이철 긴급 분석 트리거
    if alert and alert["level"] == "critical":
        is_test = test_alert_level is not None
        await _trigger_rachel_analysis(pair, alert, test=is_test)

    return result_dict


# ══════════════════════════════════════════════════════════════
#  CFD 추세추종 리포트
# ══════════════════════════════════════════════════════════════

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
    """
    cfd_trend_following 전략의 모니터링 리포트 생성.

    현물 추세추종 리포트 + CFD 고유 정보 (증거금, keep_rate, side, 보유시간).
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
        await _trigger_rachel_analysis(pair, alert, test=is_test)

    return result_dict
