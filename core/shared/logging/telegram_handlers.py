"""
Telegram 로그 핸들러 — 도메인별 채널 분리 전송.

핸들러:
- TelegramTransactionHandler : 트랜잭션 기반 핸들러 (신규, 권장)
  - 판단 도메인: 5분 정기 요약 + 시그널/체제/advisory/FNG 변경 시 즉시 전송
  - 실행 도메인: 진입/청산/스탑타이트닝 감지 시 즉시 전송
- TelegramDigestHandler : INFO 버퍼링 배치 전송 (레거시, Deprecated)
- TelegramAlertHandler : WARNING+ → 실행 도메인 그룹 즉시 (5초 디바운스, logger+level 키)

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
    # NOTE: core.strategy.base_trend 제거 — 로거명 core.judge.candle_loop/core.judge.signal로 이관
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


# ── 진화 도메인 라우팅 ────────────────────────────────

EVOLUTION_PREFIXES: frozenset[str] = frozenset({
    "core.judge.evolution",
    "core.evolution",
    "api.services.hypotheses_service",
    "api.services.lessons_service",
    "api.services.cycle_report_service",
})


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
            'fng_score': None,
            'fng_label': None,
            # 시그널 상세 (candle_loop 로그에서 파싱)
            'ema_slope_pct': None,
            'rsi': None,
            # 전략 파라미터 (seed_telegram_strategy_params로 주입)
            'entry_rsi_min': 40.0,
            'entry_rsi_max': 65.0,
            'entry_rsi_min_short': 35.0,
            'entry_rsi_max_short': 60.0,
            # 포지션 상태
            'has_position': False,
            'position_side': None,
            'entry_price': None,
            'position_size': None,
            'stop_price': None,
            'current_price': None,
            'ema_price': None,
            'realized_pnl_today': 0.0,
            # 판단 도메인 최종 결정 (orchestrator + JIT gate 로그에서 파싱)
            'decision_action': None,      # 최종 action (entry_long / hold / tighten_stop 등)
            'decision_size_pct': None,    # 사이즈 (0~1 소수)
            'decision_confidence': None,  # 확신도 (0~1 소수)
            'decision_stop_loss': None,   # SL 가격 (진입 결정 시)
            # JIT Advisory Gate 결과
            'jit_decision': None,         # 'GO' | 'NO_GO' | 'ADJUST' | None
            'jit_reasoning': None,        # JIT 자문 사유 (NO_GO/ADJUST 시)
            # 승인 게이트용 — 오케스트레이터 로그에서 파싱 (approval mode 표시용)
            'signal_confidence': None,
            'signal_size_pct': None,
            # 박스 전략 상태 (ranging 체제 시)
            'box_upper': None,   # 박스 상단가
            'box_lower': None,   # 박스 하단가
            'box_detected': False,  # 박스 감지 여부
            # 진입/청산 이벤트 (즉시 전송 후 None으로 클리어)
            'entry_event': None,
            'close_event': None,
            'stop_tighten_event': None,
            # ws_cross / entry_timeframe 진입 파라미터
            'entry_mode': 'market',        # 'market' | 'ws_cross'
            'entry_timeframe': None,       # '1h' | None — None이면 basis_timeframe(4h)
            # armed 상태 (ws_cross 모드 전용)
            'armed_direction': None,       # 'short' | 'long' | None
            'armed_ema': None,             # armed EMA 가격
            'armed_expire_at': 0.0,        # unix timestamp
            'armed_expire_sec': 14400.0,   # arm 유효 시간(초) — seed로 주입
        }
        self._advisory_warn_last: float = 0  # v1 폴백 WARNING 마지막 전송 시각
        self._last_periodic_send: float = 0  # 마지막 정기 전송 시각

    def emit(self, record: logging.LogRecord) -> None:
        """로그 메시지 파싱 → 상태 업데이트 + 즉시 전송 판단."""
        # WARNING 이상 무시
        if record.levelno > logging.INFO:
            return

        msg = record.getMessage()

        # 파싱은 레벨/도메인 무관하게 항상 수행 (DEBUG 포함)
        # candle_loop는 hold 상태에서 DEBUG로 찍히지만 price/ema 등은 여기서 파싱해야 함
        # 즉시 전송 판단은 _parse_and_update 내부에서 domain 체크로 처리
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
        
        # signal + ema_slope_pct + rsi + ema + price (candle_loop 상세 로그)
        # [BoxMgr] 로그는 무시 — 박스 전략과 추세 전략이 같은 pair를 처리하므로
        # 두 전략의 signal이 교대로 파싱되어 매분 신호 변경으로 감지되는 문제 방지
        is_box_log = (
            '[BoxMgr]' in msg
            or 'box_mean_reversion' in msg
            or 'BoxMeanReversion' in msg
        )
        m = re.search(r'signal=(\w+) ema_slope_pct=([-\d.]+|N/A) rsi=([\d.]+|N/A) ema=([\d.]+|N/A) price=([\d.]+)', msg)
        if m and not is_box_log:
            new_signal = m.group(1)
            if m.group(2) != 'N/A':
                self._state['ema_slope_pct'] = float(m.group(2))
            if m.group(3) != 'N/A':
                self._state['rsi'] = float(m.group(3))
            if m.group(4) != 'N/A':
                self._state['ema_price'] = float(m.group(4))
            self._state['current_price'] = float(m.group(5))
            if self._state['signal'] != new_signal:
                self._state['prev_signal'] = self._state['signal']
                self._state['signal'] = new_signal
                if new_signal not in ('long_setup', 'short_setup'):
                    self._state['signal_confidence'] = None
                    self._state['signal_size_pct'] = None
                if self._domain == "judge" and self._loop and self._state['prev_signal'] is not None:
                    self._loop.create_task(self._send_signal_change())
        else:
            # 기존 signal= 파싱 폴백 (상세 포맷 없는 로그)
            m = re.search(r'signal=(\w+)', msg)
            if m and not is_box_log:
                new_signal = m.group(1)
                if self._state['signal'] != new_signal:
                    self._state['prev_signal'] = self._state['signal']
                    self._state['signal'] = new_signal
                    if new_signal not in ('long_setup', 'short_setup'):
                        self._state['signal_confidence'] = None
                        self._state['signal_size_pct'] = None
                    if self._domain == "judge" and self._loop and self._state['prev_signal'] is not None:
                        self._loop.create_task(self._send_signal_change())

        # ── 박스 로그 파싱 (신호 + 박스 범위) ────────────────────────────────
        # 신호 파싱은 regime=ranging일 때만 — ranging 아닐 때 박스 로그가 추세 신호 덮어쓰는 것 방지
        if is_box_log:
            m_box = re.search(
                r'signal=(\w+) ema_slope_pct=(?:[-\d.]+|N/A) rsi=(?:[\d.]+|N/A) ema=(?:[\d.]+|N/A) price=([\d.]+)',
                msg,
            )
            if m_box:
                self._state['current_price'] = float(m_box.group(2))
                # regime=ranging일 때만 박스 신호로 state 갱신
                if self._state.get('regime_status') == 'ranging':
                    box_signal = m_box.group(1)
                    if self._state['signal'] != box_signal:
                        self._state['prev_signal'] = self._state['signal']
                        self._state['signal'] = box_signal
                        if box_signal not in ('long_setup', 'short_setup'):
                            self._state['signal_confidence'] = None
                            self._state['signal_size_pct'] = None
                        if self._domain == "judge" and self._loop and self._state['prev_signal'] is not None:
                            self._loop.create_task(self._send_signal_change())
            # 박스 범위 파싱: "박스 ¥11,400,000~¥12,200,000"
            m_bounds = re.search(r'박스 ¥([\d,]+)~¥([\d,]+)', msg)
            if m_bounds:
                self._state['box_lower'] = float(m_bounds.group(1).replace(',', ''))
                self._state['box_upper'] = float(m_bounds.group(2).replace(',', ''))
                self._state['box_detected'] = True
            # 박스 미감지
            if '박스 미감지' in msg:
                self._state['box_detected'] = False
                self._state['box_upper'] = None
                self._state['box_lower'] = None
        # 구 포맷: "확신도=0.72 사이즈=0.50"
        m = re.search(r'확신도=([\d.]+)\s+사이즈=([\d.]+)', msg)
        if m:
            conf = float(m.group(1))
            size = float(m.group(2))
            self._state['signal_confidence'] = conf
            self._state['signal_size_pct'] = size
            self._state['decision_confidence'] = conf
            self._state['decision_size_pct'] = size  # 이미 0~1 소수
        # 신 포맷(_build_narrative): "사이즈 50%, 확신도 0.72"
        m = re.search(r'사이즈 (\d+)%, 확신도 ([\d.]+)', msg)
        if m:
            size = float(m.group(1)) / 100
            conf = float(m.group(2))
            self._state['decision_size_pct'] = size
            self._state['decision_confidence'] = conf
            self._state['signal_confidence'] = conf
            self._state['signal_size_pct'] = size
        # "판단=entry_long → 안전장치 통과" 포맷에서 action 파싱
        m = re.search(r'판단=(entry_\w+|tighten_stop|exit|hold)', msg)
        if m:
            self._state['decision_action'] = m.group(1)
        # SL 파싱 (진입 결정 로그: "사이즈 50%, 확신도 0.72, SL ¥11,400,000.")
        m = re.search(r', SL ¥(\d[\d,]*)', msg)
        if m:
            self._state['decision_stop_loss'] = float(m.group(1).replace(',', ''))

        # ── JIT Advisory Gate 결과 파싱 ──────────────────────────────────
        # GO: "[JIT][xxxx] btc_jpy GO — action=entry_long size=50% conf=0.80. 사유: ..."
        m = re.search(r'\bGO — action=([\w]+) size=(\d+)%', msg)
        if m:
            self._state['jit_decision'] = 'GO'
            self._state['decision_action'] = m.group(1)
            # size는 GO 시 변경 없음, 기존 decision_size_pct 유지
        # NO_GO: "[JIT][xxxx] btc_jpy NO_GO — action=entry_long → hold. 사유: ..."
        m = re.search(r'\bNO_GO — .*사유: (.+)', msg)
        if m:
            self._state['jit_decision'] = 'NO_GO'
            self._state['jit_reasoning'] = m.group(1).strip()[:120]
            self._state['decision_action'] = 'hold'
        # fail-soft NO_GO: "[JIT][xxxx] btc_jpy fail-soft NO_GO — ..."
        m = re.search(r'fail-soft NO_GO — (.+)', msg)
        if m:
            self._state['jit_decision'] = 'NO_GO'
            self._state['jit_reasoning'] = f"타임아웃/오류: {m.group(1).strip()[:80]}"
            self._state['decision_action'] = 'hold'
        # ADJUST: "[JIT][xxxx] btc_jpy ADJUST — size 50%→30% action entry_long→entry_long 사유: ..."
        m = re.search(r'\bADJUST — size \d+%→(\d+)% action [\w]+→([\w]+) 사유: (.+)', msg)
        if m:
            self._state['jit_decision'] = 'ADJUST'
            self._state['decision_size_pct'] = float(m.group(1)) / 100
            self._state['decision_action'] = m.group(2)
            self._state['jit_reasoning'] = m.group(3).strip()[:120]

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
        
        # FNG
        m = re.search(r'FNG.*score=(\d+) \(([^)]+)\)', msg)
        if m:
            score = int(m.group(1))
            label = m.group(2)
            self._state['fng_score'] = score
            self._state['fng_label'] = label

        # ── WS 진입 armed 상태 파싱 ──────────────────────────────────
        # "short armed @ EMA ¥12,154,770 (slope=-0.0610%)"
        # "long armed @ EMA ¥12,154,770 (slope=0.0610%)"
        m = re.search(r'(short|long) armed @ EMA ¥([\d,]+)', msg)
        if m:
            self._state['armed_direction'] = m.group(1)
            self._state['armed_ema'] = float(m.group(2).replace(',', ''))
            self._state['armed_expire_at'] = time.time() + self._state.get('armed_expire_sec', 14400.0)

        # "armed 해제 (조건 소멸)" or "arm 만료 → 해제"
        if 'armed 해제' in msg or 'arm 만료' in msg:
            self._state['armed_direction'] = None
            self._state['armed_ema'] = None
            self._state['armed_expire_at'] = 0.0

        # "WS EMA 돌파 감지 direction=short price=¥12,100,000 ema=¥12,154,770 → 진입 트리거"
        if 'WS EMA 돌파 감지' in msg and '진입 트리거' in msg:
            # 트리거 후 armed 해제 (manager에서 pop하지만 여기서도 클리어)
            self._state['armed_direction'] = None
            self._state['armed_ema'] = None
            self._state['armed_expire_at'] = 0.0


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
        
        # ── 판단 도메인으로부터 받은 결과 ──────────────────────────
        jit = self._state.get('jit_decision')
        jit_reasoning = self._state.get('jit_reasoning') or ''
        decision_action = self._state.get('decision_action')
        decision_size = self._state.get('decision_size_pct')
        decision_conf = self._state.get('decision_confidence')

        _ACTION_KR = {
            'entry_long':   '롱 진입',
            'entry_short':  '숏 진입',
            'add_position': '피라미딩 추가',
            'hold':         '진입 보류',
        }
        action_kr = _ACTION_KR.get(decision_action or '', decision_action or side_kr + ' 진입')

        # 판단 흐름 요약: 어떤 과정으로 이 결론이 나왔는가
        if jit == 'GO':
            judge_flow = f"규칙 기반 판단 → JIT 자문 ✅ GO → {action_kr}"
        elif jit == 'ADJUST':
            judge_flow = f"규칙 기반 판단 → JIT 자문 ⚙️ ADJUST → {action_kr}"
            if jit_reasoning:
                judge_flow += f"\n  (JIT 조정 사유: {jit_reasoning[:70]})"
        else:
            # JIT 없음 (TRADING_MODE=v1 규칙 기반)
            judge_flow = f"규칙 기반 판단 → {action_kr}"

        # 판단 도메인 전달값 명시
        decision_parts = []
        if decision_size is not None:
            decision_parts.append(f"사이즈 {decision_size * 100:.0f}%")
        if decision_conf is not None:
            decision_parts.append(f"확신도 {decision_conf:.2f}")
        decision_detail = "  · " + " / ".join(decision_parts) if decision_parts else ""
        
        text = (
            f"⚡ [{self._exchange}·BTC] {_format_time(time.time())}  🟢 진입  (실행 사이클 · {side_kr})\n"
            f"──────────────────────────\n"
            f"진입가 ¥{entry_price:,.0f}  /  {size} BTC\n"
            f"스탑 ¥{stop:,.0f}  (리스크 {risk_pct:.2f}%,  ¥{risk_jpy:,.0f})\n"
            f"──────────────────────────\n"
            f"판단 흐름: {judge_flow}\n"
            + (f"{decision_detail}\n" if decision_detail else "")
        )
        
        await _send_telegram(self._bot_token, self._chat_id, text.rstrip())
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
        
        entry = self._state.get('entry_price') or 0
        current = self._state.get('current_price') or 0
        stop = self._state.get('stop_price') or 0
        size = self._state.get('position_size', 0)
        
        # 손익 계산
        pnl_jpy = 0
        pnl_pct = 0
        if entry > 0 and current > 0 and size > 0:
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

        # 스탑이 실제로 변경되지 않으면 발송 생략 (prev==curr 시 오해 방지)
        if diff == 0:
            self._state['stop_tighten_event'] = None
            return

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
            'long_setup': '롱 진입 가능',
            'hold': '관망',
            'short_setup': '숏 진입 가능',
        }
        
        prev_kr = signal_kr.get(prev, prev)
        curr_kr = signal_kr.get(curr, curr)
        
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
            if curr == 'long_setup':
                conclusion = "롱 진입 기회"
            elif curr == 'short_setup':
                conclusion = "숏 진입 기회"
            else:
                conclusion = "진입 조건 미충족"
        
        # ── 판단 방식 + 실행 도메인 전달값 ─────────────────────────────
        jit = self._state.get('jit_decision')
        decision_action = self._state.get('decision_action')
        decision_size = self._state.get('decision_size_pct')
        decision_conf = self._state.get('decision_confidence')

        # 판단 방식
        if jit == 'GO':
            judge_method = "규칙 기반 판단 → JIT 자문 ✅ GO"
        elif jit == 'NO_GO':
            judge_method = "규칙 기반 판단 → JIT 자문 🚫 NO_GO (진입 차단)"
        elif jit == 'ADJUST':
            judge_method = "규칙 기반 판단 → JIT 자문 ⚙️ ADJUST (사이즈 조정)"
        else:
            judge_method = "규칙 기반 판단"

        # 실행 도메인에 전달하는 값
        _ACTION_KR = {
            'entry_long': '롱 진입', 'entry_short': '숏 진입',
            'add_position': '피라미딩', 'hold': '홀드',
            'tighten_stop': '스탑 조임', 'exit': '청산',
        }
        if decision_action and decision_action not in ('hold', None):
            action_kr = _ACTION_KR.get(decision_action, decision_action)
            size_str = f"사이즈 {decision_size * 100:.0f}%" if decision_size is not None else ""
            conf_str = f"확신도 {decision_conf:.2f}" if decision_conf is not None else ""
            detail = " / ".join(x for x in [size_str, conf_str] if x)
            delivery = (
                f"→ 실행 도메인 전달: {action_kr} ({detail})\n"
                f"   (실행 도메인은 이 결정대로 주문을 냅니다)"
            )
        else:
            delivery = "→ 실행 도메인 전달: 홀드 (주문 없음)"
        
        text = (
            f"🔮 [{self._exchange}·BTC] {_format_time(time.time())}  ★ 신호 변경  (판단 사이클)\n"
            f"──────────────────────────\n"
            f"이전: {prev_kr}  →  현재: {curr_kr}\n"
            f"판단 방식: {judge_method}\n"
            + (f"  · {signal_detail}\n" if signal_detail else "")
            + f"결론: {conclusion}\n"
            f"{delivery}"
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
        
        # 체제 허용/차단 (체제 이벤트 전용 — 실제 entry signal은 별도 판단)
        gate_status = "체제 허용 (신호 대기)" if curr == 'trending' and consecutive >= 3 else "진입 차단 중"
        
        if changed and prev:
            prev_kr = regime_kr.get(prev, prev)
            conclusion = f"{curr_kr} 전환 감지 → {gate_status}"
            text = (
                f"🔮 [{self._exchange}·BTC] {_format_time(time.time())}  ★ 체제 전환  (판단 사이클 · 4H 체제 갱신)\n"
                f"──────────────────────────\n"
                f"{prev_kr} → {curr_kr} 전환\n"
                f"  BB폭 {bb:.1f}%  /  가격범위 {range_pct:.1f}%\n"
                f"  {curr_kr} 연속 {consecutive}회 → {gate_status}\n"
                f"→ {conclusion}"
            )
        else:
            conclusion = f"{curr_kr} 유지 → {gate_status}"
            text = (
                f"🔮 [{self._exchange}·BTC] {_format_time(time.time())}  (판단 사이클 · 4H 체제 갱신)\n"
                f"──────────────────────────\n"
                f"체제: {curr_kr} · {consecutive}회 연속\n"
                f"  BB폭 {bb:.1f}%  /  가격범위 {range_pct:.1f}%\n"
                f"→ {conclusion}"
            )
        
        await _send_telegram(self._bot_token, self._chat_id, text)

    async def _send_fng_update(self) -> None:
        """FNG 갱신 즉시 전송."""
        score = self._state.get('fng_score')
        label = self._state.get('fng_label')
        
        text = (
            f"🔮 [{self._exchange}·BTC] {_format_time(time.time())}  (판단 사이클 · FNG 갱신)\n"
            f"──────────────────────────\n"
            f"시장 심리: {score} ({label})"
        )
        
        await _send_telegram(self._bot_token, self._chat_id, text)

    async def _send_periodic_summary(self) -> None:
        """5분 정기 요약 전송. 판단 도메인(judge) HeartBeat 채널에만 전송."""
        # _flush_loop는 judge 핸들러에서만 시작되므로 여기는 항상 judge

        # 체제
        regime = self._state.get('regime_status')
        consecutive = self._state.get('regime_consecutive', 0)
        regime_kr = {
            'trending': '추세 진행',
            'ranging': '박스권',
            'unclear': '불명확',
        }.get(regime, regime or '미확정')

        # 방향 표시 (trending일 때: 현재가 vs EMA로 상향↑/하향↓ 판단)
        if regime == 'trending':
            _cur = self._state.get('current_price')
            _ema = self._state.get('ema_price')
            if _cur and _ema:
                regime_kr += " ↑" if _cur > _ema else " ↓"

        # gate_status: RegimeGate + 실제 signal 상태를 모두 반영
        _signal_now = self._state.get('signal')
        regime_gate_ok = (
            (regime == 'trending' and consecutive >= 3) or
            (regime == 'ranging' and consecutive >= 3)
        )
        gate_icon = "✅" if regime_gate_ok else "❌"
        if not regime_gate_ok:
            gate_status = "진입 차단 중"
        elif _signal_now == 'long_setup':
            gate_status = "신호 발생 (롱)"
        elif _signal_now == 'short_setup':
            gate_status = "신호 발생 (숏)"
        elif regime == 'ranging':
            gate_status = "체제OK · 박스 대기"
        else:
            gate_status = "체제OK · 신호 대기"

        # ── 최근 판단 결과 (실행 도메인에 전달한 값) ────────────────────
        decision_action = self._state.get('decision_action')
        decision_size = self._state.get('decision_size_pct')
        decision_conf = self._state.get('decision_confidence')
        jit = self._state.get('jit_decision')
        jit_reasoning = self._state.get('jit_reasoning') or ''

        _ACTION_KR_SUM = {
            'entry_long': '롱 진입', 'entry_short': '숏 진입',
            'add_position': '피라미딩', 'hold': '홀드',
            'tighten_stop': '스탑 조임', 'exit': '청산', 'blocked': '안전장치 차단',
        }
        if decision_action:
            action_kr = _ACTION_KR_SUM.get(decision_action, decision_action)
            jit_label = {'GO': ' (JIT ✅)', 'ADJUST': ' (JIT ⚙️ 조정)', 'NO_GO': ' (JIT 🚫 차단)'}.get(jit or '', '')
            size_str = f"사이즈 {decision_size * 100:.0f}%" if decision_size is not None else ""
            conf_str = f"확신도 {decision_conf:.2f}" if decision_conf is not None else ""
            detail = " / ".join(x for x in [size_str, conf_str] if x)
            judge_summary = f"{action_kr}{jit_label}" + (f" ({detail})" if detail else "")
            if jit in ('NO_GO', 'ADJUST') and jit_reasoning:
                judge_summary += f"\n  └ {jit_reasoning[:60]}"
        else:
            judge_summary = "아직 없음 (신호 대기)"

        # 승인 모드
        import os as _os
        _approval_mode = _os.environ.get("APPROVAL_MODE", "").lower()
        _max_size = float(_os.environ.get("AUTO_APPROVAL_MAX_SIZE", "0.40"))
        _min_conf = float(_os.environ.get("AUTO_APPROVAL_MIN_CONFIDENCE", "0.65"))
        if _approval_mode == "auto":
            _min_conf_pct = int(_min_conf * 100)
            _max_size_pct = int(_max_size * 100)
            # 신호 있을 때: 실제 confidence/size 기반으로 자동/수동 승인 여부 동적 표시
            _sig_now = self._state.get('signal')
            _sig_conf = self._state.get('signal_confidence')
            _sig_size = self._state.get('signal_size_pct')
            if _sig_now in ('long_setup', 'short_setup') and _sig_conf is not None and _sig_size is not None:
                _conf_ok = _sig_conf >= _min_conf
                _size_ok = _sig_size <= _max_size
                if _conf_ok and _size_ok:
                    _conf_disp = int(_sig_conf * 100)
                    _size_disp = int(_sig_size * 100)
                    approval_line = f"승인: 🤖 자동 승인 예정 (신뢰도 {_conf_disp}% ✅, 사이즈 {_size_disp}% ✅)"
                else:
                    _reasons: list[str] = []
                    if not _conf_ok:
                        _reasons.append(f"신뢰도 {int(_sig_conf * 100)}% < {_min_conf_pct}%")
                    if not _size_ok:
                        _reasons.append(f"사이즈 {int(_sig_size * 100)}% > {_max_size_pct}%")
                    approval_line = f"승인: 👆 수동 승인 필요 ({', '.join(_reasons)})"
            else:
                approval_line = f"승인: 🤖 자동 (신뢰도≥{_min_conf_pct}%, 사이즈≤{_max_size_pct}%)"
        elif _approval_mode in ("manual", "true", "1"):
            approval_line = "승인: 👆 수동 (1클릭 승인 대기)"
        else:
            approval_line = "승인: ⚡ 직통 (승인 게이트 없음)"

        # 진입 조건 — regime에 따라 추세/박스 분기
        current = self._state.get('current_price')
        ema = self._state.get('ema_price')
        ema_slope = self._state.get('ema_slope_pct')
        rsi = self._state.get('rsi')
        box_upper = self._state.get('box_upper')
        box_lower = self._state.get('box_lower')
        box_detected = self._state.get('box_detected', False)

        condition_lines = []
        direction_label = "숏"  # 기본값, 아래에서 결정

        # entry_timeframe 라벨 (1H 모드면 조건 옆에 표시)
        _entry_tf = self._state.get('entry_timeframe')  # '1h' | None
        _tf_label = " (1H)" if _entry_tf and _entry_tf.lower() in ('1h', '1') else ""

        if regime == 'ranging':
            # ── 박스역추세 조건 ─────────────────────────────────────────
            # 방향: signal 기반 (near_lower→롱, near_upper→숏)
            _sig = self._state.get('signal')
            if _sig == 'long_setup':
                direction_label = "롱"
            elif _sig == 'short_setup':
                direction_label = "숏"
            elif current is not None and box_upper is not None and box_lower is not None:
                mid = (box_upper + box_lower) / 2
                direction_label = "롱" if current < mid else "숏"
            else:
                direction_label = "박스"

            c1 = "✅" if box_detected else "❌"
            if box_detected and box_upper is not None and box_lower is not None:
                width_pct = (box_upper - box_lower) / box_lower * 100 if box_lower else 0
                condition_lines.append(
                    f" {c1} ① 박스 감지    ¥{box_lower:,.0f}~¥{box_upper:,.0f} (폭 {width_pct:.1f}%)"
                )
            else:
                condition_lines.append(" ❌ ① 박스 감지    미감지")

            if box_detected and current is not None and box_upper is not None and box_lower is not None:
                near_bound_pct = 0.5
                band = (box_upper - box_lower) * (near_bound_pct / 100)
                near_lower = current <= box_lower + band
                near_upper = current >= box_upper - band
                if near_lower:
                    c2 = "✅"
                    condition_lines.append(f" {c2} ② 가격 위치    하단 근처 ¥{current:,.0f} (하단 ¥{box_lower:,.0f})")
                elif near_upper:
                    c2 = "✅"
                    condition_lines.append(f" {c2} ② 가격 위치    상단 근처 ¥{current:,.0f} (상단 ¥{box_upper:,.0f})")
                else:
                    c2 = "❌"
                    condition_lines.append(f" {c2} ② 가격 위치    박스 중간 ¥{current:,.0f} — 경계 대기")
            elif current is not None:
                condition_lines.append(f" ❓ ② 가격 위치    ¥{current:,.0f} (박스 미감지)")
            else:
                condition_lines.append(" ❓ ② 가격 위치    데이터 없음")

            if rsi is not None:
                SHORT_RSI_MAX = self._state.get('entry_rsi_max_short', 60.0)
                LONG_RSI_MIN = self._state.get('entry_rsi_min', 40.0)
                if direction_label == "숏":
                    c3 = "✅" if rsi >= SHORT_RSI_MAX else "❌"
                    condition_lines.append(f" {c3} ③ RSI          {rsi:.0f}  (과매수 {SHORT_RSI_MAX:.0f}↑ 필요)")
                else:
                    c3 = "✅" if rsi <= LONG_RSI_MIN else "❌"
                    condition_lines.append(f" {c3} ③ RSI          {rsi:.0f}  (과매도 {LONG_RSI_MIN:.0f}↓ 필요)")
            else:
                condition_lines.append(" ❓ ③ RSI          데이터 없음")

    

        else:
            # ── 추세추종 조건 (trending 또는 unclear) ──────────────────
            # 방향 결정: 현재가 < EMA → 숏 대기, 아니면 롱 대기
            is_short_setup = (current is not None and ema is not None and current < ema)
            direction_label = "숏" if is_short_setup else "롱"

            if current is not None and ema is not None:
                if is_short_setup:
                    c1 = "✅"
                    condition_lines.append(f" {c1} ① 가격 < EMA    ¥{current:,.0f} (EMA ¥{ema:,.0f})")

                    SHORT_SLOPE_TH = -0.05
                    if ema_slope is not None:
                        c2 = "✅" if ema_slope < SHORT_SLOPE_TH else "❌"
                        if ema_slope >= SHORT_SLOPE_TH:
                            gap = abs(ema_slope - SHORT_SLOPE_TH)
                            condition_lines.append(f" {c2} ② EMA 기울기{_tf_label}    지금 {ema_slope:+.2f}% → {SHORT_SLOPE_TH:.2f}% 미만 필요 ({gap:.2f}%p 부족)")
                        else:
                            condition_lines.append(f" {c2} ② EMA 기울기{_tf_label}    {ema_slope:+.2f}% (충족)")
                    else:
                        condition_lines.append(f" ❓ ② EMA 기울기{_tf_label}    데이터 없음")

                    SHORT_RSI_MIN = self._state.get('entry_rsi_min_short', 35.0)
                    SHORT_RSI_MAX = self._state.get('entry_rsi_max_short', 60.0)
                    if rsi is not None:
                        c3 = "✅" if SHORT_RSI_MIN <= rsi <= SHORT_RSI_MAX else "❌"
                        condition_lines.append(f" {c3} ③ RSI 범위      {rsi:.1f}  (허용 {SHORT_RSI_MIN:.0f}~{SHORT_RSI_MAX:.0f})")
                    else:
                        condition_lines.append(" ❓ ③ RSI 범위      데이터 없음")
                else:
                    c1 = "✅" if current > ema else "❌"
                    condition_lines.append(f" {c1} ① 가격 > EMA    ¥{current:,.0f} (EMA ¥{ema:,.0f})")

                    LONG_SLOPE_TH = 0.0
                    if ema_slope is not None:
                        c2 = "✅" if ema_slope >= LONG_SLOPE_TH else "❌"
                        if ema_slope < LONG_SLOPE_TH:
                            condition_lines.append(f" {c2} ② EMA 기울기{_tf_label}    지금 {ema_slope:+.2f}% → {LONG_SLOPE_TH:.2f}% 이상 필요")
                        else:
                            condition_lines.append(f" {c2} ② EMA 기울기{_tf_label}    {ema_slope:+.2f}% (충족)")
                    else:
                        condition_lines.append(f" ❓ ② EMA 기울기{_tf_label}    데이터 없음")

                    LONG_RSI_MIN = self._state.get('entry_rsi_min', 40.0)
                    LONG_RSI_MAX = self._state.get('entry_rsi_max', 65.0)
                    if rsi is not None:
                        c3 = "✅" if LONG_RSI_MIN <= rsi <= LONG_RSI_MAX else "❌"
                        condition_lines.append(f" {c3} ③ RSI 범위      {rsi:.1f}  (허용 {LONG_RSI_MIN:.0f}~{LONG_RSI_MAX:.0f})")
                    else:
                        condition_lines.append(" ❓ ③ RSI 범위      데이터 없음")




        met_count = sum(1 for l in condition_lines if "✅" in l)
        total_count = len(condition_lines)
        has_unmet = any("❌" in l for l in condition_lines)
        signal = self._state.get('signal')

        # 결론
        if regime == 'ranging':
            _unmet_labels = ['박스감지', '가격위치', 'RSI']
        else:
            _unmet_labels = ['가격/EMA', 'EMA기울기', 'RSI']
        _unmet = [
            _unmet_labels[i]
            for i, _l in enumerate(condition_lines)
            if '❌' in _l and i < len(_unmet_labels)
        ]

        has_pos = self._state.get('has_position')
        if has_pos:
            if not has_unmet and condition_lines:
                conclusion = "진입 조건 모두 충족 · 포지션 보유 중 — 추가 진입 유보"
            else:
                conclusion = "포지션 보유 중 — 청산 조건 감시"
        elif decision_action:
            # Decision이 내려진 상태 — 실행 도메인에 넘어간 실제 값
            _dec_action_kr = _ACTION_KR_SUM.get(decision_action, decision_action)
            _jit_tag = {'GO': ' (JIT ✅)', 'ADJUST': ' (JIT ⚙️ 조정)', 'NO_GO': ' (JIT 🚫 차단)'}.get(jit or '', '')
            _parts = [_dec_action_kr + _jit_tag]
            if decision_size is not None:
                _parts.append(f"사이즈 {decision_size * 100:.0f}%")
            if decision_conf is not None:
                _parts.append(f"확신도 {decision_conf * 100:.0f}%")
            _dec_sl = self._state.get('decision_stop_loss')
            if _dec_sl is not None:
                _parts.append(f"SL ¥{_dec_sl:,.0f}")
            conclusion = ' · '.join(_parts)
            if jit in ('NO_GO', 'ADJUST') and jit_reasoning:
                conclusion += f"\n  └ {jit_reasoning[:60]}"
        else:
            # 진입 조건 미충족 — 미충족 항목 명시
            if condition_lines and not has_unmet:
                # 조건 모두 충족 (실제론 드물지만 decision 없음)
                conclusion = f"{direction_label} 진입 조건 충족"
            elif _unmet:
                conclusion = f"{direction_label} 진입 조건 미충족 — {'·'.join(_unmet)}"
            else:
                conclusion = f"{direction_label} 진입 조건 미충족"

        # 시그널 상세 블록 (조건 4개 ✅/❌ 수치 포함)
        if condition_lines:
            sig_block = f"시그널 ({direction_label} {met_count}/{total_count}):\n" + "\n".join(condition_lines)
        elif total_count > 0:
            sig_block = f"시그널: {direction_label} 조건 {met_count}/{total_count} 충족"
        else:
            sig_block = "시그널: 데이터 없음"

        # 체제 지표 상세 (BB폭 / 가격범위)
        bb = self._state.get('regime_bb_width')
        rng = self._state.get('regime_range_pct')
        regime_detail = f"\n  BB폭 {bb:.1f}% / 범위 {rng:.1f}%" if bb is not None and rng is not None else ""

        # 승인 줄 제거: 신호+판단이 같은 사이클에서 연속 실행되므로 "대기" 상태는 실제로 없음
        # 승인 정도 정보는 결론에 이미 포함(단 MANUAL모드에서만 의미 있는 도구)
        _approval_str = ""

        # ── entry_mode / entry_timeframe 표시 줄 ──────────────────────
        _entry_mode_state = self._state.get('entry_mode', 'market')
        _entry_tf_state = self._state.get('entry_timeframe')  # '1h' | None
        if _entry_mode_state == 'ws_cross':
            _tf_desc = " + 1H slope/RSI" if _entry_tf_state and _entry_tf_state.lower() in ('1h', '1') else ""
            _mode_line = f"진입 모드: ⚡ WS 돌파{_tf_desc} (EMA 실시간 감시)"
        elif _entry_tf_state and _entry_tf_state.lower() in ('1h', '1'):
            _mode_line = "진입 모드: 📊 1H slope/RSI + 4H 체제"
        else:
            _mode_line = ""  # 기본(market + 4H)은 표시 생략

        # ── armed 상태 줄 (ws_cross 모드 전용) ────────────────────────
        _armed_dir = self._state.get('armed_direction')   # 'short' | 'long' | None
        _armed_ema_v = self._state.get('armed_ema')
        _armed_expire = self._state.get('armed_expire_at', 0.0)
        if _armed_dir is not None and _armed_ema_v is not None:
            _remain_sec = max(0.0, _armed_expire - time.time())
            _remain_h = int(_remain_sec // 3600)
            _remain_m = int((_remain_sec % 3600) // 60)
            _dir_kr = '숏' if _armed_dir == 'short' else '롱'
            armed_line = f"⚡ WS 대기: {_dir_kr} armed @ ¥{_armed_ema_v:,.0f}  (만료까지 {_remain_h}h {_remain_m:02d}m)"
        elif _entry_mode_state == 'ws_cross':
            armed_line = "⏳ WS 대기: armed 조건 미충족"
        else:
            armed_line = ""  # ws_cross 아니면 표시 안 함

        # 게이트 줄: JIT 모드이면 RegimeGate가 bypass → JIT Advisory 상태 표시
        _trading_mode = _os.environ.get("TRADING_MODE", "v1").lower()
        if _trading_mode == "jit":
            _jit_icons = {'GO': '✅', 'NO_GO': '🚫', 'ADJUST': '⚙️'}
            _jit_gate_icon = _jit_icons.get(jit or '', '⏳')
            _jit_gate_status = {
                'GO': '최근 진입 승인',
                'NO_GO': '최근 진입 차단',
                'ADJUST': '최근 사이즈 조정',
            }.get(jit or '', '진입 신호 대기 중')
            gate_line = f"JIT Advisory: {_jit_gate_icon} {_jit_gate_status}"
        else:
            gate_line = f"실행 게이트: 4H×{consecutive} {gate_icon} — {gate_status}"

        text = (
            f"🔮 [{self._exchange}·BTC] {_format_time(time.time())}  (판단 사이클 · 5분 요약)\n"
            f"──────────────────────────\n"
            f"체제: {regime_kr}{regime_detail}\n"
            + (f"{_mode_line}\n" if _mode_line else "")
            + f"{gate_line}\n"
            f"──────────────────────────\n"
            f"{sig_block}\n"
            + (f"{armed_line}\n" if armed_line else "")
            + f"결론: {conclusion}"
        )

        await _send_telegram(self._bot_token, self._chat_id, text)
        self._last_periodic_send = time.time()

    async def start(self) -> None:
        """비동기 태스크 시작. 판단 도메인(judge)만 정기 요약 루프를 가진다."""
        if self._task and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        # 실행 도메인(punisher)은 이벤트 기반 즉시 전송만 — 정기 루프 불필요
        if self._domain == "judge":
            self._task = asyncio.create_task(self._flush_loop(), name="log_transaction_judge")

    async def stop(self) -> None:
        """비동기 태스크 정지."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _flush_loop(self) -> None:
        """5분 주기 정기 요약 전송 (판단 도메인 전용 — judge 핸들러만 시작됨)."""
        while True:
            await asyncio.sleep(self._interval)
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
    """WARNING 이상 → Save Us 그룹 즉시 전송 (5초 디바운스, logger+level 키)."""

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
        debounce_sec: float = 300.0,
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
        # 디바운스: 동일 logger+level+메시지 조합 debounce_sec 이내 중복 스킵
        key = f"{record.name}:{record.levelno}:{record.getMessage()}"
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


