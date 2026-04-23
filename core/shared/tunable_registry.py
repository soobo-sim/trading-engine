"""
Tunable 레지스트리 — 초기 30개 키 등록.

이 파일이 import 되는 시점에 TunableCatalog에 전체 Tunable이 등록된다.
코드/DB에 이미 존재하는 값들을 카탈로그화한 것이며 코드 동작은 변경되지 않는다.
"""
from __future__ import annotations

from core.shared.tunable_catalog import TunableCatalog, TunableSpec

# ── Layer A — 전략 파라미터 (수치) ────────────────────────────

_LAYER_A: list[TunableSpec] = [
    TunableSpec(
        key="trend.ema_slope_entry_min",
        layer="A", value_type="float", default=0.05,
        min=0.0, max=0.3,
        owner="gmoc_strategies.parameters",
        risk_level="low", autonomy="auto",
        description="추세추종 EMA 기울기 진입 최소 임계값. 낮을수록 진입 빈도 증가.",
        affects=["entry"],
        db_table="gmoc_strategies", db_path="parameters.ema_slope_entry_min",
    ),
    TunableSpec(
        key="trend.ema_slope_weak_threshold",
        layer="A", value_type="float", default=0.05,
        min=0.0, max=0.2,
        owner="gmoc_strategies.parameters",
        risk_level="low", autonomy="auto",
        description="EMA 기울기 약세 판정 임계값 — 이하면 exit_warning 발동 검토.",
        affects=["exit"],
        db_table="gmoc_strategies", db_path="parameters.ema_slope_weak_threshold",
    ),
    TunableSpec(
        key="trend.trailing_stop_atr_initial",
        layer="A", value_type="float", default=1.5,
        min=1.0, max=3.0,
        owner="gmoc_strategies.parameters",
        risk_level="medium", autonomy="auto",
        description="트레일링 스탑 초기 ATR 배수. 클수록 스탑이 멀다.",
        affects=["exit"],
        db_table="gmoc_strategies", db_path="parameters.trailing_stop_atr_initial",
    ),
    TunableSpec(
        key="trend.trailing_stop_decay_per_atr",
        layer="A", value_type="float", default=0.2,
        min=0.05, max=0.5,
        owner="gmoc_strategies.parameters",
        risk_level="medium", autonomy="auto",
        description="이익 1 ATR 증가마다 배수 감쇠량. Progressive trailing stop 감쇠율.",
        affects=["exit"],
        db_table="gmoc_strategies", db_path="parameters.trailing_stop_decay_per_atr",
    ),
    TunableSpec(
        key="trend.trailing_stop_atr_min",
        layer="A", value_type="float", default=0.3,
        min=0.1, max=1.0,
        owner="gmoc_strategies.parameters",
        risk_level="medium", autonomy="auto",
        description="Progressive trailing stop 최소 ATR 배수 하한.",
        affects=["exit"],
        db_table="gmoc_strategies", db_path="parameters.trailing_stop_atr_min",
    ),
    TunableSpec(
        key="trend.breakeven_trigger_atr",
        layer="A", value_type="float", default=1.0,
        min=0.5, max=2.0,
        owner="gmoc_strategies.parameters",
        risk_level="medium", autonomy="auto",
        description="이익이 ATR × 이 배수 이상일 때 스탑을 손익분기점 이상으로 보장.",
        affects=["exit"],
        db_table="gmoc_strategies", db_path="parameters.breakeven_trigger_atr",
    ),
    TunableSpec(
        key="trend.entry_grace_period_sec",
        layer="A", value_type="int", default=900,
        min=0, max=3600,
        owner="gmoc_strategies.parameters",
        risk_level="low", autonomy="auto",
        description="진입 후 N초 동안 기울기 하락/다이버전스 tighten_stop 억제 grace period.",
        affects=["exit"],
        db_table="gmoc_strategies", db_path="parameters.entry_grace_period_sec",
    ),
    TunableSpec(
        key="trend.candle_change_cooling_sec",
        layer="A", value_type="int", default=300,
        min=0, max=1800,
        owner="gmoc_strategies.parameters",
        risk_level="low", autonomy="auto",
        description="4H 캔들 교체 후 N초 동안 exit_warning 억제 (whipsaw 방지).",
        affects=["exit"],
        db_table="gmoc_strategies", db_path="parameters.candle_change_cooling_sec",
    ),
    TunableSpec(
        key="trend.exit_ema_atr_cushion",
        layer="A", value_type="float", default=0.1,
        min=0.0, max=0.5,
        owner="gmoc_strategies.parameters",
        risk_level="medium", autonomy="auto",
        description="exit_warning EMA 체크 시 ATR 쿠션 크기.",
        affects=["exit"],
        db_table="gmoc_strategies", db_path="parameters.exit_ema_atr_cushion",
    ),
    TunableSpec(
        key="box.box_lookback_candles",
        layer="A", value_type="int", default=40,
        min=20, max=80,
        owner="gmoc_strategies.parameters",
        risk_level="medium", autonomy="auto",
        description="박스권 감지 lookback 캔들 수 (4H 기준).",
        affects=["entry"],
        db_table="gmoc_strategies", db_path="parameters.box_lookback_candles",
    ),
    TunableSpec(
        key="box.box_min_width_pct",
        layer="A", value_type="float", default=1.0,
        min=0.5, max=3.0,
        owner="gmoc_strategies.parameters",
        risk_level="medium", autonomy="auto",
        description="유효 박스권 최소 폭 (%p). 너무 좁은 박스 필터링.",
        affects=["entry"],
        db_table="gmoc_strategies", db_path="parameters.box_min_width_pct",
    ),
    TunableSpec(
        key="box.box_touch_count_min",
        layer="A", value_type="int", default=2,
        min=1, max=4,
        owner="gmoc_strategies.parameters",
        risk_level="low", autonomy="auto",
        description="박스 상/하단 터치 최소 횟수. 낮을수록 박스 감지 빈도 증가.",
        affects=["entry"],
        db_table="gmoc_strategies", db_path="parameters.box_touch_count_min",
    ),
    TunableSpec(
        key="box.box_near_threshold_atr",
        layer="A", value_type="float", default=2.0,
        min=0.5, max=3.0,
        owner="gmoc_strategies.parameters",
        risk_level="low", autonomy="auto",
        description="박스 상/하단 '근접' 판정 ATR 배수 임계값.",
        affects=["entry"],
        db_table="gmoc_strategies", db_path="parameters.box_near_threshold_atr",
    ),
    TunableSpec(
        key="regime.bb_width_trending_min",
        layer="A", value_type="float", default=3.0,
        min=1.0, max=5.0,
        owner="gmoc_strategies.parameters",
        risk_level="high", autonomy="auto",
        description="체제 trending 판정 BB폭 최소 임계값. 낮을수록 trending 판정 빈도 증가.",
        affects=["regime"],
        db_table="gmoc_strategies", db_path="parameters.bb_width_trending_min",
    ),
    TunableSpec(
        key="regime.range_pct_ranging_max",
        layer="A", value_type="float", default=6.0,
        min=3.0, max=10.0,
        owner="gmoc_strategies.parameters",
        risk_level="high", autonomy="auto",
        description="체제 ranging 판정 가격범위 최대 임계값. 높을수록 ranging 판정 허용 범위 확대.",
        affects=["regime"],
        db_table="gmoc_strategies", db_path="parameters.range_pct_ranging_max",
    ),
]

