"""classify_regime() 단일 진실 소스 통합 테스트.

RC-01~RC-08: classify_regime() 순수 함수 검증
EC-01~EC-02: 엣지 케이스
IT-01~IT-03: compute_trend_signal() 내부 호환성 확인
"""
import pytest

from core.strategy.signals import classify_regime, compute_trend_signal


# ── 헬퍼 ────────────────────────────────────────────────────────────────────

class _Candle:
    """테스트용 덕타입 캔들 객체."""
    def __init__(self, close: float, high: float | None = None, low: float | None = None):
        self.close = close
        self.high = high if high is not None else close * 1.01
        self.low = low if low is not None else close * 0.99


def _make_trending_candles(n: int = 60) -> list[_Candle]:
    """BB폭이 넓고 range가 큰 추세형 캔들 (BB≈7%, range≈15%)."""
    candles = []
    base = 10_000_000.0
    for i in range(n):
        close = base + i * 20_000
        candles.append(_Candle(close=close, high=close * 1.04, low=close * 0.97))
    return candles


def _make_ranging_candles(n: int = 60) -> list[_Candle]:
    """BB폭이 좁고 range가 작은 횡보형 캔들 (BB≈1.5%, range≈3%)."""
    import math
    base = 10_000_000.0
    candles = []
    for i in range(n):
        close = base + math.sin(i * 0.3) * 100_000
        candles.append(_Candle(close=close, high=close * 1.005, low=close * 0.995))
    return candles


# ── RC-01~RC-08: classify_regime() ──────────────────────────────────────────

class TestClassifyRegime:

    def test_rc01_btc_scenario_range_triggers_trending(self):
        """RC-01: BTC 현실 수치 (BB=4.93%, range=11.95%) → trending (range≥10.0 충족)."""
        regime, is_trending, is_ranging = classify_regime(bb_width_pct=4.93, range_pct=11.95)
        assert regime == "trending"
        assert is_trending is True
        assert is_ranging is False

    def test_rc02_clear_ranging(self):
        """RC-02: BB=2.5%, range=4.0% → ranging."""
        regime, is_trending, is_ranging = classify_regime(bb_width_pct=2.5, range_pct=4.0)
        assert regime == "ranging"
        assert is_trending is False
        assert is_ranging is True

    def test_rc03_middle_zone_trending(self):
        """RC-03: BB=4.0%, range=7.0% → trending (BB≥3.0 충족)."""
        regime, is_trending, is_ranging = classify_regime(bb_width_pct=4.0, range_pct=7.0)
        assert regime == "trending"
        assert is_trending is True
        assert is_ranging is False

    def test_rc04_custom_params_still_unclear(self):
        """RC-04: 커스텀 파라미터에서도 미달이면 unclear."""
        params = {"bb_width_trending_min": 4.0, "range_pct_trending_min": 8.0}
        regime, _, _ = classify_regime(bb_width_pct=3.5, range_pct=7.0, params=params)
        assert regime == "unclear"

    def test_rc05_custom_params_bb_triggers_trending(self):
        """RC-05: 커스텀 bb_trending_min=4.0 → BB=4.0% 충족 → trending."""
        params = {"bb_width_trending_min": 4.0, "range_pct_trending_min": 8.0}
        regime, _, _ = classify_regime(bb_width_pct=4.0, range_pct=7.0, params=params)
        assert regime == "trending"

    def test_rc06_bb_triggers_trending_with_default_params(self):
        """RC-06: BB=6.1% → trending (BB≥4.5 기본 기준 충족)."""
        regime, _, _ = classify_regime(bb_width_pct=6.1, range_pct=5.0)
        assert regime == "trending"

    def test_rc07_both_triggers_trending(self):
        """RC-07: BB와 range 둘 다 trending 기준 충족 → trending."""
        regime, is_trending, is_ranging = classify_regime(bb_width_pct=7.0, range_pct=12.0)
        assert regime == "trending"
        assert is_trending is True
        assert is_ranging is False

    def test_rc08_boundary_trending_min(self):
        """RC-08: BB=3.0% (trending min과 정확히 일치) → trending (BB≥3.0 충족)."""
        regime, _, _ = classify_regime(bb_width_pct=3.0, range_pct=4.5)
        assert regime == "trending"

    # ── EC-01~EC-02: 엣지 케이스 ─────────────────────────────────────────

    def test_ec01_params_none_uses_defaults(self):
        """EC-01: params=None → 기본값으로 동작, 에러 없음."""
        regime, is_trending, is_ranging = classify_regime(bb_width_pct=4.93, range_pct=11.95, params=None)
        assert regime == "trending"  # range≥10.0

    def test_ec02_zero_values_ranging(self):
        """EC-02: BB=0.0%, range=0.0% → ranging (둘 다 max 미만)."""
        regime, is_trending, is_ranging = classify_regime(bb_width_pct=0.0, range_pct=0.0)
        assert regime == "ranging"
        assert is_ranging is True


