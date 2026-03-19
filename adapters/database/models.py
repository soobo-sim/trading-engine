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
)
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


def create_trade_model(prefix: str, order_id_length: int = 40):
    """
    거래 ORM 모델 팩토리.

    create_trade_model("ck", 25) → table: ck_trades, order_id VARCHAR(25)
    create_trade_model("bf", 40) → table: bf_trades, order_id VARCHAR(40)
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
            Index(f"idx_{prefix}_trade_pair_created", "pair", "created_at"),
            {"extend_existing": True},
        )

        id = Column(Integer, primary_key=True, index=True)
        order_id = Column(String(order_id_length), unique=True, nullable=False, index=True)

        pair = Column(String(20), nullable=False, index=True)
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
            CheckConstraint("status IN ('active','invalidated')", name=f"{prefix}_boxes_status_check"),
            CheckConstraint("upper_bound > lower_bound", name=f"{prefix}_boxes_bounds_check"),
            {"extend_existing": True},
        ),
        pair_column: Column(String(20), nullable=False),
        "id": Column(Integer, primary_key=True, autoincrement=True),
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
            CheckConstraint("side IN ('buy')", name=f"{prefix}_box_positions_side_check"),
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
        "__repr__": lambda self: (
            f"<{self.__class__.__name__}(box={self.box_id} status={self.status})>"
        ),
    }
    return type(cls_name, (Base,), attrs)


def create_trend_position_model(prefix: str, order_id_length: int = 40):
    """
    추세추종 포지션 ORM 모델 팩토리.

    create_trend_position_model("ck") → ck_trend_positions
    create_trend_position_model("bf") → bf_trend_positions
    """
    _table = f"{prefix}_trend_positions"
    _strategies_table = f"{prefix}_strategies"

    class TrendPosition(Base):
        __tablename__ = _table
        __table_args__ = (
            Index(f"idx_{prefix}_trend_positions_pair_status", "pair", "status"),
            Index(f"idx_{prefix}_trend_positions_strategy", "strategy_id"),
            Index(f"idx_{prefix}_trend_positions_created", "pair", "created_at"),
            CheckConstraint("status IN ('open','closed')", name=f"{prefix}_trend_positions_status_check"),
            {"extend_existing": True},
        )

        id = Column(Integer, primary_key=True, autoincrement=True)
        pair = Column(String(20), nullable=False)
        strategy_id = Column(
            Integer, ForeignKey(f"{_strategies_table}.id", ondelete="SET NULL"), nullable=True
        )

        entry_order_id = Column(String(order_id_length), nullable=False)
        entry_price = Column(Numeric(18, 8), nullable=False)
        entry_amount = Column(Numeric(18, 8), nullable=False)
        entry_jpy = Column(Numeric(18, 2), nullable=True)

        stop_loss_price = Column(Numeric(18, 8), nullable=True)

        partial_exit_count = Column(Integer, nullable=False, default=0)
        partial_exit_amount = Column(Numeric(18, 8), nullable=True)
        partial_exit_jpy = Column(Numeric(18, 2), nullable=True)
        partial_exit_reasons = Column(String(200), nullable=True)

        exit_order_id = Column(String(order_id_length), nullable=True)
        exit_price = Column(Numeric(18, 8), nullable=True)
        exit_amount = Column(Numeric(18, 8), nullable=True)
        exit_jpy = Column(Numeric(18, 2), nullable=True)
        exit_reason = Column(String(50), nullable=True)
        realized_pnl_jpy = Column(Numeric(18, 2), nullable=True)
        realized_pnl_pct = Column(Numeric(8, 4), nullable=True)

        status = Column(String(20), nullable=False, default="open")
        created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
        closed_at = Column(DateTime(timezone=True), nullable=True)

        def __repr__(self) -> str:
            return (
                f"<{self.__class__.__name__}(id={self.id}, pair={self.pair!r}, "
                f"status={self.status})>"
            )

    TrendPosition.__name__ = f"{prefix.capitalize()}TrendPosition"
    TrendPosition.__qualname__ = TrendPosition.__name__
    return TrendPosition
