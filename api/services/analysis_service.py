"""
Analysis Service — 분석 비즈니스 로직.

라우트(api/routes/analysis.py)에서 추출. 읽기 전용 쿼리 + 집계 로직.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def compute_bb_width(closes: List[float], period: int = 20) -> float:
    """Bollinger Band 폭 (%) — 횡보/추세 판단 핵심 지표."""
    if len(closes) < period:
        period = len(closes)
    if period < 2:
        return 0.0
    window = closes[-period:]
    sma = sum(window) / period
    if sma == 0:
        return 0.0
    variance = sum((c - sma) ** 2 for c in window) / period
    std = variance ** 0.5
    return (4 * std) / sma * 100


def compute_atr_pct(candles: list, period: int = 14) -> float:
    """ATR % — 변동성 수준 측정."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i].high)
        l = float(candles[i].low)
        prev_c = float(candles[i - 1].close)
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    atr_window = trs[-period:] if len(trs) >= period else trs
    atr = sum(atr_window) / len(atr_window)
    last_close = float(candles[-1].close)
    return (atr / last_close * 100) if last_close > 0 else 0.0


def compute_ema(closes: List[float], period: int) -> Optional[float]:
    """종가 리스트에서 EMA(period) 계산. 데이터 부족 시 None."""
    if len(closes) < period + 1:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema


# ──────────────────────────────────────────────────────────────────────────────
# 1. 박스 이력 + 포지션 성과 집계
# ──────────────────────────────────────────────────────────────────────────────

def _aggregate_trend_positions(trend_closed: list) -> dict:
    """추세추종 포지션 집계 공통 로직."""
    trend_valid = [p for p in trend_closed if p.realized_pnl_jpy is not None]
    trend_wins = [p for p in trend_valid if float(p.realized_pnl_jpy) > 0]
    trend_losses = [p for p in trend_valid if float(p.realized_pnl_jpy) <= 0]
    trend_unknown = len(trend_closed) - len(trend_valid)
    trend_pnl_jpy = sum(float(p.realized_pnl_jpy) for p in trend_valid)
    trend_exit_reasons: Dict[str, int] = {}
    for p in trend_closed:
        if p.exit_reason:
            trend_exit_reasons[p.exit_reason] = trend_exit_reasons.get(p.exit_reason, 0) + 1
    return {
        "total": len(trend_closed),
        "valid_trades": len(trend_valid),
        "wins": len(trend_wins),
        "losses": len(trend_losses),
        "unknown": trend_unknown,
        "win_rate": round(len(trend_wins) / len(trend_valid) * 100, 1) if trend_valid else None,
        "total_pnl_jpy": round(trend_pnl_jpy, 2),
        "exit_reason_distribution": trend_exit_reasons,
    }


async def _fetch_trend_closed(db: AsyncSession, TrendPos, pair: str, since: datetime) -> list:
    """기간 내 청산된 추세추종 포지션 조회."""
    result = await db.execute(
        select(TrendPos)
        .where(
            and_(
                TrendPos.pair == pair,
                TrendPos.status == "closed",
                TrendPos.closed_at >= since,
            )
        )
        .order_by(TrendPos.closed_at)
    )
    return result.scalars().all()


