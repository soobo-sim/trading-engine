"""
전략 Score 함수 단위 테스트 (V-12~V-20).

core/strategy/scoring.py 의 순수 함수 검증.
DB·어댑터 의존성 없음 — 동기 테스트.
"""
from __future__ import annotations

import pytest

from core.strategy.scoring import (
    StrategyScore,
    calculate_box_edge,
    calculate_box_readiness,
    calculate_box_score,
    calculate_regime_fit,
    calculate_total_score,
    calculate_trend_readiness,
    calculate_trend_edge,
    calculate_trend_score,
)


# ══════════════════════════════════════════════
# V-12: 박스 가격 near_lower → readiness ≈ 1.0
# ══════════════════════════════════════════════

def test_v12_box_near_lower_readiness_high():
    """가격이 하단에 딱 붙어 있으면 readiness=1.0."""
    readiness = calculate_box_readiness(
        current_price=159.23,
        upper=159.82,
        lower=159.23,
        near_bound_pct=0.3,
    )
    assert readiness == 1.0


def test_v12_box_slightly_above_lower_readiness_high():
    """가격이 하단近 (0.12%) → readiness 높음."""
    # lower=159.23, near_bound_pct=0.3% → 159.23 * 0.003 = 0.477
    # current = 159.42 (하단에서 0.19 = 0.119%)
    readiness = calculate_box_readiness(
        current_price=159.42,
        upper=159.82,
        lower=159.23,
        near_bound_pct=0.3,
    )
    # distance_to_lower = 0.19/159.23 ≈ 0.0012 = 0.12%
    # readiness = 1 - 0.0012/0.003 = 0.6 이상
    assert readiness > 0.5


# ══════════════════════════════════════════════
# V-13: 박스 중앙 → readiness ≈ 0.0
# ══════════════════════════════════════════════

def test_v13_box_mid_readiness_low():
    """가격이 박스 중앙 → readiness 낮음 (경계에서 멀어 0에 수렴)."""
    mid = (159.82 + 159.23) / 2  # 159.525
    readiness = calculate_box_readiness(
        current_price=mid,
        upper=159.82,
        lower=159.23,
        near_bound_pct=0.3,
    )
    # distance_to_lower = 0.295/159.23 ≈ 0.185%
    # near_pct = 0.3% → readiness = 1 - 0.185/0.3 ≈ 0.38
    # 단, lower 기준 거리 = upper 기준 거리도 비슷하므로 중간값
    assert readiness < 0.5


def test_v13_box_mid_readiness_zero_when_far():
    """near_bound_pct가 매우 좁을 때 중앙은 readiness=0.0."""
    mid = (100.0 + 99.0) / 2  # 99.5
    readiness = calculate_box_readiness(
        current_price=mid,
        upper=100.0,
        lower=99.0,
        near_bound_pct=0.1,  # 너무 좁아 중앙에서 닿지 않음
    )
    assert readiness == 0.0


# ══════════════════════════════════════════════
# V-14: 추세 entry_ok → readiness = 1.0
# ══════════════════════════════════════════════

def test_v14_trend_entry_ok_readiness_one():
    readiness = calculate_trend_readiness(
        signal="entry_ok",
        rsi=45.0,
        entry_rsi_min=30.0,
        entry_rsi_max=60.0,
    )
    assert readiness == 1.0


def test_v14_trend_entry_sell_readiness_one():
    readiness = calculate_trend_readiness(
        signal="entry_sell",
        rsi=55.0,
        entry_rsi_min=35.0,
        entry_rsi_max=60.0,
    )
    assert readiness == 1.0


# ══════════════════════════════════════════════
# V-15: 추세 exit_warning → readiness = 0.0
# ══════════════════════════════════════════════

def test_v15_trend_exit_warning_readiness_zero():
    readiness = calculate_trend_readiness(
        signal="exit_warning",
        rsi=30.0,
        entry_rsi_min=30.0,
        entry_rsi_max=60.0,
    )
    assert readiness == 0.0


# ══════════════════════════════════════════════
# V-16: regime=ranging → 박스 regime_fit 높음, 추세 낮음
# ══════════════════════════════════════════════

def test_v16_ranging_regime_box_fit_high():
    fit = calculate_regime_fit("ranging", "box_mean_reversion")
    assert fit == 1.0


def test_v16_ranging_regime_trend_fit_low():
    fit = calculate_regime_fit("ranging", "trend_following")
    assert fit == 0.1


# ══════════════════════════════════════════════
# V-17: regime=unclear → 모든 전략 regime_fit 중간
# ══════════════════════════════════════════════

