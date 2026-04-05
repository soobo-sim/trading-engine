-- 전략 분석 시스템 — 분석 보고 + 에이전트 분석 + 반성 사이클
-- 설계서: trader-common/solution-design/STRATEGY_ANALYSIS_SYSTEM.md §2

BEGIN;

-- 1. analysis_reports — 분석 보고 헤더 (목록 화면 카드 1개 = 1행)
CREATE TABLE IF NOT EXISTS analysis_reports (
    id              SERIAL PRIMARY KEY,

    -- 보고 식별
    exchange        VARCHAR(50)  NOT NULL,               -- 'gmofx'
    currency_pair   VARCHAR(20)  NOT NULL,               -- 'USD_JPY'
    report_type     VARCHAR(20)  NOT NULL,               -- 'daily', 'weekly', 'monthly'
    reported_at     TIMESTAMP WITH TIME ZONE NOT NULL,   -- 보고 시각

    -- 차트 범위 (report_type에 따라 결정)
    chart_start     TIMESTAMP WITH TIME ZONE NOT NULL,   -- daily: -7d, weekly: -14d, monthly: -30d
    chart_end       TIMESTAMP WITH TIME ZONE NOT NULL,

    -- 전략 상태 스냅샷 (보고 시점)
    strategy_active BOOLEAN NOT NULL DEFAULT FALSE,      -- true=🟢, false=⚪
    strategy_id     INTEGER,                             -- 거래소별 전략 테이블 다름 → FK 불가, 앱 레벨 참조

    -- Rachel 최종 결정 (목록 화면 표시용 요약)
    final_decision  VARCHAR(50),                         -- 'approved', 'rejected', 'conditional', 'hold'
    final_rationale TEXT,
    next_review     TIMESTAMP WITH TIME ZONE,

    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_analysis_reports UNIQUE (exchange, currency_pair, report_type, reported_at)
);

CREATE INDEX IF NOT EXISTS idx_reports_pair_time
    ON analysis_reports (currency_pair, reported_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_exchange_type
    ON analysis_reports (exchange, report_type);

-- 2. agent_analysis — 에이전트별 분석 (보고 1건 × 에이전트 N명)
CREATE TABLE IF NOT EXISTS agent_analysis (
    id              SERIAL PRIMARY KEY,
    report_id       INTEGER NOT NULL
                        REFERENCES analysis_reports(id) ON DELETE CASCADE,
    agent_name      VARCHAR(50) NOT NULL,                -- 'alice', 'samantha', 'rachel'

    -- 요약 (목록 화면 2~3줄 표시용)
    summary         TEXT NOT NULL,

    -- 구조화된 분석 (JSONB — 프로그래밍적 접근)
    -- alice:    {"trend": "uptrend", "confidence": 85, "strategy": "trend_following",
    --            "ema_direction": "up", "rsi": 52, "entry_timing": "24H"}
    -- samantha: {"risk_level": "medium", "position_size_pct": 70, "atr_status": "stable",
    --            "kill_condition": "RSI>75", "concerns": [...]}
    -- rachel:   {"decision": "conditional", "position_pct": 70,
    --            "conditions": ["이벤트 6H전 금지"], "consensus": "포지션 축소 합의"}
    structured_data JSONB NOT NULL,

    -- 전문 (상세 화면 Markdown)
    full_text       TEXT,

    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_agent_analysis UNIQUE (report_id, agent_name)
);

CREATE INDEX IF NOT EXISTS idx_agent_analysis_report
    ON agent_analysis (report_id);
CREATE INDEX IF NOT EXISTS idx_agent_analysis_agent
    ON agent_analysis (agent_name);

COMMIT;
