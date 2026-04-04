-- P-1 Step 3/4: 전략 스위칭 추천 이력 테이블
-- 실행: docker exec trader-postgres psql -U trader -d trader -f /tmp/migrate_switch_recommendations.sql

-- GMO FX
CREATE TABLE IF NOT EXISTS gmo_switch_recommendations (
    id SERIAL PRIMARY KEY,
    trigger_type VARCHAR(30) NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL,
    current_strategy_id INTEGER REFERENCES gmo_strategies(id) ON DELETE SET NULL,
    current_score NUMERIC(6,4),
    recommended_strategy_id INTEGER REFERENCES gmo_strategies(id) ON DELETE SET NULL,
    recommended_score NUMERIC(6,4),
    score_ratio NUMERIC(6,4),
    confidence VARCHAR(10),
    reason TEXT,
    decision VARCHAR(10) NOT NULL DEFAULT 'pending',
    decided_at TIMESTAMPTZ,
    decided_by VARCHAR(20),
    reject_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gmo_switch_rec_decision ON gmo_switch_recommendations(decision, created_at);
CREATE INDEX IF NOT EXISTS idx_gmo_switch_rec_created ON gmo_switch_recommendations(created_at);

-- BitFlyer
CREATE TABLE IF NOT EXISTS bf_switch_recommendations (
    id SERIAL PRIMARY KEY,
    trigger_type VARCHAR(30) NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL,
    current_strategy_id INTEGER REFERENCES bf_strategies(id) ON DELETE SET NULL,
    current_score NUMERIC(6,4),
    recommended_strategy_id INTEGER REFERENCES bf_strategies(id) ON DELETE SET NULL,
    recommended_score NUMERIC(6,4),
    score_ratio NUMERIC(6,4),
    confidence VARCHAR(10),
    reason TEXT,
    decision VARCHAR(10) NOT NULL DEFAULT 'pending',
    decided_at TIMESTAMPTZ,
    decided_by VARCHAR(20),
    reject_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bf_switch_rec_decision ON bf_switch_recommendations(decision, created_at);
CREATE INDEX IF NOT EXISTS idx_bf_switch_rec_created ON bf_switch_recommendations(created_at);
