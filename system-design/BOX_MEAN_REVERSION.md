# 박스권 역추세 전략 (Box Mean Reversion) — High Level Design

> 최종 업데이트: 2026-03-17 (trading-engine 통합 Phase 2 반영)
> 적용 거래소: Coincheck (CK) / BitFlyer (BF) — 단일 코드베이스, `EXCHANGE` 환경변수로 분기
> `trading_style = "box_mean_reversion"`
> 소스: `trading-engine/core/strategy/box_mean_reversion.py`

---

## 1. 전략 개요

횡보(박스권) 장에서 가격이 박스 하단에 접근하면 매수, 상단에 접근하면 매도하여 평균 회귀(mean reversion) 수익을 노리는 전략.

```
박스 상단 ─────────────  near_upper → 자동 매도 (이익실현)
              ↑  ↓
   중간 영역  (hold)
              ↑  ↓
박스 하단 ─────────────  near_lower → 자동 매수 (진입)
```

**적합한 시장**: 횡보장 / 가격 레인지가 명확한 구간
**부적합한 시장**: 강한 추세장 (→ 추세추종 전략으로 전환)

---

## 2. 아키텍처

```
┌─────────────────────────────────────────────────────┐
│                 BoxMeanReversionManager              │
│              (싱글턴 오케스트레이터)                    │
│                                                     │
│  ┌──────────────────┐   ┌────────────────────────┐  │
│  │  Task 1           │   │  Task 2                 │  │
│  │  BoxMonitor       │   │  EntryMonitor           │  │
│  │  (60초 폴링)       │   │  (WS 틱 실시간)          │  │
│  │                   │   │                         │  │
│  │ • 캔들 완성 감지    │   │ • is_price_in_box()     │  │
│  │ • validate box    │   │ • near_lower → 매수     │  │
│  │ • detect new box  │   │ • near_upper → 매도     │  │
│  └──────────────────┘   └────────────────────────┘  │
│           │                        │                 │
│           ▼                        ▼                 │
│   (감지/유효성 — 인라인)   (포지션 CRUD — 인라인)    │
└─────────────────────────────────────────────────────┘
         │                           │
         ▼                           ▼
  ┌──────────┐              ┌───────────────┐
  │ Candle   │              │ ExchangeAdapter │
  │ 데이터    │              │ Protocol        │
  └──────────┘              └───────────────┘
  DB 직접 조회            (CoincheckAdapter 또는
  (ORM 팩토리 prefix)       BitFlyerAdapter)

  TaskSupervisor
  (태스크 등록/감시/자동 재시작)
```

---

## 3. 생명주기

```
StrategyService.activate()
  └── trading_style == "box_mean_reversion"?
        └── BoxMeanReversionManager.start(pair/product_code, params)
              ├── Task 1: _box_monitor (asyncio.Task)
              └── Task 2: _entry_monitor (asyncio.Task)

StrategyService.archive() / reject()
  └── BoxMeanReversionManager.stop(pair/product_code)
        └── 두 태스크 cancel + await

서버 재시작 (lifespan startup)
  └── DB에서 active 전략 조회 → 각각 Manager.start() 호출
```

---

## 4. 핵심 로직 상세

### 4.1 Task 1 — BoxMonitor (캔들 기반 박스 감지/유효성)

**주기**: 60초마다 폴링

#### 4.1.1 박스 감지 (detect_and_create_box)

```
입력: 완성된 4H 캔들 lookback_candles개 (기본 60개)
출력: 신규 박스 (upper_bound, lower_bound) 또는 None

1. 이미 active 박스 존재? → 스킵
2. 캔들 부족 (< min_touches × 2)? → 스킵
3. 고점 클러스터 탐색:
   - 모든 캔들의 high 값을 수집
   - tolerance_pct 이내 가격끼리 클러스터링
   - min_touches 이상 반복된 가장 높은 클러스터 = upper_bound
4. 저점 클러스터 탐색:
   - 모든 캔들의 low 값을 수집
   - 동일 방식으로 lower_bound 결정
5. upper > lower 검증
6. 박스 폭 최소 기준 검증:
   min_width_pct = tolerance_pct × 2 + fee_rate_pct × 2
   (양쪽 진입/청산 구간 + 왕복 수수료 커버)
7. DB 저장 (status="active")
```

