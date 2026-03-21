"""
Analysis API — Rachel (전략 분석 에이전트) 전용 읽기 엔드포인트.

GET /api/analysis/box-history    — 박스 이력 + 포지션 성과 집계
GET /api/analysis/trade-stats    — 기간별 거래 통계 (승률, 기대값)
GET /api/analysis/regime         — 시장 체제 판단 (횡보/추세)
GET /api/analysis/trend-signal   — 추세추종 진입/청산 시그널 종합 판단

모든 엔드포인트는 읽기 전용. 트레이딩 로직에 영향 없음.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState, get_db, get_state

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/analysis", tags=["Analysis (Rachel)"])


def _compute_bb_width(closes: List[float], period: int = 20) -> float:
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


def _compute_atr_pct(candles: list, period: int = 14) -> float:
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


def _compute_ema(closes: List[float], period: int) -> Optional[float]:
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

@router.get("/box-history", summary="박스 이력 + 포지션 성과 집계")
async def get_box_history(
    pair: str = Query(..., description="페어 (e.g. xrp_jpy / XRP_JPY)"),
    days: int = Query(30, ge=1, le=365, description="조회 기간 (일)"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
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

    if not boxes:
        # 박스 없어도 추세추종 포지션은 조회
        trend_result = await db.execute(
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
        trend_closed = trend_result.scalars().all()
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
            "success": True,
            "pair": pair,
            "days": days,
            "boxes": [],
            "trend_positions": {
                "total": len(trend_closed),
                "valid_trades": len(trend_valid),
                "wins": len(trend_wins),
                "losses": len(trend_losses),
                "unknown": trend_unknown,
                "win_rate": round(len(trend_wins) / len(trend_valid) * 100, 1) if trend_valid else None,
                "total_pnl_jpy": round(trend_pnl_jpy, 2),
                "exit_reason_distribution": trend_exit_reasons,
            },
            "summary": {
                "total_boxes": 0,
                "active_boxes": 0,
                "invalidated_boxes": 0,
                "total_positions": len(trend_closed),
                "closed_positions": len(trend_closed),
                "valid_trades": len(trend_valid),
                "wins": len(trend_wins),
                "losses": len(trend_losses),
                "unknown": trend_unknown,
                "win_rate": round(len(trend_wins) / len(trend_valid) * 100, 1) if trend_valid else None,
                "avg_pnl_pct": None,
                "total_pnl_jpy": round(trend_pnl_jpy, 2),
            },
        }

    box_ids = [b.id for b in boxes]

    # 박스 포지션 조회
    bp_pair_col = getattr(BoxPos, state.pair_column)
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

    # 추세추종 포지션 (박스에 속하지 않는 별도 통계)
    trend_result = await db.execute(
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
    trend_closed = trend_result.scalars().all()
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
        "success": True,
        "pair": pair,
        "days": days,
        "boxes": box_list,
        "trend_positions": {
            "total": len(trend_closed),
            "valid_trades": len(trend_valid),
            "wins": len(trend_wins),
            "losses": len(trend_losses),
            "unknown": trend_unknown,
            "win_rate": round(len(trend_wins) / len(trend_valid) * 100, 1) if trend_valid else None,
            "total_pnl_jpy": round(trend_pnl_jpy, 2),
            "exit_reason_distribution": trend_exit_reasons,
        },
        "summary": {
            "total_boxes": len(boxes),
            "active_boxes": active_count,
            "invalidated_boxes": len(boxes) - active_count,
            "total_positions": total_pos + len(trend_closed),
            "closed_positions": total_closed + len(trend_closed),
            "valid_trades": total_valid + len(trend_valid),
            "wins": total_wins + len(trend_wins),
            "losses": total_losses + len(trend_losses),
            "unknown": total_unknown + trend_unknown,
            "win_rate": round(
                (total_wins + len(trend_wins)) / (total_valid + len(trend_valid)) * 100, 1
            ) if (total_valid + len(trend_valid)) > 0 else None,
            "avg_pnl_pct": round(total_pnl_pct_sum / total_valid, 4) if total_valid > 0 else None,
            "total_pnl_jpy": round(total_pnl_jpy + trend_pnl_jpy, 2),
            "exit_reason_distribution": {
                **exit_reason_agg,
                **{f"trend:{k}": v for k, v in trend_exit_reasons.items()},
            },
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# 2. 기간별 거래 통계 (승률, 기대값)
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/trade-stats", summary="기간별 거래 통계 (승률, 기대값)")
async def get_trade_stats(
    pair: str = Query(..., description="페어 (e.g. xrp_jpy / XRP_JPY)"),
    period: str = Query("weekly", description="기간: daily | weekly | monthly | all"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """박스 + 추세추종 포지션 통합 거래 통계."""
    periods = {"daily": 1, "weekly": 7, "monthly": 30, "all": 3650}
    if period not in periods:
        raise HTTPException(400, {"blocked_code": "INVALID_PERIOD", "valid": list(periods.keys())})

    since = datetime.now(timezone.utc) - timedelta(days=periods[period])

    BoxPos = state.models.box_position
    TrendPos = state.models.trend_position
    bp_pair_col = getattr(BoxPos, state.pair_column)

    # 박스 포지션
    box_result = await db.execute(
        select(BoxPos)
        .where(
            and_(
                bp_pair_col == pair,
                BoxPos.status == "closed",
                BoxPos.closed_at >= since,
            )
        )
        .order_by(BoxPos.closed_at)
    )
    box_positions = box_result.scalars().all()

    # 추세추종 포지션
    trend_result = await db.execute(
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
    trend_positions = trend_result.scalars().all()

    # 통합 (closed_at 기준 정렬)
    all_positions = sorted(
        list(box_positions) + list(trend_positions),
        key=lambda p: p.closed_at or datetime.min.replace(tzinfo=timezone.utc),
    )

    if not all_positions:
        return {
            "success": True,
            "pair": pair,
            "period": period,
            "since": since.isoformat(),
            "stats": {
                "total_trades": 0,
                "valid_trades": 0,
                "wins": 0,
                "losses": 0,
                "unknown": 0,
                "win_rate": None,
                "avg_win_pct": None,
                "avg_loss_pct": None,
                "expected_value_pct": None,
                "total_pnl_jpy": None,
                "max_consecutive_losses": 0,
                "exit_reason_distribution": {},
                "by_strategy": {},
            },
        }

    wins = [p for p in all_positions if p.realized_pnl_jpy is not None and float(p.realized_pnl_jpy) > 0]
    losses = [p for p in all_positions if p.realized_pnl_jpy is not None and float(p.realized_pnl_jpy) <= 0]

    total = len(all_positions)
    win_count = len(wins)
    loss_count = len(losses)
    valid_count = win_count + loss_count
    unknown_count = total - valid_count
    win_rate = win_count / valid_count if valid_count > 0 else 0

    avg_win_pct = (
        sum(float(p.realized_pnl_pct) for p in wins if p.realized_pnl_pct) / win_count
        if win_count > 0 else None
    )
    avg_loss_pct = (
        sum(float(p.realized_pnl_pct) for p in losses if p.realized_pnl_pct) / loss_count
        if loss_count > 0 else None
    )

    ev = None
    if avg_win_pct is not None and avg_loss_pct is not None:
        ev = round(win_rate * avg_win_pct + (1 - win_rate) * avg_loss_pct, 4)

    max_consec_loss = 0
    cur_consec = 0
    for p in all_positions:
        if p.realized_pnl_jpy is not None and float(p.realized_pnl_jpy) <= 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        elif p.realized_pnl_jpy is not None:
            cur_consec = 0
        # PnL unknown: don't affect streak

    exit_reasons: Dict[str, int] = {}
    for p in all_positions:
        if p.exit_reason:
            exit_reasons[p.exit_reason] = exit_reasons.get(p.exit_reason, 0) + 1

    total_pnl = sum(float(p.realized_pnl_jpy) for p in all_positions if p.realized_pnl_jpy)

    # 전략별 내역
    by_strategy: Dict[str, dict] = {}
    for label, pos_list in [("box_mean_reversion", box_positions), ("trend_following", trend_positions)]:
        closed = [p for p in pos_list if p.status == "closed"]
        if not closed:
            continue
        s_valid = [p for p in closed if p.realized_pnl_jpy is not None]
        s_wins = [p for p in s_valid if float(p.realized_pnl_jpy) > 0]
        s_losses = [p for p in s_valid if float(p.realized_pnl_jpy) <= 0]
        s_unknown = len(closed) - len(s_valid)
        s_pnl = sum(float(p.realized_pnl_jpy) for p in s_valid)
        by_strategy[label] = {
            "trades": len(closed),
            "valid_trades": len(s_valid),
            "wins": len(s_wins),
            "losses": len(s_losses),
            "unknown": s_unknown,
            "win_rate": round(len(s_wins) / len(s_valid) * 100, 1) if s_valid else 0.0,
            "total_pnl_jpy": round(s_pnl, 2),
        }

    return {
        "success": True,
        "pair": pair,
        "period": period,
        "since": since.isoformat(),
        "stats": {
            "total_trades": total,
            "valid_trades": valid_count,
            "wins": win_count,
            "losses": loss_count,
            "unknown": unknown_count,
            "win_rate": round(win_rate * 100, 1),
            "avg_win_pct": round(avg_win_pct, 4) if avg_win_pct is not None else None,
            "avg_loss_pct": round(avg_loss_pct, 4) if avg_loss_pct is not None else None,
            "expected_value_pct": ev,
            "total_pnl_jpy": round(total_pnl, 2),
            "max_consecutive_losses": max_consec_loss,
            "exit_reason_distribution": exit_reasons,
            "by_strategy": by_strategy,
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# 3. 시장 체제 판단 (횡보 / 추세)
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/regime", summary="시장 체제 판단 (횡보/추세)")
async def get_market_regime(
    pair: str = Query(..., description="페어 (e.g. xrp_jpy / XRP_JPY)"),
    timeframe: str = Query("4h", description="캔들 타임프레임: 1h | 4h"),
    lookback: int = Query(60, ge=20, le=200, description="분석할 캔들 수"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """완성 캔들 기반 시장 체제 판단 (ranging / trending / unclear)."""
    if timeframe not in ("1h", "4h"):
        raise HTTPException(400, {"blocked_code": "INVALID_TIMEFRAME"})

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

    bb_width_pct = _compute_bb_width(closes, period=min(20, len(closes)))
    atr_pct = _compute_atr_pct(candles, period=min(14, len(candles) - 1))

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

@router.get("/trend-signal", summary="추세추종 진입/청산 시그널")
async def get_trend_signal(
    pair: str = Query(..., description="페어 (e.g. xrp_jpy / XRP_JPY)"),
    timeframe: str = Query("4h", description="캔들 타임프레임: 1h | 4h"),
    ema_period: int = Query(20, ge=5, le=200, description="EMA 기간"),
    atr_period: int = Query(14, ge=5, le=100, description="ATR 기간"),
    rsi_entry_low: float = Query(40.0, ge=20.0, le=60.0, description="RSI 진입 하한"),
    rsi_entry_high: float = Query(65.0, ge=50.0, le=80.0, description="RSI 진입 상한"),
    ema_slope_entry_min: float = Query(0.0, ge=-0.5, le=0.5, description="EMA slope 진입 최소 임곗값(%)"),
    entry_price: Optional[float] = Query(None, description="현재 포지션 진입가 (청산 시그널 판단용)"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """추세추종 전략 진입/청산 시그널 종합 판단."""
    if timeframe not in ("1h", "4h"):
        raise HTTPException(400, {"blocked_code": "INVALID_TIMEFRAME"})

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
    ema = _compute_ema(closes, ema_period)
    ema_prev = _compute_ema(closes[:-1], ema_period) if len(closes) > ema_period + 1 else None
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
    bb_width_pct = _compute_bb_width(closes, period=min(20, len(closes)))
    atr_pct = _compute_atr_pct(candles, period=min(14, len(candles) - 1))
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
