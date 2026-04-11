-- AI 판단 기록 테이블에 사후 분석 컬럼 추가
-- ENABLE_POST_ANALYSIS=true 시 LLM 사후 분석 텍스트를 저장한다
-- 적용: docker exec trader-postgres psql -U postgres -d trading_db -f /tmp/migrate_ai_judgments_add_post_analysis.sql

ALTER TABLE ai_judgments
    ADD COLUMN IF NOT EXISTS post_analysis TEXT;
