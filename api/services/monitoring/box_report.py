"""박스 전략 모니터링 리포트 — 텍스트 조립 + 생성."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from .display import JST
from .alerts import (
    _prev_raw_cache,
    _last_alert_time,
    _build_test_alert,
    evaluate_alert,
    _trigger_rachel_analysis,
)

logger = logging.getLogger(__name__)


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
    if box:
        lines.append(f"¥{data['current_price']:,.2f} {data['position_label']} (폭 {box['box_width_pct']:.1f}%)")
        lines.append(f"하단¥{box['lower_bound']:,.2f} {box['bar_chart']} 상단¥{box['upper_bound']:,.2f}")
    else:
        lines.append(f"¥{data['current_price']:,.2f} 📭박스 미형성")
        fp = data.get("formation_progress")
        if fp:
            min_t = fp["min_touches"]
            remaining = fp["candles_remaining"]
            tf = data.get("basis_timeframe", "4h")
            lines.append(
                f"📊 형성 진행: 상단 {fp['upper_touches']}/{min_t} "
                f"하단 {fp['lower_touches']}/{min_t} — "
                f"최소 {remaining}봉({tf}) 후"
            )

    # 진입 조건 N/3 표시
    met = data.get("conditions_met", 0)
    total = data.get("conditions_total", 3)
    entry_blockers = data.get("entry_blockers", [])
    if entry_blockers:
        lines.append(f"🚫 {met}/{total} | {' | '.join(entry_blockers)}")
    else:
        lines.append(f"✅ {met}/{total} 진입 조건 충족")

    lines.append(f"{data['basis_timeframe']}봉 시작: {data['candle_open_time_jst']} JST")

    currency = pair.split("_")[0]
    pos = data.get("position")
    if pos:
        lines.append(
            f"JPY ¥{data['jpy_available']:,.0f} {currency} {data['coin_available']:.2f}개 | "
            f"보유 {pos['entry_amount']}{currency} @ ¥{pos['entry_price']:,.2f} | "
            f"미실현 ¥{pos['unrealized_pnl_jpy']:,.0f} ({pos['unrealized_pnl_pct']:.2f}%)"
        )
    else:
        lines.append(f"JPY ¥{data['jpy_available']:,.0f} {currency} {data['coin_available']:.2f}개 | 포지션 미보유")

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
) -> dict:
    """box_mean_reversion 전략의 모니터링 리포트 생성."""
    pair = pair.lower()  # DB candle pair는 소문자 — 정규화
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

    # 3. 활성 박스 조회
    pair_col = getattr(box_model, pair_column)
    result = await db.execute(
        select(box_model)
        .where(and_(pair_col == pair, box_model.status == "active"))
        .order_by(desc(box_model.created_at))
        .limit(1)
    )
    box_row = result.scalar_one_or_none()

    box_data = None
    position_label = "no_box"
    formation_progress = None
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
        }
    else:
        # 박스 미형성 시 진행 상황 계산
        try:
            from core.analysis.box_detector import detect_box_progress
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
                formation_progress = {
                    "upper_touches": progress.upper_touches,
                    "lower_touches": progress.lower_touches,
                    "min_touches": progress.min_touches,
                    "candles_remaining": progress.candles_remaining,
                    "upper_center": progress.upper_center,
                    "lower_center": progress.lower_center,
                }
        except Exception as e:
            logger.warning(f"[BoxReport] 형성 진행 계산 실패: {e}")

    # 4. 오픈 포지션 조회 (DB)
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
        unrealized_pnl_jpy = (current_price - entry_price) * entry_amount
        unrealized_pnl_pct = (
            (current_price - entry_price) / entry_price * 100
            if entry_price > 0 else 0.0
        )
        position_data = {
            "entry_price": entry_price,
            "entry_amount": entry_amount,
            "current_price": current_price,
            "unrealized_pnl_jpy": round(unrealized_pnl_jpy, 0),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
            "stop_loss_price": float(pos_row.exit_price) if pos_row.exit_price else None,
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
    candle_open_time_jst = (
        latest_open_time.astimezone(JST).strftime("%H:%M")
        if latest_open_time else "불명"
    )

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
        "jpy_available": jpy_available,
        "coin_available": coin_available,
        "basis_timeframe": basis_tf,
        "candle_open_time_jst": candle_open_time_jst,
        "strategy_name": strategy.name,
        "strategy_id": strategy.id,
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
