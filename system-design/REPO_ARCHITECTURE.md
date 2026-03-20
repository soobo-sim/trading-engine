# Repo Architecture

> 최종 업데이트: 2026-03-21
> 목적: monorepo 전체 구조 설계 및 각 프로젝트의 역할 정의

---

## 1. 전체 구조

```
repo/
├── trading-engine/           ← 통합 자동매매 엔진 (CK:8000, BF:8001, GMO FX:8003)
│   ├── core/                 ← 순수 도메인 (전략·시그널·태스크·모니터링)
│   ├── adapters/             ← 외부 시스템 (거래소·DB)
│   ├── api/                  ← FastAPI 라우트
│   ├── tests/                ← unit + integration (207개)
│   ├── system-design/        ← API_CATALOG, MONITORING, TASK_SUPERVISOR
│   ├── main.py               ← EXCHANGE env → 어댑터 선택, lifespan DI 조립
│   ├── Dockerfile            ← multi-stage (test → production)
│   └── docker-compose.yml    ← CK + BF 두 컨테이너
│
├── coinmarket-data/          ← 마켓 데이터 수집 서비스 (port 8002, 운영 중)
│
├── trader-common/            ← 설계 문서 + 에이전트 설정 + 공유 시그널
│   ├── system-design/        ← 전략 구현 설계도 (정본)
│   ├── solution-design/      ← 전략 기획/분석 문서
│   ├── openclaw/             ← 사만다 에이전트 설정 정본
│   ├── openclaw-analyst/     ← 레이첼 에이전트 설정 정본
│   └── src/trader_common/    ← 공유 시그널 (레거시, core/strategy/signals.py로 이전됨)
│
├── openclaw/                 ← AI 에이전트 게이트웨이 OSS (읽기 전용)
│
├── coincheck-trader/         ← ARCHIVED (trading-engine으로 통합됨)
└── bitflyer-trader/          ← ARCHIVED (trading-engine으로 통합됨)
```

---

## 2. 프로젝트 간 관계

```
trading-engine (단일 코드베이스)
  ├── EXCHANGE=coincheck → CoincheckAdapter → port 8000
  ├── EXCHANGE=bitflyer  → BitFlyerAdapter  → port 8001
  └── EXCHANGE=gmofx     → GmoFxAdapter     → port 8003

사만다 (Samantha) — 매매 실행 에이전트, 양쪽 거래소 겸무
  └── 스킬: trader-common/openclaw/skills/ (trading-engine 기준)

레이첼 (Rachel) — 시장 분석 에이전트
  └── trader-common/openclaw-analyst/

coinmarket-data — 마켓 데이터 수집 (port 8002)
  └── 독립 서비스, PostgreSQL 컨테이너 관리
```

---

## 3. 설계 원칙

| 원칙 | 설명 |
|------|------|
| **Hexagonal Architecture** | core/ (순수 도메인) + adapters/ (외부) + api/ (프레젠테이션) |
| **거래소 무관성** | ExchangeAdapter Protocol — 거래소별 어댑터가 구현 |
| **환경변수 분기** | `EXCHANGE=coincheck\|bitflyer\|gmofx`로 런타임 어댑터 선택 |
| **ORM 팩토리** | `create_*_model(prefix)` — ck_/bf_ 테이블 자동 분기 |
| **TaskSupervisor** | 태스크 중첩 방지, exponential backoff 재시작, 헬스 리포트 |
| **관찰 가능성** | JSON 구조화 로깅, 비즈니스 헬스체크, 포지션-잔고 정합성 |
| **하드코딩 금지** | 전략 수치는 `strategy.parameters`에서 읽기 |

---

## 4. trading-engine 구조

```
trading-engine/
├── core/                     ← 순수 도메인 (외부 의존 없음)
│   ├── exchange/
│   │   ├── base.py           ← ExchangeAdapter Protocol (11 async 메서드)
│   │   ├── types.py          ← DTO (Ticker, Candle, Order, Balance, Position)
│   │   └── errors.py         ← 표준 예외 계층
│   ├── strategy/
│   │   ├── trend_following.py  ← 추세추종 매니저
│   │   ├── box_mean_reversion.py ← 박스권 매니저
│   │   └── signals.py        ← EMA, RSI, ATR, 다이버전스 등
│   ├── task/
│   │   └── supervisor.py     ← TaskSupervisor
│   └── monitoring/
│       └── health.py         ← HealthChecker
├── adapters/
│   ├── coincheck/
│   │   ├── client.py         ← CoincheckAdapter (REST + WS)
│   │   └── signer.py         ← HMAC-SHA256 서명
│   ├── bitflyer/
│   │   ├── client.py         ← BitFlyerAdapter (REST + WS)
│   │   └── signer.py         ← HMAC-SHA256 서명
│   ├── gmo_fx/
│   │   ├── client.py         ← GmoFxAdapter (REST + WS, FX 외환)
│   │   └── signer.py         ← HMAC-SHA256 서명 (ms timestamp)
│   └── database/
│       ├── models.py         ← ORM 팩토리 (ck_/bf_ 프리픽스)
│       └── session.py        ← AsyncSession 관리
├── api/
│   ├── dependencies.py       ← AppState + ModelRegistry DI
│   └── routes/               ← system, trading, account, strategies, boxes, candles, techniques, analysis
├── main.py                   ← lifespan DI 조립(EXCHANGE 분기) + JSON 로깅
├── Dockerfile                ← multi-stage (test→production)
└── docker-compose.yml        ← coincheck-trader(:8000) + bitflyer-trader(:8001) + gmofx-trader(:8003)
```

