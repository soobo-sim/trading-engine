"""
JIT Advisory 컨텍스트 빌더.

SignalSnapshot + Decision → JITAdvisoryRequest 변환.
LLM에 보낼 컨텍스트를 최대한 풍부하게, 빠르게 구성한다.
외부 API 호출 없이 이미 계산된 데이터만 사용한다 (지연 최소화).
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from core.shared.data.dto import Decision, SignalSnapshot

from .models import JITAdvisoryRequest

logger = logging.getLogger(__name__)


def build_jit_request(
    snapshot: SignalSnapshot,
    decision: Decision,
) -> JITAdvisoryRequest:
    """SignalSnapshot + Decision → JITAdvisoryRequest.

    외부 IO 없음. 이미 존재하는 스냅샷 데이터만 참조.
    매크로 데이터(snapshot.macro)가 있으면 추출, 없으면 None.
    """
    request_id = uuid.uuid4().hex[:12]

    # ── 포지션 컨텍스트 ──────────────────────────────────────
    pos = snapshot.position
    has_pos = pos is not None
    pos_side = None
    pos_entry = None
    pos_pnl_jpy = None
    pos_pnl_pct = None
    pos_pyramid = None
    pos_total_size = None

    if has_pos and pos is not None:
        pos_side = getattr(pos, "side", None)
        pos_entry = getattr(pos, "entry_price", None)
        pos_pnl_jpy = getattr(pos, "unrealized_pnl_jpy", None)
        pos_size = getattr(pos, "size_pct", None)
        pos_pyramid = getattr(pos, "pyramid_count", None)
        pos_total_size = pos_size

        if pos_entry and snapshot.current_price and pos_entry > 0:
            if pos_side == "long":
                pos_pnl_pct = (snapshot.current_price - pos_entry) / pos_entry * 100
            elif pos_side == "short":
                pos_pnl_pct = (pos_entry - snapshot.current_price) / pos_entry * 100

    # ── 매크로 컨텍스트 ──────────────────────────────────────
    macro = snapshot.macro
    macro_fng = None
    macro_news = None
    macro_high_impact = False
    macro_vix = None
    macro_dxy = None

    if macro is not None:
        macro_fng = getattr(macro, "fear_greed_index", None)
        macro_news = getattr(macro, "news_summary", None)
        macro_high_impact = bool(getattr(macro, "high_impact_event_in_6h", False))
        macro_vix = getattr(macro, "vix", None)
        macro_dxy = getattr(macro, "dxy", None)

    # ── 박스 컨텍스트 ────────────────────────────────────────
    box_pos_label = None
    box_upper = None
    box_lower = None
    params = snapshot.params or {}

    if snapshot.strategy_type == "box_mean_reversion":
        box_pos_label = params.get("box_position")
        box_upper = params.get("box_upper")
        box_lower = params.get("box_lower")

    # ── BB/RSI/ATR 추출 ──────────────────────────────────────
    # params 에 있으면 우선 사용, 없으면 스냅샷 필드
    bb_width_pct = float(params.get("bb_width_pct", 0.0))
    range_pct = float(params.get("range_pct", 0.0))
    consecutive_count = int(params.get("consecutive_count", 0))

    atr = snapshot.atr or 0.0
    current_price = snapshot.current_price or 1.0
    atr_pct = (atr / current_price * 100) if current_price > 0 else 0.0

    # ── 안전장치 컨텍스트 ────────────────────────────────────
    kill_count = int(params.get("kill_active_count", 0))
    consec_losses = int(params.get("recent_consecutive_losses", 0))
    win_rate_30d = params.get("recent_win_rate_30d")
    ev_30d = params.get("recent_ev_30d_jpy")

    return JITAdvisoryRequest(
        request_id=request_id,
        pair=snapshot.pair,
        exchange=snapshot.exchange,
        trading_style=snapshot.strategy_type,
        proposed_action=decision.action,

        rule_signal=snapshot.signal,
        rule_confidence=decision.confidence,
        rule_size_pct=decision.size_pct,
        rule_reasoning=decision.reasoning,
        rule_stop_loss=decision.stop_loss,
        rule_take_profit=decision.take_profit,

        current_price=current_price,
        timeframe=params.get("timeframe", "4h"),
        regime=snapshot.regime or "uncertain",
        bb_width_pct=bb_width_pct,
        range_pct=range_pct,
        consecutive_count=consecutive_count,

        ema_value=snapshot.ema or 0.0,
        ema_slope_pct=snapshot.ema_slope_pct or 0.0,
        rsi=snapshot.rsi or 50.0,
        atr=atr,
        atr_pct=atr_pct,

        box_position=box_pos_label,
        box_upper=box_upper,
        box_lower=box_lower,

        has_position=has_pos,
        position_side=pos_side,
        position_entry_price=pos_entry,
        position_pnl_jpy=pos_pnl_jpy,
        position_pnl_pct=pos_pnl_pct,
        position_pyramid_count=pos_pyramid,
        position_total_size_pct=pos_total_size,

        macro_fng=macro_fng,
        macro_news_summary=macro_news,
        macro_high_impact_event_in_6h=macro_high_impact,
        macro_vix=macro_vix,
        macro_dxy=macro_dxy,

        kill_active_count=kill_count,
        recent_consecutive_losses=consec_losses,
        recent_win_rate_30d=float(win_rate_30d) if win_rate_30d is not None else None,
        recent_ev_30d_jpy=float(ev_30d) if ev_30d is not None else None,
    )