def seed_telegram_strategy_params(params: dict) -> None:
    """전략 파라미터를 핸들러 _state에 주입하여 RSI 범위 표시를 실제 파라미터와 동기화.

    setup_telegram_logging() 호출 후, 전략 파라미터 로드 직후에 호출할 것.

    Args:
        params: gmoc_strategies.parameters dict (entry_rsi_min 등 포함)
    """
    # 수치형 파라미터 (float 변환)
    float_keys = {
        'entry_rsi_min': 'entry_rsi_min',
        'entry_rsi_max': 'entry_rsi_max',
        'entry_rsi_min_short': 'entry_rsi_min_short',
        'entry_rsi_max_short': 'entry_rsi_max_short',
        'armed_expire_sec': 'armed_expire_sec',
    }
    for param_key, state_key in float_keys.items():
        val = params.get(param_key)
        if val is not None:
            for h in _handlers:
                if isinstance(h, TelegramTransactionHandler):
                    h._state[state_key] = float(val)

    # 문자열 파라미터 (entry_mode, entry_timeframe)
    for param_key in ('entry_mode', 'entry_timeframe'):
        val = params.get(param_key)
        if val is not None:
            for h in _handlers:
                if isinstance(h, TelegramTransactionHandler):
                    h._state[param_key] = str(val)


