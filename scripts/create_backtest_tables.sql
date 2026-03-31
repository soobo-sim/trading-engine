-- Result Store: 백테스트 실행 이력 + WF 윈도우 + 그리드서치 상위 결과
-- 설계서: trader-common/solution-design/BACKTEST_MODULE_DESIGN.md §3.4
--
-- 공유 테이블 (프리픽스 없음) — 모든 거래소/페어 공통

-- 1. 백테스트 실행 이력
CREATE TABLE IF NOT EXISTS backtest_runs (
    id              SERIAL PRIMARY KEY,
    pair            VARCHAR(20)  NOT NULL,
    strategy_type   VARCHAR(50)  NOT NULL,    -- trend_following / box_mean_reversion
    run_type        VARCHAR(20)  NOT NULL,    -- single / grid / walk_forward
    parameters      JSONB        NOT NULL,
    result          JSONB        NOT NULL,    -- 성과 요약 (trades, return_pct, sharpe 등)
    candle_range_from TIMESTAMP WITH TIME ZONE,
    candle_range_to   TIMESTAMP WITH TIME ZONE,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_pair_type
    ON backtest_runs (pair, strategy_type, run_type);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_created
    ON backtest_runs (created_at DESC);

-- 2. WF 윈도우별 상세
CREATE TABLE IF NOT EXISTS wf_windows (
    id              SERIAL PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    window_index    INTEGER NOT NULL,
    is_start        DATE,
    is_end          DATE,
    oos_start       DATE,
    oos_end         DATE,
    is_sharpe       FLOAT,
    oos_sharpe      FLOAT,
    is_return_pct   FLOAT,
    oos_return_pct  FLOAT,
    trades          INTEGER,
    win_rate        FLOAT,
    mdd             FLOAT
);

CREATE INDEX IF NOT EXISTS idx_wf_windows_run_id
    ON wf_windows (run_id, window_index);

-- 3. 그리드서치 상위 결과
CREATE TABLE IF NOT EXISTS grid_results (
    id              SERIAL PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    rank            INTEGER NOT NULL,
    parameters      JSONB   NOT NULL,
    sharpe          FLOAT,
    return_pct      FLOAT,
    trades          INTEGER,
    win_rate        FLOAT,
    mdd             FLOAT
);

CREATE INDEX IF NOT EXISTS idx_grid_results_run_id
    ON grid_results (run_id, rank);
