"""
Telegram 로그 핸들러 — 도메인별 채널 분리 전송.

핸들러:
- TelegramTransactionHandler : 트랜잭션 기반 핸들러 (신규, 권장)
  - 판단 도메인: 5분 정기 요약 + 시그널/체제/advisory/FNG 변경 시 즉시 전송
  - 실행 도메인: 진입/청산/스탑타이트닝 감지 시 즉시 전송
- TelegramDigestHandler : INFO 버퍼링 배치 전송 (레거시, Deprecated)
- TelegramAlertHandler : WARNING+ → 실행 도메인 그룹 즉시 (5초 디바운스)

JUDGE_PREFIXES / PUNISHER_PREFIXES 로 라우팅 규칙 관리.

사용:
    setup_telegram_logging() 을 lifespan 내에서 호출.
    shutdown_telegram_logging() 을 shutdown 시 호출.

Canonical location: core/shared/logging/telegram_handlers.py
Backward-compat shim at: core/logging/telegram_handlers.py
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_MSG_MAX = 4096
JST = timezone(timedelta(hours=9))

# ── 도메인 라우팅 규칙 ──────────────────────────────
# logger.name이 아래 prefix로 시작하면 판단 도메인 채널 전송
JUDGE_PREFIXES: frozenset[str] = frozenset({
    # canonical 경로 (core.judge.*) — Phase E 이후 정식 경로
    "core.judge",
    # 레거시 경로 (shim 유지 기간 동안 하위호환)
    "core.data",
    "core.decision",
    "core.safety",
    "core.analysis",
    "core.strategy.signals",
    "core.strategy.box_signals",
    "core.strategy.scoring",
    "core.execution.orchestrator",
    "core.execution.approval",
})

# logger.name이 아래 prefix로 시작하면 실행 도메인 채널 전송
PUNISHER_PREFIXES: frozenset[str] = frozenset({
    # canonical 경로 (core.punisher.*) — Phase E 이후 정식 경로
    "core.punisher",
    # 레거시 경로 (shim 유지 기간 동안 하위호환)
    "core.strategy.base_trend",
    "core.strategy.plugins",
    "core.strategy.registry",
    "core.strategy.snapshot_collector",
    "core.strategy.switch_recommender",
    "core.execution.regime_gate",
    "core.execution.executor",
    "core.task",
    "core.learning",
    "core.notifications",
    "adapters",
    "api",
    "main",
})


def _get_domain(logger_name: str) -> str:
    """logger name → 'judge' | 'punisher' | 'shared'."""
    for prefix in JUDGE_PREFIXES:
        if logger_name == prefix or logger_name.startswith(prefix + "."):
            return "judge"
    for prefix in PUNISHER_PREFIXES:
        if logger_name == prefix or logger_name.startswith(prefix + "."):
            return "punisher"
    return "shared"  # 미분류 → 실행 도메인으로 fallback


# ── 유틸 ─────────────────────────────────────────────

async def _send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    """Telegram Bot API 전송. 실패 시 False (예외 삼킴)."""
    if not bot_token or not chat_id:
        return False
    url = TELEGRAM_API.format(token=bot_token)
    # 메시지 길이 제한
    if len(text) > TELEGRAM_MSG_MAX:
        text = text[:TELEGRAM_MSG_MAX - 20] + "\n… (truncated)"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": text})
            return resp.status_code == 200
    except Exception:
        # 전송 실패를 로깅하면 무한 루프 가능 → 조용히 삼킴
        return False


def _format_time(ts: float) -> str:
    """epoch → HH:MM:SS JST."""
    return datetime.fromtimestamp(ts, tz=JST).strftime("%H:%M:%S")


# ── 트랜잭션 핸들러 (신규, 권장) ──────────────────────

class TelegramTransactionHandler(logging.Handler):
    """트랜잭션 기반 텔레그램 핸들러.
    
    domain="judge" 일 때:
      - 5분마다 정기 요약 전송 (판단 상태 + 포지션 현황)
      - 시그널 변경 감지 시 즉시 전송
      - 4H 체제 판정 변경 감지 시 즉시 전송
      - advisory 갱신 감지 시 즉시 전송
      - FNG/경제이벤트 갱신 감지 시 즉시 전송
      - "advisory 없음 → v1 폴백" WARNING: 1시간에 1번으로 빈도 제한
    
    domain="punisher" 일 때:
      - 진입 완료 감지 시 즉시 전송
      - 청산 완료 감지 시 즉시 전송
      - 스탑 타이트닝 감지 시 즉시 전송
      (WARNING+는 TelegramAlertHandler가 처리)
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        exchange: str = "??",
        interval_sec: int = 300,
        domain: str | None = None,
    ):
        super().__init__(level=logging.INFO)
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._exchange = exchange.upper()
        self._interval = interval_sec
        self._domain = domain  # 'judge' | 'punisher' | None
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        
        # 내부 상태
        self._state = {
            # 판단 상태
            'signal': None,
            'prev_signal': None,
            'regime_status': None,
            'prev_regime': None,
            'regime_consecutive': 0,
            'regime_bb_width': None,
            'regime_range_pct': None,
            'advisory_action': None,
            'advisory_confidence': None,
            'advisory_remaining_h': None,
            'advisory_reasoning': None,
            'fng_score': None,
            'fng_label': None,
            # 포지션 상태
            'has_position': False,
            'position_side': None,
            'entry_price': None,
            'position_size': None,
            'stop_price': None,
            'current_price': None,
            'ema_price': None,
            'realized_pnl_today': 0.0,
            # 진입/청산 이벤트 (즉시 전송 후 None으로 클리어)
            'entry_event': None,
            'close_event': None,
            'stop_tighten_event': None,
        }
        self._advisory_warn_last: float = 0  # v1 폴백 WARNING 마지막 전송 시각
        self._last_periodic_send: float = 0  # 마지막 정기 전송 시각

    def emit(self, record: logging.LogRecord) -> None:
        """로그 메시지 파싱 → 상태 업데이트 + 즉시 전송 판단."""
        # INFO만 수집 (DEBUG 제외, WARNING 이상 제외)
        if record.levelno != logging.INFO:
            return
        
        # 도메인 필터
        if self._domain is not None:
            record_domain = _get_domain(record.name)
            if self._domain == "judge" and record_domain != "judge":
                return
            if self._domain == "punisher" and record_domain == "judge":
                return
        
        msg = record.getMessage()
        
        # 파싱 (best-effort, 실패 시 조용히 넘어감)
        try:
            self._parse_and_update(msg)
        except Exception:
            pass

    def _parse_and_update(self, msg: str) -> None:
        """메시지 파싱 후 상태 업데이트 + 즉시 전송 트리거."""
        import re
        
        # 현재가
        m = re.search(r'실시간가 ¥([\d.]+)', msg)
        if m:
            self._state['current_price'] = float(m.group(1))
        
        # EMA
        m = re.search(r'EMA ¥([\d.]+)', msg)
        if m:
            self._state['ema_price'] = float(m.group(1))
        
        # 진입 완료
        m = re.search(r'(buy|sell) 진입 완료.*price=¥([\d.]+).*size=([\d.]+).*stop_loss=¥([\d.]+)', msg)
        if m:
            side = m.group(1)
            price = float(m.group(2))
            size = float(m.group(3))
            stop = float(m.group(4))
            self._state['entry_event'] = {'side': side, 'price': price, 'size': size, 'stop_loss': stop}
            self._state['has_position'] = True
            self._state['position_side'] = 'long' if side == 'buy' else 'short'
            self._state['entry_price'] = price
            self._state['position_size'] = size
            self._state['stop_price'] = stop
            if self._domain == "punisher" and self._loop:
                self._loop.create_task(self._send_entry())
            return
        
        # 청산 완료
        m = re.search(r'(buy|sell) 청산 완료 reason=(\w+)', msg)
        if m:
            side = m.group(1)
            reason = m.group(2)
            self._state['close_event'] = {'side': side, 'reason': reason}
            self._state['has_position'] = False
            self._state['position_side'] = None
            if self._domain == "punisher" and self._loop:
                self._loop.create_task(self._send_close())
            return
        
        # 스탑 타이트닝
        m = re.search(r'스탑 타이트닝 ¥([\d.]+) → ¥([\d.]+)', msg)
        if m:
            prev_stop = float(m.group(1))
            curr_stop = float(m.group(2))
            self._state['stop_tighten_event'] = {'prev': prev_stop, 'curr': curr_stop}
            self._state['stop_price'] = curr_stop
            if self._domain == "punisher" and self._loop:
                self._loop.create_task(self._send_stop_tighten())
            return
        
        # 스탑 DB 복원
        m = re.search(r'DB 스탑 복원 ¥([\d.]+)', msg)
        if m:
            self._state['stop_price'] = float(m.group(1))
        
        # 기존 포지션 감지
        if '기존 포지션 감지' in msg:
            self._state['has_position'] = True
        
        # signal
        m = re.search(r'signal=(\w+)', msg)
        if m:
            new_signal = m.group(1)
            if self._state['signal'] != new_signal:
                self._state['prev_signal'] = self._state['signal']
                self._state['signal'] = new_signal
                if self._domain == "judge" and self._loop and self._state['prev_signal'] is not None:
                    self._loop.create_task(self._send_signal_change())
        
        # regime (4H 체제 판정)
        m = re.search(r'regime=(\w+).*BB폭 ([\d.]+)%.*가격범위 ([\d.]+)%.*?(\w+) 연속 (\d+)회', msg)
        if m:
            new_regime = m.group(1)
            bb_width = float(m.group(2))
            range_pct = float(m.group(3))
            consecutive = int(m.group(5))
            
            regime_changed = self._state['regime_status'] != new_regime
            self._state['prev_regime'] = self._state['regime_status']
            self._state['regime_status'] = new_regime
            self._state['regime_bb_width'] = bb_width
            self._state['regime_range_pct'] = range_pct
            self._state['regime_consecutive'] = consecutive
            
            if self._domain == "judge" and self._loop:
                self._loop.create_task(self._send_regime_update(regime_changed))
        
        # advisory
        m = re.search(r'action=(\w+) confidence=([\d.]+).*잔여=([\d.]+)H', msg)
        if m:
            action = m.group(1)
            confidence = float(m.group(2))
            remaining_h = float(m.group(3))
            
            # reasoning 추출
            reasoning_match = re.search(r'사유:\s*([^\n]+)', msg)
            reasoning = reasoning_match.group(1) if reasoning_match else None
            
            self._state['advisory_action'] = action
            self._state['advisory_confidence'] = confidence
            self._state['advisory_remaining_h'] = remaining_h
            if reasoning:
                self._state['advisory_reasoning'] = reasoning
            
            if self._domain == "judge" and self._loop:
                self._loop.create_task(self._send_advisory_update())
        
        # advisory 없음 (v1 폴백)
        if 'advisory' in msg and '없음' in msg and 'v1 폴백' in msg:
            now = time.time()
            # 1시간에 1번만 전송
            if now - self._advisory_warn_last > 3600:
                self._advisory_warn_last = now
                # WARNING 레벨이므로 TelegramAlertHandler가 처리하지만
                # 여기서는 상태만 업데이트
                self._state['advisory_action'] = None
                self._state['advisory_confidence'] = None
        
        # FNG
        m = re.search(r'FNG.*score=(\d+) \(([^)]+)\)', msg)
        if m:
            score = int(m.group(1))
            label = m.group(2)
            self._state['fng_score'] = score
            self._state['fng_label'] = label
            
            if self._domain == "judge" and self._loop:
                self._loop.create_task(self._send_fng_update())

    async def _send_entry(self) -> None:
        """진입 이벤트 즉시 전송."""
        evt = self._state.get('entry_event')
        if not evt:
            return
        
        side_kr = '롱' if evt['side'] == 'buy' else '숏'
        entry_price = evt['price']
        size = evt['size']
        stop = evt['stop_loss']
        
        # 리스크 계산
        risk_jpy = abs(entry_price - stop) * size
        risk_pct = abs(entry_price - stop) / entry_price * 100
        
        # advisory 요약
        adv = self._state.get('advisory_action')
        if adv:
            conf = self._state.get('advisory_confidence', 0) * 100
            adv_summary = f"{adv} (확신도 {conf:.0f}%)"
        else:
            adv_summary = "규칙 기반 판단"
        
        text = (
            f"⚡ [{self._exchange}·BTC] {_format_time(time.time())}  🟢 진입  (실행 사이클 · {side_kr})\n"
            f"──────────────────────────\n"
            f"진입가 ¥{entry_price:,.0f}  /  {size} BTC\n"
            f"스탑 ¥{stop:,.0f}  (리스크 {risk_pct:.2f}%,  ¥{risk_jpy:,.0f})\n"
            f"레이첼: {adv_summary}"
        )
        
        await _send_telegram(self._bot_token, self._chat_id, text)
        self._state['entry_event'] = None

    async def _send_close(self) -> None:
        """청산 이벤트 즉시 전송."""
        evt = self._state.get('close_event')
        if not evt:
            return
        
        side_kr = '롱' if evt['side'] == 'buy' else '숏'
        reason = evt['reason']
        reason_kr = {
            'stop_loss': '하드 스탑',
            'tighten_stop': '타이트닝 스탑',
            'exit': '청산 지시',
        }.get(reason, reason)
        
        entry = self._state.get('entry_price')
        current = self._state.get('current_price')
        stop = self._state.get('stop_price')
        size = self._state.get('position_size', 0)
        
        # 손익 계산
        pnl_jpy = 0
        pnl_pct = 0
        if entry and current and size:
            if evt['side'] == 'buy':
                pnl_jpy = (current - entry) * size
            else:
                pnl_jpy = (entry - current) * size
            pnl_pct = pnl_jpy / (entry * size) * 100
        
        close_detail = ""
        if reason == 'stop_loss':
            close_detail = "🛑 손절 실행"
        elif reason == 'tighten_stop':
            close_detail = "📊 트레일링 스탑 도달"
        else:
            close_detail = "✅ 수동 청산"
        
        text = (
            f"⚡ [{self._exchange}·BTC] {_format_time(time.time())}  🔴 청산  (실행 사이클 · {reason_kr})\n"
            f"──────────────────────────\n"
            f"{close_detail}\n"
            f"  현재가 ¥{current:,.0f}  /  스탑 ¥{stop:,.0f}\n"
            f"  진입가 ¥{entry:,.0f}  →  손익 ¥{pnl_jpy:+,.0f}  ({pnl_pct:+.2f}%)\n"
            f"{side_kr} 청산 완료 · {size} BTC"
        )
        
        await _send_telegram(self._bot_token, self._chat_id, text)
        self._state['close_event'] = None

    async def _send_stop_tighten(self) -> None:
        """스탑 타이트닝 이벤트 즉시 전송."""
        evt = self._state.get('stop_tighten_event')
        if not evt:
            return
        
        prev = evt['prev']
        curr = evt['curr']
        diff = curr - prev
        pct = diff / prev * 100
        
        # 손익 상태
        entry = self._state.get('entry_price')
        size = self._state.get('position_size', 0)
        side = self._state.get('position_side')
        
        profit_line = ""
        if entry and size and side:
            if side == 'long':
                unrealized = (curr - entry) * size
            else:
                unrealized = (entry - curr) * size
            profit_line = f"미실현 ¥{unrealized:+,.0f}"
        
        text = (
            f"⚡ [{self._exchange}·BTC] {_format_time(time.time())}  📈 스탑 상향  (실행 사이클)\n"
            f"──────────────────────────\n"
            f"이익 보호 강화\n"
            f"¥{prev:,.0f}  →  ¥{curr:,.0f}  (+¥{diff:,.0f},  +{pct:.2f}%)\n"
            f"{profit_line}"
        )
        
        await _send_telegram(self._bot_token, self._chat_id, text)
        self._state['stop_tighten_event'] = None

    async def _send_signal_change(self) -> None:
        """시그널 변경 즉시 전송."""
        prev = self._state.get('prev_signal')
        curr = self._state.get('signal')
        
        signal_kr = {
            'entry_ok': '롱 진입 가능',
            'hold': '관망',
            'entry_sell': '숏 진입 가능',
        }
        
        prev_kr = signal_kr.get(prev, prev)
        curr_kr = signal_kr.get(curr, curr)
        
        # advisory 요약
        adv = self._state.get('advisory_action')
        if adv:
            conf = self._state.get('advisory_confidence', 0) * 100
            adv_summary = f"{adv} (확신도 {conf:.0f}%)"
        else:
            adv_summary = "4H advisory 없음 → 규칙 기반 판단"
        
        # signal_detail
        current = self._state.get('current_price')
        ema = self._state.get('ema_price')
        signal_detail = ""
        if current and ema:
            if current > ema:
                signal_detail = f"현재가 ¥{current:,.0f} > EMA ¥{ema:,.0f}"
            else:
                signal_detail = f"현재가 ¥{current:,.0f} < EMA ¥{ema:,.0f}"
        
        # 결론
        has_pos = self._state.get('has_position')
        conclusion = ""
        if has_pos:
            conclusion = "포지션 보유 중 — 청산 조건 감시"
        else:
            if curr == 'entry_ok':
                conclusion = "롱 진입 기회"
            elif curr == 'entry_sell':
                conclusion = "숏 진입 기회"
            else:
                conclusion = "진입 조건 미충족"
        
        text = (
            f"🧠 [{self._exchange}·BTC] {_format_time(time.time())}  ★ 신호 변경  (판단 사이클)\n"
            f"──────────────────────────\n"
            f"이전: {prev_kr}  →  현재: {curr_kr}\n"
            f"레이첼: {adv_summary}\n"
            f"  · {signal_detail}\n"
            f"결론: {conclusion}"
        )
        
        await _send_telegram(self._bot_token, self._chat_id, text)

    async def _send_regime_update(self, changed: bool) -> None:
        """체제 판정 업데이트 (변경 여부 무관하게 4H마다 전송)."""
        curr = self._state.get('regime_status')
        prev = self._state.get('prev_regime')
        consecutive = self._state.get('regime_consecutive', 0)
        bb = self._state.get('regime_bb_width')
        range_pct = self._state.get('regime_range_pct')
        
        regime_kr = {
            'trending': '추세 진행',
            'ranging': '박스권',
            'unclear': '불명확',
        }
        
        curr_kr = regime_kr.get(curr, curr)
        
        # 진입 허용/차단
        gate_status = "진입 허용" if curr == 'trending' and consecutive >= 3 else "진입 차단 중"
        
        if changed and prev:
            prev_kr = regime_kr.get(prev, prev)
            conclusion = f"{curr_kr} 전환 감지 → {gate_status}"
            text = (
                f"🧠 [{self._exchange}·BTC] {_format_time(time.time())}  ★ 체제 전환  (판단 사이클 · 4H 체제 갱신)\n"
                f"──────────────────────────\n"
                f"{prev_kr} → {curr_kr} 전환\n"
                f"  BB폭 {bb:.1f}%  /  가격범위 {range_pct:.1f}%\n"
                f"  {curr_kr} 연속 {consecutive}회 → {gate_status}\n"
                f"→ {conclusion}"
            )
        else:
            conclusion = f"{curr_kr} 유지 → {gate_status}"
            text = (
                f"🧠 [{self._exchange}·BTC] {_format_time(time.time())}  (판단 사이클 · 4H 체제 갱신)\n"
                f"──────────────────────────\n"
                f"체제: {curr_kr} · {consecutive}회 연속\n"
                f"  BB폭 {bb:.1f}%  /  가격범위 {range_pct:.1f}%\n"
                f"→ {conclusion}"
            )
        
        await _send_telegram(self._bot_token, self._chat_id, text)

    async def _send_advisory_update(self) -> None:
        """advisory 갱신 즉시 전송."""
        action = self._state.get('advisory_action')
        conf = self._state.get('advisory_confidence', 0) * 100
        remaining_h = self._state.get('advisory_remaining_h')
        reasoning = self._state.get('advisory_reasoning', '(사유 없음)')
        
        action_kr = {
            'hold': '보류',
            'entry_long': '롱 진입',
            'entry_short': '숏 진입',
            'add_position': '피라미딩',
            'exit': '청산',
        }.get(action, action)
        
        text = (
            f"🧠 [{self._exchange}·BTC] {_format_time(time.time())}  (판단 사이클 · 레이첼 advisory)\n"
            f"──────────────────────────\n"
            f"레이첼 의견: {action_kr} (확신도 {conf:.0f}%, 만료까지 {remaining_h:.1f}H)\n"
            f"  사유: {reasoning}"
        )
        
        await _send_telegram(self._bot_token, self._chat_id, text)

    async def _send_fng_update(self) -> None:
        """FNG 갱신 즉시 전송."""
        score = self._state.get('fng_score')
        label = self._state.get('fng_label')
        
        text = (
            f"🧠 [{self._exchange}·BTC] {_format_time(time.time())}  (판단 사이클 · FNG 갱신)\n"
            f"──────────────────────────\n"
            f"시장 심리: {score} ({label})"
        )
        
        await _send_telegram(self._bot_token, self._chat_id, text)

    async def _send_periodic_summary(self) -> None:
        """5분 정기 요약 전송 (판단 도메인 전용)."""
        if self._domain != "judge":
            return
        
        # 체제
        regime = self._state.get('regime_status')
        consecutive = self._state.get('regime_consecutive', 0)
        regime_kr = {
            'trending': '추세 진행',
            'ranging': '박스권',
            'unclear': '불명확',
        }.get(regime, regime or '미확정')
        
        gate_status = "진입 허용" if regime == 'trending' and consecutive >= 3 else "진입 차단 중"
        
        # 레이첼
        adv = self._state.get('advisory_action')
        if adv:
            conf = self._state.get('advisory_confidence', 0) * 100
            remaining_h = self._state.get('advisory_remaining_h', 0)
            adv_summary = f"{adv} (확신도 {conf:.0f}%, 만료까지 {remaining_h:.1f}H)"
        else:
            adv_summary = "4H advisory 없음 → 규칙 기반 판단"
        
        # 신호
        signal = self._state.get('signal')
        signal_kr = {
            'entry_ok': '롱 진입 가능',
            'hold': '관망',
            'entry_sell': '숏 진입 가능',
        }.get(signal, signal or '미확정')
        
        current = self._state.get('current_price')
        ema = self._state.get('ema_price')
        signal_detail = ""
        if current and ema:
            if current > ema:
                signal_detail = f"현재가 ¥{current:,.0f} > EMA ¥{ema:,.0f}"
            else:
                signal_detail = f"현재가 ¥{current:,.0f} < EMA ¥{ema:,.0f}"
        
        # 결론
        has_pos = self._state.get('has_position')
        if has_pos:
            conclusion = "포지션 보유 중 — 청산 조건 감시"
        else:
            if signal == 'entry_ok':
                conclusion = "롱 진입 기회"
            elif signal == 'entry_sell':
                conclusion = "숏 진입 기회"
            else:
                conclusion = "진입 조건 미충족"
        
        # 포지션 요약
        if has_pos:
            entry = self._state.get('entry_price')
            stop = self._state.get('stop_price')
            size = self._state.get('position_size', 0)
            side = self._state.get('position_side')
            
            unrealized_jpy = 0
            unrealized_pct = 0
            stop_pnl = 0
            
            if entry and current and size:
                if side == 'long':
                    unrealized_jpy = (current - entry) * size
                    stop_pnl = (stop - entry) * size if stop else 0
                else:
                    unrealized_jpy = (entry - current) * size
                    stop_pnl = (entry - stop) * size if stop else 0
                unrealized_pct = unrealized_jpy / (entry * size) * 100
            
            realized_pnl = self._state.get('realized_pnl_today', 0)
            
            position_summary = (
                f"포지션: {side or '??'} {size} BTC  (진입가 ¥{entry:,.0f})\n"
                f"  미실현: ¥{unrealized_jpy:+,.0f}  ({unrealized_pct:+.2f}%)\n"
                f"  스탑 도달 시: ¥{stop_pnl:+,.0f}  (스탑 ¥{stop:,.0f})\n"
                f"  오늘 확정이익: ¥{realized_pnl:+,.0f}"
            )
        else:
            realized_pnl = self._state.get('realized_pnl_today', 0)
            position_summary = f"포지션: 없음  /  오늘 확정이익: ¥{realized_pnl:+,.0f}"
        
        text = (
            f"🧠 [{self._exchange}·BTC] {_format_time(time.time())}  (판단 사이클 · 5분 요약)\n"
            f"──────────────────────────\n"
            f"체제: {regime_kr} · {consecutive}회 연속 ({gate_status})\n"
            f"레이첼: {adv_summary}\n"
            f"신호: {signal_kr} — {signal_detail}\n"
            f"결론: {conclusion}\n"
            f"──────────────────────────\n"
            f"{position_summary}"
        )
        
        await _send_telegram(self._bot_token, self._chat_id, text)
        self._last_periodic_send = time.time()

    async def start(self) -> None:
        """비동기 태스크 시작."""
        if self._task and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._flush_loop(), name="log_transaction")

    async def stop(self) -> None:
        """비동기 태스크 정지 + 최종 전송."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _flush_loop(self) -> None:
        """5분 주기 정기 요약 전송 (판단 도메인 전용)."""
        while True:
            await asyncio.sleep(self._interval)
            if self._domain == "judge":
                await self._send_periodic_summary()


# ── INFO 다이제스트 (HeartBeat, Deprecated) ──────────────────────

# Deprecated: TelegramTransactionHandler로 대체
class TelegramDigestHandler(logging.Handler):
    """INFO 레벨만 버퍼링 → 배치로 도메인 채널 전송.

    domain 파라미터:
        None  — 모든 INFO 수집 (레거시 동작)
        'judge'    — JUDGE_PREFIXES에 속하는 logger만 수집
        'punisher' — PUNISHER_PREFIXES 또는 미분류 logger만 수집
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        exchange: str = "??",
        interval_sec: int = 300,
        domain: str | None = None,
    ):
        super().__init__(level=logging.INFO)
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._exchange = exchange.upper()
        self._interval = interval_sec
        self._domain = domain  # 'judge' | 'punisher' | None
        self._buffer: list[tuple[float, str]] = []  # (created, message)
        self._task: asyncio.Task | None = None

    def emit(self, record: logging.LogRecord) -> None:
        # INFO만 수집 (DEBUG 제외, WARNING 이상 제외)
        if record.levelno != logging.INFO:
            return
        # 도메인 필터
        if self._domain is not None:
            record_domain = _get_domain(record.name)
            if self._domain == "judge" and record_domain != "judge":
                return
            if self._domain == "punisher" and record_domain == "judge":
                return
        self._buffer.append((record.created, record.getMessage()))

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._flush_loop(), name="log_digest")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # 남은 버퍼 최종 전송
        if self._buffer:
            await self._flush()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            if self._buffer:
                await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            return
        items = self._buffer[:100]
        self._buffer = self._buffer[100:]
        lines = [f"{_format_time(ts)} {msg}" for ts, msg in items]
        text = (
            f"📋 [{self._exchange}] 활동 로그 ({self._interval // 60}분, {len(items)}건)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(lines)
        )
        if len(self._buffer) > 0:
            text += f"\n… 외 {len(self._buffer)}건 다음 배치"
        await _send_telegram(self._bot_token, self._chat_id, text)


