-- rachel_advisories 테이블: adjustments 컬럼 추가
-- adjust_risk 액션 지원 (SL/TP 동적 재조정)
--
-- 실행:
--   docker exec trader-postgres psql -U trader -d trader_db -f /tmp/migrate_rachel_advisories_add_adjustments.sql
--
-- Created: 2026-04-11

ALTER TABLE rachel_advisories
    ADD COLUMN IF NOT EXISTS adjustments JSONB;

COMMENT ON COLUMN rachel_advisories.adjustments IS
    'adjust_risk 액션 전용. {stop_loss_pct, take_profit_ratio, trailing_atr_multiplier, force_exit}';
