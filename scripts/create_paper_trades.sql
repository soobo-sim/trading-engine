-- paper_trades 테이블 생성 (수동 마이그레이션)
-- 설계: trader-common/solution-design/ALPHA_FACTORS_PROPOSAL.md §15.3
--
-- 실행:
--   docker exec trader-postgres psql -U postgres -d trader -f /tmp/create_paper_trades.sql
--
-- 또는:
--   docker cp trading-engine/scripts/create_paper_trades.sql trader-postgres:/tmp/
--   docker exec trader-postgres psql -U postgres -d trader -f /tmp/create_paper_trades.sql

CREATE TABLE IF NOT EXISTS paper_trades (
    id          SERIAL PRIMARY KEY,
    strategy_id INTEGER       NOT NULL,          -- gmo_strategies.id 등 (FK 없음)
    pair        VARCHAR(20)   NOT NULL,           -- 'USD_JPY'
    direction   VARCHAR(10)   NOT NULL,           -- 'long' | 'short'
    entry_price NUMERIC(16,6),
    entry_time  TIMESTAMPTZ,
    exit_price  NUMERIC(16,6),
    exit_time   TIMESTAMPTZ,
    exit_reason VARCHAR(50),                      -- 'near_lower_exit', 'price_stop_loss', ...
    paper_pnl_pct NUMERIC(8,4),                  -- 손익률 (%). 슬리피지 미반영 (-0.5~1% 보정 필요)
    paper_pnl_jpy NUMERIC(12,2),                 -- 가상 JPY 손익
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_strategy_pair
    ON paper_trades (strategy_id, pair);

CREATE INDEX IF NOT EXISTS idx_paper_trades_entry_time
    ON paper_trades (entry_time);

COMMENT ON TABLE paper_trades IS
    'Proposed 전략 가상 매매 기록. 실제 주문 없이 진입/청산 시뮬레이션. '
    '슬리피지/체결실패 미반영 — 실전 결과에 -0.5~1% 보정 필요.';
