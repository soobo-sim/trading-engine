"""
전략 Score 함수 — P-1 동적 전략 스위칭 시스템.

Score = 0.4 × readiness + 0.35 × edge + 0.25 × regime_fit

- readiness: 진입 임박도 (0~1)
- edge: 기대수익 추정 (0~1)
- regime_fit: 체제 적합도 (0~1)

순수 함수 모음. DB·어댑터 의존성 없음.

참조: solution-design/DYNAMIC_STRATEGY_SWITCHING.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class StrategyScore:
    """전략 평가 점수 컨테이너."""
    score: float       # 최종: 0.4×readiness + 0.35×edge + 0.25×regime_fit
    readiness: float   # 진입 임박도 0~1
    edge: float        # 기대수익 0~1
    regime_fit: float  # 체제 적합도 0~1
    regime: str        # "trending" | "ranging" | "unclear"
    confidence: str    # "high" | "medium" | "low" | "none"
    detail: Dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────────────────────

_SCORE_NORMALIZE_EDGE_PCT = 0.5  # edge=1.0 기준 기대수익 (%)


def calculate_total_score(readiness: float, edge: float, regime_fit: float) -> float:
    """최종 Score 계산 (가중 평균: 0.4×readiness + 0.35×edge + 0.25×regime_fit)."""
    return round(0.4 * readiness + 0.35 * edge + 0.25 * regime_fit, 4)


def _determine_confidence(paper_trades: int, wf_passed: bool = False) -> str:
    """
    Paper 거래 수 + WF 통과 여부 기반 신뢰도 등급.

    paper_trades >= 20 + wf_passed → "high"
    paper_trades >= 10            → "medium"
    paper_trades >= 1             → "low"
    paper_trades == 0             → "none"
    """
    if paper_trades >= 20 and wf_passed:
        return "high"
    if paper_trades >= 10:
        return "medium"
    if paper_trades >= 1:
        return "low"
    return "none"


# ──────────────────────────────────────────────────────────────
# 요소별 계산 함수 (재사용 가능 순수 함수)
# ──────────────────────────────────────────────────────────────

def calculate_box_readiness(
    current_price: float,
    upper: float,
    lower: float,
    near_bound_pct: float,
) -> float:
    """
    박스역추세 readiness: 가격이 경계(상/하단)에 얼마나 가까운지.

    near_bound_pct = 0.3 → 상/하단 ±0.3% 이내면 readiness=1.0
    중앙에 있으면 0에 수렴.

    Returns: 0.0 ~ 1.0
    """
    if upper <= lower or lower <= 0 or upper <= 0:
        return 0.0
    near_pct = near_bound_pct / 100.0
    if near_pct <= 0:
        return 0.0
    distance_to_lower = abs(current_price - lower) / lower
    distance_to_upper = abs(current_price - upper) / upper
    distance_to_bound = min(distance_to_lower, distance_to_upper)
    readiness = max(0.0, 1.0 - distance_to_bound / near_pct)
    return round(min(1.0, readiness), 4)


def calculate_box_edge(
    box_width_pct: float,
    commission_rate: float,
    win_rate: float,
) -> float:
    """
    박스역추세 edge: 박스폭 × (1 - 수수료×2) × 승률 → 정규화.

    0.5% 기대수익 = edge 1.0 (NORMALIZE 기준).

    Returns: 0.0 ~ 1.0
    """
    net_pct = box_width_pct * (1.0 - commission_rate * 2.0) * win_rate
    return round(min(1.0, max(0.0, net_pct / _SCORE_NORMALIZE_EDGE_PCT)), 4)


def calculate_trend_readiness(
    signal: str,
    rsi: Optional[float],
    entry_rsi_min: float,
    entry_rsi_max: float,
) -> float:
    """
    추세추종 readiness: 진입 시그널 상태 + RSI 위치.

    entry_ok / entry_sell → 1.0
    wait_dip              → 0.5 × (1 - RSI 오버슈팅 정도)
    exit_warning          → 0.0
    기타                  → 0.2

    Returns: 0.0 ~ 1.0
    """
    if signal in ("entry_ok", "entry_sell"):
        return 1.0
    if signal == "exit_warning":
        return 0.0
    if signal == "wait_dip":
        if rsi is None:
            return 0.5  # RSI 불명 → 중립 폴백
        rsi_range = entry_rsi_max - entry_rsi_min
        if rsi_range > 0:
            readiness = 0.5 * (1.0 - (rsi - entry_rsi_min) / rsi_range)
            return round(max(0.0, min(1.0, readiness)), 4)
        return 0.5
    return 0.2


def calculate_trend_edge(
    atr_pct: float,
    trailing_multiplier: float,
    win_rate: float,
) -> float:
    """
    추세추종 edge: ATR × 트레일링 배수 × 승률 → 정규화.

    Returns: 0.0 ~ 1.0
    """
    net_pct = atr_pct * trailing_multiplier * win_rate
    return round(min(1.0, max(0.0, net_pct / _SCORE_NORMALIZE_EDGE_PCT)), 4)


def calculate_regime_fit(regime: str, strategy_type: str) -> float:
    """
    체제 적합도 (0~1).

    박스역추세: ranging=1.0, unclear=0.5, trending=0.1
    추세추종:   trending=1.0, unclear=0.5, ranging=0.1
    """
    if strategy_type == "box_mean_reversion":
        return {"ranging": 1.0, "unclear": 0.5, "trending": 0.1}.get(regime, 0.5)
    if strategy_type in ("trend_following", "cfd_trend_following"):
        return {"trending": 1.0, "unclear": 0.5, "ranging": 0.1}.get(regime, 0.5)
    return 0.5


# ──────────────────────────────────────────────────────────────
# 통합 Score 계산 API
# ──────────────────────────────────────────────────────────────

def calculate_box_score(
    current_price: float,
    upper: float,
    lower: float,
    near_bound_pct: float,
    box_width_pct: float,
    regime: str,
    commission_rate: float = 0.001,
    win_rate: float = 0.5,
    paper_trades: int = 0,
    wf_passed: bool = False,
    extra_detail: Optional[Dict[str, Any]] = None,
) -> StrategyScore:
    """
    박스역추세 전략 전체 Score 계산.

    Args:
        current_price: 현재가
        upper: 박스 상단
        lower: 박스 하단
        near_bound_pct: 경계 밴드 (%, 기본 0.3)
        box_width_pct: 박스 폭 (%)
        regime: "ranging" | "trending" | "unclear"
        commission_rate: 편도 수수료율 (기본 0.1%)
        win_rate: 기대 승률 (기본 0.5)
        paper_trades: Paper 거래 수 (신뢰도 계산용)
        wf_passed: Walk-forward 통과 여부
        extra_detail: 추가 상세 정보
    """
    readiness = calculate_box_readiness(current_price, upper, lower, near_bound_pct)
    edge = calculate_box_edge(box_width_pct, commission_rate, win_rate)
    regime_fit = calculate_regime_fit(regime, "box_mean_reversion")
    score = calculate_total_score(readiness, edge, regime_fit)
    confidence = _determine_confidence(paper_trades, wf_passed)

    detail: Dict[str, Any] = {
        "upper": upper,
        "lower": lower,
        "box_width_pct": box_width_pct,
        "near_bound_pct": near_bound_pct,
        "win_rate": win_rate,
        "commission_rate": commission_rate,
    }
    if extra_detail:
        detail.update(extra_detail)

    return StrategyScore(
        score=score,
        readiness=readiness,
        edge=edge,
        regime_fit=regime_fit,
        regime=regime,
        confidence=confidence,
        detail=detail,
    )


def calculate_trend_score(
    signal: str,
    rsi: Optional[float],
    entry_rsi_min: float,
    entry_rsi_max: float,
    atr_pct: float,
    trailing_multiplier: float,
    regime: str,
    win_rate: float = 0.34,
    paper_trades: int = 0,
    wf_passed: bool = False,
    extra_detail: Optional[Dict[str, Any]] = None,
) -> StrategyScore:
    """
    추세추종 전략 전체 Score 계산.

    Args:
        signal: "entry_ok" | "entry_sell" | "wait_dip" | "exit_warning" | "no_signal" 등
        rsi: 현재 RSI (wait_dip 시 사용)
        entry_rsi_min: 진입 허용 RSI 하한
        entry_rsi_max: 진입 허용 RSI 상한
        atr_pct: ATR (현재가 대비 %)
        trailing_multiplier: 트레일링 스탑 ATR 배수
        regime: "ranging" | "trending" | "unclear"
        win_rate: 기대 승률 (기본 34%, 4H WF 결과)
        paper_trades: Paper 거래 수
        wf_passed: Walk-forward 통과 여부
        extra_detail: 추가 상세 정보
    """
    readiness = calculate_trend_readiness(signal, rsi, entry_rsi_min, entry_rsi_max)
    edge = calculate_trend_edge(atr_pct, trailing_multiplier, win_rate)
    regime_fit = calculate_regime_fit(regime, "trend_following")
    score = calculate_total_score(readiness, edge, regime_fit)
    confidence = _determine_confidence(paper_trades, wf_passed)

    detail: Dict[str, Any] = {
        "signal": signal,
        "rsi": rsi,
        "atr_pct": atr_pct,
        "trailing_multiplier": trailing_multiplier,
        "win_rate": win_rate,
    }
    if extra_detail:
        detail.update(extra_detail)

    return StrategyScore(
        score=score,
        readiness=readiness,
        edge=edge,
        regime_fit=regime_fit,
        regime=regime,
        confidence=confidence,
        detail=detail,
    )
