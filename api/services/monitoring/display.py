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
    if signal == "long_caution":
        return "🔻 하락 전환·전략 유효성 점검"
    if signal == "short_caution":
        return "🔺 상승 전환·숏 전략 유효성 점검"
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


# ── 서사형(Narrative) 헬퍼 ──────────────────────────────


def get_narrative_situation(
    has_position: bool,
    signal: str,
    ema_slope_pct: Optional[float],
    rsi: Optional[float],
    current_price: float,
    ema: Optional[float],
    unrealized_pnl_pct: Optional[float] = None,
    exit_signal: Optional[dict] = None,
) -> str:
    """📊 지금: 추세추종 한줄 서사 요약."""
    if has_position:
        action = (exit_signal or {}).get("action", "hold")
        if action == "full_exit":
            return "🚨 청산 시그널 발생"
        if action == "tighten_stop":
            return "추세 약화 감지, 스탑 조임 중"
        pnl = unrealized_pnl_pct or 0.0
        if pnl > 2.0:
            return "상승추세 유지, 수익 확대 중"
        if pnl > 0:
            return "상승추세 유지 중"
        return "추세 유지, 소폭 손실 관찰"
    else:
        if ema_slope_pct is None or rsi is None or ema is None:
            return "데이터 부족"
        if current_price < ema and ema_slope_pct < 0:
            return "가격이 EMA 아래, 하락 추세"
        if current_price > ema and ema_slope_pct > 0.1 and 40 <= rsi <= 65:
            return "상승추세, 진입 조건 접근 중"
        if current_price > ema and ema_slope_pct > 0:
            return "EMA 위 상승추세, RSI 조건 대기"
        if -0.1 < ema_slope_pct <= 0:
            return "추세 약화 구간, 관망 중"
        if ema_slope_pct <= -0.1 and rsi < 30:
            return "급락 구간, 반등 대기"
        if ema_slope_pct <= -0.1:
            return "하락 추세, 전략 관망"
        return "관망"


def get_narrative_outlook(
    has_position: bool,
    exit_signal: Optional[dict],
    rsi: Optional[float],
    unrealized_pnl_pct: Optional[float],
) -> Optional[str]:
    """⚡ 전망: 포지션 보유 시만 반환. None이면 미표시."""
    if not has_position:
        return None
    action = (exit_signal or {}).get("action", "hold")
    if action == "full_exit":
        return "즉시 청산 실행 중"
    if action == "tighten_stop":
        return "추세 약화 — 스탑 조임 중. 추가 하락 시 자동 청산"
    pnl = unrealized_pnl_pct or 0.0
    if pnl > 5.0:
        return "큰 수익 구간 — 트레일링 스탑이 수익 보호 중"
    if pnl < -1.0:
        return "손절선 접근 중 — 반등 없으면 자동 청산"
    rsi_note = ""
    if rsi is not None and rsi > 65:
        rsi_note = " RSI 과열 시 스탑 조임."
    return f"추세 이어지면 트레일링 스탑 자동 상향.{rsi_note}"


def get_box_narrative_situation(
    has_position: bool,
    position_label: str,
    has_box: bool,
    side: str = "buy",
    unrealized_pnl_pct: Optional[float] = None,
) -> str:
    """📊 지금: 박스전략 한줄 서사 요약."""
    if not has_box:
        return "박스 미형성, 패턴 형성 대기"
    if has_position:
        pnl = unrealized_pnl_pct or 0.0
        if side == "buy":
            if position_label == "near_upper":
                return "상단 접근 중, 익절 구간 임박"
            if position_label == "near_lower":
                return "하단 접근, 손절선 주의"
            if position_label == "outside":
                return "박스 이탈, 청산 검토 중"
            if pnl > 1.0:
                return "박스 중심부, 수익 확대 중"
            return "박스 중심부, 익절까지 여유 있음"
        else:
            if position_label == "near_lower":
                return "익절 구간 임박 (하단 도달)"
            if position_label == "near_upper":
                return "손절선(상단) 주의"
            if position_label == "outside":
                return "박스 이탈, 청산 검토 중"
            return "박스 중심부, 익절까지 여유 있음"
    else:
        if position_label == "near_lower":
            return "하단 진입대, 진입 조건 충족"
        if position_label == "near_upper":
            return "상단 근처, 진입 지점 아님"
        if position_label == "outside":
            return "박스 밖, 박스 재형성 대기"
        return "가격이 박스 중심에 있음"


