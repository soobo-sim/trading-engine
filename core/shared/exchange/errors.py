"""
거래소 표준 예외 계층.

어댑터는 거래소 고유 예외를 이 계층으로 매핑한다.
core/ 도메인 로직은 이 예외만 처리한다.
"""


class ExchangeError(Exception):
    """거래소 어댑터의 기본 예외."""

    def __init__(self, message: str, *, exchange: str = "", raw: dict | None = None):
        self.exchange = exchange
        self.raw = raw or {}
        super().__init__(message)


class OrderError(ExchangeError):
    """주문 실행 실패 (잔고 부족, 최소 수량 미달 등)."""


class AuthenticationError(ExchangeError):
    """API 키 / 서명 오류."""


class RateLimitError(ExchangeError):
    """레이트 리밋 초과."""


class ConnectionError(ExchangeError):  # noqa: A001 — 의도적으로 builtin shadow
    """네트워크 / WS 연결 실패."""


class InsufficientBalanceError(OrderError):
    """잔고 부족."""


class MinOrderSizeError(OrderError):
    """최소 주문 수량 미달."""
