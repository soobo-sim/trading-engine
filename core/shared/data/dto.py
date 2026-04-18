"""
Data Layer DTO — 레이어 간 통신에 사용하는 불변 데이터 객체.

규칙:
  - 모든 DTO는 frozen=True dataclass. 레이어 간 공유 상태 없음.
  - ORM 모델, FastAPI 스키마와 완전히 분리됨.
  - Signal Layer 이후 모든 레이어가 이 모듈을 참조한다.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ──────────────────────────────────────────────────────────────
# Layer 1: Data
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CandleDTO:
    """OHLCV 캔들 값 객체."""
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    pair: str = ""
    timeframe: str = ""


@dataclass(frozen=True)
class PositionDTO:
    """포지션 상태 스냅샷 — 불변 복사본.

    core/exchange/types.py Position(mutable)의 값을 스냅샷으로 복사한다.
    Decision Layer가 포지션 상태를 읽기 전용으로 참조할 때 사용.
    """
    pair: str
    entry_price: Optional[float]
    entry_amount: float
    stop_loss_price: Optional[float] = None
    stop_tightened: bool = False
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MacroSnapshotDTO:
    """매크로 데이터 스냅샷 (금리, VIX, DXY 등)."""
    us_10y: Optional[float] = None
    us_2y: Optional[float] = None
    vix: Optional[float] = None
    dxy: Optional[float] = None
    fetched_at: Optional[datetime] = None


@dataclass(frozen=True)
class NewsDTO:
    """뉴스 기사 요약."""
    title: str
    source: str
    published_at: datetime
    category: str
    sentiment_score: Optional[float] = None  # -1.0 ~ 1.0


@dataclass(frozen=True)
class SentimentDTO:
    """센티먼트 지수 (Fear & Greed 등)."""
    source: str
    score: int       # 0~100
    classification: str  # "extreme_fear" | "fear" | "neutral" | "greed" | "extreme_greed"
    timestamp: datetime


@dataclass(frozen=True)
class EconomicEventDTO:
    """경제 캘린더 이벤트."""
    name: str
    datetime_jst: datetime
    importance: str  # "High" | "Medium"
    currency: str
    forecast: Optional[str] = None
    previous: Optional[str] = None


@dataclass(frozen=True)
class LessonDTO:
    """과거 거래 교훈."""
    lesson_id: int
    situation_tags: tuple[str, ...]  # hashable for frozen dataclass
    lesson_text: str
    outcome: str  # "win" | "loss"
    confidence_error: Optional[float] = None  # 확신도 오차 (AI v2용)


# ──────────────────────────────────────────────────────────────
# Layer 2: Signal (signals.py 출력 → 타입 DTO)
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SignalSnapshot:
    """시그널 계산 결과 — Decision Layer(Layer 3)의 입력.

    기존 _compute_signal()이 반환하던 dict를 타입이 있는 DTO로 교체.
    기존 로직은 동일하게 동작한다.

    signal 값:
      "entry_ok"     — 롱 진입 조건 충족
      "entry_sell"   — 숏 진입 조건 충족 (CFD/FX)
      "exit_warning" — EMA 하방 돌파, 즉시 청산
      "wait_dip"     — 과매수 대기
      "wait_regime"  — 횡보 체제 대기
      "no_signal"    — 진입 조건 없음

    exit_signal.action 값:
      "hold"         — 유지
      "full_exit"    — 전량 청산
      "tighten_stop" — 스탑 타이트닝
    """
    pair: str
    exchange: str
    timestamp: datetime

    # 기존 signals.py 출력 (필수)
    signal: str                        # "entry_ok" | "entry_sell" | "exit_warning" | ...
    current_price: float
    exit_signal: dict                  # {"action": str, "reason": str, "triggers": dict, ...}

    # 기존 signals.py 선택 출력
    ema: Optional[float] = None
    ema_slope_pct: Optional[float] = None
    rsi: Optional[float] = None
    atr: Optional[float] = None
    regime: Optional[str] = None
    stop_loss_price: Optional[float] = None  # ATR 기반 사전 계산된 SL

    # v2에서 채워짐. v1에서는 None
    macro: Optional[MacroSnapshotDTO] = None
    news: Optional[tuple[NewsDTO, ...]] = None  # frozen을 위해 list 대신 tuple
    sentiment: Optional[SentimentDTO] = None
    upcoming_events: Optional[tuple[EconomicEventDTO, ...]] = None
    relevant_lessons: Optional[tuple[LessonDTO, ...]] = None

    # 현재 포지션 스냅샷 (Optional — 포지션 없으면 None)
    position: Optional[PositionDTO] = None

    # 원본 캔들 (다이버전스 감지 등에 필요)
    candles: Optional[tuple] = None        # tuple[CandleDTO, ...]
    rsi_series: Optional[tuple] = None     # tuple[Optional[float], ...]

    # 추가 파라미터 (RuleBasedDecision에서 사이징 등에 필요)
    params: dict = field(default_factory=dict)

    # 미완성 캔들 기반 프리뷰 시그널 여부 (True → 4H 완성 시 재검증 필요)
    is_preview: bool = False

    # 이 스냅샷을 생성한 매니저 전략 타입 (듀얼 매니저 advisory 분리용)
    strategy_type: str = "trend_following"


# ──────────────────────────────────────────────────────────────
# Layer 3: Decision
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Decision:
    """판단 결과 — Guardrail(Layer 4)과 Execution(Layer 5)의 입력.

    action 값:
      "entry_long"    — 롱 진입
      "entry_short"   — 숏 진입
      "exit"          — 전량 청산
      "tighten_stop"  — 스탑 타이트닝
      "reduce"        — 부분 청산 (v2용)
      "hold"          — 유지 (아무것도 하지 않음)
      "blocked"       — Guardrail 거부
    """
    action: str
    pair: str
    exchange: str
    confidence: float        # 0.0 ~ 1.0
    size_pct: float          # 자본 대비 비율 (0.0 ~ 0.8)
    stop_loss: Optional[float]
    take_profit: Optional[float]
    reasoning: str           # 판단 근거 (디버깅/학습용)
    risk_factors: tuple[str, ...]
    source: str              # "rule_based_v1" | "ai_v2"
    trigger: str             # "regular_4h" | "event" | "stop_loss" | "trailing"
    raw_signal: str          # 원본 시그널 ("entry_ok", "exit_warning", ...)
    timestamp: Optional[datetime] = None
    meta: dict = field(default_factory=dict)   # ai_v2: alice/samantha/rachel 세부 데이터


def modify_decision(decision: Decision, **changes) -> Decision:
    """Decision DTO의 특정 필드를 변경한 새 인스턴스 반환."""
    return dataclasses.replace(decision, **changes)


# ──────────────────────────────────────────────────────────────
# Layer 4: Guardrail
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GuardrailResult:
    """안전장치 검증 결과."""
    approved: bool
    final_decision: Decision          # 통과 또는 축소된 Decision
    rejection_reason: Optional[str]   # 거부 시 이유
    violations: tuple[str, ...]       # 위반 항목 목록


# ──────────────────────────────────────────────────────────────
# Layer 5: Execution
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExecutionResult:
    """실행 결과 — 매니저의 _handle_execution_result()가 받는 값."""
    action: str              # 실제 실행된 action (또는 "hold"/"blocked"/"rejected_by_user")
    executed: bool
    decision: Optional[Decision] = None   # 최종 적용된 Decision
    reason: Optional[str] = None          # 거부/미실행 사유
    judgment_id: Optional[int] = None     # ai_judgments INSERT id (학습 루프 연결용)
