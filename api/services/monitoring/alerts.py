"""알림 평가 + 레이첼 긴급 분석 연동."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

RACHEL_WEBHOOK_URL = os.getenv("RACHEL_WEBHOOK_URL", "http://localhost:18793/hooks/market-alert")
RACHEL_WEBHOOK_TOKEN = os.getenv("RACHEL_WEBHOOK_TOKEN", "")
ALERT_COOLDOWN_SEC = 300  # 5분 (기본)
ALERT_COOLDOWN_EXTENDED_SEC = 1800  # 30분 (동일 판단 반복 시)
_last_alert_time: Dict[str, float] = {}
_consecutive_same: Dict[str, int] = {}
_last_alert_level: Dict[str, str] = {}

# 이전 사이클 데이터 캐시 (서버 메모리, 재시작 시 리셋 — 허용)
_prev_raw_cache: Dict[str, dict] = {}

# ── 포지션 없음 전용 필터 상태 (파일 영속화) ──────────────────
_NO_POS_COOLDOWN_SEC = 86400  # 24시간
_FILTER_CACHE_PATH = Path(__file__).parent / ".cache" / "no_pos_filter.json"

# 포지션 없음 상태에서도 항상 보고할 트리거 (시스템 장애 + Kill 관련)
_ALWAYS_REPORT_TRIGGERS = {
    "system_error",
    "api_failure",
    "db_error",
    "kill_condition",
    "kill_consecutive_loss",
    "kill_win_rate",
    "kill_drawdown",
    "kill_regime_transition",
}

# ── 버그 1: 파일 기반 영속화 ──────────────────────────────────

def _load_filter_cache() -> dict:
    """파일에서 필터 상태 로드. 없거나 손상된 경우 빈 dict."""
    try:
        if _FILTER_CACHE_PATH.exists():
            return json.loads(_FILTER_CACHE_PATH.read_text())
    except Exception as e:
        logger.warning(f"[AlertFilter] 캐시 파일 로드 실패: {e}")
    return {}


def _save_filter_cache(data: dict) -> None:
    """필터 상태를 파일에 저장."""
    try:
        _FILTER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FILTER_CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"[AlertFilter] 캐시 파일 저장 실패: {e}")


def _get_filter_state(pair: str) -> tuple[str, float]:
    """pair의 (last_regime, last_sent_time) 반환."""
    data = _load_filter_cache()
    entry = data.get(pair, {})
    return entry.get("regime", ""), entry.get("sent_at", 0.0)


def _set_filter_state(pair: str, regime: str) -> None:
    """pair의 필터 상태 갱신 후 파일 저장."""
    data = _load_filter_cache()
    data[pair] = {"regime": regime, "sent_at": time.time()}
    _save_filter_cache(data)


# ── 버그 2: regime 정규화 ─────────────────────────────────────

def _normalize_regime(regime) -> str:
    """None / 빈문자열 → 'unknown'으로 정규화."""
    if not regime:
        return "unknown"
    return str(regime)


def _should_send_no_position(
    pair: str,
    alert: dict,
    current_regime: str,
) -> bool:
    """
    포지션 없음 상태에서 webhook 발송 여부를 판단.

    Rules:
      - Kill/시스템 장애 트리거 → 항상 보고
      - regime 전환 (이전 발송 시 ≠ 현재, unknown 제외) → 보고
      - 최초 발송 (기록 없음, current_regime이 known인 경우) → 보고
      - 위 해당 없음 + 24h 쿨다운 내 → 묵음
    """
    triggers = set(alert.get("triggers", []))

    # 시스템 장애 / Kill → 항상 보고
    if triggers & _ALWAYS_REPORT_TRIGGERS:
        return True

    # regime 정규화 (버그 2)
    norm_regime = _normalize_regime(current_regime)

    # 파일에서 이전 상태 로드 (버그 1)
    prev_regime, last_t = _get_filter_state(pair)

    # regime이 unknown이면 전환 판단 보류 (noise 방지)
    if norm_regime != "unknown":
        if prev_regime and prev_regime != norm_regime:
            # regime 전환 → 보고
            return True
        if not prev_regime:
            # 최초 발송 → 보고
            return True

    # regime 동일(또는 unknown) + 24h 쿨다운
    if time.time() - last_t < _NO_POS_COOLDOWN_SEC:
        logger.info(
            f"[AlertFilter] {pair}: 포지션 없음 + regime={norm_regime} + 쿨다운 중 — 묵음 "
            f"({int(time.time() - last_t)}s / {_NO_POS_COOLDOWN_SEC}s)"
        )
        return False

    return True


def _record_no_pos_sent(pair: str, current_regime: str) -> None:
    """포지션 없음 상태에서 webhook 발송 후 상태 갱신 (파일 영속화)."""
    norm_regime = _normalize_regime(current_regime)
    _set_filter_state(pair, norm_regime)


async def _trigger_rachel_analysis(
    pair: str,
    alert: dict,
    test: bool = False,
    has_position: bool = True,
    current_regime: str = "",
) -> None:
    """critical alert 시 레이첼에게 긴급 분석 요청.

    포지션 없음 시 _should_send_no_position() 필터 적용.
    """
    if not RACHEL_WEBHOOK_TOKEN:
        logger.warning("RACHEL_WEBHOOK_TOKEN 미설정 — webhook 스킵")
        return

    # ── 포지션 없음 필터 ─────────────────────────────────────
    if not has_position:
        if not _should_send_no_position(pair, alert, current_regime):
            return  # 묵음

    now = time.time()
    last = _last_alert_time.get(pair, 0)

    triggers_key = ",".join(sorted(alert.get("triggers", [])))
    last_key = _last_alert_level.get(pair, "")
    if triggers_key == last_key:
        _consecutive_same[pair] = _consecutive_same.get(pair, 0) + 1
    else:
        _consecutive_same[pair] = 0
    _last_alert_level[pair] = triggers_key

    cooldown = ALERT_COOLDOWN_EXTENDED_SEC if _consecutive_same.get(pair, 0) >= 3 else ALERT_COOLDOWN_SEC
    if now - last < cooldown:
        logger.debug(f"레이첼 webhook 쿨다운 중: {pair} ({int(now - last)}s ago, cooldown={cooldown}s, repeat={_consecutive_same.get(pair, 0)})")
        return

    _last_alert_time[pair] = now

    if test:
        action_instruction = (
            "⚠️ 테스트 모드: 전략 전환은 실행하지 말 것. "
            "전환이 필요하다고 판단하면 '이렇게 전환했을 것이다'를 "
            "Telegram으로 [Rachel 긴급 테스트] prefix로 보고하라."
        )
    else:
        action_instruction = (
            "전략 전환 필요 여부를 반드시 판단하고, "
            "전환이 필요하면 수보오빠 승인 없이 즉시 실행하라 (긴급 자율 전환 권한). "
            "결과를 Telegram으로 [Rachel 긴급] prefix로 보고하라."
        )

    message = (
        f"긴급 분석 요청 (자동 트리거)\n"
        f"페어: {pair}\n"
        f"트리거: {', '.join(alert['triggers'])}\n"
        f"상세: {alert['text']}\n\n"
        f"즉시 WORKFLOW_AUTO.md 단기 분석 STEP 1~7을 실행하라. "
        f"{action_instruction}"
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                RACHEL_WEBHOOK_URL,
                headers={
                    "Authorization": f"Bearer {RACHEL_WEBHOOK_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "message": message,
                    "name": "MarketAlert",
                    "deliver": True,
                    "channel": "telegram",
                    "timeoutSeconds": 480,
                },
            )
            if resp.status_code == 200:
                logger.info(f"레이첼 긴급 분석 트리거 성공: {pair}")
                # 포지션 없음 상태 발송 기록 갱신
                if not has_position:
                    _record_no_pos_sent(pair, current_regime)
            else:
                logger.error(f"레이첼 webhook 실패: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"레이첼 webhook 오류: {e}")


def _build_test_alert(raw: dict, level: str) -> dict:
    """테스트용 alert 생성 — 실제 수치 기반으로 만들되 level만 override."""
    pair = raw.get("pair", "unknown")
    price = raw.get("current_price", 0)
    rsi = raw.get("rsi14")

    prefix = "CK" if pair.islower() else "BF"

    if level == "critical":
        return {
            "level": "critical",
            "triggers": ["test_forced_critical"],
            "text": (
                f"\U0001f6a8\U0001f6a8\U0001f6a8 [{prefix} 긴급 테스트] {pair}\n"
                f"\xa5{price:,.0f}"
                + (f" | RSI {rsi:.1f}" if rsi is not None else "")
                + "\n\u26a0\ufe0f 테스트 강제 트리거"
            ),
        }
    else:
        return {
            "level": "warning",
            "triggers": ["test_forced_warning"],
            "text": (
                f"\u26a0\ufe0f [{prefix} 주의 테스트] {pair}"
                + (f" \u2014 RSI {rsi:.1f}" if rsi is not None else "")
                + " (테스트 강제)"
            ),
        }


def evaluate_alert(raw: dict, prev_raw: Optional[dict] = None) -> Optional[dict]:
    """급변 시장 상황 평가. alert가 없으면 None 반환."""
    triggers: List[Tuple[str, str, str]] = []

    rsi = raw.get("rsi14")
    current_price = raw.get("current_price", 0)
    ema = raw.get("ema20")

    if prev_raw:
        prev_price = prev_raw.get("current_price", current_price)
        change_15m_pct = (
            (current_price - prev_price) / prev_price * 100
            if prev_price and prev_price > 0 else 0.0
        )
    else:
        change_15m_pct = 0.0

    candle_1h_change_pct = raw.get("candle_1h_change_pct", 0)

    # --- Critical triggers ---
    if rsi is not None:
        if rsi < 20:
            triggers.append(("critical", "rsi_extreme_low", f"RSI {rsi:.1f} 극단 과매도"))
        elif rsi > 85:
            triggers.append(("critical", "rsi_extreme_high", f"RSI {rsi:.1f} 극단 과열"))

    if change_15m_pct < -3:
        triggers.append(("critical", "price_crash_15m", f"15분 {change_15m_pct:.1f}% 초급락"))
    elif change_15m_pct > 3:
        triggers.append(("critical", "price_surge_15m", f"15분 +{change_15m_pct:.1f}% 초급등"))

    if candle_1h_change_pct < -5:
        triggers.append(("critical", "price_crash_1h", f"1H {candle_1h_change_pct:.1f}% 급락"))
    elif candle_1h_change_pct > 5:
        triggers.append(("critical", "price_surge_1h", f"1H +{candle_1h_change_pct:.1f}% 급등"))

    pos = raw.get("position")
    if pos and pos.get("unrealized_pnl_pct", 0) < -3:
        triggers.append(("critical", "position_at_risk",
            f"보유 포지션 {pos['unrealized_pnl_pct']:.1f}% 손실"))

    if prev_raw:
        prev_signal = prev_raw.get("signal", "")
        curr_signal = raw.get("signal", "")
        if prev_signal and curr_signal and _is_regime_shift(prev_signal, curr_signal):
            triggers.append(("critical", "regime_shift",
                f"시그널 전환: {prev_signal} → {curr_signal}"))

    # --- Warning triggers ---
    if rsi is not None:
        if rsi < 25 and not any(t[1] == "rsi_extreme_low" for t in triggers):
            triggers.append(("warning", "rsi_low", f"RSI {rsi:.1f} 과매도"))
        elif rsi > 80 and not any(t[1] == "rsi_extreme_high" for t in triggers):
            triggers.append(("warning", "rsi_high", f"RSI {rsi:.1f} 과열"))

    if abs(change_15m_pct) > 1.5 and abs(change_15m_pct) <= 3:
        triggers.append(("warning", "high_volatility_15m", f"15분 {change_15m_pct:+.1f}%"))

    if abs(candle_1h_change_pct) > 3 and abs(candle_1h_change_pct) <= 5:
        triggers.append(("warning", "high_volatility_1h", f"1H {candle_1h_change_pct:+.1f}%"))

    if ema is not None and current_price > 0:
        ema_gap_pct = abs((current_price - ema) / ema * 100)
        if ema_gap_pct > 3:
            triggers.append(("warning", "large_ema_gap", f"EMA 갭 {ema_gap_pct:.1f}%"))

    if prev_raw:
        prev_slope = prev_raw.get("ema_slope_pct")
        curr_slope = raw.get("ema_slope_pct")
        if prev_slope is not None and curr_slope is not None:
            if (prev_slope > 0 and curr_slope < 0) or (prev_slope < 0 and curr_slope > 0):
                triggers.append(("warning", "slope_reversal",
                    f"EMA slope 전환 {prev_slope:+.2f}%→{curr_slope:+.2f}%"))

    if not triggers:
        return None

    max_level = "critical" if any(t[0] == "critical" for t in triggers) else "warning"

    return {
        "level": max_level,
        "triggers": [t[1] for t in triggers],
        "text": build_alert_text(raw, triggers, max_level),
    }


def _is_regime_shift(prev: str, curr: str) -> bool:
    """방향이 반대로 바뀌는 경우만."""
    bullish = {"entry_ok", "wait_dip"}
    bearish = {"exit_warning"}
    return (prev in bullish and curr in bearish) or \
           (prev in bearish and curr in bullish)


def build_alert_text(raw: dict, triggers: List[Tuple[str, str, str]], level: str) -> str:
    """알림 텍스트 생성."""
    pair = raw.get("pair", "unknown")
    price = raw.get("current_price", 0)

    prefix = "CK" if pair.islower() else "BF"
    trigger_details = " | ".join(t[2] for t in triggers)

    if level == "critical":
        return (
            f"🚨🚨🚨 [{prefix} 긴급] {pair}\n"
            f"¥{price:,.0f}\n"
            f"{trigger_details}\n"
            f"\n"
            f"⚡ 레이첼 심층분석 권장\n"
            f'→ "레이첼 심층분석" 전송'
        )
    else:
        return f"⚠️ [{prefix} 주의] {pair} — {trigger_details}"