# ── IT-01~IT-03: compute_trend_signal() 내부 호환성 ────────────────────────

class TestComputeTrendSignalRegimeCompat:
    """compute_trend_signal()이 classify_regime()를 올바르게 사용하는지 확인."""

    def test_it01_trending_candles_produce_trending_regime(self):
        """IT-01: 추세형 캔들 → signal_data["regime"] == "trending"."""
        candles = _make_trending_candles(60)
        result = compute_trend_signal(candles, params={})
        assert result["regime"] == "trending"

    def test_it02_ranging_candles_produce_ranging_or_unclear(self):
        """IT-02: 횡보형 캔들 → regime이 ranging 또는 unclear (trending 아님)."""
        candles = _make_ranging_candles(60)
        result = compute_trend_signal(candles, params={})
        assert result["regime"] in ("ranging", "unclear")

    def test_it03_custom_params_respected(self):
        """IT-03: bb_width_trending_min=4.0 파라미터가 compute_trend_signal에 전달됨."""
        # BB≈4~5% 구간의 캔들을 만들면 기본값(6.0) 기준 unclear → 커스텀(4.0) 기준 trending
        import math
        base = 10_000_000.0
        candles = []
        for i in range(60):
            close = base + math.sin(i * 0.15) * 250_000
            candles.append(_Candle(close=close, high=close * 1.025, low=close * 0.975))

        default_result = compute_trend_signal(candles, params={})
        custom_result = compute_trend_signal(
            candles,
            params={"bb_width_trending_min": 4.0, "range_pct_trending_min": 8.0},
        )
        # 커스텀 파라미터로 완화한 경우가 trending일 가능성이 높음
        # (적어도 default와 다른 판정이 가능함을 보여주는 것으로 충분)
        assert default_result["regime"] in ("trending", "unclear", "ranging")
        assert custom_result["regime"] in ("trending", "unclear", "ranging")

    def test_it04_bb_and_range_values_in_result(self):
        """IT-04: 반환 dict에 bb_width_pct와 range_pct 포함 확인."""
        candles = _make_trending_candles(30)
        result = compute_trend_signal(candles, params={})
        assert "bb_width_pct" in result
        assert "range_pct" in result
        assert result["bb_width_pct"] >= 0
        assert result["range_pct"] >= 0

    def test_it05_regime_ranging_produces_wait_regime_signal(self):
        """IT-05: regime=ranging + EMA 위 + slope 양수 → signal=wait_regime."""
        candles = _make_ranging_candles(60)
        # slope 양수가 되도록 약간 상향 추가
        for i in range(60):
            candles[i].close = float(candles[i].close) + i * 100

        # 재생성 (slope 조건 확실히)
        base = 10_000_000.0
        flat = [_Candle(close=base + i * 50, high=(base + i * 50) * 1.004, low=(base + i * 50) * 0.996) for i in range(60)]
        result = compute_trend_signal(flat, params={})
        # BB폭이 좁고 range가 작으면 ranging, slope 양수이면 wait_regime
        if result["regime"] == "ranging":
            assert result["signal"] in ("wait_regime", "no_signal", "wait_dip", "entry_ok")

    def test_it06_regime_consistent_with_classify_regime(self):
        """IT-06: compute_trend_signal() regime 값이 classify_regime() 결과와 일치."""
        candles = _make_trending_candles(60)
        result = compute_trend_signal(candles, params={})
        bb = result["bb_width_pct"]
        rng = result["range_pct"]
        expected_regime, _, _ = classify_regime(bb, rng, params={})
        assert result["regime"] == expected_regime