# ── Layer B — 게이트·필터 룰 ─────────────────────────────────

_LAYER_B: list[TunableSpec] = [
    TunableSpec(
        key="gate.regime_streak_required",
        layer="B", value_type="int", default=3,
        min=3, max=10,
        owner="core.execution.regime_gate",
        risk_level="high", autonomy="auto",
        description="RegimeGate 체제 streak 임계값. 동일 체제가 연속 N회 이상이어야 진입 허용.",
        affects=["entry", "regime"],
        db_table="gmoc_strategies", db_path="parameters.regime_streak_required",
    ),
    TunableSpec(
        key="gate.kill_consec_loss_threshold",
        layer="B", value_type="int", default=3,
        min=2, max=6,
        owner="core.safety.kill_checker",
        risk_level="escalation_only", autonomy="escalation",
        description="Kill 스위치 연속 손실 임계값. 이 횟수 연속 손실 시 전략 정지.",
        affects=["entry"],
    ),
    TunableSpec(
        key="gate.kill_loss_jpy_threshold",
        layer="B", value_type="int", default=5000,
        min=1000, max=50000,
        owner="core.safety.kill_checker",
        risk_level="escalation_only", autonomy="escalation",
        description="Kill 스위치 누적 손실 JPY 임계값. 이 금액 초과 손실 시 전략 정지.",
        affects=["entry"],
    ),
    TunableSpec(
        key="gate.kill_drawdown_pct",
        layer="B", value_type="float", default=5.0,
        min=1.0, max=20.0,
        owner="core.safety.kill_checker",
        risk_level="escalation_only", autonomy="escalation",
        description="Kill 스위치 최대 drawdown % 임계값.",
        affects=["entry"],
    ),
    TunableSpec(
        key="safety.position_size_pct",
        layer="B", value_type="float", default=50.0,
        min=10.0, max=50.0,
        owner="gmoc_strategies.parameters",
        risk_level="escalation_only", autonomy="escalation",
        description="단일 거래 포지션 사이즈 (잔고 대비 %). 자본 곡선 형태에 직접 영향.",
        affects=["entry"],
        db_table="gmoc_strategies", db_path="parameters.position_size_pct",
    ),
]

