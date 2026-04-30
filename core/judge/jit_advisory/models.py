"""
JIT Advisory 데이터 계약 — 요청/응답 DTO.

설계서 §3.1, §3.2 기준.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class JITAdvisoryRequest:
    """JIT 자문 요청 — 룰엔진이 진입을 결정한 직후 LLM에 보내는 컨텍스트."""

    # ── 기본 식별 ──────────────────────────────────────────
    request_id: str               # uuid4 hex[:12] — DB audit 키
    pair: str                     # 'btc_jpy'
    exchange: str                 # 'gmo_coin'
    trading_style: str            # 'trend_following' | 'box_mean_reversion'
    proposed_action: str          # 'entry_long' | 'entry_short' | 'add_position'

    # ── 룰엔진 판단 ────────────────────────────────────────
    rule_signal: str              # 'long_setup' | 'short_setup' | ...
    rule_confidence: float        # 룰엔진 confidence (스코어링 기반)
    rule_size_pct: float          # 룰엔진이 산출한 사이즈 (0.0~1.0)
    rule_reasoning: str           # 룰엔진이 왜 진입하는지
    rule_stop_loss: Optional[float] = None
    rule_take_profit: Optional[float] = None

    # ── 시장 컨텍스트 ─────────────────────────────────────
    current_price: float = 0.0
    timeframe: str = "4h"
    regime: str = "uncertain"
    bb_width_pct: float = 0.0
    range_pct: float = 0.0
    consecutive_count: int = 0

    # ── 기술 지표 ─────────────────────────────────────────
    ema_value: float = 0.0
    ema_slope_pct: float = 0.0
    rsi: float = 50.0
    atr: float = 0.0
    atr_pct: float = 0.0

    # ── 박스 컨텍스트 (box_mean_reversion일 때만) ───────────
    box_position: Optional[str] = None   # 'near_lower'|'near_upper'|'middle'|'outside'|'no_box'
    box_upper: Optional[float] = None
    box_lower: Optional[float] = None

    # ── 포지션 컨텍스트 (add_position일 때만) ───────────────
    has_position: bool = False
    position_side: Optional[str] = None
    position_entry_price: Optional[float] = None
    position_pnl_jpy: Optional[float] = None
    position_pnl_pct: Optional[float] = None
    position_pyramid_count: Optional[int] = None
    position_total_size_pct: Optional[float] = None

    # ── 매크로 (선택) ──────────────────────────────────────
    macro_fng: Optional[int] = None
    macro_news_summary: Optional[str] = None
    macro_high_impact_event_in_6h: bool = False
    macro_vix: Optional[float] = None
    macro_dxy: Optional[float] = None

    # ── 안전장치 컨텍스트 ───────────────────────────────────
    kill_active_count: int = 0
    recent_consecutive_losses: int = 0
    recent_win_rate_30d: Optional[float] = None
    recent_ev_30d_jpy: Optional[float] = None

    # ── 교훈 (선택) ────────────────────────────────────────
    recalled_lessons: list[dict] = field(default_factory=list)

    def to_prompt(self) -> str:
        """LLM에 보낼 프롬프트 문자열 생성.

        한국어 자연어 컨텍스트 + JSON 지시.
        """
        lessons_str = ""
        if self.recalled_lessons:
            lessons_str = "\n\n## 참고 교훈\n" + "\n".join(
                f"- [{l.get('lesson_id','?')}] {l.get('summary','')}: "
                f"{l.get('recommendation','')}"
                for l in self.recalled_lessons[:3]
            )

        box_str = ""
        if self.trading_style == "box_mean_reversion" and self.box_position:
            box_str = (
                f"\n\n## 박스 컨텍스트\n"
                f"- 위치: {self.box_position}\n"
                f"- 박스 상단: {self.box_upper}\n"
                f"- 박스 하단: {self.box_lower}"
            )

        pos_str = ""
        if self.has_position and self.proposed_action == "add_position":
            pos_str = (
                f"\n\n## 기존 포지션\n"
                f"- 방향: {self.position_side}\n"
                f"- 진입가: {self.position_entry_price}\n"
                f"- 수익: {self.position_pnl_jpy} JPY ({self.position_pnl_pct:.1f}%)\n"
                f"- 피라미딩 횟수: {self.position_pyramid_count}"
                if self.position_pnl_pct is not None else ""
            )

        macro_str = ""
        if self.macro_fng is not None:
            macro_str = (
                f"\n\n## 매크로 컨텍스트\n"
                f"- Fear & Greed: {self.macro_fng}\n"
                f"- 6H 내 고영향 이벤트: {'예' if self.macro_high_impact_event_in_6h else '아니오'}\n"
                f"- VIX: {self.macro_vix}\n"
                f"- DXY: {self.macro_dxy}"
            )

        return f"""# JIT 진입 자문 요청

## 요청 정보
- 요청 ID: {self.request_id}
- 페어: {self.pair} ({self.exchange})
- 전략: {self.trading_style}
- 제안 액션: {self.proposed_action}

## 룰엔진 판단
- 신호: {self.rule_signal}
- 신뢰도: {self.rule_confidence:.2f}
- 사이즈: {self.rule_size_pct:.1%}
- 근거: {self.rule_reasoning}
- SL: {self.rule_stop_loss}

## 시장 컨텍스트
- 현재가: {self.current_price:,.0f} JPY
- 체제: {self.regime} (연속: {self.consecutive_count}회)
- BB폭: {self.bb_width_pct:.2f}%
- EMA: {self.ema_value:,.0f} (slope: {self.ema_slope_pct:+.3f}%)
- RSI: {self.rsi:.1f}
- ATR: {self.atr:,.0f} ({self.atr_pct:.2f}%)

## 리스크 컨텍스트
- Kill 활성: {self.kill_active_count}건
- 연속 손실: {self.recent_consecutive_losses}회
- 30일 승률: {f'{self.recent_win_rate_30d:.1%}' if self.recent_win_rate_30d is not None else '없음'}{box_str}{pos_str}{macro_str}{lessons_str}

---
**응답 형식**: 반드시 단일 JSON 객체만. 마크다운 fence·자연어 prelude 금지.

필수 필드:
- decision: "GO" | "NO_GO" | "ADJUST"
- confidence: 0.0~1.0
- reasoning: 50자 이상 결정 근거 (첫 문장에 결론)
- risk_factors: 주요 리스크 목록 (최대 3개)

ADJUST일 때 추가:
- adjusted_size_pct: 0.0~1.0 (필수)
- adjusted_action: (선택, 방향 변경 시)

예시:
{{"decision": "GO", "confidence": 0.82, "reasoning": "추세가 명확하고 BB폭이 충분하며 RSI가 중립권에 있어 진입 적합. ATR 대비 SL이 적절함.", "risk_factors": ["단기 과열 가능성"]}}
"""


@dataclass
class JITAdvisoryResponse:
    """LLM이 반환하는 JIT 자문 결과."""

    request_id: str
    decision: str   # "GO" | "NO_GO" | "ADJUST"

    adjusted_size_pct: Optional[float] = None
    adjusted_stop_loss: Optional[float] = None
    adjusted_take_profit: Optional[float] = None
    adjusted_action: Optional[str] = None

    confidence: float = 0.0
    reasoning: str = ""
    risk_factors: list[str] = field(default_factory=list)

    latency_ms: int = 0
    model: str = ""
