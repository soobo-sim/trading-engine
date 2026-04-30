-- jit_advisories 테이블 생성
-- 적용: psql $DATABASE_URL -f scripts/create_jit_advisories.sql
-- 설계서: docs/proposals/active/JIT_ADVISORY_ARCHITECTURE.md §4.4

CREATE TABLE IF NOT EXISTS jit_advisories (
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

    -- JIT 응답 (실패 시 NULL)
    jit_decision    VARCHAR(10),
    jit_confidence  FLOAT,
    jit_reasoning   TEXT,
    jit_size_pct    FLOAT,
    jit_model       VARCHAR(60),
    jit_latency_ms  INTEGER,
    jit_error       TEXT,

    -- 최종 실행 결과
    final_action    VARCHAR(20)  NOT NULL,
    final_size_pct  FLOAT,

    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_jit_pair_created
    ON jit_advisories (pair, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_jit_final
    ON jit_advisories (final_action, created_at DESC);

-- 검증
SELECT 'jit_advisories 테이블 생성 완료' AS status;
SELECT count(*) AS row_count FROM jit_advisories;