**클러스터링 알고리즘**:
- 몸통(open, close) 기준 우선, 꼬리(high, low) 보조
- `tolerance_pct` 이내 가격을 동일 클러스터로 묶음
- 클러스터 내 가격들의 중앙값을 대표값으로 사용

#### 4.1.2 박스 유효성 검사 (validate_active_box)

```
매 폴링 사이클마다 실행 (새 캔들 감지 시)

검사 항목:
1. 종가 이탈 검사:
   - close < lower_bound × (1 - tolerance%)  → "4h_close_below_lower"
   - close > upper_bound × (1 + tolerance%)  → "4h_close_above_upper"

2. 수렴 삼각형 감지:
   - 최근 캔들(최대 20개)의 고점 → 선형 회귀 기울기
   - 최근 캔들(최대 20개)의 저점 → 선형 회귀 기울기
   - 고점 기울기 < 0 AND 저점 기울기 > 0 → "converging_triangle"

무효화 시:
  - 박스 status → "invalidated"
  - 열린 포지션 있으면 → market_sell 즉시 손절
```

### 4.2 Task 2 — EntryMonitor (WS 틱 기반 진입/청산)

```
WS 채널:
  CK: {pair}-trades (예: xrp_jpy-trades)
  BF: lightning_executions_{product_code} (예: lightning_executions_XRP_JPY)

매 틱마다:
  1. is_price_in_box(price) 호출
     → "near_lower" | "near_upper" | "middle" | "outside" | None

  2. 진입 조건:
     - box_state == "near_lower"
     - 이전 상태 ≠ "near_lower" (중복 발동 방지)
     - 열린 포지션 없음
     → market_buy 실행

  3. 청산 조건:
     - box_state == "near_upper"
     - 이전 상태 ≠ "near_upper" (중복 발동 방지)
     - 열린 포지션 있음
     → market_sell 실행 (이익 실현)
```

### 4.3 가격 위치 판정 (is_price_in_box)

```
tolerance = tolerance_pct / 100

near_lower 구간: lower × (1 - tol) ≤ price ≤ lower × (1 + tol)
near_upper 구간: upper × (1 - tol) ≤ price ≤ upper × (1 + tol)
outside:         price < lower × (1 - tol)  또는  price > upper × (1 + tol)
middle:          그 외 (박스 중간)
```

---

## 5. 전략 파라미터 (strategy.parameters)

| 파라미터 | 기본값 | 설명 |
|---------|-------|------|
| `trading_style` | `"box_mean_reversion"` | 전략 유형 식별자 |
| `pair` (CK) / `product_code` (BF) | — | 거래 대상 (예: `xrp_jpy` / `XRP_JPY`) |
| `basis_timeframe` | `"4h"` | 박스 감지 및 유효성 검사 캔들 주기 |
| `box_tolerance_pct` | `0.5` | 박스 경계 허용 오차 (%) |
| `box_min_touches` | `3` | 클러스터 인정 최소 터치 횟수 |
| `box_lookback_candles` | `60` | 감지에 사용할 캔들 수 |
| `fee_rate_pct` | CK:`0.15` / BF:`0.20` | 거래소별 taker 수수료율 (%) |
| `box_min_width_pct` | (계산값) | 박스 최소 폭 (BF: 명시적 오버라이드 가능) |
| `position_size_pct` | `10.0` | JPY 가용 잔고 대비 투입 비율 (%) |
| `min_order_jpy` | `500` | 최소 주문 금액 (JPY) |
| `min_coin_size` | `0.1` (BF) | 최소 주문 수량 (코인, BF 전용) |

---

## 6. 데이터 모델 (DB)

### 박스 테이블 (`ck_boxes` / `bf_boxes`)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `id` | Serial PK | |
| `pair` / `product_code` | String | 거래 대상 |
| `upper_bound` | Decimal | 박스 상단 |
| `lower_bound` | Decimal | 박스 하단 |
| `upper_touch_count` | Integer | 상단 터치 횟수 |
| `lower_touch_count` | Integer | 하단 터치 횟수 |
| `tolerance_pct` | Decimal | 감지 시 사용된 허용 오차 |
| `basis_timeframe` | String | 캔들 주기 |
| `status` | String | `active` / `invalidated` |
| `detected_from_candle_count` | Integer | 감지에 사용된 캔들 수 |
| `detected_at_candle_open_time` | DateTime | 마지막 캔들 시각 |
| `invalidation_reason` | String | 무효화 사유 (nullable) |
| `created_at` | DateTime | |

