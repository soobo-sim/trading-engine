# 추세추종 전략 (Trend Following) — High Level Design

> 최종 업데이트: 2026-03-17 (trading-engine 통합 Phase 2 반영)
> 적용 거래소: Coincheck (CK) / BitFlyer (BF) — 단일 코드베이스, `EXCHANGE` 환경변수로 분기
> `trading_style = "trend_following"`
> 소스: `trading-engine/core/strategy/trend_following.py`

---

## 1. 전략 개요

강한 상승 추세에서 방향에 순응하여 포지션을 잡고, **추세가 살아있는 동안 100% 포지션 유지**,
고점 감지 후 신속 전량 청산으로 수익을 확보하는 전략.

**핵심 원칙: "Let winners run"** — 조기 부분 청산 없이 추세를 끝까지 탄다.

```
               ┌── 추세 시작 (EMA20 ↑, RSI 40~65)
               │       ▼
         ═══════════ entry_ok → market_buy (자동 진입)
               │
               │   가격 상승 중...
               │   ├── 적응형 트레일링 스탑 ratchet-up
               │   │   ├── 초기/가속: 현재가 - ATR × 2.0 (넓음)
               │   │   └── 성숙/과열: 현재가 - ATR × 1.2 (RSI>75 또는 기울기<0.05%)
               │   │
               │   └── [Phase 2] EMA 기울기 3캔들 연속 하락
               │           → 스탑 타이트닝 (ATR × 1.0)
               │   └── [Phase 3] RSI + 볼륨 베어리시 다이버전스 감지 (새 캔들마다)
               │           가격↑ + RSI↓(gap≥3) → 스탑 타이트닝
               │           가격↑ + 거래량↓(15%+) → 스탑 타이트닝
               │           RSI+볼륨 동시 → 높은 신뢰도 (동일 동작, 로그 구분)
               │
               │   추세 약화/반전 감지
               │   ├── 과매수/기울기 둔화/이익목표 → 스탑 타이트닝 (1회)
               │   ├── EMA 기울기 음전환 → 전량 청산
               │   └── RSI < 40 → 전량 청산
               │
               │   하드 스탑
         ═══════════ 가격 ≤ 스탑로스 → 즉시 market_sell
               │
               └── 추세 종료
```

**적합한 시장**: 강한 방향성이 있는 시장 (BB width ≥ 6% 또는 range ≥ 10%)
**부적합한 시장**: 횡보장 (→ 박스권 역추세 전략으로 전환)

---

## 2. 아키텍처

```
┌──────────────────────────────────────────────────────┐
│                 TrendFollowingManager                 │
│              (싱글턴 오케스트레이터)                      │
│                                                      │
│  ┌───────────────────┐   ┌─────────────────────────┐ │
│  │  Task 1            │   │  Task 2                  │ │
│  │  CandleMonitor     │   │  StopLossMonitor         │ │
│  │  (60초 폴링)        │   │  (WS 틱 실시간)           │ │
│  │                    │   │                          │ │
│  │ • 시그널 계산        │   │ • 현재가 vs 스탑로스       │ │
│  │ • 진입/청산 판단     │   │ • 이탈 시 즉시 market_sell │ │
│  │ • 적응형 트레일링    │   │ • 실시간 가격 캐시        │ │
│  │ • EMA slope 이력    │   │                          │ │
│  └───────────────────┘   └─────────────────────────┘ │
│           │                         │                 │
│           ▼                         ▼                 │
│   인메모리 포지션 상태        ExchangeAdapter Protocol   │
│   {entry_price, amount,      (CoincheckAdapter 또는    │
│    stop_loss_price}           BitFlyerAdapter)          │
│           │                                           │
│           ▼                                           │
│   DB 포지션 레코드 (ORM 팩토리)                          │
│   prefix로 테이블 분기: ck_trend_positions / bf_         │
│                                                      │
│   TaskSupervisor                                      │
│   (태스크 등록/감시/자동 재시작)                           │
└──────────────────────────────────────────────────────┘
```

---

## 3. 생명주기

