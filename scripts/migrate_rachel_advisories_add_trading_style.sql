-- rachel_advisories 테이블: trading_style 컬럼 추가
-- 듀얼 매니저(TrendFollowing / BoxMeanReversion) advisory 격리 목적
-- 각 매니저가 자신의 trading_style에 맞는 advisory만 조회하도록 분리.
--
-- 실행:
--   docker exec trader-postgres psql -U trader -d trader_db -f /tmp/migrate_rachel_advisories_add_trading_style.sql
--
-- Created: 2026-04-15

ALTER TABLE rachel_advisories
    ADD COLUMN IF NOT EXISTS trading_style VARCHAR(50) NOT NULL DEFAULT 'trend_following';

COMMENT ON COLUMN rachel_advisories.trading_style IS
    '이 advisory를 소비하는 전략 타입. trend_following | box_mean_reversion';

-- 기존 레코드는 모두 trend_following으로 마이그레이션 (DEFAULT 적용)
-- 신규 advisory는 POST /api/advisories 요청 시 trading_style 명시 필요

-- 인덱스: pair + exchange + trading_style + 만료 조합 조회 최적화
CREATE INDEX IF NOT EXISTS ix_rachel_advisories_style
    ON rachel_advisories (pair, exchange, trading_style, expires_at);
