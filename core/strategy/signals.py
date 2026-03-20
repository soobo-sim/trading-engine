"""
Technical indicator signal calculations — shared across all traders.

Functions
---------
compute_ema                   : Exponential Moving Average
compute_exit_signal           : Priority-based exit signal (trend strategy)
compute_adaptive_trailing_mult: Dynamic trailing stop ATR multiplier (EMA slope + RSI)
compute_trend_signal          : Full trend entry/exit signal from candle list

These functions are exchange-agnostic and operate on primitive types or
duck-typed candle objects (must have .close, .high, .low attributes).
"""
from typing import Any, List, Optional


def compute_ema(prices: List[float], period: int) -> Optional[float]:
    """
    EMA (지수이동평균) 계산.

    Args:
        prices: 종가 목록 (오래된 것부터 최신 순)
        period: EMA 기간

    Returns:
        EMA 값, 데이터 부족 시 None
    """
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def compute_rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """
    각 캔들 위치의 RSI 값 시리즈 계산.

    compute_trend_signal의 단일 RSI 계산과 동일한 단순 평균 방식(Wilder's 미적용).
    다이버전스 감지(Phase 3)에서 피봇 고점별 RSI 比較에 사용.

    Args:
        closes: 종가 목록 (오래된 것부터 최신 순)
        period: RSI 기간 (기본 14)

    Returns:
        각 위치의 RSI 값 목록. period 미만 위치는 None.
    """
    result: List[Optional[float]] = []
    for i in range(len(closes)):
        if i < period:
            result.append(None)
            continue
        window = closes[i - period: i + 1]  # period+1개 종가 → period개 변화
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
    """
    포지션 보유 중 우선순위 기반 청산 시그널 판단.
    전량 청산 > 부분 청산 > 스탑 타이트닝 > hold 순.
    signal == 'exit_warning' (가격 vs EMA) 청산은 이 함수 밖에서 별도 처리.

    Args:
        ema_slope_pct:  EMA 기울기 (%)
        rsi:            RSI 값
        atr:            ATR 값
        current_price:  현재가
        entry_price:    진입가 (이익 목표 판단용)
        params:         전략 파라미터 dict
        side:           포지션 방향 ("buy" or "sell")

    Returns:
        dict: {action, reason, triggers, adjusted_trailing_stop}
    """
    rsi_overbought_th = params.get("rsi_overbought", 75)
    rsi_extreme_th = params.get("rsi_extreme", 80)
    rsi_breakdown_th = params.get("rsi_breakdown", 40)
    slope_weak_th = params.get("ema_slope_weak_threshold", 0.03)
    profit_atr_mult = params.get("partial_exit_profit_atr", 2.0)
    tighten_atr = params.get("tighten_stop_atr", 1.0)

    is_short = side == "sell"

    # 롱: slope < 0 = 추세 반전, 숏: slope > 0 = 추세 반전
    ema_slope_reversal = (
        ema_slope_pct is not None
        and (ema_slope_pct > 0 if is_short else ema_slope_pct < 0)
    )
    ema_slope_weakening = (
        ema_slope_pct is not None
        and ((-slope_weak_th < ema_slope_pct <= 0) if is_short else (0 <= ema_slope_pct < slope_weak_th))
    )
    # 롱: RSI < breakdown = 붕괴, 숏: RSI > extreme = 매수 압력
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
    # 부분 청산 제거 (2026-03-16, Rachel 재설계)
    # RSI 극단/이익목표/과매수/기울기둔화 → 모두 스탑 타이트닝으로 통합
    # "Let winners run" 원칙 — 포지션 유지, 스탑만 조인다.
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
    """
    추세 상태에 따른 트레일링 스탑 ATR 배수 동적 조정.

    추세 상태:
      초기/가속  : EMA 기울기 양호, RSI 중립 → ATR × trailing_stop_atr_initial (기본 2.0, 넓게)
      성숙/과열  : 기울기 둔화 또는 RSI > rsi_overbought → ATR × trailing_stop_atr_mature (기본 1.2, 좁게)

    명시적 스탑 타이트닝(_stop_tightened=True 상태)은 이 함수가 아닌
    tighten_stop_atr 파라미터로 별도 처리된다.

    Args:
        ema_slope_pct:  EMA 기울기 (%)
        rsi:            RSI 값
        params:         전략 파라미터 dict

    Returns:
        float: ATR 배수 (trailing_stop_atr_initial 또는 trailing_stop_atr_mature)
    """
    slope_mature_th: float = float(params.get("ema_slope_weak_threshold", 0.05))
    rsi_mature_th: float = float(params.get("rsi_overbought", 75))

    is_mature = (
        (ema_slope_pct is not None and ema_slope_pct < slope_mature_th)
        or (rsi is not None and rsi > rsi_mature_th)
    )
    if is_mature:
        return float(params.get("trailing_stop_atr_mature", 1.2))
    return float(params.get("trailing_stop_atr_initial", 2.0))


