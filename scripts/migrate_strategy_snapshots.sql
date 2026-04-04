-- P-1 동적 전략 스위칭 — strategy_snapshots 마이그레이션
-- 설계서: solution-design/DYNAMIC_STRATEGY_SWITCHING.md §4
-- 실행: docker exec trader-postgres psql -U trader -d trader_db -f /migrate_strategy_snapshots.sql

-- ──────────────────────────────────────────────────
-- gmo_strategy_snapshots
-- ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS gmo_strategy_snapshots (
    id SERIAL PRIMARY KEY,
    strategy_id INTEGER NOT NULL REFERENCES gmo_strategies(id) ON DELETE CASCADE,
    pair VARCHAR(20) NOT NULL,
    trading_style VARCHAR(30) NOT NULL,
    trigger_type VARCHAR(30) NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL,
    score NUMERIC(6,4),
    readiness NUMERIC(6,4),
    edge NUMERIC(6,4),
    regime_fit NUMERIC(6,4),
    regime VARCHAR(20),
    confidence VARCHAR(10),
    has_position BOOLEAN NOT NULL DEFAULT FALSE,
    current_price NUMERIC(18,8),
    detail JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gmo_snapshots_strategy_time
    ON gmo_strategy_snapshots (strategy_id, snapshot_time DESC);
CREATE INDEX IF NOT EXISTS idx_gmo_snapshots_pair_time
    ON gmo_strategy_snapshots (pair, snapshot_time DESC);

-- ──────────────────────────────────────────────────
-- bf_strategy_snapshots
-- ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bf_strategy_snapshots (
    id SERIAL PRIMARY KEY,
    strategy_id INTEGER NOT NULL REFERENCES bf_strategies(id) ON DELETE CASCADE,
    pair VARCHAR(20) NOT NULL,
    trading_style VARCHAR(30) NOT NULL,
    trigger_type VARCHAR(30) NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL,
    score NUMERIC(6,4),
    readiness NUMERIC(6,4),
    edge NUMERIC(6,4),
    regime_fit NUMERIC(6,4),
    regime VARCHAR(20),
    confidence VARCHAR(10),
    has_position BOOLEAN NOT NULL DEFAULT FALSE,
    current_price NUMERIC(18,8),
    detail JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bf_snapshots_strategy_time
    ON bf_strategy_snapshots (strategy_id, snapshot_time DESC);
CREATE INDEX IF NOT EXISTS idx_bf_snapshots_pair_time
    ON bf_strategy_snapshots (pair, snapshot_time DESC);
