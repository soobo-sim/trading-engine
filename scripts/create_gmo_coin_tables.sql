-- GMO コイン 取引所レバレッジ テーブル作成 SQL
-- trading-engine (EXCHANGE=gmo_coin) で使用するテーブル
-- 実行: docker exec trader-postgres psql -U postgres -d trader_db -f /tmp/create_gmo_coin_tables.sql
--
-- 前提: strategy_techniques テーブルは既に存在
--
-- Created: 2026-04-12

-- ──────────────────────────────────────────────────
-- 1. Enum types (gmoc_ prefix)
-- ──────────────────────────────────────────────────
DO $$ BEGIN
    CREATE TYPE gmoc_strategystatus AS ENUM ('proposed', 'active', 'archived', 'rejected');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE gmoc_ordertype AS ENUM ('buy', 'sell', 'market_buy', 'market_sell');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE gmoc_orderstatus AS ENUM ('pending', 'open', 'completed', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE gmoc_analysistype AS ENUM ('daily', 'weekly', 'trade_specific', 'pattern');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- ──────────────────────────────────────────────────
-- 2. gmoc_strategies
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmoc_strategies (
    id SERIAL PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    status gmoc_strategystatus NOT NULL DEFAULT 'proposed',
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

CREATE INDEX IF NOT EXISTS idx_gmoc_strategy_status ON gmoc_strategies (status);
CREATE INDEX IF NOT EXISTS idx_gmoc_strategy_status_created ON gmoc_strategies (status, created_at);
CREATE INDEX IF NOT EXISTS idx_gmoc_strategy_technique ON gmoc_strategies (technique_code);


-- ──────────────────────────────────────────────────
-- 3. gmoc_trades
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmoc_trades (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(40) UNIQUE NOT NULL,
    pair VARCHAR(20) NOT NULL,
    order_type gmoc_ordertype NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    price DOUBLE PRECISION,
    executed_price DOUBLE PRECISION,
    executed_amount DOUBLE PRECISION DEFAULT 0.0,
    status gmoc_orderstatus DEFAULT 'pending',
    reasoning TEXT NOT NULL,
    market_pulse JSONB,
    trading_pattern VARCHAR(20),
    strategy_id INTEGER REFERENCES gmoc_strategies(id) ON DELETE SET NULL,
    profit_loss DOUBLE PRECISION,
    profit_loss_percentage DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    executed_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gmoc_trade_order_id ON gmoc_trades (order_id);
CREATE INDEX IF NOT EXISTS idx_gmoc_trade_pair ON gmoc_trades (pair);
CREATE INDEX IF NOT EXISTS idx_gmoc_trade_status ON gmoc_trades (status);
CREATE INDEX IF NOT EXISTS idx_gmoc_trade_created_status ON gmoc_trades (created_at, status);
CREATE INDEX IF NOT EXISTS idx_gmoc_trade_pair_created ON gmoc_trades (pair, created_at);
CREATE INDEX IF NOT EXISTS idx_gmoc_trade_strategy ON gmoc_trades (strategy_id);


-- ──────────────────────────────────────────────────
-- 4. gmoc_balance_entries
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmoc_balance_entries (
    id SERIAL PRIMARY KEY,
    currency VARCHAR(20) NOT NULL,
    available DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    reserved DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    trade_id INTEGER REFERENCES gmoc_trades(id) ON DELETE SET NULL,
    entry_source VARCHAR(20),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gmoc_balance_currency ON gmoc_balance_entries (currency);
CREATE INDEX IF NOT EXISTS idx_gmoc_balance_currency_created ON gmoc_balance_entries (currency, created_at);


-- ──────────────────────────────────────────────────
-- 5. gmoc_insights
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmoc_insights (
    id SERIAL PRIMARY KEY,
    trade_id INTEGER REFERENCES gmoc_trades(id) ON DELETE CASCADE,
    analysis_type gmoc_analysistype NOT NULL,
    content TEXT NOT NULL,
    key_lessons JSONB,
    metrics JSONB,
    confidence_score DOUBLE PRECISION,
    applied_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gmoc_insight_type ON gmoc_insights (analysis_type);
CREATE INDEX IF NOT EXISTS idx_gmoc_insight_type_created ON gmoc_insights (analysis_type, created_at);
CREATE INDEX IF NOT EXISTS idx_gmoc_insight_trade ON gmoc_insights (trade_id);


-- ──────────────────────────────────────────────────
-- 6. gmoc_summaries
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmoc_summaries (
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

CREATE INDEX IF NOT EXISTS idx_gmoc_summary_period ON gmoc_summaries (period_type);
CREATE INDEX IF NOT EXISTS idx_gmoc_summary_period_dates ON gmoc_summaries (period_type, start_date, end_date);


-- ──────────────────────────────────────────────────
-- 7. gmoc_candles
-- GMO Coin KLine은 실제 volume을 제공 (GMO FX와 달리 DEFAULT 0 없음).
-- pair: btc_jpy 등 (소문자)
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmoc_candles (
    pair VARCHAR(20) NOT NULL,
    timeframe VARCHAR(5) NOT NULL,
    open_time TIMESTAMPTZ NOT NULL,
    close_time TIMESTAMPTZ NOT NULL,
    "open" NUMERIC(18,8) NOT NULL,
    "high" NUMERIC(18,8) NOT NULL,
    "low" NUMERIC(18,8) NOT NULL,
    "close" NUMERIC(18,8) NOT NULL,
    volume NUMERIC(18,8) NOT NULL,
    tick_count INTEGER NOT NULL DEFAULT 0,
    is_complete BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (pair, timeframe, open_time)
);

CREATE INDEX IF NOT EXISTS idx_gmoc_candles_lookup ON gmoc_candles (pair, timeframe, open_time);
CREATE INDEX IF NOT EXISTS idx_gmoc_candles_incomplete ON gmoc_candles (pair, timeframe, is_complete);


-- ──────────────────────────────────────────────────
-- 8. gmoc_boxes
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmoc_boxes (
    id SERIAL PRIMARY KEY,
    pair VARCHAR(20) NOT NULL,
    strategy_id INTEGER,
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
    CONSTRAINT gmoc_boxes_status_check CHECK (status IN ('active','invalidated')),
    CONSTRAINT gmoc_boxes_bounds_check CHECK (upper_bound > lower_bound)
);

CREATE INDEX IF NOT EXISTS idx_gmoc_boxes_pair_status ON gmoc_boxes (pair, status);
CREATE INDEX IF NOT EXISTS idx_gmoc_boxes_created ON gmoc_boxes (pair, created_at);
CREATE INDEX IF NOT EXISTS idx_gmoc_boxes_strategy ON gmoc_boxes (strategy_id);


-- ──────────────────────────────────────────────────
-- 9. gmoc_box_positions
-- 암호화폐 레버리지는 롱/숏 모두 가능 → side CHECK에 'sell' 포함
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmoc_box_positions (
    id SERIAL PRIMARY KEY,
    pair VARCHAR(20) NOT NULL,
    box_id INTEGER REFERENCES gmoc_boxes(id) ON DELETE SET NULL,
    side VARCHAR(10) NOT NULL DEFAULT 'buy',
    entry_order_id VARCHAR(40) NOT NULL,
    entry_price NUMERIC(18,8) NOT NULL,
    entry_amount NUMERIC(18,8) NOT NULL,
    entry_jpy NUMERIC(18,2),
    exchange_position_id VARCHAR(40),                       -- GMO positionId (closeOrder용)
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
    -- 정신차리자 보고 자동화 (BUG-025)
    loss_webhook_sent BOOLEAN NOT NULL DEFAULT false,
    -- 거래소 역지정주문 SL 이중화
    exchange_sl_order_id VARCHAR(40),
    exchange_sl_price NUMERIC(20,6),
    exchange_sl_status VARCHAR(20),
    -- IFD-OCO 지정가 주문 추적
    ifdoco_root_order_id VARCHAR(40),
    ifdoco_status VARCHAR(20),
    tp_price NUMERIC(20,5),
    sl_price_registered NUMERIC(20,5),
    CONSTRAINT gmoc_box_positions_status_check CHECK (status IN ('open','closed')),
    CONSTRAINT gmoc_box_positions_side_check CHECK (side IN ('buy','sell'))
);

CREATE INDEX IF NOT EXISTS idx_gmoc_box_positions_pair_status ON gmoc_box_positions (pair, status);
CREATE INDEX IF NOT EXISTS idx_gmoc_box_positions_box_id ON gmoc_box_positions (box_id);
CREATE INDEX IF NOT EXISTS idx_gmoc_box_positions_created ON gmoc_box_positions (pair, created_at);

-- BUG-032: 기존 테이블에 누락된 컬럼 추가 (IF NOT EXISTS로 멱등성 보장)
ALTER TABLE gmoc_box_positions ADD COLUMN IF NOT EXISTS exchange_position_id VARCHAR(40);
ALTER TABLE gmoc_box_positions ADD COLUMN IF NOT EXISTS loss_webhook_sent BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE gmoc_box_positions ADD COLUMN IF NOT EXISTS exchange_sl_order_id VARCHAR(40);
ALTER TABLE gmoc_box_positions ADD COLUMN IF NOT EXISTS exchange_sl_price NUMERIC(20,6);
ALTER TABLE gmoc_box_positions ADD COLUMN IF NOT EXISTS exchange_sl_status VARCHAR(20);
ALTER TABLE gmoc_box_positions ADD COLUMN IF NOT EXISTS ifdoco_root_order_id VARCHAR(40);
ALTER TABLE gmoc_box_positions ADD COLUMN IF NOT EXISTS ifdoco_status VARCHAR(20);
ALTER TABLE gmoc_box_positions ADD COLUMN IF NOT EXISTS tp_price NUMERIC(20,5);
ALTER TABLE gmoc_box_positions ADD COLUMN IF NOT EXISTS sl_price_registered NUMERIC(20,5);


-- ──────────────────────────────────────────────────
-- 10. gmoc_trend_positions
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmoc_trend_positions (
    id SERIAL PRIMARY KEY,
    pair VARCHAR(20) NOT NULL,
    strategy_id INTEGER REFERENCES gmoc_strategies(id) ON DELETE SET NULL,
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
    -- 정신차리자 보고 자동화 (BUG-025)
    loss_webhook_sent BOOLEAN NOT NULL DEFAULT false,
    -- 진입 시그널 스냅샷 (Alice 사후 분석용)
    entry_rsi NUMERIC(8,4),
    entry_ema_slope NUMERIC(10,6),
    entry_atr NUMERIC(18,8),
    entry_regime VARCHAR(20),
    entry_bb_width NUMERIC(8,4),
    CONSTRAINT gmoc_trend_positions_status_check CHECK (status IN ('open','closed'))
);

CREATE INDEX IF NOT EXISTS idx_gmoc_trend_positions_pair_status ON gmoc_trend_positions (pair, status);
CREATE INDEX IF NOT EXISTS idx_gmoc_trend_positions_strategy ON gmoc_trend_positions (strategy_id);
CREATE INDEX IF NOT EXISTS idx_gmoc_trend_positions_created ON gmoc_trend_positions (pair, created_at);


-- ──────────────────────────────────────────────────
-- 11. gmoc_cfd_positions
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmoc_cfd_positions (
    id SERIAL PRIMARY KEY,
    pair VARCHAR(20) NOT NULL,
    strategy_id INTEGER REFERENCES gmoc_strategies(id) ON DELETE SET NULL,
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
    CONSTRAINT gmoc_cfd_positions_status_check CHECK (status IN ('open','closed')),
    CONSTRAINT gmoc_cfd_positions_side_check CHECK (side IN ('buy','sell'))
);

CREATE INDEX IF NOT EXISTS idx_gmoc_cfd_positions_pair_status ON gmoc_cfd_positions (pair, status);
CREATE INDEX IF NOT EXISTS idx_gmoc_cfd_positions_strategy ON gmoc_cfd_positions (strategy_id);
CREATE INDEX IF NOT EXISTS idx_gmoc_cfd_positions_created ON gmoc_cfd_positions (pair, created_at);


-- ──────────────────────────────────────────────────
-- 12. strategy_techniques に gmo_coin_cfd_trend_following 追加
-- ──────────────────────────────────────────────────
INSERT INTO strategy_techniques (code, name, description, risk_level, requires_candles, requires_box)
VALUES (
    'gmo_coin_cfd_trend_following',
    'GMO 코인 레버리지 추세추종',
    'GMO 코인 取引所レバレッジ 암호화폐 추세추종 전략. BTC_JPY/ETH_JPY 등에서 4H EMA 기반 진입. 2배 레버리지, 24/7 시장.',
    'medium',
    true,
    false
)
ON CONFLICT (code) DO NOTHING;


-- ──────────────────────────────────────────────────
-- 13. gmoc_strategy_snapshots  (P-1 동적 전략 스위칭)
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmoc_strategy_snapshots (
    id SERIAL PRIMARY KEY,
    strategy_id INTEGER NOT NULL REFERENCES gmoc_strategies(id) ON DELETE CASCADE,
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
CREATE INDEX IF NOT EXISTS idx_gmoc_snapshots_strategy_time
    ON gmoc_strategy_snapshots (strategy_id, snapshot_time DESC);
CREATE INDEX IF NOT EXISTS idx_gmoc_snapshots_pair_time
    ON gmoc_strategy_snapshots (pair, snapshot_time DESC);


-- ──────────────────────────────────────────────────
-- 14. gmoc_switch_recommendations  (P-1 동적 전략 스위칭)
-- ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmoc_switch_recommendations (
    id SERIAL PRIMARY KEY,
    trigger_type VARCHAR(30) NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL,
    current_strategy_id INTEGER REFERENCES gmoc_strategies(id) ON DELETE SET NULL,
    current_score NUMERIC(6,4),
    recommended_strategy_id INTEGER REFERENCES gmoc_strategies(id) ON DELETE SET NULL,
    recommended_score NUMERIC(6,4),
    score_ratio NUMERIC(6,4),
    confidence VARCHAR(10),
    reason TEXT,
    decision VARCHAR(10) NOT NULL DEFAULT 'pending',
    decided_at TIMESTAMPTZ,
    decided_by VARCHAR(20),
    reject_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gmoc_switch_rec_decision
    ON gmoc_switch_recommendations (decision, created_at);
CREATE INDEX IF NOT EXISTS idx_gmoc_switch_rec_created
    ON gmoc_switch_recommendations (created_at);