---

## 5. DB 구성

- **인스턴스**: 단일 PostgreSQL (`trader-postgres`, coinmarket-data docker-compose 관리)
- **coincheck**: `ck_trades`, `ck_strategies`, `ck_balance_entries`, `ck_insights`, `ck_summaries`, `ck_candles`, `ck_boxes`, `ck_box_positions`, `ck_trend_positions`
- **bitflyer**: `bf_trades`, `bf_strategies`, `bf_balance_entries`, `bf_insights`, `bf_summaries`, `bf_candles`, `bf_boxes`, `bf_trend_positions`, `bf_cfd_positions`
- **gmo_fx**: `gmo_trades`, `gmo_strategies`, `gmo_balance_entries`, `gmo_insights`, `gmo_summaries`, `gmo_trend_positions` (trading-engine), `gmo_candles` (coinmarket-data)
- **coinmarket-data**: `ck_candles` + `bf_candles` + `gmo_candles` (OHLCV 수집)
- **공유**: `strategy_techniques` — 기법 원형 마스터, ck/bf/gmo 양쪽 FK 참조
- **Enum 타입**: `strategystatus`, `ordertype`, `orderstatus`, `analysistype` (CK=UPPERCASE, BF=lowercase 별도 enum)
- **ORM**: `adapters/database/models.py` 팩토리 함수가 prefix로 테이블명 분기, `create_type=False`로 기존 enum 재사용

---

## 6. OpenClaw / 에이전트 심볼릭 링크

```
~/.openclaw/workspace/  (게이트웨이가 읽는 디렉터리)
  ├── AGENTS.md 등  → trader-common/openclaw/ (symlink)
  ├── memory/       ← 실제 디렉터리 (일별 로그 YYYY-MM-DD.md)
  └── skills/
       ├── coincheck-trader/ → trader-common/openclaw/skills/coincheck-trader/
       └── bitflyer-trader/  → trader-common/openclaw/skills/bitflyer-trader/

~/.openclaw-analyst/workspace/  (레이첼 게이트웨이)
  ├── AGENTS.md 등  → trader-common/openclaw-analyst/ (symlink)
  ├── memory/       ← 실제 디렉터리
  └── skills/
       ├── coincheck-analyst/ → trader-common/openclaw-analyst/skills/coincheck-analyst/
       └── bitflyer-analyst/  → trader-common/openclaw-analyst/skills/bitflyer-analyst/
```

symlink 재생성: `bash trader-common/.create-symlink.sh`

---

## 7. BitFlyer API 요약

| 대상 | 제한 |
|------|------|
| 동일 IP | 5분 500회 |
| Private API | 5분 500회 |
| 주문계 | 5분 300회 합산 |
| 0.1 이하 수량 주문 | 1분 100회 |

---

## 8. Docker / 배포

```bash
# 1. coinmarket-data (PostgreSQL 포함) — 반드시 먼저 기동
cd coinmarket-data && docker-compose up -d --build

# 2. trading-engine (CK + BF + GMO FX)
cd trading-engine && docker-compose up -d --build
# → coincheck-trader (:8000) + bitflyer-trader (:8001) + gmofx-trader (:8003)
```

- Multi-stage Dockerfile: test stage (pytest 207개) → production stage
- `EXCHANGE` 환경변수로 어댑터 선택 (coincheck / bitflyer / gmofx)
- `.env`에 API 키·시크릿·URL 일괄 관리
- 외부 네트워크 `trader-network` (coinmarket-data가 생성)
- JSON 구조화 로깅 (exchange 필드 포함, Docker logs에서 jq로 파싱 가능)

---

## 9. 변경 이력

| 날짜 | 변경 내용 |
|------|-----------|
| 2026-03-07 | 초안 작성 |
| 2026-03-10 | coinmarket-data 프로젝트 추가 |
| 2026-03-14 | 테이블명 ck_ 프리픽스 반영, 내용 정리 후 system-design/ 이동 |
| 2026-03-15 | trader-common 공통 모듈 이전 완료 |
| 2026-03-19 | **trading-engine 통합 아키텍처 반영** — coincheck-trader/bitflyer-trader 아카이브, Hexagonal Architecture, Docker 배포, JSON 로깅 |
