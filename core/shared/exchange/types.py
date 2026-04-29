"""
공통 DTO — 거래소-무관한 도메인 타입 정의.

이 모듈의 타입은 core/ 전체에서 사용된다.
adapters/는 거래소 고유 응답을 이 타입으로 변환하여 core/에 전달한다.
FastAPI, SQLAlchemy 등 프레임워크에 의존하지 않는다.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# ──────────────────────────────────────────
# Enums
# ──────────────────────────────────────────

class OrderSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    """주문 타입 (에이전트/내부 표현)"""
    BUY = "buy"
    SELL = "sell"
    MARKET_BUY = "market_buy"
    MARKET_SELL = "market_sell"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class StrategyStatus(str, enum.Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class AnalysisType(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    TRADE_SPECIFIC = "trade_specific"
    PATTERN = "pattern"


# ──────────────────────────────────────────
# 시세 / 캔들
# ──────────────────────────────────────────

@dataclass(frozen=True)
class Ticker:
    """거래소 현재가 스냅샷."""
    pair: str
    last: float
    bid: float
    ask: float
    high: float
    low: float
    volume: float
    timestamp: Optional[datetime] = None


@dataclass(frozen=True)
class Candle:
    """OHLCV 캔들. signals.py가 duck-typing으로 .close, .high, .low를 참조한다."""
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    pair: str = ""
    timeframe: str = ""


# ──────────────────────────────────────────
# 주문
# ──────────────────────────────────────────

@dataclass(frozen=True)
class Order:
    """거래소에 제출된 주문의 표준 표현."""
    order_id: str
    pair: str
    order_type: OrderType
    side: OrderSide
    price: Optional[float]
    amount: float
    status: OrderStatus = OrderStatus.PENDING
    created_at: Optional[datetime] = None
    raw: dict = field(default_factory=dict)


# ──────────────────────────────────────────
# Pending Limit Order
# ──────────────────────────────────────────

@dataclass
class PendingLimitOrder:
    """Limit order 진입 대기 상태."""
    order_id: str
    pair: str
    limit_price: float
    amount: float
    invest_jpy: float
    placed_at: float
    signal_at_placement: str
    params: dict
    atr: Optional[float] = None
    signal_data: dict = field(default_factory=dict)


# ──────────────────────────────────────────
# 잔고
# ──────────────────────────────────────────

@dataclass(frozen=True)
class CurrencyBalance:
    """단일 통화 잔고."""
    currency: str
    amount: float
    available: float


@dataclass
class Balance:
    """전체 잔고. 통화코드(소문자)로 인덱싱."""
    currencies: dict[str, CurrencyBalance] = field(default_factory=dict)

    def get(self, currency: str) -> CurrencyBalance:
        key = currency.lower()
        return self.currencies.get(key, CurrencyBalance(currency=key, amount=0.0, available=0.0))

    def get_available(self, currency: str) -> float:
        return self.get(currency).available

    def get_amount(self, currency: str) -> float:
        return self.get(currency).amount


# ──────────────────────────────────────────
# 포지션
# ──────────────────────────────────────────

@dataclass
class Position:
    """인메모리 포지션 추적용. DB 레코드와는 별개."""
    pair: str
    entry_price: Optional[float]
    entry_amount: float
    stop_loss_price: Optional[float] = None
    db_record_id: Optional[int] = None
    stop_tightened: bool = False
    extra: dict = field(default_factory=dict)


# ──────────────────────────────────────────
# 증거금 (CFD)
# ──────────────────────────────────────────

@dataclass(frozen=True)
class Collateral:
    """CFD 증거금 상태."""
    collateral: float
    open_position_pnl: float
    require_collateral: float
    keep_rate: float


@dataclass(frozen=True)
class FxPosition:
    """FX/CFD 건옥 (getpositions 응답 1건)."""
    product_code: str
    side: str
    price: float
    size: float
    pnl: float
    leverage: float
    require_collateral: float
    swap_point_accumulate: float
    sfd: float
    open_date: Optional[datetime] = None
    position_id: Optional[int] = None


# ──────────────────────────────────────────
# 거래소 제약
# ──────────────────────────────────────────

@dataclass(frozen=True)
class ExchangeConstraints:
    """거래소 고유 제약. 어댑터가 제공."""
    min_order_sizes: dict[str, float]
    rate_limit: tuple[int, int]
    order_id_type: type = str
    extra: dict = field(default_factory=dict)
