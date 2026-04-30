"""
JITAdvisory ORM 모델 — 진입 직전 JIT 자문 감사 로그.

거래 결정마다 LLM 자문 요청/응답을 영속 저장한다.
- 감사: 언제, 어떤 요청으로, 어떤 응답을 받았는지
- 분석: GO/NO_GO/ADJUST 분포, latency 추이, 오류율
- 학습: actual_outcome → 사후 정확도 보완 (Phase 5 이후)
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.sql import func

from adapters.database.session import Base


class JITAdvisory(Base):
    """jit_advisories 테이블.

    CREATE TABLE jit_advisories (
        id              SERIAL PRIMARY KEY,
        request_id      VARCHAR(36)  NOT NULL,
        pair            VARCHAR(20)  NOT NULL,
        exchange        VARCHAR(30)  NOT NULL,
        trading_style   VARCHAR(40)  NOT NULL,
        proposed_action VARCHAR(20)  NOT NULL,
        rule_signal     VARCHAR(30)  NOT NULL,
        rule_confidence FLOAT        NOT NULL,
        rule_size_pct   FLOAT        NOT NULL,
        rule_reasoning  TEXT         NOT NULL DEFAULT '',

        jit_decision    VARCHAR(10),
        jit_confidence  FLOAT,
        jit_reasoning   TEXT,
        jit_size_pct    FLOAT,
        jit_model       VARCHAR(60),
        jit_latency_ms  INTEGER,
        jit_error       TEXT,

        final_action    VARCHAR(20)  NOT NULL,
        final_size_pct  FLOAT,

        created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
    );
    CREATE INDEX ix_jit_pair_created ON jit_advisories (pair, created_at DESC);
    CREATE INDEX ix_jit_final ON jit_advisories (final_action, created_at DESC);
    """

    __tablename__ = "jit_advisories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(36), nullable=False)

    # 요청 컨텍스트
    pair = Column(String(20), nullable=False)
    exchange = Column(String(30), nullable=False)
    trading_style = Column(String(40), nullable=False)
    proposed_action = Column(String(20), nullable=False)
    rule_signal = Column(String(30), nullable=False)
    rule_confidence = Column(Float, nullable=False)
    rule_size_pct = Column(Float, nullable=False)
    rule_reasoning = Column(Text, nullable=False, default="")

    # JIT 응답
    jit_decision = Column(String(10), nullable=True)    # GO/NO_GO/ADJUST/None(fail)
    jit_confidence = Column(Float, nullable=True)
    jit_reasoning = Column(Text, nullable=True)
    jit_size_pct = Column(Float, nullable=True)
    jit_model = Column(String(60), nullable=True)
    jit_latency_ms = Column(Integer, nullable=True)
    jit_error = Column(Text, nullable=True)             # 실패 시 오류 메시지

    # 최종 실행 결과
    final_action = Column(String(20), nullable=False)   # rule_based action after JIT
    final_size_pct = Column(Float, nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_jit_pair_created", "pair", created_at.desc()),
        Index("ix_jit_final", "final_action", created_at.desc()),
    )

    def __repr__(self) -> str:
        return (
            f"<JITAdvisory(id={self.id}, pair={self.pair!r}, "
            f"proposed={self.proposed_action!r}, jit={self.jit_decision!r}, "
            f"final={self.final_action!r})>"
        )
