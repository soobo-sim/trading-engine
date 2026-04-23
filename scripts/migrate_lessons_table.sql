-- P2 Self-Evolution Loop: lessons 테이블 생성
-- ⚠️ hypothesis_id FK는 P4(hypotheses 테이블 생성) 이후 추가.
-- ⚠️ 이미 적용된 경우 멱등하게 동작 (CREATE TABLE IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS lessons (
    id                 VARCHAR(20)   PRIMARY KEY,
    hypothesis_id      VARCHAR(20)   NULL,                  -- FK는 P4에서 추가
    pattern_type       VARCHAR(40)   NOT NULL,
    market_regime      VARCHAR(20)   NULL,
    pair               VARCHAR(20)   NULL,
    conditions         JSONB         NOT NULL DEFAULT '{}',
    observation        TEXT          NOT NULL,
    recommendation     TEXT          NOT NULL,
    outcome_stats      JSONB         NULL,
    confidence         FLOAT         NOT NULL DEFAULT 0.5,
    status             VARCHAR(20)   NOT NULL DEFAULT 'active',
    superseded_by      VARCHAR(20)   NULL REFERENCES lessons(id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED,
    source             VARCHAR(20)   NOT NULL DEFAULT 'manual',
    author             VARCHAR(40)   NULL,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    last_referenced_at TIMESTAMPTZ   NULL,
    reference_count    INTEGER       NOT NULL DEFAULT 0,
    last_decay_at      TIMESTAMPTZ   NULL
);

CREATE INDEX IF NOT EXISTS idx_lessons_pattern
    ON lessons (pattern_type, market_regime, pair, status);

CREATE INDEX IF NOT EXISTS idx_lessons_status
    ON lessons (status);

CREATE INDEX IF NOT EXISTS idx_lessons_hypothesis
    ON lessons (hypothesis_id);

CREATE INDEX IF NOT EXISTS idx_lessons_conditions_gin
    ON lessons USING GIN (conditions);
