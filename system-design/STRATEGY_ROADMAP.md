---
status: review
author: Rachel
created_at: 2026-03-14
updated_at: 2026-03-14
---
# 전략 로드맵 — 단계적 확장 계획

> 작성일: 2026-03-14  
> 출처: 레이첼 전략 분석 (2026-03-14) 기반 정리  
> 목적: 현재 전략의 공백을 파악하고 단계적 확장 방향을 정의한다

---

## 1. 현재 2전략 커버리지 분석

### 운영 중인 전략

| 기법 코드 | 전략명 | 대응 시장 |
|-----------|--------|-----------|
| `box_mean_reversion` | 박스권 역추세 | 횡보장 |
| `trend_following` | 추세추종 | 상승 추세 |

### 커버리지 표

| 시장 상태 | 박스권 역추세 | 추세추종 | 상태 |
|-----------|--------------|----------|------|
| 횡보장 (ranging) | ✅ | ❌ | 대응 가능 |
| 상승 추세 | ❌ | ✅ | 대응 가능 |
| 하락 추세 | ❌ | ❌ (손절만) | ⚠️ 공백 |
| 급락 / 블랙스완 | ❌ | ❌ (손절만) | ⚠️ 공백 |
| 저변동성 (죽은 시장) | 비효율적 | 비효율적 | ⚠️ 공백 |

**결론**: 상승장 + 횡보장은 OK. 하락장과 급변동 구간에 취약. 전체 커버리지 약 60~70%.

---

## 2. 공백 해소를 위한 후보 전략

| 전략 | 커버 영역 | 필요 데이터 / API | 우선순위 |
|------|-----------|-------------------|----------|
| **숏 (CFD 공매도)** | 하락 추세 수익화 | BF CFD API (`FX_BTC_JPY`) | ⭐⭐⭐ |
| 그리드 트레이딩 | 횡보장 수익 극대화 | 지정가 주문 API | ⭐⭐ |
| 변동성 브레이크아웃 | 횡보→추세 전환 포착 | ATR, BB (이미 확보) | ⭐⭐ |
| DCA (적립 매수) | 장기 안정 수익 | 최소한의 API | ⭐ |

> **최우선 과제**: BF CFD 도입 — 숏 가능 = 가장 큰 약점 해소

---

## 3. BF CFD 단계적 도입 로드맵

### Phase 1 — 데이터 인프라 (✅ 2026-03-15 완료)

**목표**: FX_BTC_JPY 데이터 수집 및 CFD 관련 API 기반 구축

| 항목 | 내용 | 담당 서비스 | 상태 |
|------|------|------------|------|
| `FX_BTC_JPY` 캔들 수집 | `BF_WS_PRODUCTS`에 추가 → WS + 7일 백필 | coinmarket-data | ✅ |
| 펀딩레이트 수집 | `bf_funding_rates` 테이블 + 15분 폴러 | coinmarket-data | ✅ |
| 펀딩레이트 route | `GET /api/bf/funding-rate` / `history` | coinmarket-data | ✅ |
| SFD 스프레드 | `market-pulse` 응답 `fx_spread_pct` 추가 | coinmarket-data | ✅ |
| 증거금 상태 API | `GET /api/accounts/collateral` | bitflyer-trader | ✅ |
| CFD 포지션 조회 API | `GET /api/exchange/positions` | bitflyer-trader | ✅ |
| 펀딩레이트 pass-through | `GET /api/exchange/funding-rate` | bitflyer-trader | ✅ |

---

### Phase 2 — CFD 전략 설계 (XRP 추세추종 검증 후)

**전제 조건**: XRP 추세추종 전략이 실전에서 일정 기간 검증 완료

---

#### Phase 2A — 시뮬레이션 모드 구현

**목표**: 실제 증거금 없이 전략 승률·기대값을 장기 검증

| 항목 | 내용 |
|------|------|
| DB | `bf_sim_positions` 테이블 신규 추가 |
| 서버 | `CfdSimulationManager` 백그라운드 태스크 (실제 시장 가격 기반) |
| 전략 파라미터 | `"simulation_mode": true` 플래그로 시뮬레이션/실전 전환 |
| API | `/api/simulation/summary` — 승률·EV·낙폭 요약 |
| 레이첼 | WORKFLOW_AUTO.md에 시뮬레이션 성과 분석 STEP 추가 |

#### Phase 2B — 실전 CFD 트레이딩

**전제 조건**: 시뮬레이션 졸업 조건 충족 + 수보오빠 최종 승인

| 항목 | 내용 |
|------|------|
| 기법 코드 | `cfd_trend_following` (신규 등록) |
| 대상 자산 | `FX_BTC_JPY` (BTC 단독, XRP/ETH CFD 없음) |
| 방향 | 롱(BUY) / 숏(SELL) 양방향 |
| 레버리지 | 최대 `1.5x` (2x 풀 사용 금지) |
| 기반 전략 | 현재 `trend_following`의 숏 버전 확장 |

