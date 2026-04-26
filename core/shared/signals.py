"""
Technical indicator signal calculations — shared across all traders.

Functions
---------
compute_ema                   : Exponential Moving Average
compute_exit_signal           : Priority-based exit signal (trend strategy)
compute_adaptive_trailing_mult: Dynamic trailing stop ATR multiplier (EMA slope + RSI)
compute_profit_based_mult     : Trailing stop ATR multiplier based on unrealized profit
compute_trend_signal          : Full trend entry/exit signal from candle list

These functions are exchange-agnostic and operate on primitive types or
duck-typed candle objects (must have .close, .high, .low attributes).

Canonical location: core/shared/signals.py
Backward-compat shim at: core/strategy/signals.py
"""
from typing import Any, List, Optional


def compute_ema(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def compute_rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    result: List[Optional[float]] = []
    for i in range(len(closes)):
        if i < period:
            result.append(None)
            continue
        window = closes[i - period: i + 1]
        gains = [max(window[j] - window[j - 1], 0) for j in range(1, len(window))]
        losses = [max(window[j - 1] - window[j], 0) for j in range(1, len(window))]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        rsi = 100.0 if avg_loss == 0 else round(100 - (100 / (1 + avg_gain / avg_loss)), 2)
        result.append(rsi)
    return result


def compute_exit_signal(
    ema_slope_pct: Optional[float],
    rsi: Optional[float],
    atr: Optional[float],
    current_price: float,
    entry_price: Optional[float],
    params: dict,
    side: str = "buy",
) -> dict:
    rsi_overbought_th = params.get("rsi_overbought", 75)
    rsi_extreme_th = params.get("rsi_extreme", 80)
    rsi_breakdown_th = params.get("rsi_breakdown", 40)
    slope_weak_th = params.get("ema_slope_weak_threshold", 0.05)
    profit_atr_mult = params.get("partial_exit_profit_atr", 2.0)
    tighten_atr = params.get("tighten_stop_atr", 1.0)

    is_short = side == "sell"

    ema_slope_reversal = (
        ema_slope_pct is not None
        and (ema_slope_pct > 0 if is_short else ema_slope_pct < 0)
    )
    ema_slope_weakening = (
        ema_slope_pct is not None
        and ((-slope_weak_th < ema_slope_pct <= 0) if is_short else (0 <= ema_slope_pct < slope_weak_th))
    )
    rsi_breakdown_hit = rsi is not None and (rsi > rsi_extreme_th if is_short else rsi < rsi_breakdown_th)
    rsi_extreme_hit = rsi is not None and (rsi < rsi_breakdown_th if is_short else rsi > rsi_extreme_th)
    rsi_overbought_hit = rsi is not None and (
        (rsi_breakdown_th <= rsi < rsi_breakdown_th + 5) if is_short
        else (rsi_overbought_th < rsi <= rsi_extreme_th)
    )
    profit_target_hit = (
        entry_price is not None
        and entry_price > 0
        and atr is not None
        and (
            (entry_price - current_price) > atr * profit_atr_mult if is_short
            else (current_price - entry_price) > atr * profit_atr_mult
        )
    )

    triggers = {
        "ema_slope_negative": ema_slope_reversal,
        "ema_slope_weakening": ema_slope_weakening,
        "rsi_breakdown": rsi_breakdown_hit,
        "rsi_extreme": rsi_extreme_hit,
        "rsi_overbought": rsi_overbought_hit,
        "profit_target_hit": profit_target_hit,
    }

    adjusted_trailing_stop = (
        round(current_price + atr * tighten_atr, 6) if (atr and is_short)
        else round(current_price - atr * tighten_atr, 6) if atr
        else None
    )

    if ema_slope_reversal:
        reason = (
            "EMA 기울기 양전환 — 추세 반전 선제 퇴장" if is_short
            else "EMA 기울기 음전환 — 추세 반전 선제 퇴장"
        )
        return {
            "action": "full_exit",
            "reason": reason,
            "triggers": triggers,
            "adjusted_trailing_stop": adjusted_trailing_stop,
        }
    if rsi_breakdown_hit:
        reason = (
            f"RSI {rsi:.1f} 과매수 급등 — 숏 추세 붕괴 시그널" if is_short
            else f"RSI {rsi:.1f} 과매도 급락 — 추세 붕괴 시그널"
        )
        return {
            "action": "full_exit",
            "reason": reason,
            "triggers": triggers,
            "adjusted_trailing_stop": adjusted_trailing_stop,
        }
    if rsi_extreme_hit or profit_target_hit or rsi_overbought_hit or ema_slope_weakening:
        parts = []
        if rsi_extreme_hit:
            parts.append(f"RSI {rsi:.1f} 극단{'과매도' if is_short else '과매수'}")
        if profit_target_hit:
            parts.append(f"이익 ATR×{profit_atr_mult} 달성")
        if rsi_overbought_hit:
            parts.append(f"RSI {rsi:.1f} {'과매도 접근' if is_short else '과매수'}")
        if ema_slope_weakening:
            parts.append(f"EMA 기울기 {ema_slope_pct:.4f}% 둔화")
        return {
            "action": "tighten_stop",
            "reason": " + ".join(parts) + " → 스탑 타이트닝",
            "triggers": triggers,
            "adjusted_trailing_stop": adjusted_trailing_stop,
        }
    return {
        "action": "hold",
        "reason": "추세 유지 중",
        "triggers": triggers,
        "adjusted_trailing_stop": adjusted_trailing_stop,
    }


def compute_adaptive_trailing_mult(
    ema_slope_pct: Optional[float],
    rsi: Optional[float],
    params: dict,
) -> float:
    slope_mature_th: float = float(params.get("ema_slope_weak_threshold", 0.03))
    rsi_mature_th: float = float(params.get("rsi_overbought", 75))

    is_mature = (
        (ema_slope_pct is not None and ema_slope_pct < slope_mature_th)
        or (rsi is not None and rsi > rsi_mature_th)
    )
    if is_mature:
        return float(params.get("trailing_stop_atr_mature", 1.2))
    return float(params.get("trailing_stop_atr_initial", 1.5))


def compute_profit_based_mult(
    entry_price: float,
    current_price: float,
    atr: float,
    params: dict,
    side: str = "buy",
) -> float:
    initial = float(params.get("trailing_stop_atr_initial", 1.5))
    if atr <= 0 or entry_price <= 0:
        return initial

    is_short = side == "sell"
    unrealized = (entry_price - current_price) if is_short else (current_price - entry_price)

    if unrealized <= 0:
        return initial

    profit_atr_ratio = unrealized / atr
    decay = float(params.get("trailing_stop_decay_per_atr", 0.2))
    min_mult = float(params.get("trailing_stop_atr_min", 0.3))

    return max(min_mult, initial - decay * profit_atr_ratio)


def classify_regime(
    bb_width_pct: float,
    range_pct: float,
    params: Optional[dict] = None,
) -> tuple:
    """체제 판정 순수 함수.

    trending 판정: bb_width_pct 단독 (볼린저밴드 폭은 현재 변동성에 즉응).
    ranging 판정 : bb_width_pct < max AND range_pct < max (둘 다 좁아야 명확 횡보).
    unclear      : 그 사이 전이 구간.

    range_pct_trending_min 파라미터는 trending 판정에 더 이상 사용되지 않는다.
    (range_pct 는 lookback window 내 max-min 기반으로 sticky 특성이 있어
    현재 시장 상태를 즉각 반영하지 못한다 — 2026-04-21 재설계)
    """
    if params is None:
        params = {}
    bb_trending_min = float(params.get("bb_width_trending_min", 3.0))
    bb_ranging_max = float(params.get("bb_width_ranging_max", 3.0))
    range_ranging_max = float(params.get("range_pct_ranging_max", 5.0))

    regime_trending = bb_width_pct >= bb_trending_min
    regime_ranging = bb_width_pct < bb_ranging_max and range_pct < range_ranging_max

    if regime_trending:
        regime = "trending"
    elif regime_ranging:
        regime = "ranging"
    else:
        regime = "unclear"

    return regime, regime_trending, regime_ranging


def compute_trend_signal(
    candles: List[Any],
    params: Optional[dict] = None,
    entry_price: Optional[float] = None,
    side: Optional[str] = None,
) -> dict:
    if params is None:
        params = {}

    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    current_price = closes[-1]

    ema_period = int(params.get("ema_period", 20))
    atr_period = int(params.get("atr_period", 14))
    rsi_period = int(params.get("rsi_period", 14))

    ema = compute_ema(closes, ema_period)
    ema_prev = compute_ema(closes[:-1], ema_period) if len(closes) > ema_period + 1 else None
    ema_slope_pct = (
        (ema - ema_prev) / ema_prev * 100
        if (ema and ema_prev and ema_prev > 0)
        else None
    )

    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i].high)
        lo = float(candles[i].low)
        pc = float(candles[i - 1].close)
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    atr_window = trs[-atr_period:] if len(trs) >= atr_period else trs
    atr = sum(atr_window) / len(atr_window) if atr_window else None

    rsi_series = compute_rsi_series(closes, period=rsi_period)
    rsi = rsi_series[-1] if rsi_series else None

    rsi_entry_low = float(params.get("entry_rsi_min", 40.0))
    rsi_entry_high = float(params.get("entry_rsi_max", 65.0))
    slope_entry_min = float(params.get("ema_slope_entry_min", 0.0))
    price_above_ema = (current_price > ema) if ema else None
    ema_slope_positive = (ema_slope_pct >= slope_entry_min) if ema_slope_pct is not None else None
    ema_slope_negative = (ema_slope_pct < 0) if ema_slope_pct is not None else None
    rsi_in_range = (rsi_entry_low <= rsi <= rsi_entry_high) if rsi is not None else None
    rsi_overbought = (rsi > rsi_entry_high) if rsi is not None else None

    short_rsi_low = float(params.get("entry_rsi_min_short", 35.0))
    short_rsi_high = float(params.get("entry_rsi_max_short", 60.0))
    short_slope_th = float(params.get("ema_slope_short_threshold", -0.05))
    rsi_in_short_range = (short_rsi_low <= rsi <= short_rsi_high) if rsi is not None else None
    ema_slope_strong_down = (ema_slope_pct is not None and ema_slope_pct < short_slope_th)

    bb_period = min(int(params.get("bb_period", ema_period)), len(closes))
    bb_window = closes[-bb_period:]
    sma = sum(bb_window) / bb_period if bb_period > 0 else 0
    std = (sum((c - sma) ** 2 for c in bb_window) / bb_period) ** 0.5 if sma > 0 else 0
    bb_width_pct = (4 * std) / sma * 100 if sma > 0 else 0
    # BUG-040: range_pct도 bb_period 윈도우(최근 N봉)로 통일 — 전체 캔들 사용 시 unclear 과다 발생
    range_pct = (max(highs[-bb_period:]) - min(lows[-bb_period:])) / closes[-bb_period] * 100 if closes[-bb_period] > 0 else 0
    _, regime_trending, regime_ranging = classify_regime(bb_width_pct, range_pct, params)

    # BUG-042: not regime_ranging → regime_trending (unclear 체제 진입 차단)
    if price_above_ema and ema_slope_positive and rsi_in_range and regime_trending:
        signal = "entry_ok"
    elif (
        price_above_ema is False
        and ema_slope_strong_down
        and rsi_in_short_range
        and not regime_ranging
    ):
        signal = "entry_sell"
    elif price_above_ema is False:
        signal = "exit_warning"
    elif price_above_ema and ema_slope_positive and rsi_overbought:
        signal = "wait_dip"
    elif price_above_ema and ema_slope_positive and regime_ranging:
        signal = "wait_regime"
    else:
        signal = "no_signal"

    atr_multiplier = params.get("atr_multiplier_stop", 2.0)
    if side == "sell":
        stop_loss_price = round(current_price + atr * atr_multiplier, 6) if atr else None
    else:
        stop_loss_price = round(current_price - atr * atr_multiplier, 6) if atr else None

    exit_signal = compute_exit_signal(
        ema_slope_pct=ema_slope_pct,
        rsi=rsi,
        atr=atr,
        current_price=current_price,
        entry_price=entry_price,
        params=params,
        side=side or "buy",
    )

    return {
        "signal": signal,
        "current_price": current_price,
        "ema": ema,
        "ema_slope_pct": ema_slope_pct,
        "atr": atr,
        "stop_loss_price": stop_loss_price,
        "rsi": rsi,
        "rsi_series": rsi_series,
        "regime": classify_regime(bb_width_pct, range_pct, params)[0],
        "exit_signal": exit_signal,
        "bb_width_pct": bb_width_pct,
        "range_pct": range_pct,
    }


