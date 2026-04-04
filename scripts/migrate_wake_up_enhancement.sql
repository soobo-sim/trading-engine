-- Wake-Up DB 강화 마이그레이션 (WAKE_UP_DB_ENHANCEMENT.md)
-- 실행: docker exec trader-postgres psql -U trader -d trader_db -f /tmp/migrate_wake_up_enhancement.sql
-- 대상 테이블: wake_up_reviews (공유 테이블, prefix 없음)

BEGIN;

-- ── Section I: 최적 파라미터 역산 ────────────────────────────────────────────
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS optimal_params JSONB;
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS optimal_pnl NUMERIC(18,2);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS optimal_pnl_pct NUMERIC(8,4);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS actual_vs_optimal_diff_pct NUMERIC(8,4);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS optimal_long_term_ev NUMERIC(8,4);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS optimal_long_term_wr NUMERIC(8,4);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS optimal_long_term_sharpe NUMERIC(8,4);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS optimal_long_term_trades INTEGER;
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS optimal_overfit_risk VARCHAR(10);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS optimal_entry_timing VARCHAR(20);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS optimal_exit_timing VARCHAR(20);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS optimal_key_diff TEXT;

-- ── Section J: 근본 원인 ──────────────────────────────────────────────────────
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS root_cause_codes TEXT[];
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS root_cause_detail TEXT;
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS decision_date DATE;
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS decision_by VARCHAR(30);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS info_gap_had TEXT;
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS info_gap_new TEXT;

-- ── Section K: 액션 아이템 ────────────────────────────────────────────────────
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS action_items JSONB;
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS prevention_checklist JSONB;
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS review_quality_score NUMERIC(4,2);

-- ── CHECK Constraints ─────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'wur_optimal_overfit_risk_check'
    ) THEN
        ALTER TABLE wake_up_reviews ADD CONSTRAINT wur_optimal_overfit_risk_check
            CHECK (optimal_overfit_risk IS NULL OR optimal_overfit_risk IN ('low','medium','high'));
    END IF;
END $$;

COMMIT;