```
StrategyService.activate()
  └── trading_style == "trend_following"?
        └── TrendFollowingManager.start(pair/product_code, params)
              ├── _detect_existing_position() — 잔고로 기존 포지션 복원
              ├── _recover_db_position_id()   — DB 레코드 ID 복원
              ├── Task 1: _candle_monitor (asyncio.Task)
              └── Task 2: _stop_loss_monitor (asyncio.Task)

StrategyService.archive() / reject()
  └── TrendFollowingManager.stop(pair/product_code)
        └── 두 태스크 cancel + await

서버 재시작 (lifespan startup)
  └── DB에서 active 전략 조회 → 각각 Manager.start() 호출
      └── 잔고 ≥ min_coin_size이면 기존 포지션 복원 + 스탑로스 감시 재개
```

---

## 4. 핵심 로직 상세

### 4.1 시그널 계산 (_compute_trend_signal)

완성된 4H 캔들 데이터로 아래 지표를 산출:

#### 지표

| 지표 | 계산 방법 | 기본 기간 |
|------|----------|----------|
| **EMA20** | 지수 이동 평균 (k = 2/(20+1)) | 20캔들 |
| **EMA 기울기** | `(EMA_now - EMA_prev) / EMA_prev × 100` (%) | 1캔들 전 비교 |
| **ATR14** | True Range 14캔들 단순 평균 | 14캔들 |
| **RSI14** | Relative Strength Index | 14캔들 |
| **BB Width** | `4 × σ / SMA × 100` (%) — 변동성 측정 | 20캔들 |
| **Range %** | `(max_high - min_low) / first_close × 100` | 전체 lookback |

#### 시그널 결정 로직

```
if 가격 < EMA20:
    signal = "exit_warning"             ← 추세 이탈

elif 가격 > EMA20 AND EMA기울기 > 0 AND RSI 40~65 AND NOT regime_ranging:
    signal = "entry_ok"                 ← 진입 가능 (trending OR unclear 모두 허용)

elif 가격 > EMA20 AND EMA기울기 > 0 AND RSI > 65:
    signal = "wait_dip"                 ← 과매수, 눌림목 대기

elif 가격 > EMA20 AND EMA기울기 > 0 AND regime_ranging:
    signal = "wait_regime"              ← 명확한 횡보 구간, 진입 차단

else:
    signal = "no_signal"

regime_trending = (BB_width ≥ 6.0%) OR (range ≥ 10.0%)
regime_ranging  = BB_width < 3.0% AND range < 5.0%        ← 명확한 횡보, 진입 차단
# unclear = trending도 ranging도 아닌 중간 영역 → 진입 허용 (EMA+RSI 필터가 충분)
```

### 4.2 Task 1 — CandleMonitor (60초 주기)

매 사이클마다 DB에서 최신 완성 캔들 조회 → 시그널 재계산.

#### 청산 우선순위 (포지션 보유 시)

```
우선순위 1: exit_warning (가격 < EMA20)
            → 전량 청산

우선순위 2: full_exit
            조건: EMA 기울기 < 0 (음전환)
                  OR RSI < 40 (과매도 급락)
            → 전량 청산

우선순위 3: tighten_stop  [부분 청산 없음 — Let winners run]
            조건: RSI > rsi_extreme (기본 80)
                  OR 미실현 이익 > ATR × partial_exit_profit_atr
                  OR RSI > rsi_overbought (기본 75)
                  OR EMA 기울기 둔화 (< ema_slope_weak_threshold)
            → 스탑로스를 ATR × tighten_stop_atr 으로 좁힘 (1회)

[Phase 2] EMA 기울기 3캔들 연속 하락 감지 (새 캔들 도착 시 검사)
            → 스탑 타이트닝 (우선순위 3과 동일 동작)

[Phase 3] RSI + 볼륨 베어리시 다이버전스 감지 (새 캔들 도착 시 검사)
          조건: 피봇 고점A → 고점B에서 (피봇 간 거리 ≤ max_pivot_distance 캔들)
          - RSI 다이버전스:
              가격 고점B > 고점A (신고가)
            + RSI 고점B < 고점A - rsi_divergence_min_gap (에너지 소진)
          - 볼륨 다이버전스:
              가격 고점B > 고점A (신고가)
            + 거래량 고점B < 거래량 고점A × (1 - volume_divergence_min_drop)
          - 이중 (both): RSI + 볼륨 동시 충족 → 높은 신뢰도 (로그에 "높은 신뢰도" 표시)
            → 스탑 타이트닝 (볼륨/RSI/이중 모두 동일 동작 — 로그로만 구분)

우선순위 4: 적응형 트레일링 스탑 ratchet-up
            추세 상태에 따라 ATR 배수 동적 조정:
            - _stop_tightened=True  : 현재가 - ATR × tighten_stop_atr  (기본 1.0)
            - 성숙/과열 (RSI>75 OR 기울기<0.05%): 현재가 - ATR × trailing_stop_atr_mature (기본 1.2)
            - 초기/가속 (그 외)     : 현재가 - ATR × trailing_stop_atr_initial (기본 2.0)
            → 기존 스탑보다 높을 때만 갱신 (단방향 상승)
```