# ── Layer B — Canary 가드레일 (P6에서 활용) ─────────────────

_LAYER_B_CANARY: list[TunableSpec] = [
    TunableSpec(
        key="canary.rollback_pnl_jpy",
        layer="B", value_type="int", default=-3000,
        min=-30000, max=-100,
        owner="core.judge.evolution.guardrails",
        risk_level="high", autonomy="auto",
        description="Canary 자동 롤백 절대 손실 임계값 (JPY, 음수).",
        affects=["entry"],
    ),
    TunableSpec(
        key="canary.rollback_pct",
        layer="B", value_type="float", default=-2.0,
        min=-20.0, max=-0.1,
        owner="core.judge.evolution.guardrails",
        risk_level="high", autonomy="auto",
        description="Canary 자동 롤백 비율 손실 임계값 (%, 음수).",
        affects=["entry"],
    ),
    TunableSpec(
        key="canary.rollback_consec_loss",
        layer="B", value_type="int", default=3,
        min=2, max=10,
        owner="core.judge.evolution.guardrails",
        risk_level="high", autonomy="auto",
        description="Canary 자동 롤백 연속 손실 횟수 임계값.",
        affects=["entry"],
    ),
    TunableSpec(
        key="canary.rollback_max_dd_pct",
        layer="B", value_type="float", default=5.0,
        min=1.0, max=20.0,
        owner="core.judge.evolution.guardrails",
        risk_level="high", autonomy="auto",
        description="Canary 자동 롤백 max drawdown % 임계값.",
        affects=["entry"],
    ),
    TunableSpec(
        key="canary.expire_days",
        layer="B", value_type="int", default=7,
        min=3, max=30,
        owner="core.judge.evolution.guardrails",
        risk_level="low", autonomy="auto",
        description="Canary 만료 일수. 이 기간 내 min_trades 미달 시 자동 롤백.",
        affects=["entry"],
    ),
    TunableSpec(
        key="canary.min_trades",
        layer="B", value_type="int", default=3,
        min=1, max=20,
        owner="core.judge.evolution.guardrails",
        risk_level="low", autonomy="auto",
        description="Canary 유효성 판정 최소 거래 건수.",
        affects=["entry"],
    ),
]

# ── Layer C — 워크플로우 빈도·트리거 ─────────────────────────

