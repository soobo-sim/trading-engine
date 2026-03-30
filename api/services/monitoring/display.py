"""표시용 헬퍼 — 아이콘·상태·요약·차단조건."""
from __future__ import annotations

from datetime import timedelta, timezone
from typing import List, Optional

JST = timezone(timedelta(hours=9))


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
    slope_min: float = 0.0,
) -> List[str]:
    """진입까지 남은 조건 목록. 비어있으면 진입 가능."""
    blockers: List[str] = []
    if ema_slope_pct is not None and ema_slope_pct < slope_min:
        blockers.append(f"EMA slope {ema_slope_pct:+.2f}% → ≥{slope_min:+.2f}% 필요")
    if ema is not None and current_price < ema:
        gap_pct = (ema - current_price) / ema * 100
        blockers.append(f"가격 < EMA20 (¥{current_price:,.0f} vs ¥{ema:,.0f}, 갭 {gap_pct:.1f}%)")
    if rsi is not None and rsi < rsi_min:
        blockers.append(f"RSI {rsi:.1f} → {rsi_min:.0f} 이상 필요 (breakdown)")
    if rsi is not None and rsi > rsi_max:
        blockers.append(f"RSI {rsi:.1f} → {rsi_max:.0f} 이하 필요 (과열)")
    if signal == "wait_regime":
        blockers.append("횡보 레짐 (BB폭 협소) → 추세 형성 대기")
    return blockers