def test_v17_unclear_regime_box_fit_mid():
    fit = calculate_regime_fit("unclear", "box_mean_reversion")
    assert fit == 0.5


def test_v17_unclear_regime_trend_fit_mid():
    fit = calculate_regime_fit("unclear", "trend_following")
    assert fit == 0.5


# ══════════════════════════════════════════════
# V-18: edge 수치 정합 테스트
# ══════════════════════════════════════════════

def test_v18_box_edge_calculation():
    """
    박스 width 0.37%, 수수료 0.1%, 승률 60% → edge 계산.

    net_pct = 0.37 × (1 - 0.001 × 2) × 0.6 = 0.37 × 0.998 × 0.6 ≈ 0.2215%
    edge = min(1.0, 0.2215 / 0.5) ≈ 0.4430
    """
    edge = calculate_box_edge(
        box_width_pct=0.37,
        commission_rate=0.001,
        win_rate=0.6,
    )
    assert 0.40 < edge < 0.50


def test_v18_trend_edge_calculation():
    """
    ATR 0.2%, trailing 0.8배, 승률 34% → edge 계산.

    net_pct = 0.2 × 0.8 × 0.34 = 0.0544%
    edge = min(1.0, 0.0544 / 0.5) ≈ 0.1088
    """
    edge = calculate_trend_edge(
        atr_pct=0.2,
        trailing_multiplier=0.8,
        win_rate=0.34,
    )
    assert 0.09 < edge < 0.13


# ══════════════════════════════════════════════
# V-19: Paper 0건 → confidence="none"
# ══════════════════════════════════════════════

def test_v19_no_paper_trades_confidence_none():
    result = calculate_box_score(
        current_price=159.23,
        upper=159.82,
        lower=159.23,
        near_bound_pct=0.3,
        box_width_pct=0.37,
        regime="ranging",
        paper_trades=0,
    )
    assert result.confidence == "none"
    assert isinstance(result.detail, dict)


def test_v19_no_paper_trades_detail_has_win_rate():
    """paper=0 → 폴백 win_rate가 detail에 포함됨."""
    result = calculate_box_score(
        current_price=159.23,
        upper=159.82,
        lower=159.23,
        near_bound_pct=0.3,
        box_width_pct=0.37,
        regime="ranging",
        paper_trades=0,
        win_rate=0.5,
    )
    assert "win_rate" in result.detail
    assert result.detail["win_rate"] == 0.5


# ══════════════════════════════════════════════
# V-20: Paper 20건+ + WF 통과 → confidence="high"
# ══════════════════════════════════════════════

def test_v20_paper_20_wf_passed_confidence_high():
    result = calculate_trend_score(
        signal="entry_ok",
        rsi=45.0,
        entry_rsi_min=30.0,
        entry_rsi_max=60.0,
        atr_pct=0.2,
        trailing_multiplier=0.8,
        regime="trending",
        paper_trades=25,
        wf_passed=True,
    )
    assert result.confidence == "high"


def test_v20_paper_20_no_wf_confidence_medium():
    """20건이지만 WF 미통과 → medium."""
    result = calculate_trend_score(
        signal="entry_ok",
        rsi=45.0,
        entry_rsi_min=30.0,
        entry_rsi_max=60.0,
        atr_pct=0.2,
        trailing_multiplier=0.8,
        regime="trending",
        paper_trades=20,
        wf_passed=False,
    )
    assert result.confidence == "medium"


def test_v20_paper_10_confidence_medium():
    result = calculate_box_score(
        current_price=159.23,
        upper=159.82,
        lower=159.23,
        near_bound_pct=0.3,
        box_width_pct=0.37,
        regime="ranging",
        paper_trades=10,
    )
    assert result.confidence == "medium"


# ══════════════════════════════════════════════
# StrategyScore 구조 검증
# ══════════════════════════════════════════════

def test_strategy_score_dataclass():
    """StrategyScore 필드 존재 확인."""
    s = StrategyScore(
        score=0.616,
        readiness=0.8,
        edge=0.5,
        regime_fit=0.4,
        regime="ranging",
        confidence="low",
        detail={"upper": 159.82},
    )
    assert s.score == 0.616
    assert s.confidence == "low"
    assert s.detail["upper"] == 159.82


def test_total_score_weighted_average():
    """
    score = 0.4×readiness + 0.35×edge + 0.25×regime_fit

    readiness=1.0, edge=0.5, regime_fit=0.4
    → 0.4×1.0 + 0.35×0.5 + 0.25×0.4 = 0.4 + 0.175 + 0.1 = 0.675
    """
    score = calculate_total_score(readiness=1.0, edge=0.5, regime_fit=0.4)
    assert abs(score - 0.675) < 0.0001