def find_pivot_highs(
    candles: List[Any],
    rsi_values: Optional[List[Optional[float]]] = None,
    left: int = 2,
    right: int = 2,
) -> List[dict]:
    pivots = []
    for i in range(left, len(candles) - right):
        if rsi_values is not None and rsi_values[i] is None:
            continue
        is_pivot = all(float(candles[i].high) > float(candles[i - j].high) for j in range(1, left + 1))
        if not is_pivot:
            continue
        is_pivot = all(float(candles[i].high) > float(candles[i + j].high) for j in range(1, right + 1))
        if is_pivot:
            entry: dict = {
                "candle_index": i,
                "price": float(candles[i].high),
            }
            if rsi_values is not None:
                entry["rsi"] = float(rsi_values[i])  # type: ignore[index]
            vol = getattr(candles[i], "volume", None)
            if vol is not None:
                entry["volume"] = float(vol)
            pivots.append(entry)
    return pivots


def detect_bearish_divergence(
    candles: List[Any],
    rsi_values: List[Optional[float]],
    params: dict,
) -> dict:
    empty: dict = {
        "detected": False,
        "pivot_a": None, "pivot_b": None,
        "rsi_gap": None, "candle_distance": None,
    }
    if not params.get("divergence_enabled", True):
        return empty

    left = int(params.get("pivot_left", 2))
    right = int(params.get("pivot_right", 2))
    min_gap = float(params.get("rsi_divergence_min_gap", 3.0))
    max_dist = int(params.get("max_pivot_distance", 15))
    lookback = int(params.get("divergence_lookback", 40))

    window_candles = candles[-lookback:] if len(candles) > lookback else candles
    window_rsi = rsi_values[-lookback:] if len(rsi_values) > lookback else rsi_values

    pivot_highs = find_pivot_highs(window_candles, window_rsi, left=left, right=right)

    if len(pivot_highs) < 2:
        return empty

    a = pivot_highs[-2]
    b = pivot_highs[-1]

    price_higher = b["price"] > a["price"]
    rsi_lower = (a["rsi"] - b["rsi"]) >= min_gap
    distance_ok = (b["candle_index"] - a["candle_index"]) <= max_dist

    is_divergence = price_higher and rsi_lower and distance_ok

    return {
        "detected": is_divergence,
        "pivot_a": a if is_divergence else None,
        "pivot_b": b if is_divergence else None,
        "rsi_gap": round(a["rsi"] - b["rsi"], 2) if is_divergence else None,
        "candle_distance": b["candle_index"] - a["candle_index"] if is_divergence else None,
    }


