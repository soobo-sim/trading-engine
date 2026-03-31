"""
core/backtest/grid_search.py

파라미터 그리드 서치 모듈 — engine.py에서 분리.

설계서: trader-common/solution-design/BACKTEST_MODULE_DESIGN.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.backtest.engine import BacktestConfig


# ──────────────────────────────────────────────────────────────
# 데이터 클래스
# ──────────────────────────────────────────────────────────────

@dataclass
class GridSearchResult:
    """그리드 서치 결과."""
    results: List[dict] = field(default_factory=list)
    best_params: dict = field(default_factory=dict)
    best_sharpe: Optional[float] = None
    total_combinations: int = 0

    def to_dict(self) -> dict:
        return {
            "total_combinations": self.total_combinations,
            "best_params": self.best_params,
            "best_sharpe": self.best_sharpe,
            "results": self.results,
        }


# ──────────────────────────────────────────────────────────────
# 메인 함수
# ──────────────────────────────────────────────────────────────

def run_grid_search(
    candles: List[Any],
    base_params: dict,
    param_grid: Dict[str, List[Any]],
    config: Optional["BacktestConfig"] = None,
    top_n: int = 10,
    strategy_type: str = "trend_following",
) -> GridSearchResult:
    """
    파라미터 그리드 서치.

    param_grid 예시:
      {
        "trailing_stop_atr_initial": [1.5, 2.0, 2.5],
        "trailing_stop_atr_mature": [1.0, 1.2, 1.5],
        "entry_rsi_max": [60, 65, 70],
      }

    모든 조합 실행 후 Sharpe ratio 기준 정렬.
    """
    from core.backtest.engine import BacktestConfig, run_backtest

    if config is None:
        config = BacktestConfig()

    # 조합 생성
    combinations = _generate_combinations(param_grid)

    result = GridSearchResult(total_combinations=len(combinations))
    all_results = []

    for combo in combinations:
        merged_params = {**base_params, **combo}
        bt_result = run_backtest(candles, merged_params, config, strategy_type)
        summary = {
            "params": combo,
            "total_trades": bt_result.total_trades,
            "win_rate": bt_result.win_rate,
            "total_return_pct": bt_result.total_return_pct,
            "sharpe_ratio": bt_result.sharpe_ratio,
            "max_drawdown_pct": bt_result.max_drawdown_pct,
            "total_pnl_jpy": bt_result.total_pnl_jpy,
        }
        all_results.append(summary)

    # Sharpe ratio 기준 정렬 (None은 최하위)
    all_results.sort(
        key=lambda x: x["sharpe_ratio"] if x["sharpe_ratio"] is not None else -999,
        reverse=True,
    )

    result.results = all_results[:top_n]
    if all_results and all_results[0]["sharpe_ratio"] is not None:
        result.best_params = all_results[0]["params"]
        result.best_sharpe = all_results[0]["sharpe_ratio"]

    return result


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _generate_combinations(param_grid: Dict[str, List[Any]]) -> List[dict]:
    """파라미터 그리드에서 모든 조합 생성."""
    if not param_grid:
        return [{}]

    keys = list(param_grid.keys())
    values = list(param_grid.values())

    combinations = [{}]
    for key, vals in zip(keys, values):
        new_combinations = []
        for combo in combinations:
            for val in vals:
                new_combo = {**combo, key: val}
                new_combinations.append(new_combo)
        combinations = new_combinations

    return combinations
