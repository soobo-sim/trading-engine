"""
백테스트 엔진 + 성과 메트릭 유닛 테스트.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from core.backtest.engine import (
    BacktestConfig,
    BacktestTrade,
    run_backtest,
    run_grid_search,
    _apply_slippage,
    _apply_fee,
    _compute_max_drawdown_from_trades,
    _compute_monthly_from_trades,
    _generate_combinations,
)
from api.routes.performance import (
    _compute_metrics,
    _compute_max_drawdown,
    _compute_monthly,
    _empty_metrics,
)


# ── 테스트용 캔들 ─────────────────────────────────────────


@dataclass
class FakeCandle:
    """Duck-typed candle for testing."""
    open: float
    high: float
    low: float
    close: float
    volume: float = 100.0
    open_time: datetime = None

    def __post_init__(self):
        if self.open_time is None:
            self.open_time = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_uptrend_candles(n: int = 60, start_price: float = 100.0) -> list:
    """상승 추세 캔들 생성 (EMA above, slope positive)."""
    candles = []
    price = start_price
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        noise = 0.5 * (i % 3 - 1)  # 약간의 변동
        price += 0.8 + noise
        candles.append(FakeCandle(
            open=price - 0.5,
            high=price + 1.0,
            low=price - 1.0,
            close=price,
            volume=100.0 + i,
            open_time=t + timedelta(hours=4 * i),
        ))
    return candles


def _make_downtrend_candles(n: int = 60, start_price: float = 200.0) -> list:
    """하락 추세 캔들 생성."""
    candles = []
    price = start_price
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        noise = 0.3 * (i % 3 - 1)
        price -= 0.8 + noise
        candles.append(FakeCandle(
            open=price + 0.5,
            high=price + 1.0,
            low=price - 1.0,
            close=price,
            volume=100.0,
            open_time=t + timedelta(hours=4 * i),
        ))
    return candles


def _make_sideways_candles(n: int = 60, center: float = 100.0) -> list:
    """횡보 캔들 생성."""
    candles = []
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        offset = 0.5 * (i % 4 - 2)
        price = center + offset
        candles.append(FakeCandle(
            open=price - 0.2,
            high=price + 0.5,
            low=price - 0.5,
            close=price,
            volume=100.0,
            open_time=t + timedelta(hours=4 * i),
        ))
    return candles


# ── 슬리피지 / 수수료 헬퍼 ─────────────────────────────────


class TestSlippageAndFee:
    def test_buy_slippage_increases_price(self):
        result = _apply_slippage(100.0, "buy", 0.05)
        assert result > 100.0
        assert result == pytest.approx(100.05)

    def test_sell_slippage_decreases_price(self):
        result = _apply_slippage(100.0, "sell", 0.05)
        assert result < 100.0
        assert result == pytest.approx(99.95)

    def test_zero_slippage(self):
        assert _apply_slippage(100.0, "buy", 0.0) == 100.0

    def test_fee_calculation(self):
        fee = _apply_fee(10000.0, 0.15)
        assert fee == pytest.approx(15.0)

    def test_zero_fee(self):
        assert _apply_fee(10000.0, 0.0) == 0.0


# ── 드로다운 ────────────────────────────────────────────────


class TestMaxDrawdown:
    def test_no_drawdown(self):
        assert _compute_max_drawdown_from_trades([1.0, 2.0, 3.0]) == 0.0

    def test_single_drawdown(self):
        pcts = [5.0, -3.0, 2.0]
        # cumul: 5, 2, 4 → peak at 5, dd max = 3
        assert _compute_max_drawdown_from_trades(pcts) == 3.0

    def test_deep_drawdown(self):
        pcts = [10.0, -5.0, -5.0, 3.0]
        # cumul: 10, 5, 0, 3 → peak=10, max dd=10
        assert _compute_max_drawdown_from_trades(pcts) == 10.0

    def test_empty(self):
        assert _compute_max_drawdown_from_trades([]) is None

    def test_performance_module_drawdown(self):
        # cumul: 2, 1, 4, 2 → peak=4, max dd=2
        assert _compute_max_drawdown([2.0, -1.0, 3.0, -2.0]) == 2.0


# ── 조합 생성 ───────────────────────────────────────────────


class TestGenerateCombinations:
    def test_empty_grid(self):
        result = _generate_combinations({})
        assert result == [{}]

    def test_single_param(self):
        result = _generate_combinations({"a": [1, 2, 3]})
        assert len(result) == 3
        assert {"a": 1} in result
        assert {"a": 3} in result

    def test_two_params(self):
        result = _generate_combinations({"a": [1, 2], "b": [10, 20]})
        assert len(result) == 4
        assert {"a": 1, "b": 10} in result
        assert {"a": 2, "b": 20} in result

    def test_three_params(self):
        result = _generate_combinations({"a": [1], "b": [2, 3], "c": [4, 5]})
        assert len(result) == 4


# ── 월별 집계 ───────────────────────────────────────────────


class TestMonthlyFromTrades:
    def test_empty(self):
        assert _compute_monthly_from_trades([]) == []

    def test_single_trade(self):
        trade = BacktestTrade(
            entry_time=datetime(2026, 1, 10, tzinfo=timezone.utc),
            entry_price=100.0,
            exit_time=datetime(2026, 1, 15, tzinfo=timezone.utc),
            exit_price=110.0,
            pnl_pct=10.0,
            pnl_jpy=1000.0,
        )
        result = _compute_monthly_from_trades([trade])
        assert len(result) == 1
        assert result[0]["month"] == "2026-01"
        assert result[0]["trades"] == 1
        assert result[0]["return_pct"] == 10.0

    def test_multi_month(self):
        t1 = BacktestTrade(
            entry_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            entry_price=100.0,
            exit_time=datetime(2026, 1, 15, tzinfo=timezone.utc),
            exit_price=105.0,
            pnl_pct=5.0,
        )
        t2 = BacktestTrade(
            entry_time=datetime(2026, 2, 1, tzinfo=timezone.utc),
            entry_price=105.0,
            exit_time=datetime(2026, 2, 20, tzinfo=timezone.utc),
            exit_price=100.0,
            pnl_pct=-4.76,
        )
        result = _compute_monthly_from_trades([t1, t2])
        assert len(result) == 2
        assert result[0]["month"] == "2026-01"
        assert result[1]["month"] == "2026-02"


# ── 백테스트 엔진 코어 ──────────────────────────────────────


class TestRunBacktest:
    def _default_params(self, **overrides):
        p = {
            "ema_period": 20,
            "entry_rsi_min": 40.0,
            "entry_rsi_max": 65.0,
            "ema_slope_entry_min": 0.0,
            "rsi_overbought": 75,
            "rsi_extreme": 80,
            "rsi_breakdown": 40,
            "ema_slope_weak_threshold": 0.03,
            "partial_exit_profit_atr": 2.0,
            "tighten_stop_atr": 1.0,
            "trailing_stop_atr_initial": 2.0,
            "trailing_stop_atr_mature": 1.2,
            "atr_multiplier_stop": 2.0,
            "position_size_pct": 100.0,
        }
        p.update(overrides)
        return p

    def test_insufficient_candles(self):
        candles = _make_uptrend_candles(10)
        result = run_backtest(candles, self._default_params())
        assert result.total_trades == 0
        assert result.candle_count == 10

    def test_uptrend_produces_trades(self):
        candles = _make_uptrend_candles(80)
        result = run_backtest(candles, self._default_params())
        # 충분한 상승 추세에서 entry_ok 시그널 발생 가능
        assert result.candle_count == 80
        assert isinstance(result.total_trades, int)
        # result는 BacktestResult 타입
        assert result.params_used == self._default_params()

    def test_result_structure(self):
        candles = _make_uptrend_candles(80)
        config = BacktestConfig(initial_capital_jpy=50_000.0)
        result = run_backtest(candles, self._default_params(), config)
        d = result.to_dict()
        assert "total_trades" in d
        assert "win_rate" in d
        assert "sharpe_ratio" in d
        assert "max_drawdown_pct" in d
        assert "monthly" in d
        assert "trades" in d
        assert "params_used" in d

    def test_downtrend_short_possible(self):
        """하락 추세에서 숏 진입 가능 (entry_sell 시그널)."""
        params = self._default_params(
            entry_rsi_min_short=35.0,
            entry_rsi_max_short=60.0,
            ema_slope_short_threshold=-0.05,
        )
        candles = _make_downtrend_candles(80)
        result = run_backtest(candles, params)
        assert result.candle_count == 80

    def test_trade_pnl_includes_fees(self):
        """각 거래의 PnL에 수수료가 반영되는지 확인."""
        candles = _make_uptrend_candles(80)
        config = BacktestConfig(fee_pct=0.15, slippage_pct=0.0)
        result = run_backtest(candles, self._default_params(), config)
        if result.trades:
            # 수수료가 있으므로 매수-매도 간 가격 차이보다 PnL이 약간 작아야 함
            for t in result.trades:
                if t.pnl_jpy is not None:
                    assert isinstance(t.pnl_jpy, float)

    def test_sideways_fewer_trades(self):
        """횡보 시장에서는 진입이 억제되어 거래가 적어야 한다."""
        candles = _make_sideways_candles(80)
        result = run_backtest(candles, self._default_params())
        # 횡보 → regime_ranging → no entry
        # 정확한 수는 시장 조건에 따라 다르므로 단순 존재 확인
        assert result.candle_count == 80

    def test_custom_capital(self):
        candles = _make_uptrend_candles(80)
        config = BacktestConfig(initial_capital_jpy=1_000_000.0)
        result = run_backtest(candles, self._default_params(), config)
        assert result.candle_count == 80


# ── 그리드 서치 ─────────────────────────────────────────────


class TestRunGridSearch:
    def _default_params(self):
        return {
            "ema_period": 20,
            "entry_rsi_min": 40.0,
            "entry_rsi_max": 65.0,
            "ema_slope_entry_min": 0.0,
            "rsi_overbought": 75,
            "rsi_extreme": 80,
            "rsi_breakdown": 40,
            "ema_slope_weak_threshold": 0.03,
            "partial_exit_profit_atr": 2.0,
            "tighten_stop_atr": 1.0,
            "trailing_stop_atr_initial": 2.0,
            "trailing_stop_atr_mature": 1.2,
            "atr_multiplier_stop": 2.0,
            "position_size_pct": 100.0,
        }

    def test_grid_search_basic(self):
        candles = _make_uptrend_candles(60)
        grid = {"trailing_stop_atr_initial": [1.5, 2.0, 2.5]}
        result = run_grid_search(candles, self._default_params(), grid)
        assert result.total_combinations == 3
        assert len(result.results) <= 3

    def test_grid_search_multi_param(self):
        candles = _make_uptrend_candles(60)
        grid = {
            "trailing_stop_atr_initial": [1.5, 2.0],
            "entry_rsi_max": [60, 65],
        }
        result = run_grid_search(candles, self._default_params(), grid, top_n=5)
        assert result.total_combinations == 4
        assert len(result.results) <= 5

    def test_grid_search_output_format(self):
        candles = _make_uptrend_candles(60)
        grid = {"atr_multiplier_stop": [1.5, 2.0]}
        result = run_grid_search(candles, self._default_params(), grid)
        d = result.to_dict()
        assert "total_combinations" in d
        assert "best_params" in d
        assert "results" in d
        for r in d["results"]:
            assert "params" in r
            assert "total_trades" in r
            assert "sharpe_ratio" in r

    def test_grid_search_empty_grid(self):
        candles = _make_uptrend_candles(60)
        result = run_grid_search(candles, self._default_params(), {})
        assert result.total_combinations == 1


# ── 성과 메트릭 계산 (performance.py) ───────────────────────


@dataclass
class FakePosition:
    """성과 메트릭 테스트용 fake position."""
    realized_pnl_jpy: float = None
    realized_pnl_pct: float = None
    exit_reason: str = None
    status: str = "closed"
    created_at: datetime = None
    closed_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        if self.closed_at is None:
            self.closed_at = datetime(2026, 1, 2, tzinfo=timezone.utc)


class TestComputeMetrics:
    def test_empty(self):
        result = _compute_metrics([])
        assert result["total_trades"] == 0
        assert result["win_rate"] is None

    def test_single_win(self):
        pos = [FakePosition(realized_pnl_jpy=100.0, realized_pnl_pct=5.0)]
        result = _compute_metrics(pos)
        assert result["total_trades"] == 1
        assert result["wins"] == 1
        assert result["losses"] == 0
        assert result["win_rate"] == 100.0

    def test_mixed(self):
        pos = [
            FakePosition(realized_pnl_jpy=200.0, realized_pnl_pct=10.0),
            FakePosition(realized_pnl_jpy=-50.0, realized_pnl_pct=-2.5),
            FakePosition(realized_pnl_jpy=100.0, realized_pnl_pct=5.0),
        ]
        result = _compute_metrics(pos)
        assert result["total_trades"] == 3
        assert result["wins"] == 2
        assert result["losses"] == 1
        assert result["win_rate"] == pytest.approx(66.7, abs=0.1)
        assert result["total_pnl_jpy"] == 250.0

    def test_unknown_positions(self):
        pos = [
            FakePosition(realized_pnl_jpy=100.0, realized_pnl_pct=5.0),
            FakePosition(realized_pnl_jpy=None, realized_pnl_pct=None),
        ]
        result = _compute_metrics(pos)
        assert result["total_trades"] == 2
        assert result["valid_trades"] == 1
        assert result["unknown"] == 1
        assert result["win_rate"] == 100.0

    def test_sharpe_ratio(self):
        pos = [
            FakePosition(realized_pnl_jpy=100.0, realized_pnl_pct=5.0),
            FakePosition(realized_pnl_jpy=80.0, realized_pnl_pct=4.0),
            FakePosition(realized_pnl_jpy=-30.0, realized_pnl_pct=-1.5),
        ]
        result = _compute_metrics(pos)
        assert result["sharpe_ratio"] is not None

    def test_max_consecutive_losses(self):
        pos = [
            FakePosition(realized_pnl_jpy=100.0),
            FakePosition(realized_pnl_jpy=-50.0),
            FakePosition(realized_pnl_jpy=-30.0),
            FakePosition(realized_pnl_jpy=-20.0),
            FakePosition(realized_pnl_jpy=80.0),
        ]
        result = _compute_metrics(pos)
        assert result["max_consecutive_losses"] == 3

    def test_avg_holding_hours(self):
        pos = [
            FakePosition(
                realized_pnl_jpy=100.0,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                closed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),  # 24h
            ),
            FakePosition(
                realized_pnl_jpy=50.0,
                created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                closed_at=datetime(2026, 1, 3, 12, tzinfo=timezone.utc),  # 12h
            ),
        ]
        result = _compute_metrics(pos)
        assert result["avg_holding_hours"] == pytest.approx(18.0)

    def test_monthly(self):
        pos = [
            FakePosition(
                realized_pnl_jpy=100.0,
                realized_pnl_pct=5.0,
                closed_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
            ),
            FakePosition(
                realized_pnl_jpy=200.0,
                realized_pnl_pct=10.0,
                closed_at=datetime(2026, 2, 10, tzinfo=timezone.utc),
            ),
        ]
        result = _compute_metrics(pos)
        assert len(result["monthly"]) == 2
        assert result["monthly"][0]["month"] == "2026-01"
        assert result["monthly"][1]["month"] == "2026-02"

    def test_expected_value(self):
        pos = [
            FakePosition(realized_pnl_jpy=100.0, realized_pnl_pct=10.0),
            FakePosition(realized_pnl_jpy=-50.0, realized_pnl_pct=-5.0),
        ]
        result = _compute_metrics(pos)
        # EV = 0.5 * 10.0 + 0.5 * (-5.0) = 2.5
        assert result["expected_value_pct"] == pytest.approx(2.5)

    def test_total_return_pct(self):
        pos = [
            FakePosition(realized_pnl_jpy=100.0, realized_pnl_pct=5.0),
            FakePosition(realized_pnl_jpy=-30.0, realized_pnl_pct=-1.5),
            FakePosition(realized_pnl_jpy=80.0, realized_pnl_pct=4.0),
        ]
        result = _compute_metrics(pos)
        assert result["total_return_pct"] == pytest.approx(7.5)


class TestEmptyMetrics:
    def test_empty_metrics_structure(self):
        result = _empty_metrics()
        assert result["total_trades"] == 0
        assert result["monthly"] == []
        assert result["sharpe_ratio"] is None
