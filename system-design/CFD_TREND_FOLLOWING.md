# CFD 추세추종 전략 (CFD Trend Following) — High Level Design

> 최종 업데이트: 2026-03-20
> 적용 거래소: BitFlyer (BF) — FX_BTC_JPY 전용
> `trading_style = "cfd_trend_following"`
> 소스: `trading-engine/core/strategy/cfd_trend_following.py`
> 시그널: `trading-engine/core/strategy/signals.py` (현물과 공유)

---

## 1. 전략 개요

현물 추세추종(`trend_following`)과 동일한 시그널 엔진을 사용하되,
**롱 + 숏 양방향 진입**, 증거금 관리, keep_rate 감시 등 CFD 고유 로직을 추가한 전략.

**핵심 원칙: "Let winners run" + "양방향 추세 탑승"**

```
    ┌── 상승 추세 (EMA20 ↑, slope > 0, RSI 40~65)
    │       ▼
    │   entry_ok → MARKET_BUY (롱 진입, 코인 수량)
    │
    │── 하락 추세 (EMA20 ↓, slope < -0.05%, RSI 35~60)
    │       ▼
    │   entry_sell → MARKET_SELL (숏 진입, 코인 수량)
    │
    │   포지션 보유 중...
    │   ├── 적응형 트레일링 스탑
    │   │   ├── 롱: 현재가 - ATR × mult (ratchet-up)
    │   │   └── 숏: 현재가 + ATR × mult (ratchet-down)
    │   │
    │   ├── EMA 기울기 3캔들 연속 약화 → 스탑 타이트닝
    │   ├── RSI+볼륨 베어리시 다이버전스 → 스탑 타이트닝
    │   │
    │   ├── 추세 반전 감지 → 전량 청산
    │   │   ├── 롱: slope 음전환 / RSI < 40
    │   │   └── 숏: slope 양전환 / RSI > 극단 과매수
    │   │
    │   ├── exit_warning (가격 vs EMA 이탈) → 전량 청산
    │   ├── keep_rate < critical → 긴급 전량 청산
    │   └── 보유시간 초과 → 자동 청산 (스왑 비용 방지)
    │
    │   하드 스탑
    ═══════ 가격 ≤ 스탑로스 (WS 틱 기반) → 즉시 반대매매
```

---

## 2. 현물 추세추종과의 차이

| 항목 | 현물 (`trend_following`) | CFD (`cfd_trend_following`) |
|------|------------------------|---------------------------|
| 방향 | 롱 only | **롱 + 숏** |
| 주문 수량 | JPY → 어댑터가 코인 변환 | **코인 수량 직접 전달** (FX_ 분기) |
| 자금 기준 | 잔고 JPY | **여유 증거금** (collateral - require) |
| 레버리지 | 없음 | **max_leverage 제한** (기본 1.5x) |
| 리스크 감시 | 잔고 정합성 | **keep_rate 5단계 방어선** |
| 보유 제한 | 없음 | **max_holding_hours** (스왑 비용) |
| 포지션 조회 | 거래소 잔고 | **getpositions API** (실 건옥) |
| DB 테이블 | `bf_trend_positions` | **`bf_cfd_positions`** |

---

## 3. 시그널 로직

> 현물과 동일한 `compute_trend_signal()` 함수를 사용. 숏 시그널만 추가.

### 3.1 진입 시그널

#### 롱 진입 (`entry_ok`)
```
가격 > EMA20
  AND EMA 기울기 > 0
  AND RSI entry_rsi_min(40) ~ entry_rsi_max(65)
  AND NOT regime_ranging
```

#### 숏 진입 (`entry_sell`)
```
가격 < EMA20
  AND EMA 기울기 < ema_slope_short_threshold (-0.05%)
  AND RSI entry_rsi_min_short(35) ~ entry_rsi_max_short(60)
  AND NOT regime_ranging
```

> 숏 RSI 범위가 35~60인 이유: RSI < 35 = 과매도 반등 구간이므로 회피.

### 3.2 청산 시그널

`compute_exit_signal(side=...)` — side에 따라 조건 반전.