def compute_trend_signal(
    candles: List[Any],
    params: Optional[dict] = None,
    entry_price: Optional[float] = None,
    side: Optional[str] = None,
) -> dict:
    """
    캔들 목록에서 트렌드 시그널 계산.

    캔들 객체는 .close / .high / .low 속성을 가져야 한다 (duck typing).
    CkCandle, BfCandle 모두 호환.

    시그널 종류:
      entry_ok     — EMA 위, 기울기 양수, RSI 진입 범위 (롱 진입)
      entry_sell   — EMA 아래, 기울기 음수, RSI 진입 범위 (숏 진입)
      exit_warning — 가격 vs EMA 이탈 (하드 청산 트리거)
      wait_dip     — EMA 위, 기울기 양수이나 RSI 과매수
      wait_regime  — EMA 위, 기울기 양수이나 횡보 레짐
      no_signal    — 기타

    Args:
        candles:     캔들 목록 (오래된 것부터)
        params:      전략 파라미터 dict — None이면 기본값 사용
        entry_price: 현재 포지션 진입가 (이익 목표 / exit_signal 계산용)

    Returns:
        dict: {signal, current_price, ema, ema_slope_pct, atr, stop_loss_price, rsi, exit_signal}
    """
    if params is None:
        params = {}

    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    current_price = closes[-1]

    ema_period, atr_period, rsi_period = 20, 14, 14

    # EMA 및 기울기
    ema = compute_ema(closes, ema_period)
    ema_prev = compute_ema(closes[:-1], ema_period) if len(closes) > ema_period + 1 else None
    ema_slope_pct = (
        (ema - ema_prev) / ema_prev * 100
        if (ema and ema_prev and ema_prev > 0)
        else None
    )

    # ATR
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i].high)
        lo = float(candles[i].low)
        pc = float(candles[i - 1].close)
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    atr_window = trs[-atr_period:] if len(trs) >= atr_period else trs
    atr = sum(atr_window) / len(atr_window) if atr_window else None

    # RSI (시리즈 계산으로 통일 — 다이버전스 감지(Phase 3)에도 재사용)
    rsi_series = compute_rsi_series(closes, period=rsi_period)
    rsi = rsi_series[-1] if rsi_series else None

    # 조건
    rsi_entry_low = float(params.get("entry_rsi_min", 40.0))
    rsi_entry_high = float(params.get("entry_rsi_max", 65.0))
    price_above_ema = (current_price > ema) if ema else None
    ema_slope_positive = (ema_slope_pct > 0) if ema_slope_pct is not None else None
    ema_slope_negative = (ema_slope_pct < 0) if ema_slope_pct is not None else None
    rsi_in_range = (rsi_entry_low <= rsi <= rsi_entry_high) if rsi is not None else None
    rsi_overbought = (rsi > rsi_entry_high) if rsi is not None else None

    # 숏 진입 조건
    short_rsi_low = float(params.get("entry_rsi_min_short", 35.0))
    short_rsi_high = float(params.get("entry_rsi_max_short", 60.0))
    short_slope_th = float(params.get("ema_slope_short_threshold", -0.05))
    rsi_in_short_range = (short_rsi_low <= rsi <= short_rsi_high) if rsi is not None else None
    ema_slope_strong_down = (ema_slope_pct is not None and ema_slope_pct < short_slope_th)

    # Regime (BB width + 가격 레인지)
    bb_period = min(20, len(closes))
    bb_window = closes[-bb_period:]
    sma = sum(bb_window) / bb_period if bb_period > 0 else 0
    std = (sum((c - sma) ** 2 for c in bb_window) / bb_period) ** 0.5 if sma > 0 else 0
    bb_width_pct = (4 * std) / sma * 100 if sma > 0 else 0
    range_pct = (max(highs) - min(lows)) / closes[0] * 100 if closes[0] > 0 else 0
    regime_trending = bb_width_pct >= 6.0 or range_pct >= 10.0
    # ranging = BB폭 < 3% AND 가격범위 < 5% → 명확한 횡보, 진입 차단
    # unclear = trending/ranging 중간 → 진입 허용 (EMA+RSI 필터가 충분)
    regime_ranging = bb_width_pct < 3.0 and range_pct < 5.0

    # 시그널 결정
    if price_above_ema and ema_slope_positive and rsi_in_range and not regime_ranging:
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
        "regime": "trending" if regime_trending else ("ranging" if regime_ranging else "unclear"),
        "exit_signal": exit_signal,
    }


