# Trading Dashboard — 구현 정본

> 최종 갱신: 2026-03-21 | Phase 0 구현 완료

---

## 아키텍처

```
Browser (Tailscale VPN)
   │
   ├── :3000  dashboard-web (Nginx + React SPA, HTTPS self-signed)
   │    │
   │    └── /bff/*  → proxy_pass → dashboard-bff:8010
   │
   └── :8010  dashboard-bff (FastAPI, 127.0.0.1 bind)
        │
        ├── :8000  coincheck-trader   (127.0.0.1 bind)
        ├── :8001  bitflyer-trader    (127.0.0.1 bind)
        └── :8002  coinmarket-data    (127.0.0.1 bind)
```

## 기술 스택

| 레이어 | 선택 |
|--------|------|
| 프론트엔드 | React + Vite + TypeScript |
| CSS | Tailwind CSS v4 |
| UI | shadcn/ui (수동 구성) + Lucide Icons |
| 차트 | TradingView Lightweight Charts (Phase 1) |
| BFF | FastAPI (Python 3.12) port 8010 |
| HTTP Client | httpx.AsyncClient (CK/BF/coinmarket 병렬 호출) |
| DB | 기존 PostgreSQL 공유 (trader_db) |
| ORM | SQLAlchemy 2.0 (asyncpg) |
| 인증 | JWT httpOnly 쿠키, bcrypt, 24시간 만료 |
| 배포 | Docker Compose |
| HTTPS | 자체 서명 인증서 (macmini.tailc639c8.ts.net) |

## 포트 매핑

| 서비스 | 포트 | Bind |
|--------|------|------|
| dashboard-web | 3000 | 0.0.0.0 (HTTPS) |
| dashboard-bff | 8010 | 127.0.0.1 |
| coincheck-trader | 8000 | 127.0.0.1 |
| bitflyer-trader | 8001 | 127.0.0.1 |
| coinmarket-data | 8002 | 127.0.0.1 |

## 인증

- `POST /bff/auth/login` — ID/PW → JWT httpOnly 쿠키 (Secure, SameSite=Strict)
- `POST /bff/auth/logout` — 쿠키 삭제
- `GET /bff/auth/me` — 토큰 검증
- 계정 1개: 환경변수 `DASHBOARD_USER` + `DASHBOARD_PASSWORD` (bcrypt hash)
- JWT 만료: 24시간 (설정: `JWT_EXPIRE_MINUTES=1440`)

## BFF API (Phase 0)

| 엔드포인트 | 인증 | 내부 호출 |
|-----------|------|----------|
| `GET /bff/health` | 불필요 | — |
| `POST /bff/auth/login` | 불필요 | — |
| `POST /bff/auth/logout` | 불필요 | — |
| `GET /bff/auth/me` | JWT | — |
| `GET /bff/overview` | JWT | balance×2, strategies/active×2, health×3, ticker×N(coinmarket-data), positions×N |
| `GET /bff/reports` | JWT | rachel_reports 테이블 |
| `GET /bff/reports/{id}` | JWT | rachel_reports 테이블 |
| `POST /bff/reports` | JWT | rachel_reports INSERT |
| `GET /bff/assets/history` | JWT | asset_snapshots 테이블 |

## DB 테이블

### rachel_reports

| 컬럼 | 타입 | 비고 |
|------|------|------|
| id | SERIAL PK | |
| created_at | TIMESTAMPTZ | 생성 시각 |
| report_type | VARCHAR(20) | daily_am, daily_pm, deep, ad_hoc |
| timeframe | VARCHAR(10) | short, medium, long, NULL |
| market_regime | VARCHAR(20) | trending, ranging, unclear |
| regime_confidence | NUMERIC(4,2) | 0.00~1.00 |
| sections | JSONB | 구조화된 분석 배열 |
| recommendations | JSONB | 권고 배열 |
| divergence_analysis | JSONB | 괴리 분석 |
| telegram_text | TEXT | 원문 백업 |
| telegram_message_id | BIGINT | |
| model | VARCHAR(50) | |
| data_range_start | TIMESTAMPTZ | 분석 기간 |
| data_range_end | TIMESTAMPTZ | |

인덱스: `idx_rachel_reports_created (created_at DESC)`, `idx_rachel_reports_type (report_type)`

### asset_snapshots

| 컬럼 | 타입 | 비고 |
|------|------|------|
| id | SERIAL PK | |
| snapshot_at | TIMESTAMPTZ | 스냅샷 시각 |
| ck_jpy_balance | NUMERIC(16,2) | |
| ck_coin_balances | JSONB | |
| ck_total_jpy | NUMERIC(16,2) | |
| bf_jpy_balance | NUMERIC(16,2) | |
| bf_coin_balances | JSONB | |
| bf_total_jpy | NUMERIC(16,2) | |
| bf_cfd_collateral_jpy | NUMERIC(16,2) | |
| total_jpy | NUMERIC(16,2) | CK+BF 합산 |
| unrealized_pnl_jpy | NUMERIC(12,2) | |
| source | VARCHAR(20) | cron, manual |

인덱스: `idx_asset_snapshots_at (snapshot_at)`

## 자산 스냅샷 수집

- BFF 내장 asyncio cron (4시간 주기)
- `app/services/snapshot.py → snapshot_loop()`
- lifespan에서 `asyncio.create_task(snapshot_loop())`로 시작
- 수집: balance + ticker → JPY 환산 → INSERT

## 프론트엔드 구성 (Phase 0)

| 페이지 | 경로 | 설명 |
|--------|------|------|
| Login | /login | ID/PW 로그인 |
| Dashboard | / | 총 자산, 활성 포지션, 전략, 시스템 상태 |

## CORS

```python
allow_origins = [
    "http://localhost:3000",
    "https://macmini.tailc639c8.ts.net:3000",
]
```

## 배포

```bash
# 1. 인증서 생성
./scripts/gen-cert.sh

# 2. BFF .env 설정 (DASHBOARD_PASSWORD에 bcrypt hash 설정)
# python3 -c "import bcrypt; print(bcrypt.hashpw(b'your-pw', bcrypt.gensalt()).decode())"

# 3. 빌드 & 기동
cd trading-dashboard && docker-compose up -d --build

# 기동 순서: coinmarket-data → trading-engine → trading-dashboard
```

## Phase 로드맵

| Phase | 범위 | 상태 |
|-------|------|------|
| **0** | 뼈대 + 홈 + 인증 + 자산수집 | **구현 완료** |
| 1 | 차트 (TradingView LC) + 포지션 + 레이첼 리포트 뷰어 | 대기 |
| 2 | 성과 분석 + 백테스트 UI + 괴리 대시보드 | 대기 |
| 3 | 전략 관리 + 시장 현황 + 계좌 | 대기 |
| 4 | 시스템 모니터링 + 2FA + audit log | 대기 |
