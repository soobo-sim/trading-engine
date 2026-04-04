-- P-0A: boxes 테이블에 strategy_id 컬럼 + 인덱스 추가
-- active 전략 박스 = strategy_id NULL (기존 데이터 호환)
-- paper 전략 박스 = strategy_id N

ALTER TABLE gmo_boxes ADD COLUMN IF NOT EXISTS strategy_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_gmo_boxes_strategy ON gmo_boxes (strategy_id);

ALTER TABLE bf_boxes ADD COLUMN IF NOT EXISTS strategy_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_bf_boxes_strategy ON bf_boxes (strategy_id);