def get_wait_direction(
    supports_short: bool,
    signal: str,
    current_price: float,
    ema: Optional[float],
    ema_slope_pct: Optional[float],
) -> str:
    """CFD 대기 시 진입 방향. 'long' / 'short' / 'neutral'."""
    if not supports_short:
        return "long"
    if ema is None or ema_slope_pct is None:
        return "neutral"
    # long_overheated/wait_regime는 이미 롱 조건 부분 충족, short_caution/short_oversold는 숏 방향
    if signal in ("long_overheated", "wait_regime", "long_setup"):
        return "long"
    if signal in ("short_caution", "short_oversold", "short_setup"):
        return "short"
    if current_price < ema and ema_slope_pct < 0:
        return "short"
    if current_price > ema and ema_slope_pct > 0:
        return "long"
    return "neutral"


def get_entry_blockers_short(
    signal: str,
    current_price: float,
    ema: Optional[float],
    ema_slope_pct: Optional[float],
    rsi: Optional[float],
    rsi_min: float = 35.0,
    rsi_max: float = 60.0,
    slope_threshold: float = -0.05,
) -> List[str]:
    """숏 진입까지 남은 조건 목록. 비어있으면 진입 가능."""
    blockers: List[str] = []
    if ema_slope_pct is not None and ema_slope_pct >= slope_threshold:
        blockers.append(f"EMA slope {ema_slope_pct:+.2f}% → ≤{slope_threshold:+.2f}% 필요")
    if ema is not None and current_price >= ema:
        gap_pct = (current_price - ema) / ema * 100
        blockers.append(f"가격 > EMA20 (¥{current_price:,.0f} vs ¥{ema:,.0f}, 갭 {gap_pct:.1f}%)")
    if rsi is not None and rsi < rsi_min:
        blockers.append(f"RSI {rsi:.1f} → {rsi_min:.0f} 이상 필요 (과매도)")
    if rsi is not None and rsi > rsi_max:
        blockers.append(f"RSI {rsi:.1f} → {rsi_max:.0f} 이하 필요")
    if signal == "wait_regime":
        blockers.append("횡보 레짐 (BB폭 협소) → 추세 형성 대기")
    return blockers


def get_entry_condition_lines_long(
    signal: str,
    current_price: float,
    ema: Optional[float],
    ema_slope_pct: Optional[float],
    rsi: Optional[float],
    rsi_min: float = 40.0,
    rsi_max: float = 65.0,
    slope_min: float = 0.0,
    regime_consecutive: int = 0,
    regime_active: bool = True,
) -> List[str]:
    """롱 진입 4개 조건 전체 상태 라인 (✅/❌). 전체 충족 여부와 무관하게 항상 4줄 반환."""
    lines = []

    # ① 가격 > EMA
    if ema is not None:
        if current_price > ema:
            lines.append(f" ✅ ① 가격 > EMA    ¥{current_price:,.0f} (EMA ¥{ema:,.0f})")
        else:
            gap_jpy = ema - current_price
            lines.append(f" ❌ ① 가격 > EMA    지금 ¥{current_price:,.0f} < EMA ¥{ema:,.0f} (↑ ¥{gap_jpy:,.0f} 필요)")

    # ② EMA 기울기 ≥ slope_min
    if ema_slope_pct is not None:
        if ema_slope_pct >= slope_min:
            lines.append(f" ✅ ② EMA 기울기    {ema_slope_pct:+.2f}%  (≥{slope_min:+.2f}% 충족)")
        else:
            diff = abs(slope_min - ema_slope_pct)
            lines.append(
                f" ❌ ② EMA 기울기    지금 {ema_slope_pct:+.2f}% → {slope_min:+.2f}% 이상 필요 ({diff:.2f}%p 부족)"
            )

    # ③ RSI 범위
    if rsi is not None:
        if rsi_min <= rsi <= rsi_max:
            lines.append(f" ✅ ③ RSI 범위      {rsi:.1f}  (허용 {rsi_min:.0f}~{rsi_max:.0f})")
        elif rsi < rsi_min:
            diff = rsi_min - rsi
            lines.append(f" ❌ ③ RSI 범위      지금 {rsi:.1f} → {rsi_min:.0f} 이상 필요 ({diff:.1f} 부족)")
        else:
            diff = rsi - rsi_max
            lines.append(f" ❌ ③ RSI 범위      지금 {rsi:.1f} → {rsi_max:.0f} 이하 필요 ({diff:.1f} 초과, 과열)")

    # ④ 추세장 (regime)
    regime_met = regime_active and signal != "wait_regime"
    if regime_met:
        lines.append(f" ✅ ④ 추세장        ×{regime_consecutive} 연속")
    elif signal == "wait_regime":
        lines.append(" ❌ ④ 추세장        횡보 감지 (BB폭 협소) → 추세 형성 대기")
    else:
        lines.append(f" ❌ ④ 추세장        RegimeGate 차단 중 (×{regime_consecutive} / 3 미달)")

    return lines