def seed_telegram_regime_state(regime: str | None, consecutive: int) -> None:
    """서비스 재시작 후 RegimeGate DB 복원 상태를 핸들러 _state에 즉시 반영.

    정기 요약 전송 전에 호출하면 '미확정 · 0회' 표시를 방지할 수 있다.
    setup_telegram_logging() 호출 후, RegimeGate 복원 직후에 호출할 것.

    Args:
        regime: 'trending' | 'ranging' | 'unclear' | None
        consecutive: 연속 횟수 (DB의 consecutive_count)
    """
    if not regime:
        return
    for h in _handlers:
        if isinstance(h, TelegramTransactionHandler):
            h._state['regime_status'] = regime
            h._state['regime_consecutive'] = consecutive


class TelegramEvolutionHandler(logging.Handler):
    """진화 채널 전용 즉시 전송 핸들러.

    EVOLUTION_PREFIXES에 해당하는 logger 이름의 INFO+ 메시지를
    TELEGRAM_EVOLUTION_CHAT_ID 채널로 즉시 전송한다.
    """

    def __init__(self, bot_token: str, chat_id: str):
        super().__init__()
        self._bot_token = bot_token
        self._chat_id = chat_id

    def emit(self, record: logging.LogRecord) -> None:
        # 진화 도메인만 처리
        is_evolution = any(
            record.name == p or record.name.startswith(p + ".")
            for p in EVOLUTION_PREFIXES
        )
        if not is_evolution:
            return
        if record.levelno < logging.INFO:
            return
        try:
            loop = asyncio.get_running_loop()
            asyncio.ensure_future(
                _send_telegram(self._bot_token, self._chat_id, self.format(record)),
                loop=loop,
            )
        except RuntimeError:
            # 이벤트 루프가 없는 경우 조용히 건너뜀
            pass


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

    # 진화 도메인 핸들러 (Evolution 채널, 즉시 전송)
    evolution_chat = os.environ.get("TELEGRAM_EVOLUTION_CHAT_ID", "")
    if evolution_chat and not any(isinstance(h, TelegramEvolutionHandler) for h in _handlers):
        h_evo = TelegramEvolutionHandler(bot_token, evolution_chat)
        h_evo.setLevel(logging.INFO)
        root.addHandler(h_evo)
        _handlers.append(h_evo)


async def shutdown_telegram_logging() -> None:
    """비동기 태스크 정리 + 잔여 버퍼 전송."""
    for h in _handlers:
        if hasattr(h, "stop"):
            await h.stop()
    _handlers.clear()
