"""Executor — 주문 실행 추상화 계층.

RealExecutor: 실거래소 주문 위임.
PaperExecutor: 주문 스킵 + paper_trades 테이블 기록.
"""
from core.execution.executor import IExecutor, RealExecutor, PaperExecutor, create_executor

__all__ = ["IExecutor", "RealExecutor", "PaperExecutor", "create_executor"]
