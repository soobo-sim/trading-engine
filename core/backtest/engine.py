"""
백테스트 엔진 — 캔들 리플레이 + 실전 signals.py 호출 + 가상 주문 체결.

핵심 원칙:
  - compute_trend_signal() 그대로 재사용 (백테스트 전용 로직 없음)
  - 슬리피지 / 수수료 시뮬레이션 포함
  - 포지션 관리는 실전 TrendFollowingManager 로직 미러링

사용:
    result = run_backtest(candles, params)
    result = run_grid_search(candles, param_grid)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.strategy.signals import compute_trend_signal
from core.analysis.box_detector import detect_box


# ──────────────────────────────────────────────────────────────
# 데이터 클래스
# ──────────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    """가상 거래 기록."""
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    side: str = "buy"
    amount: float = 0.0
    pnl_jpy: Optional[float] = None
    pnl_pct: Optional[float] = None
    exit_reason: Optional[str] = None


@dataclass
class BacktestConfig:
    """백테스트 설정."""
    initial_capital_jpy: float = 100_000.0
    slippage_pct: float = 0.05       # 0.05% 슬리피지
    fee_pct: float = 0.15            # 0.15% 수수료 (편도)
    position_size_pct: float = 100.0  # 자본의 %를 투입


@dataclass
class BacktestResult:
    """백테스트 실행 결과."""
    trades: List[BacktestTrade] = field(default_factory=list)
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: Optional[float] = None
    total_return_pct: Optional[float] = None
    total_pnl_jpy: float = 0.0
    max_drawdown_pct: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    avg_holding_hours: Optional[float] = None
    monthly: List[dict] = field(default_factory=list)
    params_used: dict = field(default_factory=dict)
    candle_count: int = 0
    period_start: Optional[str] = None
    period_end: Optional[str] = None

    def to_dict(self) -> dict:
        """직렬화용."""
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "total_return_pct": self.total_return_pct,
            "total_pnl_jpy": self.total_pnl_jpy,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "avg_holding_hours": self.avg_holding_hours,
            "monthly": self.monthly,
            "params_used": self.params_used,
            "candle_count": self.candle_count,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "trades": [
                {
                    "entry_time": t.entry_time.isoformat(),
                    "entry_price": t.entry_price,
                    "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                    "exit_price": t.exit_price,
                    "side": t.side,
                    "pnl_pct": t.pnl_pct,
                    "pnl_jpy": t.pnl_jpy,
                    "exit_reason": t.exit_reason,
                }
                for t in self.trades
            ],
        }


# ──────────────────────────────────────────────────────────────
# 가격 시뮬레이션 헬퍼
# ──────────────────────────────────────────────────────────────

def _apply_slippage(price: float, side: str, slippage_pct: float) -> float:
    """슬리피지 적용. 매수 시 높게, 매도 시 낮게."""
    factor = slippage_pct / 100
    if side == "buy":
        return price * (1 + factor)
    return price * (1 - factor)


def _apply_fee(amount_jpy: float, fee_pct: float) -> float:
    """수수료 차감."""
    return amount_jpy * fee_pct / 100


# ──────────────────────────────────────────────────────────────
# 백테스트 엔진
# ──────────────────────────────────────────────────────────────

def run_backtest(
    candles: List[Any],
    params: dict,
    config: Optional[BacktestConfig] = None,
    strategy_type: str = "trend_following",
) -> BacktestResult:
    """
    캔들 리플레이 백테스트 실행.

    candles: 시간순 정렬된 캔들 객체 리스트 (.close, .high, .low, .open_time 필요)
    params: 전략 파라미터 (compute_trend_signal에 그대로 전달)
    config: 백테스트 설정 (자본, 슬리피지, 수수료)

    Returns: BacktestResult
    """
    if config is None:
        config = BacktestConfig()

    # strategy_type 분기
    if strategy_type == "box_mean_reversion":
        return _run_box_backtest(candles, params, config)

    # params에서 position_size_pct 오버라이드
    position_size = float(params.get("position_size_pct", config.position_size_pct))

    # EMA 계산에 최소 캔들 수 필요
    ema_period = int(params.get("ema_period", 20))
    min_candles = max(ema_period + 5, 20)  # EMA + 여유분

    if len(candles) < min_candles:
        return BacktestResult(
            candle_count=len(candles),
            params_used=params,
        )

    capital = config.initial_capital_jpy
    trades: List[BacktestTrade] = []
    current_position: Optional[BacktestTrade] = None
    stop_loss_price: Optional[float] = None
    stop_tightened = False
    trailing_high: Optional[float] = None

    result = BacktestResult(
        params_used=params,
        candle_count=len(candles),
        period_start=_candle_time_str(candles[0]),
        period_end=_candle_time_str(candles[-1]),
    )

    # 캔들 리플레이 (실전과 동일한 윈도우 크기)
    for i in range(min_candles, len(candles)):
        window = candles[max(0, i - 60):i + 1]  # 60개 윈도우 (실전과 동일)
        current_candle = candles[i]
        current_price = float(current_candle.close)
        current_high = float(current_candle.high)
        current_low = float(current_candle.low)

        if current_position is not None:
            # ── 포지션 보유 중: 스탑로스 + 시그널 청산 체크 ──

            entry_price = current_position.entry_price
            side = current_position.side

            # 트레일링 스탑 업데이트 (고점/저점 추적)
            if side == "buy":
                if trailing_high is None or current_high > trailing_high:
                    trailing_high = current_high
            else:
                if trailing_high is None or current_low < trailing_high:
                    trailing_high = current_low

            # 스탑로스 히트 체크 (캔들 내 저가/고가 기준)
            stop_hit = False
            if stop_loss_price is not None:
                if side == "buy" and current_low <= stop_loss_price:
                    stop_hit = True
                elif side == "sell" and current_high >= stop_loss_price:
                    stop_hit = True

            if stop_hit:
                exit_price = _apply_slippage(
                    stop_loss_price, "sell" if side == "buy" else "buy",
                    config.slippage_pct,
                )
                _close_position(
                    current_position, exit_price, current_candle,
                    "stop_loss", config.fee_pct, capital,
                )
                capital += current_position.pnl_jpy or 0
                trades.append(current_position)
                current_position = None
                stop_loss_price = None
                stop_tightened = False
                trailing_high = None
                continue

            # 시그널 기반 청산 판단
            sig = compute_trend_signal(
                window, params,
                entry_price=entry_price,
                side=side,
            )

            exit_signal = sig.get("exit_signal", {})
            action = exit_signal.get("action", "hold")

            # exit_warning: 가격이 EMA 아래 → 청산
            if sig["signal"] == "exit_warning":
                exit_price = _apply_slippage(
                    current_price, "sell" if side == "buy" else "buy",
                    config.slippage_pct,
                )
                _close_position(
                    current_position, exit_price, current_candle,
                    "exit_warning_ema", config.fee_pct, capital,
                )
                capital += current_position.pnl_jpy or 0
                trades.append(current_position)
                current_position = None
                stop_loss_price = None
                stop_tightened = False
                trailing_high = None
                continue

            # full_exit: EMA 기울기 음전환 or RSI 붕괴
            if action == "full_exit":
                exit_price = _apply_slippage(
                    current_price, "sell" if side == "buy" else "buy",
                    config.slippage_pct,
                )
                _close_position(
                    current_position, exit_price, current_candle,
                    exit_signal.get("reason", "full_exit"),
                    config.fee_pct, capital,
                )
                capital += current_position.pnl_jpy or 0
                trades.append(current_position)
                current_position = None
                stop_loss_price = None
                stop_tightened = False
                trailing_high = None
                continue

            # tighten_stop: 스탑 타이트닝
            if action == "tighten_stop" and not stop_tightened:
                adjusted = exit_signal.get("adjusted_trailing_stop")
                if adjusted and stop_loss_price is not None:
                    if side == "buy" and adjusted > stop_loss_price:
                        stop_loss_price = adjusted
                        stop_tightened = True
                    elif side == "sell" and adjusted < stop_loss_price:
                        stop_loss_price = adjusted
                        stop_tightened = True

            # 트레일링 스탑 라쳇업 (ATR 기반)
            atr = sig.get("atr")
            if atr and trailing_high is not None and stop_loss_price is not None:
                from core.strategy.signals import compute_adaptive_trailing_mult
                trail_mult = compute_adaptive_trailing_mult(
                    sig.get("ema_slope_pct"), sig.get("rsi"), params
                )
                if side == "buy":
                    new_stop = trailing_high - atr * trail_mult
                    if new_stop > stop_loss_price:
                        stop_loss_price = round(new_stop, 6)
                else:
                    new_stop = trailing_high + atr * trail_mult
                    if new_stop < stop_loss_price:
                        stop_loss_price = round(new_stop, 6)

        else:
            # ── 포지션 없음: 진입 시그널 체크 ──
            sig = compute_trend_signal(window, params)

            if sig["signal"] in ("entry_ok", "entry_sell"):
                side = "buy" if sig["signal"] == "entry_ok" else "sell"
                entry_price = _apply_slippage(
                    current_price, side, config.slippage_pct
                )

                # 투입 금액 계산
                invest_jpy = capital * position_size / 100
                entry_fee = _apply_fee(invest_jpy, config.fee_pct)
                invest_after_fee = invest_jpy - entry_fee
                amount = invest_after_fee / entry_price if entry_price > 0 else 0

                current_position = BacktestTrade(
                    entry_time=_candle_time(current_candle),
                    entry_price=entry_price,
                    side=side,
                    amount=amount,
                )

                # 스탑로스 설정
                stop_loss_price = sig.get("stop_loss_price")
                stop_tightened = False
                trailing_high = current_price

    # 미종료 포지션 → 마지막 캔들 종가로 강제 청산
    if current_position is not None:
        last_candle = candles[-1]
        exit_price = float(last_candle.close)
        _close_position(
            current_position, exit_price, last_candle,
            "backtest_end", config.fee_pct, capital,
        )
        capital += current_position.pnl_jpy or 0
        trades.append(current_position)

    # ── 결과 집계 ──
    result.trades = trades
    result.total_trades = len(trades)
    valid = [t for t in trades if t.pnl_jpy is not None]
    wins = [t for t in valid if t.pnl_jpy > 0]
    losses_list = [t for t in valid if t.pnl_jpy <= 0]
    result.wins = len(wins)
    result.losses = len(losses_list)
    result.win_rate = round(len(wins) / len(valid) * 100, 1) if valid else None

    pnl_pcts = [t.pnl_pct for t in valid if t.pnl_pct is not None]
    result.total_return_pct = round(sum(pnl_pcts), 2) if pnl_pcts else None
    result.total_pnl_jpy = round(sum(t.pnl_jpy for t in valid if t.pnl_jpy), 2)

    # Max drawdown
    result.max_drawdown_pct = _compute_max_drawdown_from_trades(pnl_pcts)

    # Sharpe
    if len(pnl_pcts) >= 2:
        mean_pct = sum(pnl_pcts) / len(pnl_pcts)
        variance = sum((x - mean_pct) ** 2 for x in pnl_pcts) / (len(pnl_pcts) - 1)
        std = math.sqrt(variance)
        result.sharpe_ratio = round(mean_pct / std, 2) if std > 0 else None

    # Avg holding hours
    holding_hours = []
    for t in valid:
        if t.entry_time and t.exit_time:
            diff = (t.exit_time - t.entry_time).total_seconds() / 3600
            if diff > 0:
                holding_hours.append(diff)
    result.avg_holding_hours = (
        round(sum(holding_hours) / len(holding_hours), 1) if holding_hours else None
    )

    # Monthly
    result.monthly = _compute_monthly_from_trades(valid)

    return result


def _close_position(
    position: BacktestTrade,
    exit_price: float,
    exit_candle: Any,
    reason: str,
    fee_pct: float,
    capital: float,
) -> None:
    """포지션 종료 처리 (in-place mutation)."""
    position.exit_price = exit_price
    position.exit_time = _candle_time(exit_candle)
    position.exit_reason = reason

    if position.side == "buy":
        gross_pnl = (exit_price - position.entry_price) * position.amount
    else:
        gross_pnl = (position.entry_price - exit_price) * position.amount

    exit_fee = _apply_fee(abs(exit_price * position.amount), fee_pct)
    net_pnl = gross_pnl - exit_fee

    position.pnl_jpy = round(net_pnl, 2)
    invest_jpy = position.entry_price * position.amount
    position.pnl_pct = round(net_pnl / invest_jpy * 100, 4) if invest_jpy > 0 else 0.0


def _candle_time(candle: Any) -> datetime:
    """캔들 타임스탬프 추출."""
    t = getattr(candle, "open_time", None) or getattr(candle, "close_time", None)
    return t if t else datetime.min


def _candle_time_str(candle: Any) -> Optional[str]:
    t = _candle_time(candle)
    return t.isoformat() if t != datetime.min else None


def _compute_max_drawdown_from_trades(pnl_pcts: List[float]) -> Optional[float]:
    """누적 PnL% 기준 최대 드로다운."""
    if not pnl_pcts:
        return None
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pct in pnl_pcts:
        cumulative += pct
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2) if max_dd > 0 else 0.0


def _compute_monthly_from_trades(trades: List[BacktestTrade]) -> List[dict]:
    """월별 성과 집계."""
    monthly_map: Dict[str, list] = {}
    for t in trades:
        if not t.exit_time or t.pnl_pct is None:
            continue
        key = t.exit_time.strftime("%Y-%m")
        monthly_map.setdefault(key, []).append(t.pnl_pct)

    result = []
    for month in sorted(monthly_map.keys()):
        pcts = monthly_map[month]
        result.append({
            "month": month,
            "trades": len(pcts),
            "return_pct": round(sum(pcts), 2),
        })
    return result


# ──────────────────────────────────────────────────────────────
# 파라미터 그리드 서치
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


def run_grid_search(
    candles: List[Any],
    base_params: dict,
    param_grid: Dict[str, List[Any]],
    config: Optional[BacktestConfig] = None,
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


def _run_box_backtest(
    candles: List[Any],
    params: dict,
    config: BacktestConfig,
) -> BacktestResult:
    """
    박스역추세 백테스트.
    params 키:
      tolerance_pct   (default 0.5)  — 박스 클러스터 허용 오차
      min_touches     (default 3)    — 최소 터치 횟수
      box_window      (default 40)   — 박스 감지 윈도우 캔들 수
      position_size_pct (default 100)
      stop_loss_pct   (default 0.5)  — 박스 이탈 시 스탑 (박스 폭 배수)
      take_profit_pct (default 0.8)  — 반대 벽 도달 비율 (0~1, 1=반대 벽 전부)
    """
    tolerance_pct = float(params.get("tolerance_pct", 0.5))
    min_touches = int(params.get("min_touches", 3))
    box_window = int(params.get("box_window", 40))
    position_size = float(params.get("position_size_pct", config.position_size_pct))
    stop_loss_pct = float(params.get("stop_loss_pct", 0.5))
    take_profit_pct = float(params.get("take_profit_pct", 0.8))

    min_candles = max(box_window, 10)
    if len(candles) < min_candles:
        return BacktestResult(candle_count=len(candles), params_used=params)

    capital = config.initial_capital_jpy
    trades: List[BacktestTrade] = []
    current_position: Optional[BacktestTrade] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    active_box = None  # {"upper": float, "lower": float}

    result = BacktestResult(
        params_used=params,
        candle_count=len(candles),
        period_start=_candle_time_str(candles[0]),
        period_end=_candle_time_str(candles[-1]),
    )

    for i in range(min_candles, len(candles)):
        window = candles[max(0, i - box_window):i]
        current_candle = candles[i]
        current_price = float(current_candle.close)
        current_high = float(current_candle.high)
        current_low = float(current_candle.low)

        if current_position is not None:
            # ── 포지션 보유: 스탑로스 / 목표가 체크 ──
            side = current_position.side
            stop_hit = False
            tp_hit = False

            if stop_loss_price is not None:
                if side == "buy" and current_low <= stop_loss_price:
                    stop_hit = True
                elif side == "sell" and current_high >= stop_loss_price:
                    stop_hit = True

            if take_profit_price is not None:
                if side == "buy" and current_high >= take_profit_price:
                    tp_hit = True
                elif side == "sell" and current_low <= take_profit_price:
                    tp_hit = True

            if stop_hit or tp_hit:
                exit_reason = "take_profit" if tp_hit else "stop_loss"
                if tp_hit:
                    exit_price = _apply_slippage(
                        take_profit_price, "sell" if side == "buy" else "buy",
                        config.slippage_pct,
                    )
                else:
                    exit_price = _apply_slippage(
                        stop_loss_price, "sell" if side == "buy" else "buy",
                        config.slippage_pct,
                    )
                _close_position(
                    current_position, exit_price, current_candle,
                    exit_reason, config.fee_pct, capital,
                )
                capital += current_position.pnl_jpy or 0
                trades.append(current_position)
                current_position = None
                stop_loss_price = None
                take_profit_price = None
                active_box = None

        else:
            # ── 포지션 없음: 박스 감지 + 진입 ──
            highs = [float(c.high) for c in window]
            lows = [float(c.low) for c in window]
            box = detect_box(highs, lows, tolerance_pct=tolerance_pct, min_touches=min_touches)

            if not box.box_detected:
                continue

            upper = box.upper_bound
            lower = box.lower_bound
            box_width = upper - lower

            # 하단 근처 → 매수 진입
            lower_entry_zone = lower * (1 + tolerance_pct / 100)
            upper_entry_zone = upper * (1 - tolerance_pct / 100)

            side = None
            if current_price <= lower_entry_zone:
                side = "buy"
            elif current_price >= upper_entry_zone:
                side = "sell"

            if side is None:
                continue

            entry_price = _apply_slippage(current_price, side, config.slippage_pct)
            invest_jpy = capital * position_size / 100
            entry_fee = _apply_fee(invest_jpy, config.fee_pct)
            invest_after_fee = invest_jpy - entry_fee
            amount = invest_after_fee / entry_price if entry_price > 0 else 0

            current_position = BacktestTrade(
                entry_time=_candle_time(current_candle),
                entry_price=entry_price,
                side=side,
                amount=amount,
            )
            active_box = {"upper": upper, "lower": lower}

            # 스탑로스: 박스 이탈 기준
            if side == "buy":
                stop_loss_price = lower - box_width * stop_loss_pct
                take_profit_price = lower + (upper - lower) * take_profit_pct
            else:
                stop_loss_price = upper + box_width * stop_loss_pct
                take_profit_price = upper - (upper - lower) * take_profit_pct

    # 미종료 포지션 강제 청산
    if current_position is not None:
        last_candle = candles[-1]
        exit_price = float(last_candle.close)
        _close_position(
            current_position, exit_price, last_candle,
            "backtest_end", config.fee_pct, capital,
        )
        capital += current_position.pnl_jpy or 0
        trades.append(current_position)

    # 결과 집계 (trend_following과 동일 로직)
    result.trades = trades
    result.total_trades = len(trades)
    valid = [t for t in trades if t.pnl_jpy is not None]
    wins = [t for t in valid if t.pnl_jpy > 0]
    losses_list = [t for t in valid if t.pnl_jpy <= 0]
    result.wins = len(wins)
    result.losses = len(losses_list)
    result.win_rate = round(len(wins) / len(valid) * 100, 1) if valid else None

    pnl_pcts = [t.pnl_pct for t in valid if t.pnl_pct is not None]
    result.total_return_pct = round(sum(pnl_pcts), 2) if pnl_pcts else None
    result.total_pnl_jpy = round(sum(t.pnl_jpy for t in valid if t.pnl_jpy), 2)
    result.max_drawdown_pct = _compute_max_drawdown_from_trades(pnl_pcts)

    if len(pnl_pcts) >= 2:
        mean_pct = sum(pnl_pcts) / len(pnl_pcts)
        variance = sum((x - mean_pct) ** 2 for x in pnl_pcts) / (len(pnl_pcts) - 1)
        std = math.sqrt(variance)
        result.sharpe_ratio = round(mean_pct / std, 2) if std > 0 else None

    holding_hours = []
    for t in valid:
        if t.entry_time and t.exit_time:
            diff = (t.exit_time - t.entry_time).total_seconds() / 3600
            if diff > 0:
                holding_hours.append(diff)
    result.avg_holding_hours = (
        round(sum(holding_hours) / len(holding_hours), 1) if holding_hours else None
    )

    return result


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
