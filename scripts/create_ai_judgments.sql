-- ai_judgments 테이블 생성
-- 설계서: trader-common/docs/specs/ai-native/02_JUDGMENT_ENGINE.md
-- 생성일: 2026-04-11

CREATE TABLE IF NOT EXISTS ai_judgments (
    id                      SERIAL PRIMARY KEY,
    trigger_type            VARCHAR(20)   NOT NULL,
    timestamp               TIMESTAMPTZ   NOT NULL,
    pair                    VARCHAR(20)   NOT NULL,
    exchange                VARCHAR(10)   NOT NULL,

    -- alice
    alice_action            VARCHAR(20),
    alice_confidence        FLOAT,
    alice_reasoning         JSONB,
    alice_risk_factors      JSONB,

    -- samantha
    samantha_verdict        VARCHAR(20),
    samantha_confidence_adj FLOAT,
    samantha_reasoning      TEXT,
    samantha_missed_risks   JSONB,

    -- rachel
    rachel_action           VARCHAR(20),
    rachel_confidence       FLOAT,
    rachel_reasoning        TEXT,
    rachel_failure_note     TEXT,

    -- 최종 결정
    final_action            VARCHAR(20)   NOT NULL,
    final_confidence        FLOAT         NOT NULL,
    final_size_pct          FLOAT,
    stop_loss               FLOAT,
    take_profit             FLOAT,
    source                  VARCHAR(30)   NOT NULL,

    -- 안전장치 결과
    guardrail_approved      BOOLEAN,
    guardrail_violations    JSONB,

    -- 결과 추적 (사후 업데이트)
    outcome                 VARCHAR(10),
    realized_pnl            FLOAT,
    hold_duration_hours     FLOAT,
    confidence_error        FLOAT,
    post_analysis           TEXT,

    created_at              TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_ai_judgments_pair_ts ON ai_judgments (pair, timestamp);
CREATE INDEX IF NOT EXISTS ix_ai_judgments_source  ON ai_judgments (source);

COMMENT ON TABLE ai_judgments IS 'AI 판단 기록 — alice-samantha-rachel 3단계 판단 로그';