async def get_box_history(
    pair: str, days: int, state: AppState, db: AsyncSession,
) -> dict:
    """박스 이력 + 각 박스 포지션 성과 + 추세추종 포지션 별도 집계."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    BoxModel = state.models.box
    BoxPos = state.models.box_position
    TrendPos = state.models.trend_position
    pair_col = getattr(BoxModel, state.pair_column)

    # 기간 내 박스 조회
    boxes_result = await db.execute(
        select(BoxModel)
        .where(and_(pair_col == pair, BoxModel.created_at >= since))
        .order_by(BoxModel.created_at.desc())
    )
    boxes = boxes_result.scalars().all()

    trend_closed = await _fetch_trend_closed(db, TrendPos, pair, since)
    trend_agg = _aggregate_trend_positions(trend_closed)

    if not boxes:
        return {
            "success": True,
            "pair": pair,
            "days": days,
            "boxes": [],
            "trend_positions": trend_agg,
            "summary": {
                "total_boxes": 0,
                "active_boxes": 0,
                "invalidated_boxes": 0,
                "total_positions": trend_agg["total"],
                "closed_positions": trend_agg["total"],
                "valid_trades": trend_agg["valid_trades"],
                "wins": trend_agg["wins"],
                "losses": trend_agg["losses"],
                "unknown": trend_agg["unknown"],
                "win_rate": trend_agg["win_rate"],
                "avg_pnl_pct": None,
                "total_pnl_jpy": trend_agg["total_pnl_jpy"],
            },
        }

    box_ids = [b.id for b in boxes]

    # 박스 포지션 조회
    positions_result = await db.execute(
        select(BoxPos)
        .where(BoxPos.box_id.in_(box_ids))
        .order_by(BoxPos.created_at)
    )
    positions = positions_result.scalars().all()

    # 박스별 포지션 그룹화
    pos_by_box: Dict[int, list] = {}
    for pos in positions:
        pos_by_box.setdefault(pos.box_id, []).append(pos)

    # 박스별 집계
    box_list = []
    total_pos = 0
    total_closed = 0
    total_valid = 0
    total_wins = 0
    total_losses = 0
    total_unknown = 0
    total_pnl_jpy = 0.0
    total_pnl_pct_sum = 0.0
    exit_reason_agg: Dict[str, int] = {}

    for box in boxes:
        box_positions = pos_by_box.get(box.id, [])
        closed_pos = [p for p in box_positions if p.status == "closed"]
        valid_pos = [p for p in closed_pos if p.realized_pnl_jpy is not None]
        wins = [p for p in valid_pos if float(p.realized_pnl_jpy) > 0]
        pos_losses = [p for p in valid_pos if float(p.realized_pnl_jpy) <= 0]
        pos_unknown = len(closed_pos) - len(valid_pos)
        pnl_jpy = sum(float(p.realized_pnl_jpy) for p in valid_pos)
        pnl_pct_sum = sum(float(p.realized_pnl_pct) for p in valid_pos if p.realized_pnl_pct is not None)

        exit_reasons: Dict[str, int] = {}
        for p in closed_pos:
            if p.exit_reason:
                exit_reasons[p.exit_reason] = exit_reasons.get(p.exit_reason, 0) + 1
                exit_reason_agg[p.exit_reason] = exit_reason_agg.get(p.exit_reason, 0) + 1

        total_pos += len(box_positions)
        total_closed += len(closed_pos)
        total_valid += len(valid_pos)
        total_wins += len(wins)
        total_losses += len(pos_losses)
        total_unknown += pos_unknown
        total_pnl_jpy += pnl_jpy
        total_pnl_pct_sum += pnl_pct_sum

        box_list.append({
            "id": box.id,
            "upper_bound": float(box.upper_bound),
            "lower_bound": float(box.lower_bound),
            "upper_touch_count": box.upper_touch_count,
            "lower_touch_count": box.lower_touch_count,
            "tolerance_pct": float(box.tolerance_pct),
            "status": box.status,
            "invalidation_reason": box.invalidation_reason,
            "created_at": box.created_at.isoformat() if box.created_at else None,
            "invalidated_at": box.invalidated_at.isoformat() if box.invalidated_at else None,
            "duration_hours": round(
                (
                    (box.invalidated_at or datetime.now(timezone.utc).replace(tzinfo=None if box.created_at.tzinfo is None else timezone.utc)) - box.created_at
                ).total_seconds() / 3600, 1
            ) if box.created_at else None,
            "positions": {
                "total": len(box_positions),
                "closed": len(closed_pos),
                "open": len([p for p in box_positions if p.status == "open"]),
                "valid_trades": len(valid_pos),
                "wins": len(wins),
                "losses": len(pos_losses),
                "unknown": pos_unknown,
                "win_rate": round(len(wins) / len(valid_pos) * 100, 1) if valid_pos else None,
                "avg_pnl_pct": round(pnl_pct_sum / len(valid_pos), 4) if valid_pos else None,
                "total_pnl_jpy": round(pnl_jpy, 2),
                "exit_reasons": exit_reasons,
            },
        })

    active_count = sum(1 for b in boxes if b.status == "active")

    return {
        "success": True,
        "pair": pair,
        "days": days,
        "boxes": box_list,
        "trend_positions": trend_agg,
        "summary": {
            "total_boxes": len(boxes),
            "active_boxes": active_count,
            "invalidated_boxes": len(boxes) - active_count,
            "total_positions": total_pos + trend_agg["total"],
            "closed_positions": total_closed + trend_agg["total"],
            "valid_trades": total_valid + trend_agg["valid_trades"],
            "wins": total_wins + trend_agg["wins"],
            "losses": total_losses + trend_agg["losses"],
            "unknown": total_unknown + trend_agg["unknown"],
            "win_rate": round(
                (total_wins + trend_agg["wins"]) / (total_valid + trend_agg["valid_trades"]) * 100, 1
            ) if (total_valid + trend_agg["valid_trades"]) > 0 else None,
            "avg_pnl_pct": round(total_pnl_pct_sum / total_valid, 4) if total_valid > 0 else None,
            "total_pnl_jpy": round(total_pnl_jpy + trend_agg["total_pnl_jpy"], 2),
            "exit_reason_distribution": {
                **exit_reason_agg,
                **{f"trend:{k}": v for k, v in trend_agg["exit_reason_distribution"].items()},
            },
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# 2. 기간별 거래 통계
# ──────────────────────────────────────────────────────────────────────────────

async def get_trade_stats(
    pair: str, days: int, state: AppState, db: AsyncSession,
) -> dict:
    """기간별 거래 통계 (승률, 기대값, 연속 손실 등).

    _trades 테이블 + _trend_positions 테이블을 모두 집계한다.
    BF의 경우 bf_trades가 비어있고 bf_trend_positions에 실거래 이력이 있으므로
    양쪽 모두 조회하여 합산한다 (BUG-027).
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    TradeModel = state.models.trade
    TrendPosModel = state.models.trend_position
    StrategyModel = state.models.strategy
    # Trade 모델의 pair 속성은 항상 `pair` (DB 컬럼명은 거래소별로 다르지만 ORM 속성은 통일). BUG-027
    pair_col = TradeModel.pair

    trades_result = await db.execute(
        select(TradeModel)
        .where(and_(pair_col == pair, TradeModel.created_at >= since))
        .order_by(TradeModel.created_at.asc())
    )
    trades = trades_result.scalars().all()

    trend_result = await db.execute(
        select(TrendPosModel)
        .where(
            and_(
                TrendPosModel.pair == pair,
                TrendPosModel.status == "closed",
                TrendPosModel.closed_at >= since,
            )
        )
        .order_by(TrendPosModel.closed_at.asc())
    )
    trend_positions = trend_result.scalars().all()

    sell_trades = [t for t in trades if t.order_type in ("sell", "market_sell")]
    buy_trades = [t for t in trades if t.order_type in ("buy", "market_buy")]

    total = len(sell_trades)
    wins = 0
    losses = 0
    total_pnl = 0.0
    total_pnl_pct = 0.0
    max_consecutive_losses = 0
    current_consecutive_losses = 0
    pnl_list: List[float] = []

    for t in sell_trades:
        pnl = float(t.pnl_jpy) if t.pnl_jpy is not None else None
        if pnl is not None:
            pnl_list.append(pnl)
            total_pnl += pnl
            if pnl > 0:
                wins += 1
                current_consecutive_losses = 0
            else:
                losses += 1
                current_consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, current_consecutive_losses)
            if t.pnl_pct is not None:
                total_pnl_pct += float(t.pnl_pct)

    # trend_positions 병합: _trades에서 집계한 결과에 추가
    for pos in trend_positions:
        pnl = float(pos.realized_pnl_jpy) if pos.realized_pnl_jpy is not None else None
        if pnl is not None:
            pnl_list.append(pnl)
            total_pnl += pnl
            total += 1
            if pnl > 0:
                wins += 1
                current_consecutive_losses = 0
            else:
                losses += 1
                current_consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, current_consecutive_losses)
            if pos.realized_pnl_pct is not None:
                total_pnl_pct += float(pos.realized_pnl_pct)

    valid_count = wins + losses
    win_rate = round(wins / valid_count * 100, 2) if valid_count > 0 else None
    avg_pnl = round(total_pnl / valid_count, 2) if valid_count > 0 else None
    avg_pnl_pct = round(total_pnl_pct / valid_count, 4) if valid_count > 0 else None

    avg_win = round(sum(p for p in pnl_list if p > 0) / wins, 2) if wins > 0 else None
    avg_loss = round(sum(p for p in pnl_list if p <= 0) / losses, 2) if losses > 0 else None
    expected_value = round(
        (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss), 2
    ) if (win_rate is not None and avg_win is not None and avg_loss is not None) else None

    # 전략별 통계
    strat_result = await db.execute(select(StrategyModel))
    strategies = {s.id: s for s in strat_result.scalars().all()}

    by_strategy: Dict[str, dict] = {}
    for t in sell_trades:
        sid = t.strategy_id
        sname = strategies[sid].name if sid and sid in strategies else "unknown"
        if sname not in by_strategy:
            by_strategy[sname] = {"wins": 0, "losses": 0, "pnl_jpy": 0.0, "trades": 0}
        by_strategy[sname]["trades"] += 1
        pnl = float(t.pnl_jpy) if t.pnl_jpy is not None else None
        if pnl is not None:
            by_strategy[sname]["pnl_jpy"] += pnl
            if pnl > 0:
                by_strategy[sname]["wins"] += 1
            else:
                by_strategy[sname]["losses"] += 1
    for pos in trend_positions:
        sid = pos.strategy_id
        sname = strategies[sid].name if sid and sid in strategies else "unknown"
        if sname not in by_strategy:
            by_strategy[sname] = {"wins": 0, "losses": 0, "pnl_jpy": 0.0, "trades": 0}
        by_strategy[sname]["trades"] += 1
        pnl = float(pos.realized_pnl_jpy) if pos.realized_pnl_jpy is not None else None
        if pnl is not None:
            by_strategy[sname]["pnl_jpy"] += pnl
            if pnl > 0:
                by_strategy[sname]["wins"] += 1
            else:
                by_strategy[sname]["losses"] += 1

    for s_data in by_strategy.values():
        sv = s_data["wins"] + s_data["losses"]
        s_data["win_rate"] = round(s_data["wins"] / sv * 100, 2) if sv > 0 else None
        s_data["pnl_jpy"] = round(s_data["pnl_jpy"], 2)

    total_wins_jpy = round(sum(p for p in pnl_list if p > 0), 2)
    total_losses_jpy = round(sum(p for p in pnl_list if p <= 0), 2)

    return {
        "success": True,
        "pair": pair,
        "days": days,
        "total_trades": len(trades) + len(trend_positions),
        "buy_trades": len(buy_trades),
        "sell_trades": total,
        "valid_sell_trades": valid_count,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "avg_pnl_jpy": avg_pnl,
        "avg_pnl_pct": avg_pnl_pct,
        "total_pnl_jpy": round(total_pnl, 2),
        "total_wins_jpy": total_wins_jpy,
        "total_losses_jpy": total_losses_jpy,
        "avg_win_jpy": avg_win,
        "avg_loss_jpy": avg_loss,
        "expected_value_jpy": expected_value,
        "max_consecutive_losses": max_consecutive_losses,
        "by_strategy": by_strategy,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 3. 시장 체제 판단
# ──────────────────────────────────────────────────────────────────────────────

async def get_market_regime(
    pair: str, timeframe: str, lookback: int, state: AppState, db: AsyncSession,
) -> dict:
    """시장 체제 판단 (횡보/추세)."""
    CandleModel = state.models.candle
    pair_col = getattr(CandleModel, state.pair_column)

    result = await db.execute(
        select(CandleModel)
        .where(
            and_(
                pair_col == pair,
                CandleModel.timeframe == timeframe,
                CandleModel.is_complete == True,
            )
        )
        .order_by(CandleModel.open_time.desc())
        .limit(lookback)
    )
    candles = list(reversed(result.scalars().all()))

    if len(candles) < 10:
        return {
            "success": True,
            "pair": pair,
            "timeframe": timeframe,
            "lookback_requested": lookback,
            "lookback_actual": len(candles),
            "regime": "unclear",
            "confidence": "low",
            "reason": "캔들 데이터 부족 (10개 미만)",
            "metrics": {},
        }

    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]

    bb_width_pct = compute_bb_width(closes, period=min(20, len(closes)))
    atr_pct = compute_atr_pct(candles, period=min(14, len(candles) - 1))

    first_close = closes[0]
    last_close = closes[-1]
    price_change_pct = abs((last_close - first_close) / first_close * 100) if first_close > 0 else 0

    period_high = max(highs)
    period_low = min(lows)
    range_pct = (period_high - period_low) / first_close * 100 if first_close > 0 else 0

    sma_period = min(20, len(closes))
    sma_early = sum(closes[:sma_period]) / sma_period
    sma_late = sum(closes[-sma_period:]) / sma_period
    sma_slope_pct = (sma_late - sma_early) / sma_early * 100 if sma_early > 0 else 0

    ranging_score = 0
    trending_score = 0

    if bb_width_pct < 4.0:
        ranging_score += 2
    elif bb_width_pct >= 6.0:
        trending_score += 2

    if range_pct < 8.0:
        ranging_score += 2
    elif range_pct >= 10.0:
        trending_score += 2

    if atr_pct < 1.5:
        ranging_score += 1
    elif atr_pct >= 2.5:
        trending_score += 1

    if abs(sma_slope_pct) < 2.0:
        ranging_score += 1
    elif abs(sma_slope_pct) >= 4.0:
        trending_score += 1

    if ranging_score >= 4:
        regime = "ranging"
        confidence = "high" if ranging_score >= 5 else "medium"
    elif trending_score >= 4:
        regime = "trending"
        confidence = "high" if trending_score >= 5 else "medium"
    else:
        regime = "unclear"
        confidence = "low"

    strategy_suggestion = {
        "ranging": "박스권 역추세 전략 유효 — 현재 전략 유지 권고",
        "trending": "추세 전략으로 전환 검토 — 박스권 무효화 위험 증가",
        "unclear": "추가 관찰 필요 — 전략 변경 보류",
    }[regime]

    return {
        "success": True,
        "pair": pair,
        "timeframe": timeframe,
        "lookback_requested": lookback,
        "lookback_actual": len(candles),
        "regime": regime,
        "confidence": confidence,
        "strategy_suggestion": strategy_suggestion,
        "metrics": {
            "bb_width_pct": round(bb_width_pct, 3),
            "range_pct": round(range_pct, 3),
            "price_change_pct": round(price_change_pct, 3),
            "atr_pct": round(atr_pct, 3),
            "sma_slope_pct": round(sma_slope_pct, 3),
            "ranging_score": ranging_score,
            "trending_score": trending_score,
        },
        "candle_range": {
            "from": candles[0].open_time.isoformat() if candles else None,
            "to": candles[-1].open_time.isoformat() if candles else None,
            "period_high": round(period_high, 4),
            "period_low": round(period_low, 4),
            "current_close": round(last_close, 4),
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# 4. 추세추종 진입/청산 시그널 종합 판단
# ──────────────────────────────────────────────────────────────────────────────

async def get_trend_signal(
    pair: str,
    timeframe: str,
    ema_period: int,
    atr_period: int,
    rsi_entry_low: float,
    rsi_entry_high: float,
    ema_slope_entry_min: float,
    entry_price: Optional[float],
    state: AppState,
    db: AsyncSession,
) -> dict:
    """추세추종 전략 진입/청산 시그널 종합 판단."""
    CandleModel = state.models.candle
    pair_col = getattr(CandleModel, state.pair_column)

    limit = max(ema_period * 2, atr_period + 1, 60)
    result = await db.execute(
        select(CandleModel)
        .where(
            and_(
                pair_col == pair,
                CandleModel.timeframe == timeframe,
                CandleModel.is_complete == True,
            )
        )
        .order_by(CandleModel.open_time.desc())
        .limit(limit)
    )
    candles = list(reversed(result.scalars().all()))

    min_required = ema_period + 1
    if len(candles) < min_required:
        return {
            "success": True,
            "pair": pair,
            "timeframe": timeframe,
            "signal": "no_signal",
            "reason": f"캔들 데이터 부족 ({len(candles)}개 / 최소 {min_required}개 필요)",
            "indicators": {},
            "conditions": {},
        }

    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    current_price = closes[-1]

    # EMA
    ema = compute_ema(closes, ema_period)
    ema_prev = compute_ema(closes[:-1], ema_period) if len(closes) > ema_period + 1 else None
    ema_slope_pct = round((ema - ema_prev) / ema_prev * 100, 4) if (ema and ema_prev and ema_prev > 0) else None

    # ATR
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i].high)
        l = float(candles[i].low)
        prev_c = float(candles[i - 1].close)
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    atr_window = trs[-atr_period:] if len(trs) >= atr_period else trs
    atr = sum(atr_window) / len(atr_window) if atr_window else None

    # RSI
    rsi_candles = candles[-(14 + 1):]
    rsi = None
    if len(rsi_candles) >= 15:
        rsi_closes = [float(c.close) for c in rsi_candles]
        gains = [max(rsi_closes[i] - rsi_closes[i - 1], 0) for i in range(1, len(rsi_closes))]
        rsi_losses = [max(rsi_closes[i - 1] - rsi_closes[i], 0) for i in range(1, len(rsi_closes))]
        avg_gain = sum(gains) / 14
        avg_loss = sum(rsi_losses) / 14
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = round(100 - (100 / (1 + rs)), 2)

    # 조건 평가
    price_above_ema = (current_price > ema) if ema else None
    ema_slope_positive = (ema_slope_pct >= ema_slope_entry_min) if ema_slope_pct is not None else None
    rsi_in_entry_range = (rsi_entry_low <= rsi <= rsi_entry_high) if rsi is not None else None
    rsi_overbought = (rsi > rsi_entry_high) if rsi is not None else None

    # Regime (간이)
    bb_width_pct = compute_bb_width(closes, period=min(20, len(closes)))
    atr_pct_val = compute_atr_pct(candles, period=min(14, len(candles) - 1))
    first_close = closes[0]
    period_high = max(highs)
    period_low = min(lows)
    range_pct = (period_high - period_low) / first_close * 100 if first_close > 0 else 0
    regime_trending = bb_width_pct >= 6.0 or range_pct >= 10.0
    regime_ranging = bb_width_pct < 3.0 and range_pct < 5.0

    # 시그널 결정
    if price_above_ema is False:
        signal = "exit_warning"
        reason = "현재가가 EMA 아래 이탈 — 포지션 보유 중이라면 청산 검토"
    elif price_above_ema and ema_slope_positive and rsi_in_entry_range and not regime_ranging:
        signal = "entry_ok"
        reason = "모든 진입 조건 충족 — EMA 위, EMA 기울기 양수, RSI 눌림목, 체제 trending/unclear"
    elif price_above_ema and ema_slope_positive and rsi_overbought:
        signal = "wait_dip"
        reason = f"RSI {rsi} 과매수 — 눌림목(RSI {rsi_entry_low}~{rsi_entry_high}) 대기"
    elif price_above_ema and ema_slope_positive and regime_ranging:
        signal = "wait_regime"
        reason = "EMA 조건 충족이나 명확한 횡보(ranging) 체제 — BB폭<3% AND 가격범위<5%"
    else:
        signal = "no_signal"
        reason = "진입 조건 미충족"

    # exit_signal
    exit_params = {
        "rsi_overbought": 75, "rsi_extreme": 80, "rsi_breakdown": 40,
        "ema_slope_weak_threshold": 0.03,
        "partial_exit_profit_atr": 2.0, "partial_exit_profit_pct": 30,
        "partial_exit_rsi_pct": 50, "tighten_stop_atr": 1.0,
        "atr_multiplier_stop": 2.0,
    }
    from core.strategy.signals import compute_exit_signal
    exit_signal = compute_exit_signal(
        ema_slope_pct=ema_slope_pct, rsi=rsi, atr=atr,
        current_price=current_price, entry_price=entry_price, params=exit_params,
    )
    stop_loss_price = round(current_price - atr * exit_params["atr_multiplier_stop"], 6) if atr else None
    trailing_stop_distance = round(atr * 1.5, 6) if atr else None

    return {
        "success": True,
        "pair": pair,
        "timeframe": timeframe,
        "signal": signal,
        "reason": reason,
        # BUG-015: 주요 필드를 최상위에도 노출
        "current_price": round(current_price, 6),
        "ema20": round(ema, 6) if ema else None,
        "ema_slope_pct": ema_slope_pct,
        "rsi14": rsi,
        "atr": round(atr, 6) if atr else None,
        "trailing_stop_distance": trailing_stop_distance,
        "indicators": {
            "current_price": round(current_price, 6),
            f"ema{ema_period}": round(ema, 6) if ema else None,
            "ema_slope_pct": ema_slope_pct,
            "atr": round(atr, 6) if atr else None,
            "atr_pct": round(atr / current_price * 100, 4) if (atr and current_price > 0) else None,
            "rsi14": rsi,
            "bb_width_pct": round(bb_width_pct, 3),
            "regime": "trending" if regime_trending else ("ranging" if regime_ranging else "unclear"),
        },
        "conditions": {
            "price_above_ema": price_above_ema,
            "ema_slope_positive": ema_slope_positive,
            "rsi_in_entry_range": rsi_in_entry_range,
            "rsi_entry_range": f"{rsi_entry_low}~{rsi_entry_high}",
            "regime_trending": regime_trending,
            "regime_ranging": regime_ranging,
        },
        "levels": {
            f"ema{ema_period}": round(ema, 6) if ema else None,
            "stop_loss_price": stop_loss_price,
            "trailing_stop_distance": trailing_stop_distance,
        },
        "candle_count": len(candles),
        "exit_signal": {
            "action": exit_signal["action"],
            "reason": exit_signal["reason"],
            "triggers": exit_signal["triggers"],
            "adjusted_trailing_stop": exit_signal["adjusted_trailing_stop"],
        },
    }