#### 실시간 가격 보정

```
캔들모니터는 60초마다 4H 캔들을 확인하지만,
StopLossMonitor가 캐시한 실시간 가격을 참조하여
exit_warning을 즉각 보정한다.

if 실시간가격 < EMA20:
    signal = "exit_warning" 로 오버라이드 (4H 시그널과 무관)
```

#### 진입 (포지션 없을 때)

```
if signal == "entry_ok":
    invest_jpy = 가용 JPY × position_size_pct%
    market_buy 실행
    인메모리 포지션 기록 + DB 포지션 레코드 생성
    초기 스탑로스 = 현재가 - ATR × atr_multiplier_stop
```

### 4.3 Task 2 — StopLossMonitor (WS 틱 실시간)

```
WS 채널:
  CK: {pair}-trades
  BF: lightning_executions_{product_code}

매 틱마다:
  1. 실시간 가격 캐시 갱신 (_latest_price)
  2. 포지션 있음 AND 스탑로스 설정됨?
     - 현재가 ≤ 스탑로스 → 즉시 market_sell (하드 스탑)
```

---

### 4.4 Task 3 (제거됨 — 2026-03-16)

기존 PartialExitMonitor (15분 주기, 1H RSI 기반 부분 청산) 제거.

**이유**: 레이첼 분석 — "Let winners run" 원칙. 조기 부분 청산은 강한 상승장에서
수익 기회를 크게 축소한다. RSI 80은 강한 추세에서 수주 유지될 수 있음.

RSI + 볼륨 다이버전스 기반 스탑 타이트닝은 Phase 3으로 CandleMonitor(Task 1) 내부에 구현됨.
부분 청산(포지션 분할 매도)은 여전히 하지 않는다 — 스탑만 조인 뒤 추세 이탈 시 전량 청산.

---

### 4.5 부분 청산 (_partial_close_position)

> **현재 미사용.** Phase 3은 스탑 타이트닝만 수행(포지션 유지) — 부분 매도 없음.

---

### 4.6 포지션 복원 (서버 재시작)

```
1. _detect_existing_position():
   - 거래소 잔고 조회
   - 해당 통화 amount > 0.001 이면 포지션 존재로 판단
   - entry_price는 None (불확실) → stop_loss_price도 None
   - 다음 캔들 시그널 계산 시 스탑로스 재설정

2. _recover_db_position_id():
   - DB에서 status="open" 포지션 레코드 조회
   - ID 복원 → 이후 청산 시 기존 레코드에 기록
```

---

## 5. 전략 파라미터 (strategy.parameters)