### 포지션 테이블 (`ck_box_positions` / BF: `bf_box_positions` 미구현)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `id` | Serial PK | |
| `pair` / `product_code` | String | |
| `box_id` | FK → boxes | 소속 박스 |
| `side` | String | `"buy"` (현물 매수 only) |
| `entry_order_id` | String | 진입 주문 ID |
| `entry_price` | Decimal | 진입가 |
| `entry_amount` | Decimal | 진입 수량 |
| `entry_jpy` | Decimal | 투입 JPY |
| `exit_order_id` | String | 청산 주문 ID (nullable) |
| `exit_price` | Decimal | 청산가 (nullable) |
| `exit_amount` | Decimal | 청산 수량 (nullable) |
| `exit_jpy` | Decimal | 회수 JPY (nullable) |
| `exit_reason` | String | 청산 사유 (nullable) |
| `realized_pnl` | Decimal | 실현 손익 (자동 계산) |
| `status` | String | `open` / `closed` |
| `created_at` | DateTime | |

---

## 7. CK vs BF 차이점 (ExchangeAdapter로 추상화)

trading-engine은 `ExchangeAdapter` Protocol로 거래소 차이를 추상화한다.
`BoxMeanReversionManager`는 거래소를 알지 못하며, 어댑터에 의존한다.

| 항목 | CoincheckAdapter | BitFlyerAdapter |
|------|------------------|------------------|
| 식별자 | `pair` (소문자: `xrp_jpy`) | `product_code` (대문자: `XRP_JPY`) |
| ORM 모델 프리픽스 | `ck_` | `bf_` |
| 기본 수수료 | 0.15% | 0.20% |
| 최소 주문 수량 | N/A | 0.001 (BTC), 0.1 (XRP) |

> 상세: `adapters/coincheck/client.py`, `adapters/bitflyer/client.py`

---

## 8. 안전장치 (Safety Mechanisms)

| 장치 | 설명 |
|------|------|
| 중복 발동 방지 | `prev_position_state` 추적 — 같은 상태 재진입 시 스킵 |
| 1박스 1포지션 | `has_open_position()` 확인 후 진입 |
| 수수료 커버 검증 | 박스 폭 < `tolerance×2 + fee×2` 이면 박스 생성 거부 |
| 최소 주문 금액 | `invest_jpy < min_order_jpy` 시 스킵 |
| 박스 무효화 자동 손절 | `validate_active_box()` 이탈 감지 시 포지션 즉시 청산 |
| 수렴 삼각형 감지 | 고점↓ + 저점↑ 패턴 → 박스 무효화 (돌파 예상) |
| 태스크 헬스 체크 | `get_task_health()` — 태스크 생존 여부 모니터링 |

---

## 9. 소스 파일 맵 (trading-engine 통합)

| 파일 | 경로 |
|------|------|
| 전략 매니저 (통합) | `core/strategy/box_mean_reversion.py` |
| 거래소 어댑터 | `adapters/coincheck/client.py` / `adapters/bitflyer/client.py` |
| ORM 모델 팩토리 | `adapters/database/models.py` (`create_box_model`, `create_box_position_model`) |
| 태스크 관리 | `core/task/supervisor.py` (`TaskSupervisor`) |
| 헬스 모니터링 | `core/monitoring/health.py` (`HealthChecker`) |
| API 라우트 | `api/routes/boxes.py` |
| 엔트리포인트 | `main.py` (`EXCHANGE` 환경변수) |

> **기존 경로 (레거시)**: `coincheck-trader/app/services/box_mean_reversion_manager.py` + `box_service.py` + `box_position_service.py`

---

## 10. 관련 문서

- [추세추종 전략 설계](TREND_FOLLOWING.md) — 추세장 전환 시 사용
- [전략 일반 설계](../../trader-common/solution-design/STRATEGY_DESIGN.md) — 전략 유형 원칙
- [XRP 특화 전략](../../trader-common/solution-design/XRP_STRATEGY_DESIGN.md) — XRP 자산 특성