def find_pivot_highs(
    candles: List[Any],
    rsi_values: Optional[List[Optional[float]]] = None,
    left: int = 2,
    right: int = 2,
) -> List[dict]:
    """
    캔들 목록에서 로컬 고점(피봇 하이) 목록 반환.

    candles[i].high가 좌측 left개, 우측 right개 캔들의 high보다 모두 크면 피봇 고점으로 판정.
    반환 dict에는 price, candle_index가 항상 포함되고,
    rsi_values가 주어지면 rsi가 포함됨 (None인 위치는 피봇 제외),
    캔들에 volume 속성이 있으면 volume이 포함됨.

    Args:
        candles:    캔들 목록 (오래된 것부터)
        rsi_values: 캔들별 RSI 시리즈 (compute_rsi_series 반환값, candles와 동일 길이).
                    None이면 RSI 필터링 체크 안 함.
        left:       좌측 비교 캔들 수 (기본 2)
        right:      우측 비교 캔들 수 (기본 2)

    Returns:
        list of dicts: {"candle_index": int, "price": float, "rsi"?: float, "volume"?: float}
    """
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
    """
    베어리시 다이버전스 감지 (가격 신고가 + RSI 고점 하락 = 에너지 소진 신호).

    Phase 3 구현. 두 피봇 고점을 비교:
      1. 가격 고점B > 가격 고점A  (신고가)
      2. RSI 고점B < RSI 고점A - min_gap  (에너지 감소, 노이즈 필터 포함)
      3. 두 피봇 간 거리 <= max_pivot_distance  (너무 먼 과거 비교 방지)

    divergence_enabled=False 이면 즉시 detected=False 반환.

    Args:
        candles:    캔들 목록 (오래된 것부터)
        rsi_values: 캔들별 RSI 시리즈 (candles와 동일 길이)
        params:     전략 파라미터. 사용 키:
                    divergence_enabled (bool, 기본 True)
                    pivot_left / pivot_right (int, 기본 2)
                    rsi_divergence_min_gap (float, 기본 3.0)
                    max_pivot_distance (int, 기본 15)
                    divergence_lookback (int, 기본 40)

    Returns:
        dict: {
            "detected": bool,
            "pivot_a": {"price": float, "rsi": float, "candle_index": int} | None,
            "pivot_b": {"price": float, "rsi": float, "candle_index": int} | None,
            "rsi_gap": float | None,
            "candle_distance": int | None,
        }
    """
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

    # lookback 윈도우로 제한
    window_candles = candles[-lookback:] if len(candles) > lookback else candles
    window_rsi = rsi_values[-lookback:] if len(rsi_values) > lookback else rsi_values

    pivot_highs = find_pivot_highs(window_candles, window_rsi, left=left, right=right)

    if len(pivot_highs) < 2:
        return empty

    a = pivot_highs[-2]  # 이전 피봇 고점
    b = pivot_highs[-1]  # 최신 피봇 고점

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
    """
    RSI 다이버전스 + 볼륨 다이버전스를 동시 판정 (통합, Phase 3 권장).

    피봇을 1회만 계산하고 RSI + 볼륨을 함께 비교 — 코드 중복 없이 두 신호 수신.

    왔녕 신호: 두 다이버전스가 동시 발동되면 고점 확률 매우 높음.

    Args:
        candles:    캔들 목록 (오래된 것부터)
        rsi_values: 캔들별 RSI 시리즈
        params:     전략 파라미터. 사용 키:
                    divergence_enabled (bool, 기본 True)
                    volume_divergence_enabled (bool, 기본 True)
                    pivot_left / pivot_right (int, 기본 2)
                    rsi_divergence_min_gap (float, 기본 3.0)
                    volume_divergence_min_drop (float, 기본 0.15  = 15%)
                    max_pivot_distance (int, 기본 15)
                    divergence_lookback (int, 기본 40)

    Returns:
        dict: {
            "rsi_divergence": bool,
            "volume_divergence": bool,
            "both": bool,
            "pivot_a": dict | None,
            "pivot_b": dict | None,
            "rsi_gap": float | None,
            "volume_drop_pct": float | None,   # % 감소율 (e.g. 20.5)
            "candle_distance": int | None,
        }
    """
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

    # RSI None 필터링을 포함하여 피봇 산출 (볼륨도 함께)
    pivot_highs = find_pivot_highs(window_candles, window_rsi, left=left, right=right)

    if len(pivot_highs) < 2:
        return empty

    a = pivot_highs[-2]
    b = pivot_highs[-1]

    price_higher = b["price"] > a["price"]
    distance_ok = (b["candle_index"] - a["candle_index"]) <= max_dist

    # RSI 다이버전스
    rsi_div = (
        price_higher and distance_ok
        and "rsi" in a and "rsi" in b
        and (a["rsi"] - b["rsi"]) >= rsi_min_gap
    )

    # 볼륨 다이버전스
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