| 파라미터 | 기본값 | 설명 |
|---------|-------|------|
| `trading_style` | `"trend_following"` | 전략 유형 식별자 |
| `pair` (CK) / `product_code` (BF) | — | 거래 대상 |
| `basis_timeframe` | `"4h"` | 시그널 계산 캔들 주기 |
| `position_size_pct` | `60` | JPY 가용 잔고 대비 투입 비율 (%) |
| `atr_multiplier_stop` | `2.0` | 초기 스탑로스: 현재가 - ATR × 이 값 |
| `trailing_stop_atr_initial` | `2.0` | 적응형 트레일링 (초기/가속기): 현재가 - ATR × 이 값 |
| `trailing_stop_atr_mature` | `1.2` | 적응형 트레일링 (성숙/과열기): 현재가 - ATR × 이 값 |
| `tighten_stop_atr` | `1.0` | 타이트닝 후 스탑: 현재가 - ATR × 이 값 |
| `rsi_overbought` | `75` | 과매수 임계값 (tighten_stop 트리거) |
| `rsi_extreme` | `80` | 극단 과매수 임계값 (tighten_stop 트리거) |
| `rsi_breakdown` | `40` | 급락 (전량 청산) 임계값 |
| `ema_slope_weak_threshold` | `0.03` | EMA 기울기 둔화 임계값 (%) — tighten_stop 트리거 |
| `partial_exit_profit_atr` | `2.0` | 이익 목표 ATR 배수 — tighten_stop 트리거 (부분 청산 아님) |
| `jpy_floor` | `1000` | 최소 투입 JPY |
| `divergence_enabled` | `true` | RSI + 볼륨 다이버전스 감지 ON/OFF (Phase 3) |
| `pivot_left` | `2` | 피봇 좌측 비교 캔들 수 |
| `pivot_right` | `2` | 피봇 우측 비교 캔들 수 (확정까지 `right×4h` 지연) |
| `rsi_divergence_min_gap` | `3.0` | RSI 고점 차이 최소값 (노이즈 필터) |
| `max_pivot_distance` | `15` | 두 피봇 간 최대 캔들 거리 (4H×15=60시간) |
| `divergence_lookback` | `40` | 피봇 탐색 캔들 수 (4H×40≈7일, `_compute_signal` limit 결정에도 사용) |
| `volume_divergence_enabled` | `true` | 볼륨 다이버전스 감지 ON/OFF (divergence_enabled와 독립) |
| `volume_divergence_min_drop` | `0.15` | 거래량 최소 감소율 (0.15=15%, 노이즈 필터) |
| `ema_slope_entry_min` | `0.0` | EMA slope 진입 최소 임곗값 (%) — 현물은 음수 허용으로 조기 진입 가능 |

> **제거된 파라미터**: `trailing_stop_atr` (1.5 고정) → `trailing_stop_atr_initial/mature`로 교체.
> `partial_exit_rsi_pct`, `partial_exit_profit_pct` — 부분 청산 제거로 미사용.

---

## 6. 데이터 모델 (DB)

### 포지션 테이블 (`ck_trend_positions` / `bf_trend_positions`)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `id` | Serial PK | |
| `pair` / `product_code` | String | 거래 대상 |
| `side` | String | `"buy"` (현물 only) |
| `entry_order_id` | String | 진입 주문 ID |
| `entry_price` | Decimal | 진입가 |
| `entry_amount` | Decimal | 진입 수량 |
| `entry_jpy` | Decimal | 투입 JPY |
| `exit_order_id` | String | 청산 주문 ID (nullable) |
| `exit_price` | Decimal | 청산가 (nullable) |
| `exit_amount` | Decimal | 청산 수량 (nullable) |
| `exit_jpy` | Decimal | 회수 JPY (nullable) |
| `exit_reason` | String | 청산 사유 코드 (nullable) |
| `realized_pnl` | Decimal | 실현 손익 |
| `trailing_stop_price`| Decimal | 현재 트레일링 스탑 가격 |
| `status` | String | `open` / `closed` |
| `created_at` | DateTime | |

### 인메모리 포지션 상태

```python
_position[pair] = {
    "entry_price": float,      # 진입가
    "entry_amount": float,     # 현재 보유 수량
    "stop_loss_price": float,  # 현재 스탑로스 가격
}
# + _db_position_id[pair] = int         (DB 레코드 PK)
# + _latest_price[pair] = float         (WS 실시간 가격 캐시)
# + _stop_tightened[pair] = bool        (스탑 타이트닝 발동 여부)
# + _ema_slope_history[pair] = list     (최근 3캔들 EMA 기울기 이력, Phase 2)
# + _ema_slope_last_key[pair] = str     (마지막 처리한 캔들 open_time, Phase 2+3 중복 실행 방지)
```

---

## 7. CK vs BF 차이점 (ExchangeAdapter로 추상화)

trading-engine은 `ExchangeAdapter` Protocol로 거래소 차이를 추상화한다.
`TrendFollowingManager`는 거래소를 알지 못하며, 어댑터에 의존한다.

| 항목 | CoincheckAdapter | BitFlyerAdapter |
|------|------------------|------------------|
| 식별자 | `pair` (소문자: `xrp_jpy`) | `product_code` (대문자: `XRP_JPY`) |
| ORM 모델 프리픽스 | `ck_` | `bf_` |
| 시그니처 | HMAC-SHA256 (`nonce + url + body`) | HMAC-SHA256 (`timestamp + METHOD + path + body`) |
| 수수료 | 0.15% | 0.20% |