**시뮬레이션 졸업 조건** (레이첼 판단 → 수보오빠 최종 결정)

| 조건 | 기준 |
|------|------|
| 최소 거래 횟수 | ≥ 30회 |
| 승률 | ≥ 55% |
| 기대값(EV) | > 0% |
| 최대 연속 손실 | ≤ 4회 |
| 최대 낙폭 | < 10% |

**리스크 관리 원칙**

| 항목 | 기준 |
|------|------|
| 최대 레버리지 | `max_leverage: 1.5x` |
| 증거금 유지율 경고 | `keep_rate < 150%` → 신규 주문 차단 |
| 포지션 보유 시간 | 최소화 (스왑 비용 관리) |
| 펀딩레이트 모니터링 | 보유 비용 계산에 포함 |

**실행 아키텍처 원칙**

에이전트는 파라미터를 정하고, 서버가 파라미터대로 실행한다. 에이전트 사이클(15분/일 1회)과 무관하게 서버 백그라운드 태스크(30초~1분)가 독립적으로 동작한다.

| 역할 | 담당 | 주기 |
|------|------|------|
| 진입/청산/리스크 임계값 파라미터 결정 | 레이첼 (전략 파라미터로 저장) | 일 1회 |
| 파라미터 기준 진입 · 청산 주문 실행 | bitflyer-trader 백그라운드 태스크 | 30초~1분 |
| keep_rate 감시 + 긴급 청산 | bitflyer-trader 백그라운드 태스크 | 30초~1분 |
| 결과 리뷰 + 파라미터 조정 제안 | 사만다 / 레이첼 | 15분 / 일 1회 |

> 상세 아키텍처 → [BF_CFD_DESIGN.md § 8-5](./BF_CFD_DESIGN.md)

---

### Phase 3 — 고급 전략 (중장기)

**목표**: 헤지 및 차익거래 전략으로 시장 중립적 수익 추구

| 전략 | 설명 |
|------|------|
| 현물 BTC 헤지 + CFD 숏 | 시장 중립 전략 — 현물 롱 포지션을 CFD 숏으로 헤지 |
| 펀딩레이트 차익거래 | FX vs 현물 펀딩레이트 차이 활용 |

> Phase 3는 Phase 2 운영 경험이 충분히 쌓인 후 설계 시작.

---

## 4. 확장 전략 후보 상세 (미정, 검토 대기)

### 그리드 트레이딩

- 일정 간격으로 지정가 매수/매도 주문 격자 배치
- 횡보장에서 반복 수익 추구
- 구현 복잡도 높음, 자금 분산 필요

### 변동성 브레이크아웃 (Volatility Breakout)

- ATR 기반 밴드 브레이크아웃 포착
- 현재 BB / ATR 지표 이미 구현됨 → 진입 로직만 추가
- 횡보 → 추세 전환 구간 포착에 특화

### DCA (달러 코스트 에버리징)

- 정기 분할 매수
- 장기 보유 전제
- 자동화 단순, 단기 수익 기대 낮음

---

## 5. 우선순위 요약

```
[현재]
  XRP 추세추종 전략 실전 검증 중
        ↓
[Phase 1] FX_BTC_JPY 데이터 수집 + CFD API 기반 구축
        ↓
[Phase 2] cfd_trend_following 전략 설계 + 소액 실전 투입
        ↓
[Phase 3] 헤지 / 차익거래 고급 전략
        ↓
[별도 검토] 그리드 트레이딩, 변동성 브레이크아웃
```

| 시점 | 작업 | 상태 |
|------|------|------|
| 현재 | XRP 추세추종 검증 | 🔄 진행 중 |
| Phase 1 | FX_BTC_JPY 수집 + CFD API | ⬜ 대기 |
| Phase 2 | cfd_trend_following 전략 | ⬜ 대기 |
| Phase 2 | ETH 현물 추가 검토 | ⬜ 대기 (CFD 이후에도 늦지 않음) |
| Phase 3 | 헤지 / 펀딩레이트 차익거래 | ⬜ 대기 |

---

## 6. 관련 문서

| 문서 | 내용 |
|------|------|
| [BF_CFD_DESIGN.md](../../trader-common/solution-design/archive/BF_CFD_DESIGN.md) | BF CFD 상품 개요, API 현황, 구현 포인트 |
| [STRATEGY_DESIGN.md](./STRATEGY_DESIGN.md) | 일반 전략 유형 및 조합 원칙 |
| [TREND_FOLLOWING_DESIGN.md](../../trader-common/solution-design/archive/TREND_FOLLOWING_DESIGN.md) | 추세추종 전략 상세 설계 |
| [XRP_STRATEGY_DESIGN.md](../../trader-common/solution-design/archive/XRP_STRATEGY_DESIGN.md) | XRP 특화 전략 설계 |
| `trading-engine/system-design/BOX_MEAN_REVERSION.md` | 박스권 역추세 구현 정본 |
| `trading-engine/system-design/TREND_FOLLOWING.md` | 추세추종 구현 정본 |
