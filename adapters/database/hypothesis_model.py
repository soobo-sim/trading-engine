"""
Hypothesis ORM 모델 — P4 Self-Evolution Loop.

가설(Hypothesis)은 기존 Tunable 값 변경 제안으로,
proposed → backtested → paper → canary → adopted 생애주기를 거친다.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.sql import func

from adapters.database.session import Base


class Hypothesis(Base):
    """에이전트 가설 레코드.

    ID 형식: H-{YYYY}-{NNN} (예: H-2026-001)
    track: standard | escalation
    status: proposed / backtested / paper / canary / adopted / rejected / rolled_back / archived
    """

    __tablename__ = "hypotheses"

    id = Column(String(20), primary_key=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)
    track = Column(String(20), nullable=False, default="standard")
    status = Column(String(20), nullable=False, default="proposed")

    # Tunable 변경 명세 배열 (JSON — SQLite 호환, 프로덕션은 JSONB)
    changes = Column(JSON, nullable=False, default=list)

    # 단계별 검증 결과
    backtest_result = Column(JSON, nullable=True)
    paper_result = Column(JSON, nullable=True)
    canary_result = Column(JSON, nullable=True)
    baseline_metrics = Column(JSON, nullable=True)   # 비교 기준

    # 거버넌스
    proposer = Column(String(40), nullable=False)
    approver = Column(String(40), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)  # escalation: 7일
    rejection_reason = Column(Text, nullable=True)
    rollback_reason = Column(Text, nullable=True)

    # Lesson 연결 (양방향)
    source_lessons = Column(JSON, nullable=True)          # 참고한 lesson IDs 배열
    resulting_lesson_id = Column(
        String(20),
        ForeignKey("lessons.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )

    # 메타
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
