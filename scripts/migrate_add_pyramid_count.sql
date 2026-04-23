-- BUG-029: pyramid_count 컬럼 추가
-- 대상: gmoc_trend_positions, bf_trend_positions
-- 실행: psql -U trader -d trader_db -f migrate_add_pyramid_count.sql

BEGIN;

-- gmoc_trend_positions
ALTER TABLE gmoc_trend_positions
    ADD COLUMN IF NOT EXISTS pyramid_count INTEGER NOT NULL DEFAULT 0;

-- bf_trend_positions
ALTER TABLE bf_trend_positions
    ADD COLUMN IF NOT EXISTS pyramid_count INTEGER NOT NULL DEFAULT 0;

COMMIT;

-- 백필: 현재 활성 gmoc BTC/JPY 포지션 (피라미딩 #2 상태 반영)
-- entry_price/amount/sl/pyramid_count 모두 실제 거래소 상태로 갱신
BEGIN;

UPDATE gmoc_trend_positions
SET
    entry_price    = 12460755,
    entry_amount   = 0.009,
    stop_loss_price = 12342973.142857,
    pyramid_count  = 2
WHERE status = 'open'
  AND pair   = 'btc_jpy';

COMMIT;

-- 확인
SELECT id, pair, status, entry_price, entry_amount, stop_loss_price, pyramid_count
FROM gmoc_trend_positions
WHERE status = 'open';
