-- 거래소 역지정주문 SL 영속화 마이그레이션
-- DASHBOARD_EXCHANGE_SL_DISPLAY.md Phase 구현
-- 실행: psql -U trader -d trader_db -f scripts/migrate_exchange_sl.sql

-- GMO FX 박스 포지션 테이블
ALTER TABLE gmo_box_positions
  ADD COLUMN IF NOT EXISTS exchange_sl_order_id VARCHAR(40) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS exchange_sl_price NUMERIC(20,6) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS exchange_sl_status VARCHAR(20) DEFAULT NULL;

-- BF 박스 포지션 테이블 (ORM 정합 — 현물러 거래소 SL 미사용이지만 구조 통일)
ALTER TABLE bf_box_positions
  ADD COLUMN IF NOT EXISTS exchange_sl_order_id VARCHAR(40) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS exchange_sl_price NUMERIC(20,6) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS exchange_sl_status VARCHAR(20) DEFAULT NULL;