def get_entry_condition_lines_short(
    signal: str,
    current_price: float,
    ema: Optional[float],
    ema_slope_pct: Optional[float],
    rsi: Optional[float],
    rsi_min: float = 35.0,
    rsi_max: float = 60.0,
    slope_threshold: float = -0.05,
    regime_consecutive: int = 0,
    regime_active: bool = True,
) -> List[str]:
    """숏 진입 4개 조건 전체 상태 라인 (✅/❌). 전체 충족 여부와 무관하게 항상 4줄 반환."""
    lines = []

    # ① 가격 < EMA
    if ema is not None:
        if current_price < ema:
            lines.append(f" ✅ ① 가격 < EMA    ¥{current_price:,.0f} (EMA ¥{ema:,.0f})")
        else:
            gap_jpy = current_price - ema
            lines.append(f" ❌ ① 가격 < EMA    지금 ¥{current_price:,.0f} > EMA ¥{ema:,.0f} (↓ ¥{gap_jpy:,.0f} 필요)")

    # ② EMA 기울기 < slope_threshold (e.g., < -0.05%)
    if ema_slope_pct is not None:
        if ema_slope_pct < slope_threshold:
            lines.append(f" ✅ ② EMA 기울기    {ema_slope_pct:+.2f}%  (≤{slope_threshold:+.2f}% 충족)")
        else:
            diff = abs(ema_slope_pct - slope_threshold)
            lines.append(
                f" ❌ ② EMA 기울기    지금 {ema_slope_pct:+.2f}% → {slope_threshold:+.2f}% 미만 필요 ({diff:.2f}%p 부족)"
            )

    # ③ RSI 범위
    if rsi is not None:
        if rsi_min <= rsi <= rsi_max:
            lines.append(f" ✅ ③ RSI 범위      {rsi:.1f}  (허용 {rsi_min:.0f}~{rsi_max:.0f})")
        elif rsi < rsi_min:
            diff = rsi_min - rsi
            lines.append(f" ❌ ③ RSI 범위      지금 {rsi:.1f} → {rsi_min:.0f} 이상 필요 (과매도, {diff:.1f} 부족)")
        else:
            diff = rsi - rsi_max
            lines.append(f" ❌ ③ RSI 범위      지금 {rsi:.1f} → {rsi_max:.0f} 이하 필요 ({diff:.1f} 초과)")

    # ④ 추세장 (regime)
    regime_met = regime_active and signal != "wait_regime"
    if regime_met:
        lines.append(f" ✅ ④ 추세장        ×{regime_consecutive} 연속")
    elif signal == "wait_regime":
        lines.append(" ❌ ④ 추세장        횡보 감지 (BB폭 협소) → 추세 형성 대기")
    else:
        lines.append(f" ❌ ④ 추세장        RegimeGate 차단 중 (×{regime_consecutive} / 3 미달)")

    return lines


def get_box_narrative_outlook(
    has_position: bool,
    position_label: str,
    side: str = "buy",
) -> Optional[str]:
    """⚡ 전망: 박스 포지션 보유 시만 반환."""
    if not has_position:
        return None
    if side == "buy":
        if position_label == "near_upper":
            return "상단 근처 도달 시 자동 익절"
        if position_label == "near_lower":
            return "손절선 접근 중, 하단 이탈 시 자동 청산"
        return "상단(익절대)까지 여유 있음, 추세 관찰 중"
    else:
        if position_label == "near_lower":
            return "하단 근처 도달 시 자동 익절"
        if position_label == "near_upper":
            return "손절선(상단) 접근 중, 이탈 시 자동 청산"
        return "하단(익절대)까지 여유 있음, 추세 관찰 중"