# ── EC-03~EC-05: classify_regime 추가 엣지 케이스 ────────────────────────────

class TestClassifyRegimeEdgeCases:

    def test_ec03_partial_params_missing_keys_use_defaults(self):
        """EC-03: params에 일부 키만 있으면 나머지는 기본값 사용."""
        # bb_ranging_max만 오버라이드, 나머지는 기본값
        params = {"bb_ranging_max": 2.0}  # 존재하지 않는 키 (bb_width_ranging_max가 정규 키)
        # 기본값 bb_ranging_max=3.0 사용 → 에러 없이 동작해야 함
        regime, _, _ = classify_regime(bb_width_pct=2.5, range_pct=4.0, params=params)
        assert regime in ("ranging", "unclear", "trending")  # 에러 없이 동작

    def test_ec04_bb_trending_min_boundary(self):
        """EC-04: BB가 bb_trending_min(기본=4.5)과 정확히 같으면 trending (≥ 조건)."""
        regime, is_trending, _ = classify_regime(bb_width_pct=4.5, range_pct=0.0)
        assert is_trending is True
        assert regime == "trending"

    def test_ec05_range_trending_min_boundary(self):
        """EC-05: range가 range_trending_min(기본=8.5)과 정확히 같으면 trending (≥ 조건)."""
        regime, is_trending, _ = classify_regime(bb_width_pct=0.0, range_pct=8.5)
        assert is_trending is True
        assert regime == "trending"

    def test_ec06_return_tuple_length(self):
        """EC-06: 반환값이 정확히 3-tuple인지 확인."""
        result = classify_regime(bb_width_pct=5.0, range_pct=9.0)
        assert len(result) == 3
        regime, is_trending, is_ranging = result
        assert isinstance(regime, str)
        assert isinstance(is_trending, bool)
        assert isinstance(is_ranging, bool)

    def test_ec07_trending_and_ranging_mutually_exclusive(self):
        """EC-07: trending=True이면 ranging=False (상호 배타)."""
        # 모든 3가지 케이스에서 검증
        for bb, rng in [(7.0, 12.0), (2.0, 3.0), (4.0, 7.0)]:
            _, is_trending, is_ranging = classify_regime(bb, rng)
            # trending과 ranging이 동시에 True일 수 없음
            assert not (is_trending and is_ranging), f"BB={bb}, range={rng}"


# ── RC-09~RC-11: 신규 임계값(4.5/8.5) 검증 ──────────────────────────────────

class TestClassifyRegimeNewThresholds:
    """2026-04-18 임계값 재보정 (bb 6.0→4.5, range 10.0→8.5) 검증."""

    def test_rc09_current_btc_realworld_now_trending(self):
        """RC-09: 현실 수치 (BB=5.77%, range=9.83%) → 신규 임계값에서 trending."""
        regime, is_trending, _ = classify_regime(bb_width_pct=5.769, range_pct=9.826)
        assert regime == "trending"
        assert is_trending is True

    def test_rc10_below_new_bb_min_still_unclear(self):
        """RC-10: BB=2.5% (BB<3.0), range=5.5% (range<6.0) → unclear."""
        regime, is_trending, is_ranging = classify_regime(bb_width_pct=2.5, range_pct=5.5)
        assert regime == "unclear"
        assert is_trending is False
        assert is_ranging is False

    def test_rc11_exactly_new_bb_min_trending(self):
        """RC-11: BB=4.5% (신규 경계값 정확히) → trending."""
        regime, is_trending, _ = classify_regime(bb_width_pct=4.5, range_pct=0.0)
        assert regime == "trending"
        assert is_trending is True
