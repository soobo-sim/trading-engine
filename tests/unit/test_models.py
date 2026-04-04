"""
ORM 모델 팩토리 단위 테스트.

DB 연결 없이 팩토리 함수 동작만 검증.
"""
from __future__ import annotations

import pytest

from adapters.database.models import (
    StrategyTechnique,
    create_balance_entry_model,
    create_box_model,
    create_box_position_model,
    create_candle_model,
    create_insight_model,
    create_strategy_model,
    create_strategy_snapshot_model,
    create_summary_model,
    create_trade_model,
    create_trend_position_model,
)


# ──────────────────────────────────────────────────────────────
# create_trade_model
# ──────────────────────────────────────────────────────────────


def test_create_trade_model_bf_tablename() -> None:
    """BF trade 팩토리: __tablename__ == 'bf_trades'."""
    BfTrade = create_trade_model("bf", order_id_length=40)
    assert BfTrade.__tablename__ == "bf_trades"


def test_create_trade_model_bf_order_id_length() -> None:
    """BF order_id: VARCHAR(40)."""
    BfTrade = create_trade_model("bf", order_id_length=40)
    col = BfTrade.__table__.columns["order_id"]
    assert col.type.length == 40


def test_create_trade_model_class_name() -> None:
    """팩토리 반환 클래스의 __name__ 에 prefix 가 반영된다."""
    BfTrade = create_trade_model("bf", 40)
    assert BfTrade.__name__ == "BfTrade"


# ──────────────────────────────────────────────────────────────
# StrategyTechnique 공유 테이블
# ──────────────────────────────────────────────────────────────

def test_strategy_technique_tablename() -> None:
    """StrategyTechnique: prefix 없는 공유 테이블."""
    assert StrategyTechnique.__tablename__ == "strategy_techniques"


def test_strategy_technique_has_no_prefix() -> None:
    """테이블명에 ck_/bf_ 프리픽스가 없어야 한다."""
    tablename = StrategyTechnique.__tablename__
    assert not tablename.startswith("ck_")
    assert not tablename.startswith("bf_")


# ──────────────────────────────────────────────────────────────
# 나머지 팩토리 — 에러 없이 클래스 반환
# ──────────────────────────────────────────────────────────────

def test_all_factories_return_classes_for_ck() -> None:
    """모든 팩토리 함수가 CK prefix로 에러 없이 클래스를 반환한다."""
    CkStrategy = create_strategy_model("ck")
    CkBalance = create_balance_entry_model("ck")
    CkInsight = create_insight_model("ck")
    CkSummary = create_summary_model("ck")
    CkCandle = create_candle_model("ck", pair_column="pair")
    CkBox = create_box_model("ck", pair_column="pair")
    CkBoxPos = create_box_position_model("ck", pair_column="pair", order_id_length=40)
    CkTrendPos = create_trend_position_model("ck", order_id_length=40)

    assert CkStrategy.__tablename__ == "ck_strategies"
    assert CkBalance.__tablename__ == "ck_balance_entries"
    assert CkInsight.__tablename__ == "ck_insights"
    assert CkSummary.__tablename__ == "ck_summaries"
    assert CkCandle.__tablename__ == "ck_candles"
    assert CkBox.__tablename__ == "ck_boxes"
    assert CkBoxPos.__tablename__ == "ck_box_positions"
    assert CkTrendPos.__tablename__ == "ck_trend_positions"
    # pair_column 검증
    assert "pair" in CkCandle.__table__.columns
    assert "pair" in CkBox.__table__.columns
    assert "pair" in CkBoxPos.__table__.columns
    assert "pair" in CkTrendPos.__table__.columns


def test_all_factories_return_classes_for_bf() -> None:
    """모든 팩토리 함수가 BF prefix로 에러 없이 클래스를 반환한다."""
    BfStrategy = create_strategy_model("bf")
    BfBalance = create_balance_entry_model("bf")
    BfInsight = create_insight_model("bf")
    BfSummary = create_summary_model("bf")
    BfCandle = create_candle_model("bf", pair_column="product_code")
    BfBox = create_box_model("bf", pair_column="product_code")
    BfBoxPos = create_box_position_model("bf", pair_column="product_code", order_id_length=40)
    BfTrendPos = create_trend_position_model("bf", order_id_length=40)

    assert BfStrategy.__tablename__ == "bf_strategies"
    assert BfBalance.__tablename__ == "bf_balance_entries"
    assert BfInsight.__tablename__ == "bf_insights"
    assert BfSummary.__tablename__ == "bf_summaries"
    assert BfCandle.__tablename__ == "bf_candles"
    assert BfBox.__tablename__ == "bf_boxes"
    assert BfBoxPos.__tablename__ == "bf_box_positions"
    assert BfTrendPos.__tablename__ == "bf_trend_positions"
    # pair_column 검증
    assert "product_code" in BfCandle.__table__.columns
    assert "product_code" in BfBox.__table__.columns
    assert "product_code" in BfBoxPos.__table__.columns
    assert "pair" in BfTrendPos.__table__.columns  # BUG-011: trend_position은 항상 pair


# ──────────────────────────────────────────────────────────────
# create_strategy_snapshot_model (P-1 동적 전략 스위칭)
# ──────────────────────────────────────────────────────────────

def test_strategy_snapshot_gmo_tablename() -> None:
    """GMO prefix: __tablename__ == 'gmo_strategy_snapshots'."""
    GmoSnapshot = create_strategy_snapshot_model("gmo")
    assert GmoSnapshot.__tablename__ == "gmo_strategy_snapshots"


def test_strategy_snapshot_bf_tablename() -> None:
    """BF prefix: __tablename__ == 'bf_strategy_snapshots'."""
    BfSnapshot = create_strategy_snapshot_model("bf")
    assert BfSnapshot.__tablename__ == "bf_strategy_snapshots"


def test_strategy_snapshot_required_columns() -> None:
    """필수 컬럼 존재 확인: strategy_id, pair, trading_style, trigger_type, snapshot_time, score."""
    GmoSnapshot = create_strategy_snapshot_model("gmo")
    cols = GmoSnapshot.__table__.columns
    for col_name in ("strategy_id", "pair", "trading_style", "trigger_type", "snapshot_time",
                     "score", "readiness", "edge", "regime_fit", "regime", "confidence",
                     "has_position", "current_price", "detail", "created_at"):
        assert col_name in cols, f"컬럼 누락: {col_name}"


def test_strategy_snapshot_detail_is_json() -> None:
    """detail 컬럼이 JSON 타입이어야 한다."""
    from sqlalchemy import JSON
    GmoSnapshot = create_strategy_snapshot_model("gmo")
    col = GmoSnapshot.__table__.columns["detail"]
    assert isinstance(col.type, JSON)


def test_strategy_snapshot_class_name() -> None:
    """클래스명에 prefix 반영 확인."""
    GmoSnapshot = create_strategy_snapshot_model("gmo")
    assert GmoSnapshot.__name__ == "GmoStrategySnapshot"


def test_strategy_snapshot_has_position_default_false() -> None:
    """has_position의 server_default가 'false'."""
    GmoSnapshot = create_strategy_snapshot_model("gmo")
    col = GmoSnapshot.__table__.columns["has_position"]
    assert str(col.server_default.arg) == "false"
