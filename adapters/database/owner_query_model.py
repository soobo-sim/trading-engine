"""
owner_query_model.py — P8 OwnerQuery ORM 모델.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, String, Text, func

from adapters.database.session import Base


class OwnerQuery(Base):
    __tablename__ = "owner_queries"
    __table_args__ = (
        Index("idx_owner_queries_status", "status", "priority"),
        Index("idx_owner_queries_asked", "asked_at"),
    )

    id = Column(String(20), primary_key=True)  # OQ-2026-001
    content = Column(Text(), nullable=False)
    category = Column(String(40), nullable=False, default="general")
    status = Column(String(20), nullable=False, default="open")
    priority = Column(String(10), nullable=False, default="medium")

    # 가설/사이클 연결
    addressed_in_cycle = Column(String(20), nullable=True)
    addressed_in_hypothesis = Column(String(20), nullable=True)
    outcome_summary = Column(Text(), nullable=True)

    # 메타
    asked_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    source = Column(String(40), nullable=False, default="samantha")
