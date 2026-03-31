"""
core/backtest/walk_forward.py

Rolling Walk-Forward 검증 모듈.

설계서: trader-common/solution-design/BACKTEST_MODULE_DESIGN.md

통과 기준 (자동 판정):
  - OOS 양수 윈도우 ≥ 60% (5윈도우 중 3개 이상)
  - 합산 수익률 > 0%
  - OOS 거래수 합산 ≥ 30건
  - IS/OOS Sharpe 괴리 < 60% (과적합 필터)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Optional

from core.backtest.engine import BacktestConfig, BacktestResult, run_backtest


# ──────────────────────────────────────────────────────────────
# 데이터 클래스
# ──────────────────────────────────────────────────────────────

@dataclass
class WFWindow:
    """단일 WF 윈도우 결과."""
    index: int
    # IS (in-sample) 기간
    is_start: date
    is_end: date
    is_trades: int
    is_sharpe: Optional[float]
    is_return_pct: Optional[float]
    # OOS (out-of-sample) 기간
    oos_start: date
    oos_end: date
    oos_trades: int
    oos_win_rate: Optional[float]
    oos_return_pct: Optional[float]
    oos_sharpe: Optional[float]
    oos_mdd: Optional[float]


@dataclass
class WFResult:
    """Walk-Forward 검증 결과."""
    pass_fail: bool = False
    fail_reason: str = ""
    total_windows: int = 0
    positive_windows: int = 0
    total_trades: int = 0
    total_return_pct: float = 0.0
    avg_sharpe: Optional[float] = None
    max_mdd: Optional[float] = None
    windows: List[WFWindow] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# 통과 기준 상수
# ──────────────────────────────────────────────────────────────

WF_PASS_POSITIVE_RATIO = 0.6     # OOS 양수 윈도우 ≥ 60%
WF_PASS_MIN_RETURN = 0.0         # 합산 수익률 > 0%
WF_PASS_MIN_TRADES = 30          # OOS 거래수 합산 ≥ 30
WF_PASS_SHARPE_DECAY_MAX = 0.6   # IS/OOS Sharpe 괴리 < 60%


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _candle_time(c: Any) -> datetime:
    t = c.open_time
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def _slice_candles(candles: List[Any], start_dt: datetime, end_dt: datetime) -> List[Any]:
    """[start_dt, end_dt) 범위의 캔들 슬라이스."""
    return [c for c in candles if start_dt <= _candle_time(c) < end_dt]


def _to_date(dt: datetime) -> date:
    return dt.date() if isinstance(dt, datetime) else dt


def _safe_sharpe(result: BacktestResult) -> Optional[float]:
    return result.sharpe_ratio


def _safe_return(result: BacktestResult) -> Optional[float]:
    return result.total_return_pct


# ──────────────────────────────────────────────────────────────
# 메인 함수
# ──────────────────────────────────────────────────────────────

def run_walk_forward(
    candles: List[Any],
    params: dict,
    strategy_type: str = "trend_following",
    train_days: int = 240,
    valid_days: int = 60,
    step_days: int = 30,
    min_windows: int = 3,
    min_trades_per_window: int = 5,
    initial_capital_jpy: float = 100_000.0,
    slippage_pct: float = 0.05,
    fee_pct: float = 0.0,
) -> WFResult:
    """
    Rolling Walk-Forward 검증.

    윈도우 분할:
      IS 기간 = train_days (학습)
      OOS 기간 = valid_days (검증)
      슬라이드 = step_days

    전체 기간 = IS + (N-1)*step + OOS
    """
    result = WFResult()

    if not candles:
        result.fail_reason = "캔들 없음"
        return result

    config = BacktestConfig(
        initial_capital_jpy=initial_capital_jpy,
        position_size_pct=float(params.get("position_size_pct", 100.0)),
        slippage_pct=slippage_pct,
        fee_pct=fee_pct,
    )

    # 전체 캔들 시간 범위
    first_dt = _candle_time(candles[0])
    last_dt = _candle_time(candles[-1])
    total_days = (last_dt - first_dt).days

    min_total = train_days + valid_days
    if total_days < min_total:
        result.fail_reason = f"캔들 기간 부족: {total_days}일 < {min_total}일 (IS {train_days} + OOS {valid_days})"
        return result

    # 윈도우 생성
    windows: List[WFWindow] = []
    window_idx = 1
    is_start_dt = first_dt

    while True:
        is_end_dt = is_start_dt + timedelta(days=train_days)
        oos_start_dt = is_end_dt
        oos_end_dt = oos_start_dt + timedelta(days=valid_days)

        if oos_end_dt > last_dt + timedelta(days=1):
            break

        is_candles = _slice_candles(candles, is_start_dt, is_end_dt)
        oos_candles = _slice_candles(candles, oos_start_dt, oos_end_dt)

        if not is_candles or not oos_candles:
            is_start_dt += timedelta(days=step_days)
            continue

        # IS 백테스트
        is_result = run_backtest(is_candles, params, config, strategy_type)
        # OOS 백테스트 (동일 파라미터)
        oos_result = run_backtest(oos_candles, params, config, strategy_type)

        win = WFWindow(
            index=window_idx,
            is_start=_to_date(is_start_dt),
            is_end=_to_date(is_end_dt),
            is_trades=is_result.total_trades or 0,
            is_sharpe=_safe_sharpe(is_result),
            is_return_pct=_safe_return(is_result),
            oos_start=_to_date(oos_start_dt),
            oos_end=_to_date(oos_end_dt),
            oos_trades=oos_result.total_trades or 0,
            oos_win_rate=oos_result.win_rate,
            oos_return_pct=_safe_return(oos_result),
            oos_sharpe=_safe_sharpe(oos_result),
            oos_mdd=oos_result.max_drawdown_pct,
        )
        windows.append(win)
        window_idx += 1
        is_start_dt += timedelta(days=step_days)

    result.windows = windows
    result.total_windows = len(windows)

    if result.total_windows < min_windows:
        result.fail_reason = f"윈도우 부족: {result.total_windows}개 < {min_windows}개"
        return result

    # 집계
    oos_returns = [w.oos_return_pct for w in windows if w.oos_return_pct is not None]
    oos_sharpes = [w.oos_sharpe for w in windows if w.oos_sharpe is not None]
    oos_mdds = [w.oos_mdd for w in windows if w.oos_mdd is not None]

    result.total_trades = sum(w.oos_trades for w in windows)
    result.positive_windows = sum(1 for r in oos_returns if r > 0)
    result.total_return_pct = round(sum(oos_returns), 2) if oos_returns else 0.0
    result.avg_sharpe = round(sum(oos_sharpes) / len(oos_sharpes), 3) if oos_sharpes else None
    result.max_mdd = round(max(oos_mdds), 2) if oos_mdds else None

    # 통과 판정
    positive_ratio = result.positive_windows / result.total_windows if result.total_windows else 0

    fail_reasons = []
    if positive_ratio < WF_PASS_POSITIVE_RATIO:
        fail_reasons.append(
            f"OOS 양수 윈도우 {result.positive_windows}/{result.total_windows} "
            f"({positive_ratio*100:.0f}% < {WF_PASS_POSITIVE_RATIO*100:.0f}%)"
        )
    if result.total_return_pct <= WF_PASS_MIN_RETURN:
        fail_reasons.append(f"합산 수익률 {result.total_return_pct:.2f}% ≤ 0%")
    if result.total_trades < WF_PASS_MIN_TRADES:
        fail_reasons.append(f"OOS 거래수 {result.total_trades} < {WF_PASS_MIN_TRADES}")

    # IS/OOS Sharpe 괴리 (과적합 필터)
    is_sharpes = [w.is_sharpe for w in windows if w.is_sharpe is not None]
    if oos_sharpes and is_sharpes:
        avg_is = sum(is_sharpes) / len(is_sharpes)
        avg_oos = sum(oos_sharpes) / len(oos_sharpes)
        if avg_is > 0 and avg_oos is not None:
            decay = (avg_is - avg_oos) / avg_is
            if decay > WF_PASS_SHARPE_DECAY_MAX:
                fail_reasons.append(
                    f"Sharpe 괴리 {decay*100:.0f}% > {WF_PASS_SHARPE_DECAY_MAX*100:.0f}% (과적합)"
                )

    if fail_reasons:
        result.pass_fail = False
        result.fail_reason = " | ".join(fail_reasons)
    else:
        result.pass_fail = True

    return result