| 조건 | 롱 (side=buy) | 숏 (side=sell) | 행동 |
|------|-------------|--------------|------|
| EMA 기울기 반전 | slope < 0 | slope > 0 | **full_exit** |
| RSI 붕괴 | RSI < breakdown(40) | RSI > extreme(80) | **full_exit** |
| 과열/이익목표 | RSI > extreme, profit ATR×2 달성 | RSI < breakdown 접근, profit ATR×2 달성 | **tighten_stop** |
| 기울기 둔화 | 0 ≤ slope < weak_th | -weak_th < slope ≤ 0 | **tighten_stop** |
| 가격 vs EMA | 가격 < EMA → exit_warning | 가격 > EMA → exit_warning | **전량 청산** |

### 3.3 레짐 판별 (현물과 동일)

```
regime_trending = BB_width ≥ 6.0% OR range ≥ 10.0%
regime_ranging  = BB_width < 3.0% AND range < 5.0%   ← 진입 차단
unclear         = 그 외                                ← 진입 허용
```

---

## 4. 증거금 기반 주문

### 4.1 포지션 사이징

```python
available = collateral - require_collateral
invest_jpy = available × position_size_pct / 100
coin_size = invest_jpy / current_price

# 레버리지 제한
effective_leverage = (coin_size × price) / collateral
if effective_leverage > max_leverage:
    coin_size = collateral × max_leverage / price
```

### 4.2 주문 실행

**진입/청산 모두 코인 수량을 직접 전달.**

BF 어댑터 `place_order()`는 MARKET_BUY 시 JPY→코인 변환을 하지만,
FX_ 상품(`product_code.startswith("FX_")`)은 **변환을 건너뛤다.**
→ CFD 매니저는 항상 `amount=coin_size`를 전달.

```python
# 진입
order = adapter.place_order(order_type=MARKET_BUY/SELL, pair="FX_BTC_JPY", amount=coin_size)

# 청산 (반대매매)
order = adapter.place_order(order_type=MARKET_SELL/BUY, pair="FX_BTC_JPY", amount=close_size)
```

---

## 5. keep_rate 5단계 방어선

| 방어선 | 조건 | 행동 |
|--------|------|------|
| 1차 | keep_rate < keep_rate_warn | 신규 진입 차단 |
| 2차 | EMA 이탈 / RSI collapse / slope 반전 | exit_warning / full_exit 청산 |
| 3차 | 하드 스탑로스 (ATR × atr_multiplier_stop) | WS 틱 기반 즉시 반대매매 |
| 4차 | keep_rate < keep_rate_critical | 긴급 전량 청산 |
| 5차 (BF) | keep_rate < 50% | BF 자동 로스컷 (최후 방어) |

> keep_rate = collateral / require_collateral. 포지션 없으면 999.0.

---

## 6. 적응형 트레일링 스탑

현물과 동일한 `compute_adaptive_trailing_mult()` + 방향 분기.

| 추세 상태 | 배수 | 롱 스탑 | 숏 스탑 |
|-----------|------|---------|---------|
| 초기/가속 | trailing_stop_atr_initial (2.0) | price - ATR × 2.0 | price + ATR × 2.0 |
| 성숙/과열 | trailing_stop_atr_mature (1.2) | price - ATR × 1.2 | price + ATR × 1.2 |
| 타이트닝 | tighten_stop_atr (1.0) | price - ATR × 1.0 | price + ATR × 1.0 |

> 롱: ratchet-up only (스탑은 올라가기만), 숏: ratchet-down only (스탑은 내려가기만).

---

## 7. 태스크 구성

product_code당 2개 asyncio 태스크 (TaskSupervisor 관리):

| 태스크 | 주기 | 역할 |
|--------|------|------|
| CandleMonitor | 60초 폴링 | 시그널 계산 → 진입/청산/트레일링, keep_rate 체크, 보유시간 체크, 포지션 정합성(5분마다) |
| StopLossMonitor | WS 실시간 | 틱 기반 하드 스탑, keep_rate 긴급 감시 |

---

## 8. 청산 우선순위 (CandleMonitor)

