-- Migration: rachel_advisories에 hold_override_policy 컬럼 추가 (BUG-037)
--
-- hold advisory가 기술적 사유(RSI 과매수 등)인 경우, 엔진이 시그널로 자율 진입할 수 있도록
-- Rachel이 명시적 허가를 부여하는 정책 컬럼.
--
-- 값:
--   "none"             — 절대 hold 유지 (기본값, 기존 동작과 동일)
--   "signal_entry_ok"  — entry_ok/entry_sell 시그널 시 진입 허용
--
-- 적용 방법:
--   docker exec trader-postgres psql -U trader -d trader_db \
--     -f /tmp/migrate_rachel_advisories_add_hold_override.sql

ALTER TABLE rachel_advisories
  ADD COLUMN IF NOT EXISTS hold_override_policy VARCHAR(30) NOT NULL DEFAULT 'none';

COMMENT ON COLUMN rachel_advisories.hold_override_policy IS
  'hold 시 엔진 자율 진입 허용 정책. none=절대 hold | signal_entry_ok=entry_ok/entry_sell 시그널 시 진입 허용';