# ── WARNING+ 즉시 알림 (실행 도메인 채널) ───────────────────

class TelegramAlertHandler(logging.Handler):
    """WARNING 이상 → Save Us 그룹 즉시 전송 (5초 디바운스)."""

    LEVEL_EMOJI = {
        logging.WARNING: "⚠️",
        logging.ERROR: "🚨",
        logging.CRITICAL: "🔴",
    }

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        exchange: str = "??",
        debounce_sec: float = 5.0,
    ):
        super().__init__(level=logging.WARNING)
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._exchange = exchange.upper()
        self._debounce = debounce_sec
        self._last_sent: dict[str, float] = {}  # key → last send time
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        # 디바운스: 동일 logger+level 조합 5초 이내 중복 스킵
        key = f"{record.name}:{record.levelno}"
        now = time.time()
        if now - self._last_sent.get(key, 0) < self._debounce:
            return
        self._last_sent[key] = now

        emoji = self.LEVEL_EMOJI.get(record.levelno, "⚠️")
        text = (
            f"{emoji} [{record.levelname}] [{self._exchange}]\n"
            f"{record.getMessage()}"
        )
        if record.exc_info and record.exc_info[0] is not None:
            text += f"\n{self.format(record)}"

        if self._loop and self._loop.is_running():
            self._loop.create_task(
                _send_telegram(self._bot_token, self._chat_id, text)
            )