> 상세: `adapters/coincheck/client.py`, `adapters/bitflyer/client.py`

---

## 8. 안전장치 (Safety Mechanisms)

| 장치 | 설명 |
|------|------|
| 하드 스탑로스 | WS 틱 기반 즉시 청산 (60초 지연 없음) |
| 적응형 트레일링 스탑 ratchet-up | 추세 상태에 따라 ATR 배수 동적 조정 (초기 2.0 / 성숙 1.2) |
| 스탑 타이트닝 | 과매수/기울기 둔화/이익 목표 감지 시 스탑 간격 축소 |
| **EMA 기울기 경고** | **3캔들 연속 기울기 하락 → 스탑 타이트닝 (Phase 2)** |
| **RSI 다이버전스 경고** | **피봇 고점 가격↑+RSI↓(gap≥3) → 스탑 타이트닝 (Phase 3)** |
| **볼륨 다이버전스 경고** | **피봇 고점 가격↑+거래량↓(15%+) → 스탑 타이트닝 (Phase 3)** |
| **이중 다이버전스 경고** | **RSI+볼륨 동시 → 높은 신뢰도 스탑 타이트닝 (Phase 3)** |
| 실시간 가격 보정 | 4H 캔들 대기 없이 EMA 이탈 즉시 감지 |
| 포지션 복원 | 서버 재시작 시 잔고 기반 자동 복원 |
| dust 처리 | min_coin_size 미만 잔고는 포지션 없음으로 취급 + DB 종료 |
| 최소 투입 금액 | `invest_jpy < jpy_floor` 시 진입 스킵 |
| regime 필터 | `BB_width < 6% AND range < 10%` 시 진입 거부 |

---

## 9. 청산 사유 코드

| 코드 | 설명 |
|------|------|
| `exit_warning` | 가격 < EMA20 — 추세 이탈 |
| `full_exit_ema_slope` | EMA 기울기 음전환 — 추세 반전 |
| `full_exit_rsi_breakdown` | RSI < 40 — 급락 |
| `stop_loss` | 하드 스탑로스 (WS 실시간) |
| ~~`partial_exit_rsi_extreme`~~ | ~~RSI > 80 — 극단 과매수 부분 청산~~ (제거됨) |
| ~~`partial_exit_profit_target`~~ | ~~이익 > ATR×2 — 부분 청산~~ (제거됨) |

> 부분 청산 사유 코드는 사용하지 않는다. Phase 3(RSI + 볼륨 다이버전스)는 스탑 타이트닝만 발동 — 포지션 유지 후 추세 이탈 시 전량 청산.

---

## 10. 소스 파일 맵 (trading-engine 통합)

| 파일 | 경로 |
|------|------|
| 전략 매니저 (통합) | `core/strategy/trend_following.py` |
| 시그널 함수 | `core/strategy/signals.py` (`compute_trend_signal`) |
| 거래소 어댑터 | `adapters/coincheck/client.py` / `adapters/bitflyer/client.py` |
| ORM 모델 팩토리 | `adapters/database/models.py` (`create_trend_position_model`) |
| 태스크 관리 | `core/task/supervisor.py` (`TaskSupervisor`) |
| 헬스 모니터링 | `core/monitoring/health.py` (`HealthChecker`) |
| API 라우트 | `api/routes/` (7개 파일) |
| 엔트리포인트 | `main.py` (`EXCHANGE` 환경변수) |

> **기존 경로 (레거시)**: `coincheck-trader/app/services/trend_following_manager.py`, `bitflyer-trader/app/services/trend_following_manager.py` — Phase 3 마이그레이션 후 제거 예정.

---

## 11. 관련 문서

- [박스권 역추세 전략 설계](BOX_MEAN_REVERSION.md) — 횡보장 전환 시 사용
- [전략 일반 설계](STRATEGY_DESIGN.md) — 전략 유형 원칙
- [추세추종 설계안 (Rachel 보고서)](../../trader-common/solution-design/archive/TREND_FOLLOWING_DESIGN.md) — 초기 설계 분석
