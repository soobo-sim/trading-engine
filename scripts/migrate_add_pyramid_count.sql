-- BUG-029: pyramid_count 컬럼 추가 (수정판)
-- 실제 영향 테이블: gmoc_cfd_positions (GmoCoinTrendManager가 사용)
--                   gmoc_trend_positions, bf_trend_positions (예비)
-- 실행: psql -U trader -d trader_db -f migrate_add_pyramid_count.sql

BEGIN;

-- gmoc_cfd_positions (GmoCoinTrendManager의 실제 포지션 테이블)
ALTER TABLE gmoc_cfd_positions
    ADD COLUMN IF NOT EXISTS pyramid_count INTEGER NOT NULL DEFAULT 0;

-- gmoc_trend_positions (예비 — 이미 존재)
ALTER TABLE gmoc_trend_positions
    ADD COLUMN IF NOT EXISTS pyramid_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE gmoc_trend_positions ALTER COLUMN pyramid_count SET NOT NULL;
ALTER TABLE gmoc_trend_positions ALTER COLUMN pyramid_count SET DEFAULT 0;

-- bf_trend_positions (예비)
ALTER TABLE bf_trend_positions
    ADD COLUMN IF NOT EXISTS pyramid_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bf_trend_positions ALTER COLUMN pyramid_count SET NOT NULL;
ALTER TABLE bf_trend_positions ALTER COLUMN pyramid_count SET DEFAULT 0;

COMMIT;

-- 백필: gmoc_cfd_positions 활성 포지션 (피라미딩 #2 상태 반영)
-- 실행 전 거래소 실제 상태 재확인 필수
BEGIN;

UPDATE gmoc_cfd_positions
SET
    entry_price     = 12460755,
    entry_size      = 0.009,
    stop_loss_price = 12342973.142857,
    pyramid_count   = 2
WHERE status = 'open'
  AND pair   = 'btc_jpy';

COMMIT;

-- 확인
SELECT id, pair, status, entry_price, entry_size, stop_loss_price, pyramid_count
FROM gmoc_cfd_positions
WHERE pair = 'btc_jpy'
ORDER BY id DESC LIMIT 3;