_LAYER_C: list[TunableSpec] = [
    TunableSpec(
        key="workflow.advisory_4h_cron",
        layer="C", value_type="str", default="5 0,4,8,12,16,20 * * *",
        owner="agents/rachel/cron/jobs.json",
        risk_level="escalation_only", autonomy="escalation",
        description="Rachel 4H advisory cron 표현식. 변경 시 에이전트 재기동 필요.",
        affects=["advisory_format"],
    ),
    TunableSpec(
        key="workflow.regime_review_cron",
        layer="C", value_type="str", default="0 9 * * *",
        owner="agents/rachel/cron/jobs.json",
        risk_level="escalation_only", autonomy="escalation",
        description="일일 체제 점검 cron 표현식.",
        affects=["advisory_format"],
    ),
    TunableSpec(
        key="workflow.evolution_cycle_cron",
        layer="C", value_type="str", default="30 9 */3 * *",
        owner="agents/rachel/cron/jobs.json",
        risk_level="escalation_only", autonomy="escalation",
        description="3일 진화 사이클 cron 표현식.",
        affects=["advisory_format"],
    ),
    TunableSpec(
        key="workflow.advisory_lookback_candles",
        layer="C", value_type="int", default=100,
        min=50, max=200,
        owner="api.services.analysis_service",
        risk_level="medium", autonomy="auto",
        description="Advisory 분석 시 lookback 캔들 수.",
        affects=["advisory_format"],
    ),
]

# ── Layer D — 데이터 셋·advisory 형식 ─────────────────────────

_LAYER_D: list[TunableSpec] = [
    TunableSpec(
        key="data.macro_brief_items",
        layer="D", value_type="json",
        default=["fng", "news", "events", "macro"],
        owner="api.services.analysis_service",
        risk_level="medium", autonomy="auto",
        description="macro-brief API 응답에 포함할 항목 목록.",
        affects=["advisory_format"],
    ),
    TunableSpec(
        key="data.macro_brief_news_lookback_h",
        layer="D", value_type="int", default=24,
        min=1, max=168,
        owner="api.services.analysis_service",
        risk_level="low", autonomy="auto",
        description="macro-brief 뉴스 lookback 시간 (hours).",
        affects=["advisory_format"],
    ),
    TunableSpec(
        key="advisory.required_reasoning_sections",
        layer="D", value_type="json",
        default=["technical", "macro"],
        owner="agents/rachel/WORKFLOW_4H_ENTRY_ADVISORY.md",
        risk_level="medium", autonomy="auto",
        description="Advisory reasoning 필수 포함 섹션 목록.",
        affects=["advisory_format"],
    ),
    TunableSpec(
        key="advisory.lessons_recall_top_k",
        layer="D", value_type="int", default=3,
        min=1, max=10,
        owner="api.services.lessons_recall",
        risk_level="low", autonomy="auto",
        description="Advisory 시 자동 소환할 Lesson 최대 건수.",
        affects=["advisory_format"],
    ),
]

# ── Layer E — 프롬프트·워크플로우 본문 ───────────────────────

_LAYER_E: list[TunableSpec] = [
    TunableSpec(
        key="prompt.alice.regime_check_template_version",
        layer="E", value_type="int", default=1,
        min=1, max=99,
        owner="agents/alice/instructions/",
        risk_level="escalation_only", autonomy="escalation",
        description="Alice 체제 판정 프롬프트 템플릿 버전. 변경 시 에이전트 재검증 필요.",
        affects=["regime", "advisory_format"],
    ),
    TunableSpec(
        key="prompt.rachel.judgment_template_version",
        layer="E", value_type="int", default=1,
        min=1, max=99,
        owner="agents/rachel/instructions/",
        risk_level="escalation_only", autonomy="escalation",
        description="Rachel 판단 프롬프트 템플릿 버전.",
        affects=["advisory_format"],
    ),
]


def register_all() -> None:
    """모든 Tunable을 TunableCatalog에 등록한다. main.py lifespan에서 호출.
    이미 등록된 키는 건너뛴다 (멱등성 보장).
    """
    all_specs = (
        _LAYER_A
        + _LAYER_B
        + _LAYER_B_CANARY
        + _LAYER_C
        + _LAYER_D
        + _LAYER_E
    )
    for spec in all_specs:
        if TunableCatalog.get(spec.key) is None:
            TunableCatalog.register(spec)


# 모듈 import 시 자동 등록
register_all()
