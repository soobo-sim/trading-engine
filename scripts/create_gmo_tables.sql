-- GMO FX テーブル作成 SQL
-- trading-engine (EXCHANGE=gmofx) で使用するテーブル
-- 実行: docker exec trader-postgres psql -U postgres -d trader -f /tmp/gmo_tables.sql
-- 
-- 前提: strategy_techniques テーブルは既に存在
-- 
-- Created: 2026-03-21

-- ──────────────────────────────────────────────────
-- 1. Enum types (gmo_ prefix)
-- ──────────────────────────────────────────────────
DO $$ BEGIN
    CREATE TYPE gmo_strategystatus AS ENUM ('proposed', 'active', 'archived', 'rejected');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE gmo_ordertype AS ENUM ('buy', 'sell', 'market_buy', 'market_sell');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE gmo_orderstatus AS ENUM ('pending', 'open', 'completed', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE gmo_analysistype AS ENUM ('daily', 'weekly', 'trade_specific', 'pattern');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- ──────────────────────────────────────────────────
-- 2. gmo_strategies
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmo_strategies (
    id SERIAL PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    status gmo_strategystatus NOT NULL DEFAULT 'proposed',
    name VARCHAR(100) NOT NULL,
    description TEXT NOT NULL,
    parameters JSONB NOT NULL,
    rationale TEXT NOT NULL,
    rejection_reason TEXT,
    performance_summary JSONB,
    technique_code VARCHAR(50) REFERENCES strategy_techniques(code) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gmo_strategy_status ON gmo_strategies (status);
CREATE INDEX IF NOT EXISTS idx_gmo_strategy_status_created ON gmo_strategies (status, created_at);
CREATE INDEX IF NOT EXISTS idx_gmo_strategy_technique ON gmo_strategies (technique_code);


-- ──────────────────────────────────────────────────
-- 3. gmo_trades
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmo_trades (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(40) UNIQUE NOT NULL,
    pair VARCHAR(20) NOT NULL,
    order_type gmo_ordertype NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    price DOUBLE PRECISION,
    executed_price DOUBLE PRECISION,
    executed_amount DOUBLE PRECISION DEFAULT 0.0,
    status gmo_orderstatus DEFAULT 'pending',
    reasoning TEXT NOT NULL,
    market_pulse JSONB,
    trading_pattern VARCHAR(20),
    strategy_id INTEGER REFERENCES gmo_strategies(id) ON DELETE SET NULL,
    profit_loss DOUBLE PRECISION,
    profit_loss_percentage DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    executed_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gmo_trade_order_id ON gmo_trades (order_id);
CREATE INDEX IF NOT EXISTS idx_gmo_trade_pair ON gmo_trades (pair);
CREATE INDEX IF NOT EXISTS idx_gmo_trade_status ON gmo_trades (status);
CREATE INDEX IF NOT EXISTS idx_gmo_trade_created_status ON gmo_trades (created_at, status);
CREATE INDEX IF NOT EXISTS idx_gmo_trade_pair_created ON gmo_trades (pair, created_at);
CREATE INDEX IF NOT EXISTS idx_gmo_trade_strategy ON gmo_trades (strategy_id);


-- ──────────────────────────────────────────────────
-- 4. gmo_balance_entries
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmo_balance_entries (
    id SERIAL PRIMARY KEY,
    currency VARCHAR(20) NOT NULL,
    available DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    reserved DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    trade_id INTEGER REFERENCES gmo_trades(id) ON DELETE SET NULL,
    entry_source VARCHAR(20),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gmo_balance_currency ON gmo_balance_entries (currency);
CREATE INDEX IF NOT EXISTS idx_gmo_balance_currency_created ON gmo_balance_entries (currency, created_at);


-- ──────────────────────────────────────────────────
-- 5. gmo_insights
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmo_insights (
    id SERIAL PRIMARY KEY,
    trade_id INTEGER REFERENCES gmo_trades(id) ON DELETE CASCADE,
    analysis_type gmo_analysistype NOT NULL,
    content TEXT NOT NULL,
    key_lessons JSONB,
    metrics JSONB,
    confidence_score DOUBLE PRECISION,
    applied_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gmo_insight_type ON gmo_insights (analysis_type);
CREATE INDEX IF NOT EXISTS idx_gmo_insight_type_created ON gmo_insights (analysis_type, created_at);
CREATE INDEX IF NOT EXISTS idx_gmo_insight_trade ON gmo_insights (trade_id);


-- ──────────────────────────────────────────────────
-- 6. gmo_summaries
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmo_summaries (
    id SERIAL PRIMARY KEY,
    period_type VARCHAR(20) NOT NULL,
    start_date TIMESTAMPTZ NOT NULL,
    end_date TIMESTAMPTZ NOT NULL,
    content TEXT NOT NULL,
    key_learnings JSONB,
    metrics JSONB NOT NULL,
    recommendations JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gmo_summary_period ON gmo_summaries (period_type);
CREATE INDEX IF NOT EXISTS idx_gmo_summary_period_dates ON gmo_summaries (period_type, start_date, end_date);


-- ──────────────────────────────────────────────────
-- 7. gmo_candles (trading-engine 用 — pair カラム)
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmo_candles (
    pair VARCHAR(20) NOT NULL,
    timeframe VARCHAR(5) NOT NULL,
    open_time TIMESTAMPTZ NOT NULL,
    close_time TIMESTAMPTZ NOT NULL,
    "open" NUMERIC(18,8) NOT NULL,
    "high" NUMERIC(18,8) NOT NULL,
    "low" NUMERIC(18,8) NOT NULL,
    "close" NUMERIC(18,8) NOT NULL,
    volume NUMERIC(18,8) NOT NULL DEFAULT 0,
    tick_count INTEGER NOT NULL DEFAULT 0,
    is_complete BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (pair, timeframe, open_time)
);

CREATE INDEX IF NOT EXISTS idx_gmo_candles_lookup ON gmo_candles (pair, timeframe, open_time);
CREATE INDEX IF NOT EXISTS idx_gmo_candles_incomplete ON gmo_candles (pair, timeframe, is_complete);


-- ──────────────────────────────────────────────────
-- 8. gmo_boxes
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmo_boxes (
    id SERIAL PRIMARY KEY,
    pair VARCHAR(20) NOT NULL,
    upper_bound NUMERIC(18,8) NOT NULL,
    lower_bound NUMERIC(18,8) NOT NULL,
    upper_touch_count INTEGER NOT NULL DEFAULT 0,
    lower_touch_count INTEGER NOT NULL DEFAULT 0,
    tolerance_pct NUMERIC(5,3) NOT NULL DEFAULT 0.500,
    basis_timeframe VARCHAR(5) NOT NULL DEFAULT '4h',
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    invalidation_reason VARCHAR(50),
    detected_from_candle_count INTEGER,
    detected_at_candle_open_time TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    invalidated_at TIMESTAMPTZ,
    CONSTRAINT gmo_boxes_status_check CHECK (status IN ('active','invalidated')),
    CONSTRAINT gmo_boxes_bounds_check CHECK (upper_bound > lower_bound)
);

CREATE INDEX IF NOT EXISTS idx_gmo_boxes_pair_status ON gmo_boxes (pair, status);
CREATE INDEX IF NOT EXISTS idx_gmo_boxes_created ON gmo_boxes (pair, created_at);


-- ──────────────────────────────────────────────────
-- 9. gmo_box_positions
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmo_box_positions (
    id SERIAL PRIMARY KEY,
    pair VARCHAR(20) NOT NULL,
    box_id INTEGER REFERENCES gmo_boxes(id) ON DELETE SET NULL,
    side VARCHAR(10) NOT NULL DEFAULT 'buy',
    entry_order_id VARCHAR(40) NOT NULL,
    entry_price NUMERIC(18,8) NOT NULL,
    entry_amount NUMERIC(18,8) NOT NULL,
    entry_jpy NUMERIC(18,2),
    exit_order_id VARCHAR(40),
    exit_price NUMERIC(18,8),
    exit_amount NUMERIC(18,8),
    exit_jpy NUMERIC(18,2),
    exit_reason VARCHAR(50),
    realized_pnl_jpy NUMERIC(18,2),
    realized_pnl_pct NUMERIC(8,4),
    status VARCHAR(20) NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    CONSTRAINT gmo_box_positions_status_check CHECK (status IN ('open','closed')),
    CONSTRAINT gmo_box_positions_side_check CHECK (side IN ('buy'))
);

CREATE INDEX IF NOT EXISTS idx_gmo_box_positions_pair_status ON gmo_box_positions (pair, status);
CREATE INDEX IF NOT EXISTS idx_gmo_box_positions_box_id ON gmo_box_positions (box_id);
CREATE INDEX IF NOT EXISTS idx_gmo_box_positions_created ON gmo_box_positions (pair, created_at);


-- ──────────────────────────────────────────────────
-- 10. gmo_trend_positions
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmo_trend_positions (
    id SERIAL PRIMARY KEY,
    pair VARCHAR(20) NOT NULL,
    strategy_id INTEGER REFERENCES gmo_strategies(id) ON DELETE SET NULL,
    entry_order_id VARCHAR(40) NOT NULL,
    entry_price NUMERIC(18,8) NOT NULL,
    entry_amount NUMERIC(18,8) NOT NULL,
    entry_jpy NUMERIC(18,2),
    stop_loss_price NUMERIC(18,8),
    partial_exit_count INTEGER NOT NULL DEFAULT 0,
    partial_exit_amount NUMERIC(18,8),
    partial_exit_jpy NUMERIC(18,2),
    partial_exit_reasons VARCHAR(200),
    exit_order_id VARCHAR(40),
    exit_price NUMERIC(18,8),
    exit_amount NUMERIC(18,8),
    exit_jpy NUMERIC(18,2),
    exit_reason VARCHAR(50),
    realized_pnl_jpy NUMERIC(18,2),
    realized_pnl_pct NUMERIC(8,4),
    status VARCHAR(20) NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    CONSTRAINT gmo_trend_positions_status_check CHECK (status IN ('open','closed'))
);

CREATE INDEX IF NOT EXISTS idx_gmo_trend_positions_pair_status ON gmo_trend_positions (pair, status);
CREATE INDEX IF NOT EXISTS idx_gmo_trend_positions_strategy ON gmo_trend_positions (strategy_id);
CREATE INDEX IF NOT EXISTS idx_gmo_trend_positions_created ON gmo_trend_positions (pair, created_at);


-- ──────────────────────────────────────────────────
-- 11. gmo_cfd_positions
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmo_cfd_positions (
    id SERIAL PRIMARY KEY,
    pair VARCHAR(20) NOT NULL,
    strategy_id INTEGER REFERENCES gmo_strategies(id) ON DELETE SET NULL,
    side VARCHAR(10) NOT NULL,
    entry_order_id VARCHAR(40) NOT NULL,
    entry_price NUMERIC(18,8) NOT NULL,
    entry_size NUMERIC(18,8) NOT NULL,
    entry_collateral_jpy NUMERIC(18,2),
    stop_loss_price NUMERIC(18,8),
    exit_order_id VARCHAR(40),
    exit_price NUMERIC(18,8),
    exit_size NUMERIC(18,8),
    exit_reason VARCHAR(50),
    realized_pnl_jpy NUMERIC(18,2),
    realized_pnl_pct NUMERIC(8,4),
    status VARCHAR(20) NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    CONSTRAINT gmo_cfd_positions_status_check CHECK (status IN ('open','closed')),
    CONSTRAINT gmo_cfd_positions_side_check CHECK (side IN ('buy','sell'))
);

CREATE INDEX IF NOT EXISTS idx_gmo_cfd_positions_pair_status ON gmo_cfd_positions (pair, status);
CREATE INDEX IF NOT EXISTS idx_gmo_cfd_positions_strategy ON gmo_cfd_positions (strategy_id);
CREATE INDEX IF NOT EXISTS idx_gmo_cfd_positions_created ON gmo_cfd_positions (pair, created_at);


-- ──────────────────────────────────────────────────
-- 12. strategy_techniques に gmo_fx_trend_following 追加
-- ──────────────────────────────────────────────────
INSERT INTO strategy_techniques (code, name, description, risk_level, requires_candles, requires_box)
VALUES (
    'gmo_fx_trend_following',
    'GMO FX 추세추종',
    'GMO외환FX 시장 추세추종 전략. USD/JPY, EUR/JPY 등 FX 페어에서 4H EMA20 기반 추세 진입. 레버리지 5~15배, 증거금 유지율 감시.',
    'medium',
    true,
    false
)
ON CONFLICT (code) DO NOTHING;
