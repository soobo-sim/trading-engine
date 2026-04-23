"""
Lesson ORM 모델 — P2 Self-Evolution Loop 외장 기억 저장소.

패턴 교훈을 영속 저장하여 LLM stateless 한계 극복.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.sql import func

from adapters.database.session import Base


class Lesson(Base):
    """에이전트 학습 교훈 레코드.

    ID 형식: L-{YYYY}-{NNN} (예: L-2026-001)
    status: active / deprecated / superseded / draft
    source: manual / hypothesis / post_analyzer
    """

    __tablename__ = "lessons"

    id = Column(String(20), primary_key=True)

    # P4에서 hypotheses 테이블 생성 후 FK 추가. P2에서는 단순 String.
    hypothesis_id = Column(String(20), nullable=True)

    # 패턴 키 (검색용)
    pattern_type = Column(String(40), nullable=False)
    market_regime = Column(String(20), nullable=True)   # trending/ranging/unclear/any
    pair = Column(String(20), nullable=True)             # btc_jpy / any

    # 조건 (JSON — GIN 인덱스는 PostgreSQL의 SQL 마이그레이션에서 별도 생성)
    conditions = Column(JSON, nullable=False, default=dict)

    # 본문
    observation = Column(Text, nullable=False)
    recommendation = Column(Text, nullable=False)
    outcome_stats = Column(JSON, nullable=True)

    # 거버넌스
    confidence = Column(Float, nullable=False, default=0.5)
    status = Column(String(20), nullable=False, default="active")
    superseded_by = Column(
        String(20),
        ForeignKey("lessons.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    source = Column(String(20), nullable=False, default="manual")
    author = Column(String(40), nullable=True)

    # 메타
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    last_referenced_at = Column(DateTime(timezone=True), nullable=True)
    reference_count = Column(Integer, nullable=False, default=0)
    last_decay_at = Column(DateTime(timezone=True), nullable=True)