# ── 세팅 헬퍼 ───────────────────────────────────────

_handlers: list[TelegramTransactionHandler | TelegramDigestHandler | TelegramAlertHandler] = []


async def setup_telegram_logging(exchange: str) -> None:
    """Telegram 핸들러를 루트 로거에 등록 + 비동기 태스크 시작.

    환경변수:
        TELEGRAM_BOT_TOKEN         — 공유 봇 토큰 (필수)
        TELEGRAM_HEARTBEAT_CHAT_ID — 판단 도메인 채널 (Judge INFO+, WARNING+ 이중 전송)
        TELEGRAM_SAVEUS_CHAT_ID    — 실행 도메인 채널 (Punisher INFO+, WARNING+ 즉시)
        LOG_DIGEST_INTERVAL_SEC    — 다이제스트 주기 (기본 300초)

    라우팅:
        - logger prefix → JUDGE_PREFIXES  : 판단 도메인 채널
        - logger prefix → PUNISHER_PREFIXES : 실행 도메인 채널
        - WARNING+ : 양쪽 채널에 모두 전송 (이중 안전)
        - 미분류(shared/api) : 실행 도메인 채널로 fallback
    """
    import os

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return

    root = logging.getLogger()
    loop = asyncio.get_running_loop()

    existing_types = {type(h) for h in _handlers}

    judge_chat = os.environ.get("TELEGRAM_HEARTBEAT_CHAT_ID", "")
    punisher_chat = os.environ.get("TELEGRAM_SAVEUS_CHAT_ID", "")
    digest_interval = int(os.environ.get("LOG_DIGEST_INTERVAL_SEC", "300"))

    # 판단 도메인 핸들러 (Judge 채널, JUDGE logger만 수집)
    if judge_chat:
        judge_registered = any(
            isinstance(h, TelegramTransactionHandler) and getattr(h, "_domain", None) == "judge"
            for h in _handlers
        )
        if not judge_registered:
            h_judge = TelegramTransactionHandler(
                bot_token, judge_chat,
                exchange=exchange, interval_sec=digest_interval, domain="judge",
            )
            root.addHandler(h_judge)
            await h_judge.start()
            _handlers.append(h_judge)

    # 실행 도메인 핸들러 (Punisher 채널, Punisher/shared logger 수집)
    if punisher_chat:
        punisher_registered = any(
            isinstance(h, TelegramTransactionHandler) and getattr(h, "_domain", None) == "punisher"
            for h in _handlers
        )
        if not punisher_registered:
            h_punisher = TelegramTransactionHandler(
                bot_token, punisher_chat,
                exchange=exchange, interval_sec=digest_interval, domain="punisher",
            )
            root.addHandler(h_punisher)
            await h_punisher.start()
            _handlers.append(h_punisher)

    # WARNING+ 즉시 알림 — 실행 도메인 채널 (기존 TelegramAlertHandler 재사용)
    if punisher_chat and TelegramAlertHandler not in existing_types:
        h_alert = TelegramAlertHandler(
            bot_token, punisher_chat, exchange=exchange,
        )
        h_alert.set_loop(loop)
        root.addHandler(h_alert)
        _handlers.append(h_alert)


async def shutdown_telegram_logging() -> None:
    """비동기 태스크 정리 + 잔여 버퍼 전송."""
    for h in _handlers:
        if hasattr(h, "stop"):
            await h.stop()
    _handlers.clear()
