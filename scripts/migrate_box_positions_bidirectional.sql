-- BOX_BIDIRECTIONAL 마이그레이션: side constraint 완화 (롱→양방향)
-- 실행: docker exec trader-postgres psql -U postgres -d trader -f /tmp/migrate_box_positions_bidirectional.sql
-- 
-- 변경 내용:
--   bf_box_positions.side CHECK ('buy') → CHECK ('buy','sell')
--   gmo_box_positions.side CHECK ('buy') → CHECK ('buy','sell')
-- 기존 데이터: 전부 side='buy' (롱) — 변경 없음
-- Created: 2026-04-02

-- ──────────────────────────────────────────────────
-- bf_box_positions
-- ──────────────────────────────────────────────────
ALTER TABLE bf_box_positions
    DROP CONSTRAINT IF EXISTS bf_box_positions_side_check;

ALTER TABLE bf_box_positions
    ADD CONSTRAINT bf_box_positions_side_check CHECK (side IN ('buy', 'sell'));

-- ──────────────────────────────────────────────────
-- gmo_box_positions
-- ──────────────────────────────────────────────────
ALTER TABLE gmo_box_positions
    DROP CONSTRAINT IF EXISTS gmo_box_positions_side_check;

ALTER TABLE gmo_box_positions
    ADD CONSTRAINT gmo_box_positions_side_check CHECK (side IN ('buy', 'sell'));

-- 결과 확인
SELECT
    tc.table_name,
    cc.constraint_name,
    cc.check_clause
FROM information_schema.table_constraints tc
JOIN information_schema.check_constraints cc
    ON tc.constraint_name = cc.constraint_name
WHERE tc.table_name IN ('bf_box_positions', 'gmo_box_positions')
  AND cc.constraint_name LIKE '%side_check';