1. **keep_rate < critical** → 긴급 전량 청산
2. **보유시간 초과** → 자동 청산 (스왑 비용)
3. **exit_warning** (가격 vs EMA 이탈) → 전량 청산
4. **full_exit** (slope 반전 / RSI 붕괴) → 전량 청산
5. **tighten_stop** (과열 / 이익목표) → 스탑 조임 (1회)
6. **적응형 트레일링** → ratchet-up/down

---

## 9. 파라미터 레퍼런스

> 모든 값은 `strategy.parameters`에서 읽는다. 하드코딩 금지.

| 파라미터 | 기본값 | 설명 |
|---------|--------|------|
| `pair` | `FX_BTC_JPY` | 거래 상품 |
| `basis_timeframe` | `4h` | 캔들 타임프레임 |
| `ema_period` | 20 | EMA 기간 |
| `position_size_pct` | 30 | 여유 증거금 대비 투입 비율 (%) |
| `max_leverage` | 1.5 | 최대 레버리지 배수 |
| `min_coin_size` | 0.001 | 최소 코인 수량 |
| `min_order_jpy` | 500 | 최소 투입 JPY |
| `max_slippage_pct` | 0.3 | 최대 슬리피지 (%) |
| `jpy_floor` | 1000 | 증거금 최소 잔류 (JPY) |
| `keep_rate_warn` | 250 | 진입 차단 keep_rate (%) |
| `keep_rate_critical` | 120 | 긴급 청산 keep_rate (%) |
| `max_holding_hours` | 72 | 최대 보유 시간 (스왑 비용) |
| `atr_multiplier_stop` | 2.0 | 초기 스탑로스 ATR 배수 |
| `trailing_stop_atr_initial` | 2.0 | 트레일링 초기 ATR 배수 |
| `trailing_stop_atr_mature` | 1.2 | 트레일링 성숙 ATR 배수 |
| `tighten_stop_atr` | 1.0 | 타이트닝 ATR 배수 |
| `entry_rsi_min` | 40.0 | 롱 RSI 하한 |
| `entry_rsi_max` | 65.0 | 롱 RSI 상한 |
| `entry_rsi_min_short` | 35.0 | 숏 RSI 하한 |
| `entry_rsi_max_short` | 60.0 | 숏 RSI 상한 |
| `ema_slope_short_threshold` | -0.05 | 숏 진입 EMA 기울기 임계값 (%) |
| `rsi_overbought` | 75 | RSI 과매수 임계값 |
| `rsi_extreme` | 80 | RSI 극단 임계값 |
| `rsi_breakdown` | 40 | RSI 붕괴 임계값 |
| `ema_slope_weak_threshold` | 0.05 | 기울기 둔화 임계값 (%) |
| `divergence_enabled` | true | 다이버전스 감지 활성화 |
| `window_sec` | 120 | 슬리피지 체크 윈도우 (초) |

---

## 10. DB 스키마

테이블: `bf_cfd_positions`

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | Integer PK | |
| product_code | String | `FX_BTC_JPY` |
| side | String | `buy` / `sell` |
| status | String | `open` / `closed` |
| entry_price | Numeric | 진입가 |
| exit_price | Numeric | 청산가 (nullable) |
| size | Numeric | 코인 수량 |
| collateral_jpy | Numeric | 투입 증거금 (JPY) |
| stop_loss_price | Numeric | 스탑로스 가격 |
| realized_pnl_jpy | Numeric | 실현 손익 |
| exit_reason | String | 청산 사유 |
| strategy_id | Integer | FK → bf_strategies |
| order_id / exit_order_id | String(40) | BF 주문 ID |
| opened_at / closed_at | DateTime | 시각 |

> P&L 계산: 롱 = (청산가 - 진입가) × 수량, 숏 = (진입가 - 청산가) × 수량

---

## 11. 스탑로스 안전장치

- **쿨다운**: 청산 5회 연속 실패 → 60초 백오프
- **dust 감지**: 청산 후 잔량 < min_coin_size → 포지션 정리 + DB 기록
- **포지션 정합성**: 5분마다 `getpositions` 실잔고 ↔ 인메모리 비교, 1% 초과 시 갱신
