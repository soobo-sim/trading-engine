-- rachel_advisories 테이블 생성
-- 레이첼 OpenClaw 에이전트가 저장하는 전략 자문 (Advisory Model)
-- TRADING_MODE=rachel 일 때 candle_monitor()가 읽어 진입/청산 판단에 활용
--
-- 실행:
--   docker exec trader-postgres psql -U trader -d trader_db -f /tmp/create_rachel_advisories.sql
--
-- Created: 2026-04-11

CREATE TABLE IF NOT EXISTS rachel_advisories (
    id          SERIAL PRIMARY KEY,
    pair        VARCHAR(20)  NOT NULL,
    exchange    VARCHAR(10)  NOT NULL,

    -- 판정
    action      VARCHAR(20)  NOT NULL,   -- entry_long|entry_short|hold|exit
    confidence  FLOAT        NOT NULL,   -- 0.0 ~ 1.0
    size_pct    FLOAT,                   -- 포지션 사이즈 비율 (0.0~0.80, NULL=재량)
    stop_loss   FLOAT,
    take_profit FLOAT,

    -- 컨텍스트
    regime      VARCHAR(20),             -- trending|ranging|uncertain
    reasoning   TEXT         NOT NULL,   -- 판정 근거 요약 (20자 이상)
    risk_notes  TEXT,

    -- 에이전트 요약 (학습 루프용)
    alice_summary   TEXT,
    samantha_summary TEXT,

    -- 시간
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ  NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_rachel_advisories_pair_exchange_created
    ON rachel_advisories (pair, exchange, created_at);
