"""
Evolution 도메인 Pydantic 스키마 — P1~P8 전체가 이 파일을 공유.

각 Phase 구현 시 이 파일에 스키마를 추가한다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, ConfigDict

# ── P1: Tunable 카탈로그 ──────────────────────────────────────


class TunableResponse(BaseModel):
    key: str
    layer: str
    value_type: str
    default: Any
    current_value: Any | None = None   # DB 조회 결과 (있을 때만)
    min: Any | None = None
    max: Any | None = None
    allowed_values: list[Any] | None = None
    owner: str
    risk_level: str
    autonomy: str
    description: str
    affects: list[str]
    db_table: str | None = None
    db_path: str | None = None


class TunableListResponse(BaseModel):
    total: int
    tunables: list[TunableResponse]
    by_layer_count: dict[str, int]    # {"A": 15, "B": 5, ...}


# ── P2: Lessons (외장 기억 저장소) ────────────────────────────

PatternType = Literal[
    "entry_condition",
    "exit_condition",
    "regime_transition",
    "parameter_calibration",
    "macro_context",
    "risk_management",
    "data_quality",
    "workflow_process",
    "meta",
]

LessonStatus = Literal["active", "deprecated", "superseded", "draft"]
LessonSource = Literal["manual", "hypothesis", "post_analyzer"]

_VALID_REGIMES = {"trending", "ranging", "unclear", "any"}


class LessonCreate(BaseModel):
    pattern_type: PatternType
    market_regime: str | None = Field(None)
    pair: str | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    observation: str = Field(..., min_length=20, max_length=1000)
    recommendation: str = Field(..., min_length=20, max_length=500)
    outcome_stats: dict[str, Any] | None = None
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    source: LessonSource = "manual"
    author: str | None = Field(None, max_length=40)
    hypothesis_id: str | None = None

    @field_validator("market_regime")
    @classmethod
    def _validate_regime(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_REGIMES:
            raise ValueError(f"market_regime must be one of {_VALID_REGIMES}")
        return v


class LessonUpdate(BaseModel):
    """부분 업데이트 — 모든 필드 optional."""
    observation: str | None = Field(None, min_length=20, max_length=1000)
    recommendation: str | None = Field(None, min_length=20, max_length=500)
    outcome_stats: dict[str, Any] | None = None
    confidence: float | None = Field(None, ge=0.0, le=1.0)
    status: LessonStatus | None = None
    superseded_by: str | None = None   # L-YYYY-NNN 형식


class LessonResponse(BaseModel):
    id: str
    hypothesis_id: str | None
    pattern_type: str
    market_regime: str | None
    pair: str | None
    conditions: dict[str, Any]
    observation: str
    recommendation: str
    outcome_stats: dict[str, Any] | None
    confidence: float
    status: str
    superseded_by: str | None
    source: str
    author: str | None
    created_at: datetime
    updated_at: datetime
    last_referenced_at: datetime | None
    reference_count: int

    model_config = ConfigDict(from_attributes=True)


class LessonListResponse(BaseModel):
    total: int
    lessons: list[LessonResponse]


class LessonStatsResponse(BaseModel):
    total: int
    by_status: dict[str, int]
    by_pattern_type: dict[str, int]


# ── P3: Lessons Recall ────────────────────────────────────────


class RecallRequest(BaseModel):
    pair: str
    market_regime: str
    has_position: bool = False
    position_side: str | None = None
    bb_width_pct: float | None = None
    atr_pct: float | None = None
    last_4h_change_pct: float | None = None
    macro_context: dict[str, Any] | None = None
    workflow: str = "4h_advisory"
    top_k: int = Field(3, ge=1, le=10)


class RecalledLesson(BaseModel):
    id: str
    pattern_type: str
    observation: str
    recommendation: str
    confidence: float
    match_score: float
    summary: str   # observation 100자 요약 (advisory 인용용)


class RecallResponse(BaseModel):
    context: RecallRequest
    matched_count: int
    lessons: list[RecalledLesson]


# ── P4: Hypotheses (가설 생애주기) ─────────────────────────────


class TunableChange(BaseModel):
    """가설에서 제안하는 단일 Tunable 변경 명세."""
    tunable_key: str
    current_value: Any
    proposed_value: Any
    rationale: str = Field(..., min_length=10)


class HypothesisCreate(BaseModel):
    title: str = Field(..., min_length=5, max_length=200)
    description: str = Field(..., min_length=20)
    changes: list[TunableChange] = Field(..., min_length=1)
    proposer: str = Field(..., max_length=40)
    source_lessons: list[str] | None = None
    baseline_metrics: dict[str, Any] | None = None


class HypothesisTransition(BaseModel):
    new_status: str
    actor: str = Field(..., max_length=40)
    payload: dict[str, Any] | None = None


class HypothesisResponse(BaseModel):
    id: str
    title: str
    description: str
    track: str
    status: str
    changes: list[dict[str, Any]]
    backtest_result: dict[str, Any] | None
    paper_result: dict[str, Any] | None
    canary_result: dict[str, Any] | None
    baseline_metrics: dict[str, Any] | None
    proposer: str
    approver: str | None
    approved_at: datetime | None
    expires_at: datetime | None
    rejection_reason: str | None
    rollback_reason: str | None
    source_lessons: list[str] | None
    resulting_lesson_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HypothesisListResponse(BaseModel):
    total: int
    hypotheses: list[HypothesisResponse]


class HypothesisStatsResponse(BaseModel):
    total: int
    by_status: dict[str, int]
    by_track: dict[str, int]


# ── P5: CycleReport (진화 사이클 보고서) ─────────────────────


class ParameterChangeSummary(BaseModel):
    """가설의 단일 파라미터 변경 — 비교표 렌더링용."""
    key: str              # "trend.atr_entry_min"
    label: str            # "ATR 진입 임계값"
    before: Any
    after: Any
    unit: str = ""        # "%", "배", "건" 등
    rationale: str        # 변경 근거 1줄


class TradeStatsSummary(BaseModel):
    """거래 통계 요약 — 관찰 단계 수치 근거."""
    total: int
    wins: int
    losses: int
    win_rate_pct: float      # 37.5 (%)
    pnl_jpy: int             # 손익 합계 (음수 가능)
    avg_pnl_jpy: int         # 평균 손익
    max_loss_jpy: int        # 최대 단일 손실
    losing_patterns: list[str] = []  # 손실 패턴 설명 (3개 이내)
    lesson_adherence_rate: float | None = None  # 교훈 준수율 0~1


class MarketContextSummary(BaseModel):
    """시장 컨텍스트 — 관찰 단계 외부 요인 근거."""
    period: str                # "2026-04-19 ~ 2026-04-22"
    btc_range_jpy: str         # "¥11,800,000 ~ ¥13,100,000"
    atr_avg_pct: float
    regime_changes: list[str] = []  # ["2026-04-20: ranging→trending"]
    fng_start: int | None = None
    fng_end: int | None = None
    fng_label: str = ""        # "Fear" / "Greed"
    vix: float | None = None
    dxy: float | None = None
    key_events: list[str] = []  # ["2026-04-22: Retail Sales +0.3% (예상 상회)"]
    key_news: list[str] = []   # 주요 뉴스 헤드라인


class BacktestSummary(BaseModel):
    """백테스트 결과 비교 테이블 — 검증 단계 근거."""
    period: str                # "90일 walk-forward"
    trades: int
    sharpe_before: float
    sharpe_after: float
    wr_before_pct: float
    wr_after_pct: float
    max_dd_before_pct: float
    max_dd_after_pct: float
    avg_pnl_before_jpy: int | None = None
    avg_pnl_after_jpy: int | None = None
    samantha_comment: str = ""  # Samantha 검토 의견


class CycleReportDetail(BaseModel):
    """보고서 각 단계의 구조화된 근거 데이터."""
    parameter_changes: list[ParameterChangeSummary] = []
    trade_stats: TradeStatsSummary | None = None
    market_context: MarketContextSummary | None = None
    backtest: BacktestSummary | None = None


class CycleReportInput(BaseModel):
    """Rachel이 생성하는 6단계 보고서 입력."""
    hypothesis_id: str | None = None          # None = no-signal
    mode: Literal["full", "no_signal", "failed"] = "full"

    observation: str = Field(..., min_length=10)
    hypothesis: str = Field(default="(없음)", min_length=1)
    validation: str = Field(default="(없음)", min_length=1)
    application: str = Field(default="(없음)", min_length=1)
    evaluation: str = Field(default="(없음)", min_length=1)
    lesson: str = Field(default="(없음)", min_length=1)

    causality_self_check: dict[str, bool] = Field(default_factory=dict)
    references: dict[str, list[str]] = Field(default_factory=dict)
    detail: CycleReportDetail | None = None  # 구조화된 상세 근거 (선택)


class CycleReportResponse(BaseModel):
    cycle_id: str
    cycle_at: datetime
    hypothesis_id: str | None
    mode: str
    observation: str
    hypothesis: str
    validation: str
    application: str
    evaluation: str
    lesson: str
    causality_self_check: dict[str, bool]
    references: dict[str, list[str]]
    detail: CycleReportDetail | None = None
    telegram_sent: bool = False


class CanaryStatusResponse(BaseModel):
    hypothesis_id: str
    title: str
    started_at: datetime | None
    elapsed_hours: float
    start_balance_jpy: float
    current_balance_jpy: float
    pnl_pct: float
    current_violation: dict | None = None


class OwnerQueryCreate(BaseModel):
    content: str = Field(..., min_length=10, max_length=500)
    category: str = "general"
    priority: str = "medium"
    source: str = "samantha"


class OwnerQueryClose(BaseModel):
    cycle_id: str
    hypothesis_id: str | None = None
    outcome_summary: str = Field(..., min_length=20)


class OwnerQueryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    content: str
    category: str
    priority: str
    status: str
    asked_at: datetime
    closed_at: datetime | None = None
    outcome_summary: str | None = None
    addressed_in_cycle: str | None = None
    addressed_in_hypothesis: str | None = None
    source: str
