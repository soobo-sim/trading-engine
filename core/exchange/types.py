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
    """거래소에 제출된 주문의 표준 표현.

    order_id는 str로 통일 — CK는 int지만 str 변환, BF는 원래 str(26자+).
    """
    order_id: str
    pair: str
    order_type: OrderType
    side: OrderSide
    price: Optional[float]          # 시장가이면 None
    amount: float                    # 수량 (BF의 size에 대응)
    status: OrderStatus = OrderStatus.PENDING
    created_at: Optional[datetime] = None
    raw: dict = field(default_factory=dict)  # 거래소 원본 응답 보전


# ──────────────────────────────────────────
# Pending Limit Order
# ──────────────────────────────────────────

@dataclass
class PendingLimitOrder:
    """Limit order 진입 대기 상태.

    place_order(BUY, price=X) 후 체결 대기 중인 주문 정보.
    _pending_limit_orders[pair] 에 저장되며, 60초 사이클마다 체결 여부를 확인한다.
    """
    order_id: str
    pair: str
    limit_price: float
    amount: float           # 코인 수량
    invest_jpy: float       # 투입 JPY (포지션 등록용)
    placed_at: float        # time.time() — 타임아웃 계산용
    signal_at_placement: str  # 진입 시 신호 ("entry_ok" | "entry_preview")
    params: dict            # 진입 시 전략 파라미터 스냅샷
    atr: Optional[float] = None
    signal_data: dict = field(default_factory=dict)
    is_preview: bool = False  # True → 체결 후 extra["preview_entry"]=True 설정


# ──────────────────────────────────────────
# 잔고
# ──────────────────────────────────────────

@dataclass(frozen=True)
class CurrencyBalance:
    """단일 통화 잔고."""
    currency: str           # "jpy", "xrp", "btc" (소문자 통일)
    amount: float           # 총 보유량
    available: float        # 주문 가능 금액


@dataclass
class Balance:
    """전체 잔고. 통화코드(소문자)로 인덱싱."""
    currencies: dict[str, CurrencyBalance] = field(default_factory=dict)

    def get(self, currency: str) -> CurrencyBalance:
        """통화 잔고 반환. 없으면 0 잔고."""
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
    db_record_id: Optional[int] = None          # ORM 레코드 FK
    stop_tightened: bool = False
    extra: dict = field(default_factory=dict)    # 전략별 추가 상태


# ──────────────────────────────────────────
# 증거금 (CFD)
# ──────────────────────────────────────────

@dataclass(frozen=True)
class Collateral:
    """CFD 증거금 상태."""
    collateral: float               # 証拠金 평가액 (JPY)
    open_position_pnl: float        # 미결제 포지션 평가손익
    require_collateral: float       # 필요 증거금
    keep_rate: float                # 증거금 유지율 (collateral / require_collateral)


@dataclass(frozen=True)
class FxPosition:
    """FX/CFD 건옥 (getpositions 응답 1건)."""
    product_code: str
    side: str                       # "BUY" or "SELL"
    price: float                    # 건값 (진입가)
    size: float                     # 수량 (BTC or 통화수량)
    pnl: float                      # 평가 P&L (JPY)
    leverage: float
    require_collateral: float
    swap_point_accumulate: float
    sfd: float
    open_date: Optional[datetime] = None
    position_id: Optional[int] = None   # GMO FX positionId (closeOrder에 필수)


# ──────────────────────────────────────────
# 거래소 제약
# ──────────────────────────────────────────

@dataclass(frozen=True)
class ExchangeConstraints:
    """거래소 고유 제약. 어댑터가 제공."""
    min_order_sizes: dict[str, float]       # currency → 최소 주문 수량
    rate_limit: tuple[int, int]             # (calls, seconds)
    order_id_type: type = str               # CK: int→str 변환, BF: str 그대로
    extra: dict = field(default_factory=dict)
