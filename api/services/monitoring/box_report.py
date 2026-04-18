"""박스 전략 모니터링 리포트 — 텍스트 조립 + 생성."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from .display import JST, get_box_narrative_situation, get_box_narrative_outlook
from .alerts import (
    _prev_raw_cache,
    _last_alert_time,
    _build_test_alert,
    evaluate_alert,
    _trigger_rachel_analysis,
)

logger = logging.getLogger(__name__)

# 박스 age 소프트 경고 임계 (~20일, 4H × 120)
_BOX_AGE_WARNING_CANDLES = 120


def check_box_age_warning(created_at: datetime) -> str | None:
    """박스 생성 후 120캔들(~20일) 경과 시 경고 문자열 반환."""
    # DB에서 naive datetime으로 반환될 수 있으므로 UTC로 통일
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()
    threshold_seconds = _BOX_AGE_WARNING_CANDLES * 4 * 3600
    if age_seconds > threshold_seconds:
        age_days = int(age_seconds / 86400)
        return f"⚠️ 장기 박스 ({age_days}일째)"
    return None



def build_bar_chart(price: float, lower: float, upper: float) -> str:
    """10칸 바 차트로 현재가의 박스 내 위치 시각화."""
    if price < lower:
        return "●[━━━━━━━━━━]"
    if price > upper:
        return "[━━━━━━━━━━]●"
    bar_pos = round((price - lower) / (upper - lower) * 10)
    bar_pos = max(0, min(10, bar_pos))
    return "[" + "━" * bar_pos + "●" + "━" * (10 - bar_pos) + "]"


def build_health_line(health_report: Any) -> str:
    """HealthReport → 한줄 상태 요약."""
    ws = "✅" if health_report.ws_connected else "🔴"

    tasks = health_report.tasks
    alive = sum(1 for t in tasks.values() if t.get("alive"))
    total = len(tasks)
    restarts = sum(t.get("restarts", 0) for t in tasks.values())
    task_icon = "✅" if alive == total else "⚠️"
    restart_text = f"(재시작{restarts})" if restarts > 0 else ""

    balance_issues = health_report.position_balance
    balance_icon = "✅" if not balance_issues else "⚠️"

    prefix = "🟢" if health_report.healthy and not balance_issues else "🚨"

    return f"{prefix} WS{ws} 태스크{alive}/{total}{task_icon}{restart_text} 잔고{balance_icon}"


def get_box_position_label(price: float, lower: float, upper: float, tolerance_pct: float) -> str:
    """현재가의 박스 내 위치 라벨."""
    tol = tolerance_pct / 100.0
    box_range = upper - lower
    if box_range <= 0:
        return "middle"

    if price < lower * (1 - tol) or price > upper * (1 + tol):
        return "outside"
    if abs(price - lower) <= box_range * 0.2:
        return "near_lower"
    if abs(price - upper) <= box_range * 0.2:
        return "near_upper"
    return "middle"


def get_box_entry_blockers(
    box_data: dict | None,
    position_label: str,
    has_position: bool,
) -> list[str]:
    """박스 전략 진입까지 남은 조건 목록. 비어있으면 진입 가능."""
    blockers: list[str] = []
    if not box_data:
        blockers.append("박스 미형성 → 형성 대기")
    elif position_label not in ("near_lower",):
        zone_labels = {
            "near_upper": "상한 근접",
            "middle": "중심부",
            "outside": "박스 밖",
        }
        label = zone_labels.get(position_label, position_label)
        blockers.append(f"가격 {label} → 하한 진입대 대기")
    if has_position:
        blockers.append("포지션 보유 중 → 청산 대기")
    return blockers


def build_box_telegram_text(prefix: str, time_str: str, pair: str, data: dict) -> str:
    lines = [f"[{prefix}] {time_str} | {pair} 📦박스"]
    lines.append(data["health_line"])

    box = data.get("box")
    currency = pair.split("_")[0]
    pos = data.get("position")
    has_position = pos is not None
    is_margin = data.get("is_margin_trading", False)
    current_price = data["current_price"]
    position_label = data.get("position_label", "no_box")

    if box:
        lines.append(f"¥{current_price:,.2f} {position_label} (폭 {box['box_width_pct']:.1f}%)")
        lines.append(f"하단¥{box['lower_bound']:,.2f} {box['bar_chart']} 상단¥{box['upper_bound']:,.2f}")
        if box.get("age_warning"):
            lines.append(f"   {box['age_warning']}")
    else:
        lines.append(f"¥{current_price:,.2f} 📭박스 미형성")
        scan_dt = data.get("next_scan_jst")
        scan_min = data.get("next_scan_minutes_str", "")
        cond_str = data.get("box_conditions_str", "")
        if scan_dt:
            try:
                scan_str = scan_dt.strftime("%-m/%-d %H:%M")
                lines.append(f"   다음 스캔: {scan_str} JST ({scan_min})")
            except Exception:
                pass
        if cond_str:
            lines.append(f"   조건: {cond_str}")
        fp = data.get("formation_progress")
        if fp:
            min_t = fp["min_touches"]
            tf = data.get("basis_timeframe", "4h")
            fail_reason = fp.get("fail_reason", "터치 부족")
            upper_t = fp["upper_touches"]
            lower_t = fp["lower_touches"]

            if fail_reason == "폭 부족" and fp.get("width_pct") is not None:
                width = fp["width_pct"]
                min_w = fp.get("min_width_pct", 0)
                lines.append(
                    f"📊 터치 충분(상{upper_t}/하{lower_t}) but "
                    f"박스 폭 {width:.2f}% < 최소 {min_w:.2f}% → 더 넓은 박스 대기"
                )
            else:
                remaining = fp["candles_remaining"]
                if remaining > 0:
                    lines.append(
                        f"📊 터치 진행: 상단 {upper_t}/{min_t} 하단 {lower_t}/{min_t} — "
                        f"최소 {remaining}봉({tf}) 더 필요"
                    )
                else:
                    lines.append(
                        f"📊 터치 진행: 상단 {upper_t}/{min_t} 하단 {lower_t}/{min_t}"
                    )

    # ── 포지션 유무에 따른 분기 ──
    if has_position:
        entry_price = pos["entry_price"]
        entry_amount = pos["entry_amount"]
        pnl = pos["unrealized_pnl_jpy"]
        pnl_pct = pos["unrealized_pnl_pct"]
        pnl_sign = "+" if pnl >= 0 else ""
        pos_side = pos.get("side", "buy")
        pos_icon = "📈" if pos_side == "buy" else "📉"
        pos_label = "롱" if pos_side == "buy" else "숏"

        lines.append(f"{pos_icon} {pos_label} 보유")
        lines.append(f" · 진입 {entry_amount:.0f}{currency} @ ¥{entry_price:,.2f}")
        lines.append(f" · 미실현 {pnl_sign}¥{pnl:,.0f} ({pnl_sign}{pnl_pct:.2f}%)")

        if box:
            near_bound_pct = float(data.get("near_bound_pct", 1.5))
            tolerance_pct_val = float(data.get("tolerance_pct", 1.5))
            stop_loss_pct = float(data.get("stop_loss_pct", 1.5))
            upper = box["upper_bound"]
            lower_val = box["lower_bound"]

            if pos_side == "buy":
                tp_low = upper * (1 - near_bound_pct / 100)
                tp_high = upper * (1 + near_bound_pct / 100)
                tp_low_pct = (tp_low - current_price) / current_price * 100
                tp_high_pct = (tp_high - current_price) / current_price * 100
                tp_sign_low = "+" if tp_low_pct >= 0 else ""
                tp_sign_high = "+" if tp_high_pct >= 0 else ""
                lines.append(
                    f"🎯 익절: near_upper ¥{tp_low:,.2f}~¥{tp_high:,.2f} "
                    f"(현재가 {tp_sign_low}{tp_low_pct:.1f}%~{tp_sign_high}{tp_high_pct:.1f}%)"
                )
                inv_price = lower_val * (1 - tolerance_pct_val / 100)
                inv_pct = (inv_price - current_price) / current_price * 100
                sl_price = entry_price * (1 - stop_loss_pct / 100)
                lines.append(
                    f"🛑 손절: 박스 무효화 ¥{inv_price:,.2f} (현재가 {inv_pct:.1f}%) / "
                    f"가격SL ¥{sl_price:,.2f} (-{stop_loss_pct:.1f}%)"
                )
            else:
                tp_low = lower_val * (1 - near_bound_pct / 100)
                tp_high = lower_val * (1 + near_bound_pct / 100)
                tp_low_pct = (current_price - tp_high) / current_price * 100
                tp_high_pct = (current_price - tp_low) / current_price * 100
                tp_sign_low = "+" if tp_low_pct >= 0 else ""
                tp_sign_high = "+" if tp_high_pct >= 0 else ""
                lines.append(
                    f"🎯 익절: near_lower ¥{tp_low:,.2f}~¥{tp_high:,.2f} "
                    f"(현재가 {tp_sign_low}{tp_low_pct:.1f}%~{tp_sign_high}{tp_high_pct:.1f}%)"
                )
                inv_price = upper * (1 + tolerance_pct_val / 100)
                inv_pct = (inv_price - current_price) / current_price * 100
                sl_price = entry_price * (1 + stop_loss_pct / 100)
                lines.append(
                    f"🛑 손절: 박스 무효화 ¥{inv_price:,.2f} (현재가 +{inv_pct:.1f}%) / "
                    f"가격SL ¥{sl_price:,.2f} (+{stop_loss_pct:.1f}%)"
                )
            if is_margin:
                exchange_sl_status = pos.get("exchange_sl_status")
                exchange_sl_price = pos.get("exchange_sl_price")
                if exchange_sl_status == "registered" and exchange_sl_price:
                    lines.append(f"🛡️ 거래소SL ¥{exchange_sl_price:,.2f} ✅등록")
                elif exchange_sl_status == "failed":
                    lines.append("🛡️ 거래소SL ❌등록실패")
                else:
                    lines.append("🛡️ 거래소SL ⚠️미등록")

        sit = get_box_narrative_situation(True, position_label, bool(box), pos_side, pnl_pct)
        lines.append(f"📊 지금: {sit}")
        out = get_box_narrative_outlook(True, position_label, pos_side)
        if out:
            lines.append(f"⚡ 전망: {out}")

    else:
        # ── 포지션 없음: 진입 대기 모드 ──
        met = data.get("conditions_met", 0)
        total = data.get("conditions_total", 3)
        entry_blockers = data.get("entry_blockers", [])

        if box:
            if entry_blockers:
                lines.append(f"🚫 {met}/{total} 진입까지:")
                for b in entry_blockers:
                    lines.append(f" · {b}")
            else:
                lines.append(f"✅ {met}/{total} 진입 조건 충족")
        else:
            sit = get_box_narrative_situation(False, "no_box", False)
            lines.append(f"📊 지금: {sit}")

        next_min_str = data.get("next_candle_minutes_str", "")
        tf_label = data.get("basis_timeframe", "4h")
        candle_time = data.get("candle_open_time_jst", "불명")
        if candle_time != "불명":
            suffix = f" ({next_min_str})" if next_min_str else ""
            lines.append(f"⏰ 다음 {tf_label}봉: {candle_time} JST{suffix}")
        else:
            lines.append(f"⏰ 다음 {tf_label}봉: 불명")

    # ── 체제 라인 ──
    _rg = data.get("regime_gate_info")
    if _rg is not None:
        _last = _rg.get("last_regime")
        _cnt = _rg.get("consecutive_count", 0)
        _active = _rg.get("active_strategy")
        _REGIME_LABEL_BOX = {"trending": "추세장", "ranging": "횡보장", "unclear": "불명확"}
        _STRATEGY_LABEL_BOX = {"trend_following": "추세추종", "box_mean_reversion": "박스역추세"}
        _rl = _REGIME_LABEL_BOX.get(_last, _last or "-")
        if _active is not None:
            lines.append(f"⚙️ 체제: {_rl}(×{_cnt}) | 활성: {_STRATEGY_LABEL_BOX.get(_active, _active)}")
        else:
            lines.append(f"⚙️ 체제: {_rl}(×{_cnt}) | 진입 차단 중")

    # ── 잔고 라인 (FX/현물 분기) ──
    jpy_part = f"JPY ¥{data['jpy_available']:,.0f}"
    if is_margin:
        lines.append(f"{jpy_part}" if has_position else f"{jpy_part} | 포지션 미보유")
    else:
        coin_part = f"{currency} {data['coin_available']:.2f}개"
        lines.append(f"{jpy_part} {coin_part}" if has_position else f"{jpy_part} {coin_part} | 포지션 미보유")

    return "\n".join(lines)

def build_box_memory_block(prefix: str, time_str: str, pair: str, data: dict) -> str:
    strategy_name = data.get("strategy_name", "unknown")
    strategy_id = data.get("strategy_id", "?")
    lines = [
        f"## [{time_str} JST] {prefix}: {pair} | 모니터링 | strategy: {strategy_name}(id={strategy_id})",
        "",
        "### 시스템 상태",
        f"- {data['health_line']}",
        "",
        "### 박스 상태",
    ]

    box = data.get("box")
    if box:
        lines.append(
            f"- box_id: {box['id']} | upper: ¥{box['upper_bound']:,.2f}"
            f" | lower: ¥{box['lower_bound']:,.2f} | width: {box['box_width_pct']:.1f}%"
        )
        lines.append(f"- 현재가: ¥{data['current_price']:,.2f} | 위치: {data['position_label']}")
    else:
        lines.append(f"- 박스 미형성 | 현재가: ¥{data['current_price']:,.2f}")

    lines.extend(["", "### 포지션 상태"])
    pos = data.get("position")
    currency = pair.split("_")[0]
    if pos:
        lines.append(
            f"- 보유: {currency} {pos['entry_amount']}개"
            f" @ ¥{pos['entry_price']:,.2f}"
            f" | 미실현 ¥{pos['unrealized_pnl_jpy']:,.0f} ({pos['unrealized_pnl_pct']:.2f}%)"
        )
    else:
        lines.append("- 포지션 없음")

    lines.extend([
        "",
        "### 자산 현황",
        f"- JPY: ¥{data['jpy_available']:,.0f} | {currency}: {data['coin_available']:.4f}개",
        "",
        "### 특이사항",
        "- 없음",
    ])

    return "\n".join(lines)


async def generate_box_report(
    pair: str,
    prefix: str,
    pair_column: str,
    strategy: Any,
    adapter: Any,
    health_checker: Any,
    box_model: Any,
    box_position_model: Any,
    candle_model: Any,
    db: AsyncSession,
    test_alert_level: str | None = None,
    reset_cooldown: bool = False,
    regime_gate: Any = None,
) -> dict:
    """box_mean_reversion 전략의 모니터링 리포트 생성."""
    params = strategy.parameters or {}
    now_jst = datetime.now(JST)
    time_str = now_jst.strftime("%H:%M")
    basis_tf = params.get("basis_timeframe", "4h")

    # 1. 헬스체크
    try:
        health_report = await health_checker.check()
        health_line = build_health_line(health_report)
    except Exception as e:
        logger.warning(f"[BoxReport] 헬스체크 실패: {e}")
        health_line = "⚠️ 헬스 미확인"

    # 2. 현재가 조회
    ticker = await adapter.get_ticker(pair)
    current_price = ticker.last

    # 3. 활성 박스 조회 (active 전략 박스만 — paper 박스 제외)
    pair_col = getattr(box_model, pair_column)
    result = await db.execute(
        select(box_model)
        .where(and_(pair_col == pair, box_model.status == "active", box_model.strategy_id.is_(None)))
        .order_by(desc(box_model.created_at))
        .limit(1)
    )
    box_row = result.scalar_one_or_none()

    box_data = None
    position_label = "no_box"
    formation_progress = None
    next_box_estimate_at = None
    next_scan_jst = None
    next_scan_minutes_str = ""
    box_conditions_str = ""
    tol_str = str(params.get("box_tolerance_pct", 0.5))
    min_t_str = str(params.get("box_min_touches", params.get("min_touches", 3)))
    if box_row:
        upper = float(box_row.upper_bound)
        lower = float(box_row.lower_bound)
        tolerance_pct = float(box_row.tolerance_pct)
        box_width_pct = (upper - lower) / lower * 100 if lower > 0 else 0.0
        position_label = get_box_position_label(current_price, lower, upper, tolerance_pct)
        bar_chart = build_bar_chart(current_price, lower, upper)

        box_data = {
            "id": box_row.id,
            "upper_bound": upper,
            "lower_bound": lower,
            "box_width_pct": round(box_width_pct, 1),
            "status": box_row.status,
            "bar_chart": bar_chart,
            "age_warning": check_box_age_warning(box_row.created_at),
        }
    else:
        # 박스 미형성 시 진행 상황 계산
        try:
            from core.judge.analysis.box_detector import detect_box_progress
            lookback = int(params.get("lookback_candles", 40))
            tol = float(params.get("box_tolerance_pct", 0.5))
            min_t = int(params.get("min_touches", 3))
            candle_pair_col = getattr(candle_model, pair_column)
            prog_result = await db.execute(
                select(candle_model)
                .where(
                    and_(
                        candle_pair_col == pair,
                        candle_model.timeframe == basis_tf,
                        candle_model.is_complete == True,
                    )
                )
                .order_by(candle_model.open_time.desc())
                .limit(lookback)
            )
            prog_candles = list(reversed(prog_result.scalars().all()))
            if prog_candles:
                highs = [max(float(c.open), float(c.close)) for c in prog_candles]
                lows = [min(float(c.open), float(c.close)) for c in prog_candles]
                progress = detect_box_progress(highs, lows, tol, min_t)
                # 박스 폭 + 미형성 사유 계산
                width_pct = None
                min_width_pct = None
                box_fail_reason = "터치 부족"
                if progress.upper_center and progress.lower_center and progress.lower_center > 0:
                    width_pct = round(
                        (progress.upper_center - progress.lower_center)
                        / progress.lower_center * 100, 3
                    )
                    fee_rate = float(params.get("fee_rate_pct", 0.0))
                    min_width_pct = round(tol * 2 + fee_rate * 2, 3)
                    if progress.upper_touches >= min_t and progress.lower_touches >= min_t:
                        box_fail_reason = "폭 부족"
                    else:
                        box_fail_reason = "터치 부족"
                formation_progress = {
                    "upper_touches": progress.upper_touches,
                    "lower_touches": progress.lower_touches,
                    "min_touches": progress.min_touches,
                    "candles_remaining": progress.candles_remaining,
                    "upper_center": progress.upper_center,
                    "lower_center": progress.lower_center,
                    "width_pct": width_pct,
                    "min_width_pct": min_width_pct,
                    "fail_reason": box_fail_reason,
                }
        except Exception as e:
            logger.warning(f"[BoxReport] 형성 진행 계산 실패: {e}")

        # 다음 스캔 시각: 현재 기준 다음 tf 캔들 경계 (0/4/8/12/16/20h UTC)
        next_scan_jst = None
        next_scan_minutes_str = ""
        try:
            tf_hours = int(basis_tf.replace("h", "")) if basis_tf.endswith("h") else 4
            from datetime import timezone as _tz
            now_utc_ts = now_jst.astimezone(_tz.utc)
            current_h = now_utc_ts.hour
            next_boundary_h = ((current_h // tf_hours) + 1) * tf_hours
            next_utc = now_utc_ts.replace(
                hour=next_boundary_h % 24, minute=0, second=0, microsecond=0
            )
            if next_boundary_h >= 24:
                next_utc += timedelta(days=1)
            next_scan_jst = next_utc.astimezone(JST)
            diff_min = int((next_utc - now_utc_ts).total_seconds() / 60)
            next_scan_minutes_str = f"{diff_min}분 후"
        except Exception as e:
            logger.warning(f"[BoxReport] next_scan 계산 실패: {e}")

        # 박스 조건 문자열 (전략 파라미터 기반)
        tol_str = str(params.get("box_tolerance_pct", 0.5))
        min_t_str = str(params.get("box_min_touches", params.get("min_touches", 3)))

    pos_pair_col = getattr(box_position_model, pair_column)
    result = await db.execute(
        select(box_position_model)
        .where(and_(pos_pair_col == pair, box_position_model.status == "open"))
        .order_by(desc(box_position_model.created_at))
        .limit(1)
    )
    pos_row = result.scalar_one_or_none()

    position_data = None
    if pos_row and pos_row.entry_price:
        entry_price = float(pos_row.entry_price)
        entry_amount = float(pos_row.entry_amount)
        pos_side = getattr(pos_row, "side", "buy")  # 'buy'=롱, 'sell'=숏
        if pos_side == "sell":
            unrealized_pnl_jpy = (entry_price - current_price) * entry_amount
        else:
            unrealized_pnl_jpy = (current_price - entry_price) * entry_amount
        unrealized_pnl_pct = (
            unrealized_pnl_jpy / (entry_price * entry_amount) * 100
            if entry_price > 0 and entry_amount > 0 else 0.0
        )
        position_data = {
            "side": pos_side,
            "entry_price": entry_price,
            "entry_amount": entry_amount,
            "current_price": current_price,
            "unrealized_pnl_jpy": round(unrealized_pnl_jpy, 0),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
            "stop_loss_price": float(pos_row.exit_price) if pos_row.exit_price else None,
            "exchange_sl_price": float(pos_row.exchange_sl_price) if getattr(pos_row, "exchange_sl_price", None) else None,
            "exchange_sl_status": getattr(pos_row, "exchange_sl_status", None),
        }

    # 5. 잔고 조회
    balance = await adapter.get_balance()
    jpy_available = balance.get_available("jpy")
    coin_currency = pair.split("_")[0].lower()
    coin_available = balance.get_available(coin_currency)

    # 6. 최신 캔들 open_time + close (4H 변동률용)
    candle_pair_col = getattr(candle_model, pair_column)
    result = await db.execute(
        select(candle_model)
        .where(
            and_(
                candle_pair_col == pair,
                candle_model.timeframe == basis_tf,
                candle_model.is_complete == True,
            )
        )
        .order_by(candle_model.open_time.desc())
        .limit(1)
    )
    latest_candle = result.scalar_one_or_none()
    latest_open_time = latest_candle.open_time if latest_candle else None
    candle_open_time_jst = "불명"
    next_candle_jst = None
    next_candle_minutes_str = ""
    if latest_open_time:
        tf_hours = int(basis_tf.replace("h", "")) if basis_tf.endswith("h") else 4
        # timezone-aware 처리
        if latest_open_time.tzinfo is None:
            from datetime import timezone as _tz
            latest_open_time = latest_open_time.replace(tzinfo=_tz.utc)
        next_open = latest_open_time + timedelta(hours=tf_hours)
        now_utc = now_jst.astimezone(next_open.tzinfo)
        while next_open <= now_utc:
            next_open += timedelta(hours=tf_hours)
        next_open_jst = next_open.astimezone(JST)
        candle_open_time_jst = next_open_jst.strftime("%H:%M")
        next_candle_jst = next_open_jst
        diff_min = int((next_open - now_utc).total_seconds() / 60)
        if diff_min > 0:
            next_candle_minutes_str = f"{diff_min}분 후"
        else:
            next_candle_minutes_str = "대기 중"

    last_candle_close = float(latest_candle.close) if latest_candle else None
    candle_change_pct = (
        (current_price - last_candle_close) / last_candle_close * 100
        if last_candle_close and last_candle_close > 0 else 0.0
    )

    # 1H 변동률
    result_1h = await db.execute(
        select(candle_model)
        .where(
            and_(
                candle_pair_col == pair,
                candle_model.timeframe == "1h",
                candle_model.is_complete == True,
            )
        )
        .order_by(candle_model.open_time.desc())
        .limit(1)
    )
    candle_1h = result_1h.scalar_one_or_none()
    candle_1h_close = float(candle_1h.close) if candle_1h else None
    candle_1h_change_pct = (
        (current_price - candle_1h_close) / candle_1h_close * 100
        if candle_1h_close and candle_1h_close > 0 else 0.0
    )

    # 7. 진입 조건 판정 + 텍스트 조립
    has_position = position_data is not None
    entry_blockers = get_box_entry_blockers(box_data, position_label, has_position)
    # 3 conditions: box active, zone=near_lower, no_position
    conditions_total = 3
    conditions_met = conditions_total - len(entry_blockers)

    report_data = {
        "current_price": current_price,
        "health_line": health_line,
        "box": box_data,
        "position_label": position_label,
        "position": position_data,
        "entry_blockers": entry_blockers,
        "conditions_met": conditions_met,
        "conditions_total": conditions_total,
        "formation_progress": formation_progress,
        "next_box_estimate_at": next_scan_jst.isoformat() if next_scan_jst else None,
        "next_scan_jst": next_scan_jst,
        "next_scan_minutes_str": next_scan_minutes_str,
        "box_conditions_str": f"tol={tol_str}% / {min_t_str}+ 터치 필요",
        "coin_available": coin_available,
        "jpy_available": round(jpy_available, 0),
        "basis_timeframe": basis_tf,
        "candle_open_time_jst": candle_open_time_jst,
        "next_candle_minutes_str": next_candle_minutes_str,
        "strategy_name": strategy.name,
        "strategy_id": strategy.id,
        # P0.7: 익절/손절 계산용 파라미터 + FX 분기
        "near_bound_pct": float(params.get("near_bound_pct", 1.5)),
        "tolerance_pct": float(box_row.tolerance_pct) if box_row else float(params.get("box_tolerance_pct", 1.5)),
        "stop_loss_pct": float(params.get("stop_loss_pct", 1.5)),
        "is_margin_trading": getattr(adapter, "is_margin_trading", False),
        "regime_gate_info": (
            {
                "last_regime": regime_gate.regime_history[-1] if regime_gate.regime_history else None,
                "consecutive_count": regime_gate.consecutive_count,
                "active_strategy": regime_gate.active_strategy,
            }
            if regime_gate is not None else None
        ),
    }

    telegram_text = build_box_telegram_text(prefix.upper(), time_str, pair, report_data)
    memory_block = build_box_memory_block(prefix.upper(), time_str, pair, report_data)

    result_dict = {
        "success": True,
        "generated_at": now_jst.isoformat(),
        "report": {
            "telegram_text": telegram_text,
            "memory_block": memory_block,
        },
        "alert": None,
        "raw": {
            "pair": pair,
            "trading_style": "box_mean_reversion",
            "strategy_name": strategy.name,
            "strategy_id": strategy.id,
            "current_price": round(current_price, 6),
            "health_line": health_line,
            "box": box_data,
            "position_label": position_label,
            "position": position_data,
            "formation_progress": formation_progress,
            "entry_blockers": entry_blockers,
            "conditions_met": conditions_met,
            "conditions_total": conditions_total,
            "jpy_available": round(jpy_available, 0),
            "coin_available": round(coin_available, 6),
            "candle_change_pct": round(candle_change_pct, 2),
            "candle_1h_change_pct": round(candle_1h_change_pct, 2),
            "candle_open_time": latest_open_time.isoformat() if latest_open_time else None,
        },
    }

    # Alert 평가
    if test_alert_level:
        alert = _build_test_alert(result_dict["raw"], test_alert_level)
    else:
        prev_raw = _prev_raw_cache.get(pair)
        alert = evaluate_alert(result_dict["raw"], prev_raw)
    _prev_raw_cache[pair] = result_dict["raw"]
    result_dict["alert"] = alert

    if reset_cooldown:
        _last_alert_time.pop(pair, None)

    if alert and alert["level"] == "critical":
        is_test = test_alert_level is not None
        has_position = bool(result_dict.get("raw", {}).get("position"))
        await _trigger_rachel_analysis(
            pair, alert, test=is_test,
            has_position=has_position,
            current_regime="box",
        )

    return result_dict
