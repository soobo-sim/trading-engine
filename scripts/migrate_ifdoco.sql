-- BOX_IFDOCO_MIGRATION: gmo_box_positions에 IFD-OCO 추적 컬럼 추가
-- BF 현물은 Phase 1 미지원 → gmo_box_positions만 적용

ALTER TABLE gmo_box_positions ADD COLUMN IF NOT EXISTS ifdoco_root_order_id VARCHAR(40);
ALTER TABLE gmo_box_positions ADD COLUMN IF NOT EXISTS ifdoco_status VARCHAR(20);
ALTER TABLE gmo_box_positions ADD COLUMN IF NOT EXISTS tp_price DECIMAL(20,5);
ALTER TABLE gmo_box_positions ADD COLUMN IF NOT EXISTS sl_price_registered DECIMAL(20,5);

-- 검증
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'gmo_box_positions'
  AND column_name IN ('ifdoco_root_order_id', 'ifdoco_status', 'tp_price', 'sl_price_registered')
ORDER BY column_name;
