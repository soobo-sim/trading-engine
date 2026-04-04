-- migrate_wake_up_pipeline.sql
-- BUG-025: 정신차리자 파이프라인 추적 필드 추가
-- Step 1: review_status에 pending_pipeline 추가
-- Step 2: 파이프라인 추적 컬럼 추가
-- Step 3: WakeUpReview bf FK 제거 → 논리 참조 (exchange + position_type)
-- Step 4: 박스 포지션에 loss_webhook_sent 컬럼 추가

BEGIN;

-- 1) review_status CHECK constraint 재생성 (pending_pipeline 추가)
ALTER TABLE wake_up_reviews DROP CONSTRAINT IF EXISTS wur_review_status_check;
ALTER TABLE wake_up_reviews ADD CONSTRAINT wur_review_status_check
  CHECK (review_status IN (
    'draft', 'pending_pipeline', 'alice_submitted',
    'samantha_approved', 'samantha_rejected', 'rachel_decided'
  ));

-- 2) 파이프라인 추적 컬럼
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS exchange VARCHAR(10);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS position_type VARCHAR(20);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS pipeline_status VARCHAR(30);
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ;
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS pipeline_started_at TIMESTAMPTZ;
ALTER TABLE wake_up_reviews ADD COLUMN IF NOT EXISTS pipeline_completed_at TIMESTAMPTZ;

-- 3) FK 제거 (bf 하드코딩 → 논리 참조)
ALTER TABLE wake_up_reviews DROP CONSTRAINT IF EXISTS wake_up_reviews_position_id_fkey;
ALTER TABLE wake_up_reviews DROP CONSTRAINT IF EXISTS wake_up_reviews_strategy_id_fkey;

-- 4) 논리 참조 인덱스
CREATE INDEX IF NOT EXISTS idx_wur_exchange_position
  ON wake_up_reviews (exchange, position_type, position_id);
CREATE INDEX IF NOT EXISTS idx_wur_pipeline_status
  ON wake_up_reviews (pipeline_status, scheduled_at)
  WHERE pipeline_status = 'pending_pipeline';

-- 5) 박스 포지션에 loss_webhook_sent 추가
ALTER TABLE bf_box_positions ADD COLUMN IF NOT EXISTS loss_webhook_sent BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE gmo_box_positions ADD COLUMN IF NOT EXISTS loss_webhook_sent BOOLEAN NOT NULL DEFAULT false;

-- 6) 기존 데이터 보정 (수동 생성된 리뷰에 exchange/position_type 설정)
UPDATE wake_up_reviews SET exchange = 'bf', position_type = 'trend' WHERE exchange IS NULL;

COMMIT;

-- 확인
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'wake_up_reviews'
  AND column_name IN ('exchange','position_type','pipeline_status','scheduled_at',
                      'pipeline_started_at','pipeline_completed_at')
ORDER BY column_name;

SELECT table_name, column_name FROM information_schema.columns
WHERE table_name IN ('bf_box_positions','gmo_box_positions')
  AND column_name = 'loss_webhook_sent';