def detect_bearish_divergences(
    candles: List[Any],
    rsi_values: List[Optional[float]],
    params: dict,
) -> dict:
    empty: dict = {
        "rsi_divergence": False, "volume_divergence": False, "both": False,
        "pivot_a": None, "pivot_b": None,
        "rsi_gap": None, "volume_drop_pct": None, "candle_distance": None,
    }
    if not params.get("divergence_enabled", True):
        return empty

    left = int(params.get("pivot_left", 2))
    right = int(params.get("pivot_right", 2))
    max_dist = int(params.get("max_pivot_distance", 15))
    lookback = int(params.get("divergence_lookback", 40))
    rsi_min_gap = float(params.get("rsi_divergence_min_gap", 3.0))
    vol_min_drop = float(params.get("volume_divergence_min_drop", 0.15))
    vol_enabled = bool(params.get("volume_divergence_enabled", True))

    window_candles = candles[-lookback:] if len(candles) > lookback else candles
    window_rsi = rsi_values[-lookback:] if len(rsi_values) > lookback else rsi_values

    pivot_highs = find_pivot_highs(window_candles, window_rsi, left=left, right=right)

    if len(pivot_highs) < 2:
        return empty

    a = pivot_highs[-2]
    b = pivot_highs[-1]

    price_higher = b["price"] > a["price"]
    distance_ok = (b["candle_index"] - a["candle_index"]) <= max_dist

    rsi_div = (
        price_higher and distance_ok
        and "rsi" in a and "rsi" in b
        and (a["rsi"] - b["rsi"]) >= rsi_min_gap
    )

    vol_div = False
    volume_drop_pct = None
    if vol_enabled and price_higher and distance_ok:
        va = a.get("volume")
        vb = b.get("volume")
        if va and vb and va > 0:
            drop = (va - vb) / va
            volume_drop_pct = round(drop * 100, 1)
            vol_div = drop >= vol_min_drop

    pivot_a = a if (rsi_div or vol_div) else None
    pivot_b = b if (rsi_div or vol_div) else None

    return {
        "rsi_divergence": rsi_div,
        "volume_divergence": vol_div,
        "both": rsi_div and vol_div,
        "pivot_a": pivot_a,
        "pivot_b": pivot_b,
        "rsi_gap": round(a["rsi"] - b["rsi"], 2) if rsi_div and "rsi" in a and "rsi" in b else None,
        "volume_drop_pct": volume_drop_pct if vol_div else None,
        "candle_distance": b["candle_index"] - a["candle_index"] if (rsi_div or vol_div) else None,
    }


def compute_candle_limit(params: Optional[dict] = None) -> int:
    """compute_trend_signal에 전달할 캔들 수 계산. 단일 진실 소스.

    _judge_mixin.py, regime_simulator 등이 모두 이 함수를 호출한다.
    """
    p = params or {}
    ema_period = int(p.get("ema_period", 20))
    atr_period = int(p.get("atr_period", 14))
    lookback = int(p.get("divergence_lookback", 40))
    return max(ema_period * 2, atr_period + 1, lookback)
