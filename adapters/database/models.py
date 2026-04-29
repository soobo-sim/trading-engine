"""
ORM 모델 팩토리 — ck_ / bf_ 프리픽스를 파라미터로 추상화.

설계 원칙:
- 기존 DB 스키마 변경 없음. 프리픽스 + 일부 컬럼 크기만 파라미터화.
- 팩토리 함수가 동적 클래스를 반환. SQLAlchemy mapper registry에 등록됨.
- StrategyTechnique (strategy_techniques) 만 공유 테이블 — 팩토리 아님.

사용 예:
    CkTrade = create_trade_model("ck", order_id_length=25)
    BfTrade = create_trade_model("bf", order_id_length=40)
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY as PgArray
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from adapters.database.session import Base


# ──────────────────────────────────────────────────────────────
# 공유 테이블 (prefix 없음)
# ──────────────────────────────────────────────────────────────

class StrategyTechnique(Base):
    """
    투자 기법 마스터 테이블 (공유, prefix 없음).
    ck_/bf_ 양쪽에서 참조.
    """
    __tablename__ = "strategy_techniques"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(50), nullable=False, unique=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=False)
    best_conditions = Column(Text, nullable=True)
    weaknesses = Column(Text, nullable=True)
    risk_level = Column(String(20), nullable=False, default="medium")
    requires_candles = Column(Boolean, nullable=False, default=False)
    requires_box = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    observed_wins = Column(Integer, nullable=False, default=0)
    observed_losses = Column(Integer, nullable=False, default=0)
    avg_pnl_pct = Column(Numeric(8, 4), nullable=True)
    experience_notes = Column(Text, nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<StrategyTechnique(code={self.code!r}, wins={self.observed_wins}, "
            f"losses={self.observed_losses})>"
        )


# ──────────────────────────────────────────────────────────────
# 팩토리 함수
# ──────────────────────────────────────────────────────────────

def create_strategy_model(prefix: str):
    """
    전략 ORM 모델 팩토리.

    create_strategy_model("ck") → table: ck_strategies
    create_strategy_model("bf") → table: bf_strategies
    """
    # CK: "strategystatus", BF: "bf_strategystatus" — 기존 DB enum 이름
    if prefix == "ck":
        enum_name = "strategystatus"
    else:
        enum_name = f"{prefix}_strategystatus"

    strategy_enum = Enum(
        "proposed", "active", "archived", "rejected",
        name=enum_name, create_type=False,
    )

    class Strategy(Base):
        __tablename__ = f"{prefix}_strategies"
        __table_args__ = (
            Index(f"idx_{prefix}_strategy_status_created", "status", "created_at"),
            {"extend_existing": True},
        )

        id = Column(Integer, primary_key=True, index=True)
        version = Column(Integer, nullable=False, default=1)
        status = Column(strategy_enum, nullable=False, default="proposed", index=True)

        name = Column(String(100), nullable=False)
        description = Column(Text, nullable=False)
        parameters = Column(JSON, nullable=False)
        rationale = Column(Text, nullable=False)
        rejection_reason = Column(Text, nullable=True)
        performance_summary = Column(JSON, nullable=True)

        technique_code = Column(
            String(50),
            ForeignKey("strategy_techniques.code", ondelete="SET NULL"),
            nullable=True,
            index=True,
        )

        created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
        activated_at = Column(DateTime(timezone=True), nullable=True)
        archived_at = Column(DateTime(timezone=True), nullable=True)

        def __repr__(self) -> str:
            return f"<{self.__class__.__name__}(id={self.id}, name={self.name!r}, status={self.status})>"

    Strategy.__name__ = f"{prefix.capitalize()}Strategy"
    Strategy.__qualname__ = Strategy.__name__
    return Strategy


def create_trade_model(prefix: str, order_id_length: int = 40, pair_column: str = "pair"):
    """
    거래 ORM 모델 팩토리.

    create_trade_model("ck", 25) → table: ck_trades, order_id VARCHAR(25)
    create_trade_model("bf", 40, pair_column="product_code") → table: bf_trades, DB 컬럼=product_code

    pair_column: 실제 DB 컬럼명 (BF: "product_code", GMO/CK: "pair").
    Python 속성명은 항상 `pair`.
    """
    _table = f"{prefix}_trades"
    _strategies_table = f"{prefix}_strategies"

    # CK: 대문자 enum ("BUY", "PENDING"), BF: 소문자 enum ("buy", "pending")
    if prefix == "ck":
        ot_enum_name = "ordertype"
        os_enum_name = "orderstatus"
        ot_values = ("BUY", "SELL", "MARKET_BUY", "MARKET_SELL")
        os_values = ("PENDING", "OPEN", "COMPLETED", "CANCELLED")
    else:
        ot_enum_name = f"{prefix}_ordertype"
        os_enum_name = f"{prefix}_orderstatus"
        ot_values = ("buy", "sell", "market_buy", "market_sell")
        os_values = ("pending", "open", "completed", "cancelled")

    order_type_enum = Enum(*ot_values, name=ot_enum_name, create_type=False)
    order_status_enum = Enum(*os_values, name=os_enum_name, create_type=False)

    class Trade(Base):
        __tablename__ = _table
        __table_args__ = (
            Index(f"idx_{prefix}_trade_created_status", "created_at", "status"),
            Index(f"idx_{prefix}_trade_pair_created", pair_column, "created_at"),
            {"extend_existing": True},
        )

        id = Column(Integer, primary_key=True, index=True)
        order_id = Column(String(order_id_length), unique=True, nullable=False, index=True)

        # pair 속성은 항상 이 이름으로 접근. DB 컬럼명은 pair_column (BF: product_code).
        pair = Column(pair_column, String(20), nullable=False, index=True)
        order_type = Column(order_type_enum, nullable=False)
        amount = Column(Float, nullable=False)
        price = Column(Float, nullable=True)
        executed_price = Column(Float, nullable=True)
        executed_amount = Column(Float, default=0.0)

        status = Column(order_status_enum, default=os_values[0], index=True)

        reasoning = Column(Text, nullable=False)
        market_pulse = Column(JSON, nullable=True)
        trading_pattern = Column(String(20), nullable=True)
        strategy_id = Column(
            Integer, ForeignKey(f"{_strategies_table}.id", ondelete="SET NULL"),
            nullable=True, index=True,
        )

        profit_loss = Column(Float, nullable=True)
        profit_loss_percentage = Column(Float, nullable=True)

        created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
        executed_at = Column(DateTime(timezone=True), nullable=True)
        closed_at = Column(DateTime(timezone=True), nullable=True)
        updated_at = Column(DateTime(timezone=True), onupdate=func.now())

        @property
        def pnl_jpy(self) -> float | None:
            """analysis_service.py 호환 alias → profit_loss."""
            return self.profit_loss

        @property
        def pnl_pct(self) -> float | None:
            """analysis_service.py 호환 alias → profit_loss_percentage."""
            return self.profit_loss_percentage

        def __repr__(self) -> str:
            return f"<{self.__class__.__name__}(id={self.id}, order_id={self.order_id!r})>"

    Trade.__name__ = f"{prefix.capitalize()}Trade"
    Trade.__qualname__ = Trade.__name__
    return Trade


def create_balance_entry_model(prefix: str):
    """잔고 이력 ORM 모델 팩토리."""

    class BalanceEntry(Base):
        __tablename__ = f"{prefix}_balance_entries"
        __table_args__ = (
            Index(f"idx_{prefix}_balance_currency_created", "currency", "created_at"),
            {"extend_existing": True},
        )

        id = Column(Integer, primary_key=True, index=True)
        currency = Column(String(20), nullable=False, index=True)
        available = Column(Float, nullable=False, default=0.0)
        reserved = Column(Float, nullable=False, default=0.0)
        trade_id = Column(
            Integer, ForeignKey(f"{prefix}_trades.id", ondelete="SET NULL"), nullable=True
        )
        entry_source = Column(String(20), nullable=True)
        created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

        def __repr__(self) -> str:
            return f"<{self.__class__.__name__}(id={self.id}, currency={self.currency!r})>"

    BalanceEntry.__name__ = f"{prefix.capitalize()}BalanceEntry"
    BalanceEntry.__qualname__ = BalanceEntry.__name__
    return BalanceEntry


def create_insight_model(prefix: str):
    """AI 인사이트 ORM 모델 팩토리."""
    # CK: 대문자 ("DAILY", "WEEKLY"), BF: 소문자 ("daily", "weekly")
    if prefix == "ck":
        at_enum_name = "analysistype"
        at_values = ("DAILY", "WEEKLY", "TRADE_SPECIFIC", "PATTERN")
    else:
        at_enum_name = f"{prefix}_analysistype"
        at_values = ("daily", "weekly", "trade_specific", "pattern")

    analysis_type_enum = Enum(*at_values, name=at_enum_name, create_type=False)

    class Insight(Base):
        __tablename__ = f"{prefix}_insights"
        __table_args__ = (
            Index(f"idx_{prefix}_insight_type_created", "analysis_type", "created_at"),
            {"extend_existing": True},
        )

        id = Column(Integer, primary_key=True, index=True)
        trade_id = Column(
            Integer, ForeignKey(f"{prefix}_trades.id", ondelete="CASCADE"), nullable=True, index=True
        )
        analysis_type = Column(analysis_type_enum, nullable=False, index=True)
        content = Column(Text, nullable=False)
        key_lessons = Column(JSON, nullable=True)
        metrics = Column(JSON, nullable=True)
        confidence_score = Column(Float, nullable=True)
        applied_count = Column(Integer, default=0)
        created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

        def __repr__(self) -> str:
            return f"<{self.__class__.__name__}(id={self.id}, type={self.analysis_type!r})>"

    Insight.__name__ = f"{prefix.capitalize()}Insight"
    Insight.__qualname__ = Insight.__name__
    return Insight


def create_summary_model(prefix: str):
    """거래 성과 요약 ORM 모델 팩토리."""

    class Summary(Base):
        __tablename__ = f"{prefix}_summaries"
        __table_args__ = (
            Index(f"idx_{prefix}_summary_period_dates", "period_type", "start_date", "end_date"),
            {"extend_existing": True},
        )

        id = Column(Integer, primary_key=True, index=True)
        period_type = Column(String(20), nullable=False, index=True)
        start_date = Column(DateTime(timezone=True), nullable=False, index=True)
        end_date = Column(DateTime(timezone=True), nullable=False, index=True)
        content = Column(Text, nullable=False)
        key_learnings = Column(JSON, nullable=True)
        metrics = Column(JSON, nullable=False)
        recommendations = Column(JSON, nullable=True)
        created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

        def __repr__(self) -> str:
            return f"<{self.__class__.__name__}(id={self.id}, period={self.period_type!r})>"

    Summary.__name__ = f"{prefix.capitalize()}Summary"
    Summary.__qualname__ = Summary.__name__
    return Summary


def create_candle_model(prefix: str, pair_column: str = "pair"):
    """
    OHLCV 캔들 ORM 모델 팩토리.

    create_candle_model("ck", pair_column="pair")           → ck_candles, pair VARCHAR
    create_candle_model("bf", pair_column="product_code")   → bf_candles, product_code VARCHAR
    """
    _table = f"{prefix}_candles"
    _pk_name = f"{prefix}_candles_pkey"

    attrs: dict = {
        "__tablename__": _table,
        "__table_args__": (
            PrimaryKeyConstraint(pair_column, "timeframe", "open_time", name=_pk_name),
            Index(f"idx_{prefix}_candles_lookup", pair_column, "timeframe", "open_time"),
            Index(f"idx_{prefix}_candles_incomplete", pair_column, "timeframe", "is_complete"),
            {"extend_existing": True},
        ),
        pair_column: Column(String(20), nullable=False),
        "timeframe": Column(String(5), nullable=False),
        "open_time": Column(DateTime(timezone=True), nullable=False),
        "close_time": Column(DateTime(timezone=True), nullable=False),
        "open": Column(Numeric(18, 8), nullable=False),
        "high": Column(Numeric(18, 8), nullable=False),
        "low": Column(Numeric(18, 8), nullable=False),
        "close": Column(Numeric(18, 8), nullable=False),
        "volume": Column(Numeric(18, 8), nullable=False, default=0),
        "tick_count": Column(Integer, nullable=False, default=0),
        "is_complete": Column(Boolean, nullable=False, default=False),
        "created_at": Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
        "updated_at": Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
        "__repr__": lambda self: (
            f"<{self.__class__.__name__}({self.timeframe} {self.open_time} "
            f"C={self.close} complete={self.is_complete})>"
        ),
    }

    cls_name = f"{prefix.capitalize()}Candle"
    Candle = type(cls_name, (Base,), attrs)
    return Candle


def create_box_model(prefix: str, pair_column: str = "pair"):
    """박스권 ORM 모델 팩토리."""
    _table = f"{prefix}_boxes"
    cls_name = f"{prefix.capitalize()}Box"

    attrs: dict = {
        "__tablename__": _table,
        "__table_args__": (
            Index(f"idx_{prefix}_boxes_{pair_column}_status", pair_column, "status"),
            Index(f"idx_{prefix}_boxes_created", pair_column, "created_at"),
            Index(f"idx_{prefix}_boxes_strategy", "strategy_id"),
            CheckConstraint("status IN ('active','invalidated')", name=f"{prefix}_boxes_status_check"),
            CheckConstraint("upper_bound > lower_bound", name=f"{prefix}_boxes_bounds_check"),
            {"extend_existing": True},
        ),
        pair_column: Column(String(20), nullable=False),
        "id": Column(Integer, primary_key=True, autoincrement=True),
        "strategy_id": Column(Integer, nullable=True),
        "upper_bound": Column(Numeric(18, 8), nullable=False),
        "lower_bound": Column(Numeric(18, 8), nullable=False),
        "upper_touch_count": Column(Integer, nullable=False, default=0),
        "lower_touch_count": Column(Integer, nullable=False, default=0),
        "tolerance_pct": Column(Numeric(5, 3), nullable=False, default="0.500"),
        "basis_timeframe": Column(String(5), nullable=False, default="4h"),
        "status": Column(String(20), nullable=False, default="active"),
        "invalidation_reason": Column(String(50), nullable=True),
        "detected_from_candle_count": Column(Integer, nullable=True),
        "detected_at_candle_open_time": Column(DateTime(timezone=True), nullable=True),
        "created_at": Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
        "invalidated_at": Column(DateTime(timezone=True), nullable=True),
        "__repr__": lambda self: (
            f"<{self.__class__.__name__}(U={self.upper_bound} L={self.lower_bound} "
            f"status={self.status})>"
        ),
    }
    return type(cls_name, (Base,), attrs)


def create_box_position_model(prefix: str, pair_column: str = "pair", order_id_length: int = 40):
    """박스권 포지션 ORM 모델 팩토리."""
    _table = f"{prefix}_box_positions"
    _boxes_table = f"{prefix}_boxes"
    cls_name = f"{prefix.capitalize()}BoxPosition"

    attrs: dict = {
        "__tablename__": _table,
        "__table_args__": (
            Index(f"idx_{prefix}_box_positions_{pair_column}_status", pair_column, "status"),
            Index(f"idx_{prefix}_box_positions_box_id", "box_id"),
            Index(f"idx_{prefix}_box_positions_created", pair_column, "created_at"),
            CheckConstraint("status IN ('open','closed')", name=f"{prefix}_box_positions_status_check"),
            CheckConstraint("side IN ('buy','sell')", name=f"{prefix}_box_positions_side_check"),
            {"extend_existing": True},
        ),
        pair_column: Column(String(20), nullable=False),
        "id": Column(Integer, primary_key=True, autoincrement=True),
        "box_id": Column(Integer, ForeignKey(f"{_boxes_table}.id", ondelete="SET NULL"), nullable=True),
        "side": Column(String(10), nullable=False, default="buy"),
        "entry_order_id": Column(String(order_id_length), nullable=False),
        "entry_price": Column(Numeric(18, 8), nullable=False),
        "entry_amount": Column(Numeric(18, 8), nullable=False),
        "entry_jpy": Column(Numeric(18, 2), nullable=True),
        "exchange_position_id": Column(String(40), nullable=True),  # GMO FX positionId (closeOrder용)
        "exit_order_id": Column(String(order_id_length), nullable=True),
        "exit_price": Column(Numeric(18, 8), nullable=True),
        "exit_amount": Column(Numeric(18, 8), nullable=True),
        "exit_jpy": Column(Numeric(18, 2), nullable=True),
        "exit_reason": Column(String(50), nullable=True),
        "realized_pnl_jpy": Column(Numeric(18, 2), nullable=True),
        "realized_pnl_pct": Column(Numeric(8, 4), nullable=True),
        "status": Column(String(20), nullable=False, default="open"),
        "created_at": Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
        "closed_at": Column(DateTime(timezone=True), nullable=True),
        # 정신차리자 보고 자동화 (BUG-025)
        "loss_webhook_sent": Column(Boolean, nullable=False, server_default="false"),
        # 거래소 역지정주문 SL 이중화 (DASHBOARD_EXCHANGE_SL_DISPLAY)
        "exchange_sl_order_id": Column(String(40), nullable=True),
        "exchange_sl_price": Column(Numeric(20, 6), nullable=True),
        "exchange_sl_status": Column(String(20), nullable=True),  # registered/cancelled/executed/failed
        # IFD-OCO 지정가 주문 추적 (BOX_IFDOCO_MIGRATION)
        "ifdoco_root_order_id": Column(String(40), nullable=True),
        "ifdoco_status": Column(String(20), nullable=True),  # pending/first_filled/completed_tp/completed_sl/cancelled
        "tp_price": Column(Numeric(20, 5), nullable=True),
        "sl_price_registered": Column(Numeric(20, 5), nullable=True),
        "__repr__": lambda self: (
            f"<{self.__class__.__name__}(box={self.box_id} status={self.status})>"
        ),
    }
    return type(cls_name, (Base,), attrs)


def create_trend_position_model(prefix: str, pair_column: str = "pair", order_id_length: int = 40):
    """
    추세추종 포지션 ORM 모델 팩토리 (GMO Coin 레버리지 양방향 스키마).

    create_trend_position_model("gmoc") → gmoc_trend_positions
    create_trend_position_model("test") → test_trend_positions
    """
    _table = f"{prefix}_trend_positions"
    _strategies_table = f"{prefix}_strategies"
    cls_name = f"{prefix.capitalize()}TrendPosition"

    attrs: dict = {
        "__tablename__": _table,
        "__table_args__": (
            Index(f"idx_{prefix}_trend_positions_{pair_column}_status", pair_column, "status"),
            Index(f"idx_{prefix}_trend_positions_strategy", "strategy_id"),
            Index(f"idx_{prefix}_trend_positions_created", pair_column, "created_at"),
            CheckConstraint("status IN ('open','closed')", name=f"{prefix}_trend_positions_status_check"),
            CheckConstraint("side IN ('buy','sell')", name=f"{prefix}_trend_positions_side_check"),
            {"extend_existing": True},
        ),
        "id": Column(Integer, primary_key=True, autoincrement=True),
        "strategy_id": Column(
            Integer, ForeignKey(f"{_strategies_table}.id", ondelete="SET NULL"), nullable=True
        ),
        "side": Column(String(10), nullable=False),
        pair_column: Column(String(20), nullable=False),
        "entry_order_id": Column(String(order_id_length), nullable=False),
        "entry_price": Column(Numeric(18, 8), nullable=False),
        "entry_size": Column(Numeric(18, 8), nullable=False),
        "entry_collateral_jpy": Column(Numeric(18, 2), nullable=True),
        "stop_loss_price": Column(Numeric(18, 8), nullable=True),
        "pyramid_count": Column(Integer, nullable=False, server_default="0"),
        "loss_webhook_sent": Column(Boolean, nullable=False, server_default="false"),
        "exit_order_id": Column(String(order_id_length), nullable=True),
        "exit_price": Column(Numeric(18, 8), nullable=True),
        "exit_size": Column(Numeric(18, 8), nullable=True),
        "exit_reason": Column(String(50), nullable=True),
        "realized_pnl_jpy": Column(Numeric(18, 2), nullable=True),
        "realized_pnl_pct": Column(Numeric(8, 4), nullable=True),
        "status": Column(String(20), nullable=False, default="open"),
        "created_at": Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
        "closed_at": Column(DateTime(timezone=True), nullable=True),
        "__repr__": lambda self: (
            f"<{self.__class__.__name__}(id={self.id}, side={self.side!r}, "
            f"status={self.status})>"
        ),
    }
    return type(cls_name, (Base,), attrs)


def create_cfd_position_model(prefix: str, pair_column: str = "pair", order_id_length: int = 40):
    """하위 호환성 alias → create_trend_position_model으로 위임."""
    return create_trend_position_model(prefix, pair_column=pair_column, order_id_length=order_id_length)


# ──────────────────────────────────────────
# WakeUpReview — 정신차리자 리뷰 (Alice/Samantha/Rachel 파이프라인)
# ──────────────────────────────────────────

CAUSE_CODES = (
    "ENTRY_TIMING", "EXIT_TIMING", "REGIME_MISMATCH", "PARAM_SUBOPTIMAL",
    "SIZE_EXCESS", "EXECUTION_GAP", "BLACK_SWAN", "SIGNAL_CONFLICT",
)
ROOT_CAUSE_CODES = (
    "NO_GRID_SEARCH", "NARROW_RANGE", "NO_WF", "STALE_PARAMS",
    "REGIME_BLIND", "OVERFITTED", "MANUAL_OVERRIDE", "DATA_GAP",
)
REVIEW_STATUSES = (
    "draft", "pending_pipeline", "alice_submitted",
    "samantha_approved", "samantha_rejected", "rachel_decided",
)
SIMULATION_VERDICTS = ("justified", "premature", "lucky_hold", "reentry_opportunity")
OVERFIT_RISKS = ("low", "medium", "high")
RACHEL_VERDICTS = ("maintain", "modify", "archive")


class WakeUpReview(Base):
    __tablename__ = "wake_up_reviews"
    __table_args__ = (
        CheckConstraint(
            "cause_code IN ('ENTRY_TIMING','EXIT_TIMING','REGIME_MISMATCH',"
            "'PARAM_SUBOPTIMAL','SIZE_EXCESS','EXECUTION_GAP','BLACK_SWAN','SIGNAL_CONFLICT')",
            name="wur_cause_code_check",
        ),
        CheckConstraint(
            "review_status IN ('draft','pending_pipeline','alice_submitted',"
            "'samantha_approved','samantha_rejected','rachel_decided')",
            name="wur_review_status_check",
        ),
        CheckConstraint(
            "simulation_verdict IS NULL OR simulation_verdict IN "
            "('justified','premature','lucky_hold','reentry_opportunity')",
            name="wur_simulation_verdict_check",
        ),
        CheckConstraint(
            "overfit_risk IS NULL OR overfit_risk IN ('low','medium','high')",
            name="wur_overfit_risk_check",
        ),
        CheckConstraint(
            "rachel_verdict IS NULL OR rachel_verdict IN ('maintain','modify','archive')",
            name="wur_rachel_verdict_check",
        ),
        CheckConstraint(
            "optimal_overfit_risk IS NULL OR optimal_overfit_risk IN ('low','medium','high')",
            name="wur_optimal_overfit_risk_check",
        ),
        Index("idx_wur_cause", "strategy_id", "pair", "cause_code", "created_at"),
        Index("idx_wur_position", "position_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # FK 제거: bf 하드코딩 → exchange + position_type 논리 참조 (BUG-025)
    position_id = Column(Integer, nullable=True)
    strategy_id = Column(Integer, nullable=True)
    exchange = Column(String(10), nullable=True)       # bf / gmo
    position_type = Column(String(20), nullable=True)  # trend / box
    pair = Column(String(20), nullable=False)
    entry_price = Column(Numeric(18, 8), nullable=False)
    exit_price = Column(Numeric(18, 8), nullable=False)
    realized_pnl = Column(Numeric(18, 2), nullable=False)
    cause_code = Column(String(30), nullable=False)
    review_status = Column(String(30), nullable=False, default="draft")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # nullable fields
    cause_detail = Column(Text, nullable=True)
    sub_cause = Column(String(50), nullable=True)
    holding_duration_min = Column(Integer, nullable=True)
    entry_regime = Column(String(20), nullable=True)
    actual_regime = Column(String(20), nullable=True)
    simulation_hold_pnl = Column(Numeric(18, 2), nullable=True)
    simulation_best_exit_pnl = Column(Numeric(18, 2), nullable=True)
    simulation_verdict = Column(String(30), nullable=True)
    capital_at_entry = Column(Numeric(18, 2), nullable=True)
    position_size_pct = Column(Numeric(8, 4), nullable=True)
    alice_analysis = Column(Text, nullable=True)
    samantha_audit = Column(Text, nullable=True)
    rachel_verdict = Column(String(20), nullable=True)
    rachel_rationale = Column(Text, nullable=True)
    lessons_learned = Column(Text, nullable=True)
    param_changes = Column(JSON, nullable=True)
    optimistic_ev = Column(Numeric(8, 4), nullable=True)
    pessimistic_ev = Column(Numeric(8, 4), nullable=True)
    pessimistic_max_loss = Column(Numeric(18, 2), nullable=True)
    grid_search_result = Column(JSON, nullable=True)
    overfit_risk = Column(String(10), nullable=True)
    kill_condition_met = Column(Boolean, nullable=False, server_default="false")
    kill_condition_text = Column(String(200), nullable=True)
    safety_check_ok = Column(Boolean, nullable=True)
    stop_loss_price = Column(Numeric(18, 8), nullable=True)
    actual_stop_hit_price = Column(Numeric(18, 8), nullable=True)
    rejection_count = Column(Integer, nullable=False, server_default="0")

    # ── Section I: 최적 파라미터 역산 ─────────────────────────────────────────
    optimal_params = Column(JSON, nullable=True)
    optimal_pnl = Column(Numeric(18, 2), nullable=True)
    optimal_pnl_pct = Column(Numeric(8, 4), nullable=True)
    actual_vs_optimal_diff_pct = Column(Numeric(8, 4), nullable=True)
    optimal_long_term_ev = Column(Numeric(8, 4), nullable=True)
    optimal_long_term_wr = Column(Numeric(8, 4), nullable=True)
    optimal_long_term_sharpe = Column(Numeric(8, 4), nullable=True)
    optimal_long_term_trades = Column(Integer, nullable=True)
    optimal_overfit_risk = Column(String(10), nullable=True)   # low|medium|high
    optimal_entry_timing = Column(String(20), nullable=True)   # 동일|더 일찍|더 늦게|미진입
    optimal_exit_timing = Column(String(20), nullable=True)    # 더 일찍|더 늦게|안 나감
    optimal_key_diff = Column(Text, nullable=True)

    # ── Section J: 근본 원인 ───────────────────────────────────────────────────
    root_cause_codes = Column(PgArray(Text), nullable=True)    # ROOT_CAUSE_CODES 배열
    root_cause_detail = Column(Text, nullable=True)
    decision_date = Column(Date, nullable=True)
    decision_by = Column(String(30), nullable=True)            # alice|rachel|soobo
    info_gap_had = Column(Text, nullable=True)
    info_gap_new = Column(Text, nullable=True)

    # ── Section K: 액션 아이템 ─────────────────────────────────────────────────
    action_items = Column(JSON, nullable=True)
    prevention_checklist = Column(JSON, nullable=True)
    review_quality_score = Column(Numeric(4, 2), nullable=True)

    # ── 파이프라인 추적 (BUG-025) ──────────────────────────────────────────────
    # pipeline_status: pending_pipeline → triggered → completed / failed
    pipeline_status = Column(String(30), nullable=True)
    scheduled_at = Column(DateTime(timezone=True), nullable=True)      # 24h 후 발동 예정
    pipeline_started_at = Column(DateTime(timezone=True), nullable=True)
    pipeline_completed_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<WakeUpReview(id={self.id}, pair={self.pair!r}, "
            f"cause={self.cause_code!r}, status={self.review_status!r})>"
        )


# ──────────────────────────────────────────
# StrategyChange — 전략 변경 이력
# ──────────────────────────────────────────

SC_CHANGE_TYPES = ("param_change", "style_change", "new_strategy", "archive_only")
SC_STATUSES = ("active", "killed", "graduated")


class StrategyChange(Base):
    __tablename__ = "strategy_changes"
    __table_args__ = (
        CheckConstraint(
            "change_type IN ('param_change','style_change','new_strategy','archive_only')",
            name="sc_change_type_check",
        ),
        CheckConstraint(
            "status IN ('active','killed','graduated')",
            name="sc_status_check",
        ),
        Index("idx_sc_pair_status", "pair", "status", "created_at"),
        Index("idx_sc_new_strategy", "new_strategy_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair = Column(String(20), nullable=False)
    old_strategy_id = Column(
        Integer, ForeignKey("bf_strategies.id", ondelete="SET NULL"), nullable=True
    )
    new_strategy_id = Column(
        Integer, ForeignKey("bf_strategies.id", ondelete="RESTRICT"), nullable=False
    )
    change_type = Column(String(30), nullable=False)
    changed_params = Column(JSON, nullable=True)
    trigger = Column(Text, nullable=True)
    rationale = Column(Text, nullable=True)
    alice_opinion = Column(Text, nullable=True)
    samantha_opinion = Column(Text, nullable=True)
    rachel_verdict = Column(Text, nullable=True)
    kill_conditions = Column(JSON, nullable=True)
    observation_period = Column(String(50), nullable=True)
    status = Column(String(20), nullable=False, server_default="active")
    kill_triggered_at = Column(DateTime(timezone=True), nullable=True)
    outcome_summary = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<StrategyChange(id={self.id}, pair={self.pair!r}, "
            f"type={self.change_type!r}, status={self.status!r})>"
        )


# ──────────────────────────────────────────────────────────────
# 백테스트 Result Store (공유 테이블, prefix 없음)
# 설계서: trader-common/solution-design/BACKTEST_MODULE_DESIGN.md §3.4
# ──────────────────────────────────────────────────────────────

class BacktestRun(Base):
    """백테스트 실행 이력."""
    __tablename__ = "backtest_runs"
    __table_args__ = (
        Index("idx_backtest_runs_pair_type", "pair", "strategy_type", "run_type"),
        Index("idx_backtest_runs_created", "created_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair = Column(String(20), nullable=False)
    strategy_type = Column(String(50), nullable=False)
    run_type = Column(String(20), nullable=False)
    parameters = Column(JSON, nullable=False)
    result = Column(JSON, nullable=False)
    candle_range_from = Column(DateTime(timezone=True), nullable=True)
    candle_range_to = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    wf_windows = relationship("WfWindow", back_populates="run", cascade="all, delete-orphan")
    grid_results = relationship("GridResult", back_populates="run", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return (
            f"<BacktestRun(id={self.id}, pair={self.pair!r}, "
            f"type={self.run_type!r}, strategy={self.strategy_type!r})>"
        )


class WfWindow(Base):
    """WF 윈도우별 상세."""
    __tablename__ = "wf_windows"
    __table_args__ = (
        Index("idx_wf_windows_run_id", "run_id", "window_index"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False)
    window_index = Column(Integer, nullable=False)
    is_start = Column(DateTime, nullable=True)
    is_end = Column(DateTime, nullable=True)
    oos_start = Column(DateTime, nullable=True)
    oos_end = Column(DateTime, nullable=True)
    is_sharpe = Column(Float, nullable=True)
    oos_sharpe = Column(Float, nullable=True)
    is_return_pct = Column(Float, nullable=True)
    oos_return_pct = Column(Float, nullable=True)
    trades = Column(Integer, nullable=True)
    win_rate = Column(Float, nullable=True)
    mdd = Column(Float, nullable=True)

    run = relationship("BacktestRun", back_populates="wf_windows")


class GridResult(Base):
    """그리드서치 상위 결과."""
    __tablename__ = "grid_results"
    __table_args__ = (
        Index("idx_grid_results_run_id", "run_id", "rank"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False)
    rank = Column(Integer, nullable=False)
    parameters = Column(JSON, nullable=False)
    sharpe = Column(Float, nullable=True)
    return_pct = Column(Float, nullable=True)
    trades = Column(Integer, nullable=True)
    win_rate = Column(Float, nullable=True)
    mdd = Column(Float, nullable=True)

    run = relationship("BacktestRun", back_populates="grid_results")


# ──────────────────────────────────────────────────────────────
# 전략 분석 시스템 (공유 테이블, prefix 없음)
# 설계서: trader-common/solution-design/STRATEGY_ANALYSIS_SYSTEM.md §2
# ──────────────────────────────────────────────────────────────

class AnalysisReport(Base):
    """분석 보고 헤더 — 목록 화면 카드 1개 = 1행, 상세 화면 스크롤 항목 1개 = 1행."""

    __tablename__ = "analysis_reports"
    __table_args__ = (
        Index("idx_reports_pair_time", "currency_pair", "reported_at"),
        Index("idx_reports_exchange_type", "exchange", "report_type"),
        UniqueConstraint("exchange", "currency_pair", "report_type", "reported_at",
                         name="uq_analysis_reports"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    exchange = Column(String(50), nullable=False)           # 'gmofx'
    currency_pair = Column(String(20), nullable=False)      # 'USD_JPY'
    report_type = Column(String(20), nullable=False)        # 'daily', 'weekly', 'monthly'
    reported_at = Column(DateTime(timezone=True), nullable=False)
    chart_start = Column(DateTime(timezone=True), nullable=False)
    chart_end = Column(DateTime(timezone=True), nullable=False)
    strategy_active = Column(Boolean, nullable=False, default=False)
    strategy_id = Column(Integer, nullable=True)            # FK 없음 — 거래소별 테이블 다름
    final_decision = Column(String(50), nullable=True)      # 'approved','rejected','conditional','hold'
    final_rationale = Column(Text, nullable=True)
    next_review = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    analyses = relationship(
        "AgentAnalysis", back_populates="report", cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return (
            f"<AnalysisReport(id={self.id}, pair={self.currency_pair!r}, "
            f"type={self.report_type!r}, decision={self.final_decision!r})>"
        )


class AgentAnalysis(Base):
    """에이전트별 분석 — 보고 1건 × alice / samantha / rachel 각 1행."""

    __tablename__ = "agent_analysis"
    __table_args__ = (
        Index("idx_agent_analysis_report", "report_id"),
        Index("idx_agent_analysis_agent", "agent_name"),
        UniqueConstraint("report_id", "agent_name", name="uq_agent_analysis"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_id = Column(
        Integer, ForeignKey("analysis_reports.id", ondelete="CASCADE"), nullable=False
    )
    agent_name = Column(String(50), nullable=False)         # 'alice', 'samantha', 'rachel'
    summary = Column(Text, nullable=False)                  # 목록 화면 2~3줄 요약
    structured_data = Column(JSON, nullable=False)          # JSONB — 프로그래밍적 접근용
    full_text = Column(Text, nullable=True)                 # 상세 화면 Markdown 전문
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    report = relationship("AnalysisReport", back_populates="analyses")

    def __repr__(self) -> str:
        return (
            f"<AgentAnalysis(id={self.id}, report_id={self.report_id}, "
            f"agent={self.agent_name!r})>"
        )


# ──────────────────────────────────────────────────────────────
# Paper Trading 기록 (공유 테이블, prefix 없음)
# 설계서: trader-common/solution-design/ALPHA_FACTORS_PROPOSAL.md §15.3
# ──────────────────────────────────────────────────────────────

class PaperTrade(Base):
    """Proposed 전략 가상 매매 기록. 실제 주문 없이 진입/청산 시뮬레이션."""

    __tablename__ = "paper_trades"
    __table_args__ = (
        Index("idx_paper_trades_strategy_pair", "strategy_id", "pair"),
        Index("idx_paper_trades_entry_time", "entry_time"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(Integer, nullable=False)           # gmo_strategies.id 등 (FK 없음 — prefix 다름)
    pair = Column(String(20), nullable=False)               # 'USD_JPY'
    direction = Column(String(10), nullable=False)          # 'long' | 'short'
    entry_price = Column(Numeric(16, 6), nullable=True)
    entry_time = Column(DateTime(timezone=True), nullable=True)
    exit_price = Column(Numeric(16, 6), nullable=True)
    exit_time = Column(DateTime(timezone=True), nullable=True)
    exit_reason = Column(String(50), nullable=True)         # 'near_lower_exit', 'price_stop_loss', ...
    paper_pnl_pct = Column(Numeric(8, 4), nullable=True)    # 손익률 (%) — 슬리피지 미반영
    paper_pnl_jpy = Column(Numeric(12, 2), nullable=True)   # 가상 JPY 손익
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<PaperTrade(id={self.id}, strategy_id={self.strategy_id}, "
            f"pair={self.pair!r}, direction={self.direction!r}, "
            f"pnl_pct={self.paper_pnl_pct})>"
        )


# ──────────────────────────────────────────────────────────────
# 전략 스냅샷 (P-1 동적 전략 스위칭)
# 설계서: solution-design/DYNAMIC_STRATEGY_SWITCHING.md §4
# ──────────────────────────────────────────────────────────────

def create_strategy_snapshot_model(prefix: str):
    """
    전략 상태 스냅샷 ORM 모델 팩토리 (P-1 동적 전략 스위칭 시스템).

    매 4H봉 확정 또는 포지션 이벤트 시 전 전략(active+paper)의
    Score + 체제 + 상세 상태를 기록.

    create_strategy_snapshot_model("gmo") → table: gmo_strategy_snapshots
    create_strategy_snapshot_model("bf")  → table: bf_strategy_snapshots
    """
    _table = f"{prefix}_strategy_snapshots"
    _strategies_table = f"{prefix}_strategies"
    cls_name = f"{prefix.capitalize()}StrategySnapshot"

    attrs: dict = {
        "__tablename__": _table,
        "__table_args__": (
            Index(f"idx_{prefix}_snapshots_strategy_time", "strategy_id", "snapshot_time"),
            Index(f"idx_{prefix}_snapshots_pair_time", "pair", "snapshot_time"),
            {"extend_existing": True},
        ),
        "id": Column(Integer, primary_key=True, autoincrement=True),
        "strategy_id": Column(
            Integer,
            ForeignKey(f"{_strategies_table}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        "pair": Column(String(20), nullable=False),
        "trading_style": Column(String(30), nullable=False),
        # trigger_type: '4h_candle' | 'position_open' | 'position_close' | 'switch_eval'
        "trigger_type": Column(String(30), nullable=False),
        "snapshot_time": Column(DateTime(timezone=True), nullable=False),
        "score": Column(Numeric(6, 4), nullable=True),
        "readiness": Column(Numeric(6, 4), nullable=True),
        "edge": Column(Numeric(6, 4), nullable=True),
        "regime_fit": Column(Numeric(6, 4), nullable=True),
        "regime": Column(String(20), nullable=True),       # 'ranging' | 'trending' | 'unclear'
        "confidence": Column(String(10), nullable=True),   # 'high' | 'medium' | 'low' | 'none'
        "has_position": Column(Boolean, nullable=False, server_default="false"),
        "current_price": Column(Numeric(18, 8), nullable=True),
        "detail": Column(JSON, nullable=True),             # 전략별 상세 (박스 상태, 추세 시그널 등)
        "created_at": Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
        "__repr__": lambda self: (
            f"<{self.__class__.__name__}(strategy_id={self.strategy_id}, "
            f"score={self.score}, trigger={self.trigger_type!r})>"
        ),
    }
    return type(cls_name, (Base,), attrs)


# ──────────────────────────────────────────────────────────────
# 스위칭 추천 (P-1 Step 3/4)
# 설계서: solution-design/DYNAMIC_STRATEGY_SWITCHING.md §4
# ──────────────────────────────────────────────────────────────

def create_switch_recommendation_model(prefix: str):
    """
    전략 스위칭 추천 이력 ORM 모델 팩토리 (P-1 동적 전략 스위칭 시스템).

    create_switch_recommendation_model("gmo") → table: gmo_switch_recommendations
    create_switch_recommendation_model("bf")  → table: bf_switch_recommendations
    """
    _table = f"{prefix}_switch_recommendations"
    _strategies_table = f"{prefix}_strategies"
    cls_name = f"{prefix.capitalize()}SwitchRecommendation"

    attrs: dict = {
        "__tablename__": _table,
        "__table_args__": (
            Index(f"idx_{prefix}_switch_rec_decision", "decision", "created_at"),
            Index(f"idx_{prefix}_switch_rec_created", "created_at"),
            {"extend_existing": True},
        ),
        "id": Column(Integer, primary_key=True, autoincrement=True),
        "trigger_type": Column(String(30), nullable=False),       # T1_position_close | T2_candle_close
        "triggered_at": Column(DateTime(timezone=True), nullable=False),
        "current_strategy_id": Column(
            Integer, ForeignKey(f"{_strategies_table}.id", ondelete="SET NULL"), nullable=True
        ),
        "current_score": Column(Numeric(6, 4), nullable=True),
        "recommended_strategy_id": Column(
            Integer, ForeignKey(f"{_strategies_table}.id", ondelete="SET NULL"), nullable=True
        ),
        "recommended_score": Column(Numeric(6, 4), nullable=True),
        "score_ratio": Column(Numeric(6, 4), nullable=True),      # recommended / current
        "confidence": Column(String(10), nullable=True),           # high | medium | low | none
        "reason": Column(Text, nullable=True),
        "decision": Column(String(10), nullable=False, server_default="'pending'"),  # pending|approved|rejected|expired
        "decided_at": Column(DateTime(timezone=True), nullable=True),
        "decided_by": Column(String(20), nullable=True),          # rachel | soobo
        "reject_reason": Column(Text, nullable=True),
        "created_at": Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
        "__repr__": lambda self: (
            f"<{self.__class__.__name__}(id={self.id}, "
            f"decision={self.decision!r}, ratio={self.score_ratio})>"
        ),
    }
    return type(cls_name, (Base,), attrs)


# ──────────────────────────────────────────────────────────────
# AI 판단 기록 (공유 테이블, prefix 없음)
# 설계서: trader-common/docs/specs/ai-native/02_JUDGMENT_ENGINE.md §7-4
# ──────────────────────────────────────────────────────────────

class AiJudgment(Base):
    """alice-samantha-rachel 3단계 판단 기록.

    v2(TRADING_MODE=ai) 에서 매 4H 봉 시 INSERT.
    v1(rule_based) source = 'rule_based_v1' 이고 agent 컬럼은 NULL.
    """

    __tablename__ = "ai_judgments"
    __table_args__ = (
        Index("ix_ai_judgments_pair_ts", "pair", "timestamp"),
        Index("ix_ai_judgments_source", "source"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    trigger_type = Column(String(20), nullable=False)    # "regular_4h"|"event"|"daily_briefing"
    timestamp = Column(DateTime(timezone=True), nullable=False)
    pair = Column(String(20), nullable=False)
    exchange = Column(String(10), nullable=False)

    # alice
    alice_action = Column(String(20), nullable=True)
    alice_confidence = Column(Float, nullable=True)
    alice_reasoning = Column(JSON, nullable=True)
    alice_risk_factors = Column(JSON, nullable=True)

    # samantha
    samantha_verdict = Column(String(20), nullable=True)
    samantha_confidence_adj = Column(Float, nullable=True)
    samantha_reasoning = Column(Text, nullable=True)
    samantha_missed_risks = Column(JSON, nullable=True)

    # rachel
    rachel_action = Column(String(20), nullable=True)
    rachel_confidence = Column(Float, nullable=True)
    rachel_reasoning = Column(Text, nullable=True)
    rachel_failure_note = Column(Text, nullable=True)

    # 최종 결정
    final_action = Column(String(20), nullable=False)
    final_confidence = Column(Float, nullable=False)
    final_size_pct = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    source = Column(String(30), nullable=False)           # "ai_v2"|"ai_v2_fallback_v1"|"rule_based_v1"

    # 안전장치 결과
    guardrail_approved = Column(Boolean, nullable=True)
    guardrail_violations = Column(JSON, nullable=True)

    # 결과 추적 (사후 업데이트)
    outcome = Column(String(10), nullable=True)           # "win"|"loss"|None
    realized_pnl = Column(Float, nullable=True)
    hold_duration_hours = Column(Float, nullable=True)
    confidence_error = Column(Float, nullable=True)       # |predicted - actual_success|
    post_analysis = Column(Text, nullable=True)           # LLM 사후 분석 텍스트 (ENABLE_POST_ANALYSIS=true 시)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<AiJudgment(id={self.id}, pair={self.pair!r}, "
            f"action={self.final_action!r}, source={self.source!r})>"
        )


# ──────────────────────────────────────────────────────────────
# 레이첼 전략 자문 (공유 테이블, prefix 없음)
# 설계서: trader-common/docs/specs/ai-native/02_JUDGMENT_ENGINE.md
# ──────────────────────────────────────────────────────────────

class RachelAdvisory(Base):
    """레이첼 OpenClaw 에이전트가 저장하는 전략 자문.

    TRADING_MODE=rachel 일 때 candle_monitor()가 이 레코드를 읽어
    실시간 시그널과 결합하여 진입/청산 판단을 내린다.
    레이첼이 정기 분석(WORKFLOW_1 등) 완료 후 POST /api/advisories 로 저장.
    """

    __tablename__ = "rachel_advisories"
    __table_args__ = (
        Index("ix_rachel_advisories_pair_exchange_created", "pair", "exchange", "created_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair = Column(String(20), nullable=False)
    exchange = Column(String(10), nullable=False)

    # 판정
    action = Column(String(20), nullable=False)            # "entry_long"|"entry_short"|"hold"|"exit"
    confidence = Column(Float, nullable=False)             # 0.0 ~ 1.0
    size_pct = Column(Float, nullable=True)                # 포지션 사이즈 비율 (0.0~0.80)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)

    # 컨텍스트
    regime = Column(String(20), nullable=True)             # "trending"|"ranging"|"uncertain"
    reasoning = Column(Text, nullable=False)               # 판정 근거 요약
    risk_notes = Column(Text, nullable=True)               # 리스크 노트

    # 에이전트 요약 (학습 루프용)
    alice_summary = Column(Text, nullable=True)            # 앨리스 제안 1줄
    samantha_summary = Column(Text, nullable=True)         # 사만다 감사 1줄

    # 전략 타입 분리 (듀얼 매니저 — RegimeGate)
    trading_style = Column(String(50), nullable=False, server_default="trend_following")

    # adjust_risk 전용 (action=adjust_risk 시에만 사용)
    adjustments = Column(JSON, nullable=True)              # {stop_loss_pct, take_profit_ratio, trailing_atr_multiplier, force_exit}

    # hold 시 엔진 자율 진입 허용 정책
    # "none" = 절대 hold 유지 (기본값)
    # "signal_long_setup" = long_setup/short_setup 시그널이면 진입 허용 (Rachel이 기술적 사유로 hold 시)
    hold_override_policy = Column(String(30), nullable=False, server_default="none")

    # 매크로 컨텍스트 (AI 판단 추적·학습용)
    # {raw: {fng, news_avg, vix, dxy}, interpretation: str, impact_direction: str, impact_notes: str}
    macro_context = Column(JSON, nullable=True, default=None)

    # 시간
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)  # 이후 v1 폴백

    def __repr__(self) -> str:
        return (
            f"<RachelAdvisory(id={self.id}, pair={self.pair!r}, "
            f"action={self.action!r}, expires_at={self.expires_at!r})>"
        )