def test_calculate_box_score_returns_strategy_score():
    """calculate_box_score가 StrategyScore를 반환하고 필드가 일관성 있음."""
    result = calculate_box_score(
        current_price=159.23,
        upper=159.82,
        lower=159.23,
        near_bound_pct=0.3,
        box_width_pct=0.37,
        regime="ranging",
    )
    assert isinstance(result, StrategyScore)
    assert 0.0 <= result.score <= 1.0
    assert 0.0 <= result.readiness <= 1.0
    assert 0.0 <= result.edge <= 1.0
    assert 0.0 <= result.regime_fit <= 1.0
    assert result.regime == "ranging"
    # 가중 평균과 일치하는지 확인
    expected = 0.4 * result.readiness + 0.35 * result.edge + 0.25 * result.regime_fit
    assert abs(result.score - round(expected, 4)) < 0.0001


# ══════════════════════════════════════════════
# 엣지 케이스 — 경계값 및 방어 코드 검증
# ══════════════════════════════════════════════

class TestEdgeCases:
    """경계값·방어 코드 검증 — 큐니 보강."""

    # ─── calculate_box_readiness ───

    def test_box_readiness_outside_above_upper(self):
        """가격이 박스 위를 벗어나도 0.0~1.0 범위 유지."""
        readiness = calculate_box_readiness(
            current_price=160.50,   # upper=159.82 초과
            upper=159.82,
            lower=159.23,
            near_bound_pct=0.3,
        )
        assert 0.0 <= readiness <= 1.0

    def test_box_readiness_outside_below_lower(self):
        """가격이 박스 아래를 벗어나도 0.0~1.0 범위 유지."""
        readiness = calculate_box_readiness(
            current_price=158.00,   # lower=159.23 미만
            upper=159.82,
            lower=159.23,
            near_bound_pct=0.3,
        )
        assert 0.0 <= readiness <= 1.0

    def test_box_readiness_invalid_bounds_returns_zero(self):
        """upper <= lower 비정상 입력 → 0.0 반환 (방어 코드)."""
        assert calculate_box_readiness(100.0, upper=99.0, lower=100.0, near_bound_pct=0.3) == 0.0
        assert calculate_box_readiness(100.0, upper=100.0, lower=100.0, near_bound_pct=0.3) == 0.0

    def test_box_readiness_zero_near_pct_returns_zero(self):
        """near_bound_pct=0 → 0으로 나누기 방어 → 0.0 반환."""
        assert calculate_box_readiness(159.23, upper=159.82, lower=159.23, near_bound_pct=0.0) == 0.0

    def test_box_readiness_negative_near_pct_returns_zero(self):
        """near_bound_pct 음수 → 0.0 반환."""
        assert calculate_box_readiness(159.23, upper=159.82, lower=159.23, near_bound_pct=-0.5) == 0.0

    # ─── calculate_box_edge ───

    def test_box_edge_never_negative(self):
        """win_rate=0, width=0 → edge는 0.0이어야 함 (음수 불가)."""
        edge = calculate_box_edge(box_width_pct=0.0, commission_rate=0.001, win_rate=0.0)
        assert edge == 0.0

    def test_box_edge_never_exceeds_one(self):
        """매우 큰 폭과 승률이어도 edge<=1.0."""
        edge = calculate_box_edge(box_width_pct=100.0, commission_rate=0.0, win_rate=1.0)
        assert edge == 1.0

    def test_box_edge_high_commission_reduces_edge(self):
        """수수료가 높으면 edge가 낮아진다."""
        edge_low_fee = calculate_box_edge(box_width_pct=0.5, commission_rate=0.001, win_rate=0.6)
        edge_high_fee = calculate_box_edge(box_width_pct=0.5, commission_rate=0.01, win_rate=0.6)
        assert edge_low_fee > edge_high_fee

    # ─── calculate_trend_readiness ───

    def test_trend_readiness_wait_dip_rsi_none(self):
        """wait_dip + rsi=None → 0.5 폴백."""
        readiness = calculate_trend_readiness(
            signal="wait_dip", rsi=None, entry_rsi_min=30.0, entry_rsi_max=60.0
        )
        assert readiness == 0.5

    def test_trend_readiness_wait_dip_rsi_at_min(self):
        """wait_dip + rsi = entry_rsi_min → readiness = 0.5 × (1-0) = 0.5."""
        readiness = calculate_trend_readiness(
            signal="wait_dip", rsi=30.0, entry_rsi_min=30.0, entry_rsi_max=60.0
        )
        assert readiness == 0.5

    def test_trend_readiness_wait_dip_rsi_at_max(self):
        """wait_dip + rsi = entry_rsi_max → readiness = 0.5 × (1-1) = 0.0."""
        readiness = calculate_trend_readiness(
            signal="wait_dip", rsi=60.0, entry_rsi_min=30.0, entry_rsi_max=60.0
        )
        assert readiness == 0.0

    def test_trend_readiness_wait_dip_rsi_above_max_clamped(self):
        """rsi가 entry_rsi_max 초과해도 readiness는 0.0 이하로 내려가지 않음."""
        readiness = calculate_trend_readiness(
            signal="wait_dip", rsi=80.0, entry_rsi_min=30.0, entry_rsi_max=60.0
        )
        assert readiness == 0.0

    def test_trend_readiness_unknown_signal_returns_default(self):
        """알 수 없는 시그널 → 0.2 반환."""
        readiness = calculate_trend_readiness(
            signal="no_signal", rsi=45.0, entry_rsi_min=30.0, entry_rsi_max=60.0
        )
        assert readiness == 0.2

    # ─── calculate_trend_edge ───

    def test_trend_edge_never_negative(self):
        """atr=0 → edge=0.0."""
        edge = calculate_trend_edge(atr_pct=0.0, trailing_multiplier=0.8, win_rate=0.34)
        assert edge == 0.0

    def test_trend_edge_never_exceeds_one(self):
        """큰 ATR도 edge<=1.0."""
        edge = calculate_trend_edge(atr_pct=999.0, trailing_multiplier=10.0, win_rate=1.0)
        assert edge == 1.0

    # ─── calculate_regime_fit ───

    def test_regime_fit_trending_for_box(self):
        """trending이면 박스전략 regime_fit=0.1."""
        assert calculate_regime_fit("trending", "box_mean_reversion") == 0.1

    def test_regime_fit_ranging_for_trend(self):
        """ranging이면 추세전략 regime_fit=0.1."""
        assert calculate_regime_fit("ranging", "trend_following") == 0.1

    def test_regime_fit_cfd_trend_same_as_trend(self):
        """cfd_trend_following도 trend_following과 동일 적합도."""
        assert calculate_regime_fit("trending", "cfd_trend_following") == 1.0
        assert calculate_regime_fit("ranging", "cfd_trend_following") == 0.1

    def test_regime_fit_unknown_strategy_returns_midpoint(self):
        """알 수 없는 전략 타입 → 0.5 중립값."""
        assert calculate_regime_fit("ranging", "unknown_strategy") == 0.5

    def test_regime_fit_unknown_regime_returns_midpoint(self):
        """알 수 없는 regime → 0.5 중립값."""
        assert calculate_regime_fit("unknown_regime", "box_mean_reversion") == 0.5

    # ─── calculate_total_score 경계값 ───

    def test_total_score_all_zero(self):
        """모두 0 → 0.0."""
        assert calculate_total_score(0.0, 0.0, 0.0) == 0.0

    def test_total_score_all_one(self):
        """모두 1.0 → 1.0."""
        assert calculate_total_score(1.0, 1.0, 1.0) == 1.0

    def test_total_score_readiness_only(self):
        """readiness=1만 → 0.4."""
        assert calculate_total_score(1.0, 0.0, 0.0) == 0.4

    def test_total_score_edge_only(self):
        """edge=1만 → 0.35."""
        assert calculate_total_score(0.0, 1.0, 0.0) == 0.35

    def test_total_score_regime_fit_only(self):
        """regime_fit=1만 → 0.25."""
        assert calculate_total_score(0.0, 0.0, 1.0) == 0.25

    # ─── extra_detail 병합 검증 ───

    def test_box_score_extra_detail_merged(self):
        """extra_detail 키가 결과 detail에 포함된다."""
        result = calculate_box_score(
            current_price=159.23,
            upper=159.82,
            lower=159.23,
            near_bound_pct=0.3,
            box_width_pct=0.37,
            regime="ranging",
            extra_detail={"box_age_hours": 36.5, "touch_count": 12},
        )
        assert result.detail["box_age_hours"] == 36.5
        assert result.detail["touch_count"] == 12

    def test_trend_score_extra_detail_merged(self):
        """추세 extra_detail 병합."""
        result = calculate_trend_score(
            signal="entry_ok",
            rsi=45.0,
            entry_rsi_min=30.0,
            entry_rsi_max=60.0,
            atr_pct=0.2,
            trailing_multiplier=0.8,
            regime="trending",
            extra_detail={"ema": 159.5},
        )
        assert result.detail["ema"] == 159.5
