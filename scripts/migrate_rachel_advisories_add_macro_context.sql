-- Migration: rachel_advisories에 macro_context 컬럼 추가
-- AI 판단 추적·학습용 매크로 컨텍스트 저장
--
-- 적용:
--   docker cp trading-engine/scripts/migrate_rachel_advisories_add_macro_context.sql \
--     trader-postgres:/tmp/
--   docker exec trader-postgres psql -U trader -d trader_db \
--     -f /tmp/migrate_rachel_advisories_add_macro_context.sql

ALTER TABLE rachel_advisories
ADD COLUMN IF NOT EXISTS macro_context JSONB DEFAULT NULL;

COMMENT ON COLUMN rachel_advisories.macro_context IS
'AI 판단 추적·학습용 매크로 컨텍스트. 구조: {raw: {fng, news_avg, vix, dxy}, interpretation: str, impact_direction: str, impact_notes: str}';
