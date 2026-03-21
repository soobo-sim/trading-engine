# Trading Engine — API Catalog

> 최종 업데이트: 2026-03-21 (Phase 1-B/1-C: 성과 메트릭 + 백테스트 엔진)
> 단일 코드베이스, `EXCHANGE` 환경변수로 CK(port 8000)/BF(port 8001)/GMO FX(port 8003) 분기

---

## 엔드포인트 요약

| # | Method | Path | 태그 | 설명 |
|---|--------|------|------|------|
| 1 | GET | `/api/system/health` | System | 비즈니스 수준 헬스 체크 |
| 2 | GET | `/api/accounts/balance` | Account | 전체 잔고 |
| 3 | GET | `/api/exchange/constraints` | Trading | 거래소 제약 |
| 4 | POST | `/api/exchange/orders` | Trading | 주문 생성 |
| 5 | DELETE | `/api/exchange/orders/{order_id}` | Trading | 주문 취소 |
| 6 | GET | `/api/exchange/orders/opens` | Trading | 미체결 주문 목록 |
| 7 | GET | `/api/exchange/orders/{order_id}` | Trading | 주문 상세 |
| 8 | GET | `/api/strategies` | Strategies | 전략 목록 |
| 9 | GET | `/api/strategies/active` | Strategies | 활성 전략 목록 |
| 10 | GET | `/api/strategies/{strategy_id}` | Strategies | 전략 상세 |
| 11 | POST | `/api/strategies` | Strategies | 전략 생성 |
| 12 | PUT | `/api/strategies/{strategy_id}/activate` | Strategies | 전략 활성화 |
| 13 | PUT | `/api/strategies/{strategy_id}/archive` | Strategies | 전략 아카이브 |
| 14 | PUT | `/api/strategies/{strategy_id}/reject` | Strategies | 전략 거부 |
| 15 | GET | `/api/boxes/{pair}` | Boxes | 활성 박스 |
| 16 | GET | `/api/boxes/{pair}/history` | Boxes | 박스 이력 |
| 17 | GET | `/api/boxes/{pair}/position` | Boxes | 현재가 박스 내 위치 |
| 18 | GET | `/api/boxes/{pair}/active-position` | Boxes | 활성 포지션 (전략 타입 자동 판별) |
| 19 | GET | `/api/boxes/{pair}/positions/history` | Boxes | 포지션 이력 |
| 20 | GET | `/api/candles/{pair}/{timeframe}/rsi` | Candles | RSI 지표 |
| 21 | GET | `/api/techniques` | Techniques | 기법 목록 |
| 22 | GET | `/api/analysis/box-history` | Analysis | 박스 이력 + 포지션 성과 집계 |
| 23 | GET | `/api/analysis/trade-stats` | Analysis | 기간별 거래 통계 (승률, 기대값) |
| 24 | GET | `/api/analysis/regime` | Analysis | 시장 체제 판단 (횡보/추세) |
| 25 | GET | `/api/analysis/trend-signal` | Analysis | 추세추종 진입/청산 시그널 |
| 26 | GET | `/api/monitoring/report` | Monitoring | 사만다 15분 보고용 리포트 |
| 27 | GET | `/api/cfd/status` | CFD | CFD 실시간 상태 (포지션 + keep_rate) |
| 28 | GET | `/api/cfd/positions` | CFD | CFD 포지션 이력 |
| 29 | GET | `/api/performance` | Performance | 종합 성과 메트릭 (수익률, 샤프, 드로다운) |
| 30 | GET | `/api/performance/compare` | Performance | 백테스트 vs 실전 괴리 비교 |
| 31 | POST | `/api/backtest/run` | Backtest | 백테스트 실행 (캔들 리플레이) |
| 32 | POST | `/api/backtest/grid` | Backtest | 파라미터 그리드 서치 |

---

## 상세 명세

### 1. System

#### `GET /api/system/health`

비즈니스 수준 헬스 체크. [MONITORING.md](MONITORING.md) 참조.

**응답**: `200` (healthy) / `503` (unhealthy)

```json
{
    "healthy": true,
    "checked_at": "2026-03-17T12:00:00+00:00",
    "issues": [],
    "ws_connected": true,
    "tasks": {"trend_candle:xrp_jpy": {"alive": true, "restarts": 0, ...}},
    "active_strategies": [...],
    "position_balance": [...]
}
```

---

### 2. Account

#### `GET /api/accounts/balance`

거래소 잔고 조회 (ExchangeAdapter 경유).

```json
{
    "exchange": "coincheck",
    "currencies": {
        "jpy": {"currency": "jpy", "amount": 1000000.0, "available": 900000.0},
        "xrp": {"currency": "xrp", "amount": 150.0, "available": 150.0}
    }
}
```

---

### 3. Trading

#### `GET /api/exchange/constraints`

거래소 제약 조건 (최소 주문, 레이트 리밋).

```json
{
    "exchange": "coincheck",
    "min_order_sizes": {"xrp_jpy": 500},
    "rate_limit": {"calls": 180, "seconds": 60}
}
```

#### `POST /api/exchange/orders`

주문 생성. 거래소에 직접 전송.

**Body**:
```json
{
    "pair": "xrp_jpy",
    "order_type": "market_buy",
    "amount": 10000,
    "price": null,
    "reasoning": "RSI 45, EMA 양의 기울기, 추세 진입"
}
```
- `reasoning`: 최소 20자 필수 (에이전트 판단 근거)

#### `DELETE /api/exchange/orders/{order_id}?pair=xrp_jpy`

주문 취소.

#### `GET /api/exchange/orders/opens?pair=xrp_jpy`

미체결 주문 목록.

#### `GET /api/exchange/orders/{order_id}?pair=xrp_jpy`

주문 상세 조회.

---

### 4. Strategies

#### `GET /api/strategies?status=active&limit=50`

전략 목록. `status` 필터 선택적.

#### `GET /api/strategies/active`

활성 전략만 조회.

#### `GET /api/strategies/{strategy_id}`

단일 전략 상세.

#### `POST /api/strategies`

전략 생성 (status=proposed).

**Body**:
```json
{
    "name": "XRP 추세추종 v2",
    "description": "4H EMA20 기반 추세추종",
    "parameters": {
        "trading_style": "trend_following",
        "pair": "xrp_jpy",
        "basis_timeframe": "4h",
        "position_size_pct": 60
    },
    "rationale": "강한 상승 추세에서 방향에 순응하여 진입",
    "technique_code": "trend_following"
}
```

#### `PUT /api/strategies/{strategy_id}/activate`

proposed → active. 동일 pair 기존 active 전략은 자동 archive.

#### `PUT /api/strategies/{strategy_id}/archive`

active|proposed → archived.

#### `PUT /api/strategies/{strategy_id}/reject`

proposed → rejected.

**Body**:
```json
{"rejection_reason": "백테스트 결과 불충분"}
```

---

### 5. Boxes

#### `GET /api/boxes/{pair}`

활성 박스 조회. 없으면 `{"box": null}`.

#### `GET /api/boxes/{pair}/history?limit=10`

박스 이력 (active + invalidated).

#### `GET /api/boxes/{pair}/position`

현재가(ticker)의 박스 내 위치.

```json
{
    "pair": "xrp_jpy",
    "price": 95.5,
    "position": "near_lower",
    "box": {"upper_bound": 110.0, "lower_bound": 90.0, ...}
}
```

`position` 값: `near_lower` | `near_upper` | `middle` | `outside` | `no_box`

#### `GET /api/boxes/{pair}/active-position`

활성 포지션 (trading_style 자동 판별: trend_following → trend_positions, box_mean_reversion → box_positions).

#### `GET /api/boxes/{pair}/positions/history?limit=20`

포지션 이력 (closed).

---

### 6. Candles

#### `GET /api/candles/{pair}/{timeframe}/rsi?period=14`

완성 캔들 기반 Wilder RSI 계산.

```json
{
    "pair": "xrp_jpy",
    "timeframe": "1h",
    "period": 14,
    "rsi": 57.24,
    "candle_count": 15,
    "latest_candle_time": "2026-03-17T08:00:00+00:00"
}
```

---

### 7. Techniques

#### `GET /api/techniques`

기법 원형 마스터 목록 (strategy_techniques 테이블).

---

### 8. Analysis (Rachel)

레이첼(전략 분석 에이전트) 전용 읽기 엔드포인트. 모든 엔드포인트는 읽기 전용.

#### `GET /api/analysis/box-history`

박스 이력 + 각 박스 포지션 성과 + 추세추종 포지션 별도 집계.

**쿼리 파라미터**: `pair` (필수), `days` (기본 30, 1~365)

**응답**:
```json
{
    "success": true,
    "pair": "xrp_jpy",
    "days": 30,
    "boxes": [{...}],
    "trend_positions": {
        "total": 2, "valid_trades": 2, "wins": 2, "losses": 0, "unknown": 0,
        "win_rate": 100.0, "total_pnl_jpy": 7250.0,
        "exit_reason_distribution": {"trailing_stop": 1, "ema_breakdown": 1}
    },
    "summary": {
        "total_boxes": 1, "active_boxes": 1,
        "total_positions": 4, "closed_positions": 4,
        "valid_trades": 4, "wins": 3, "losses": 1, "unknown": 0,
        "win_rate": 75.0,
        "total_pnl_jpy": 8750.0,
        "exit_reason_distribution": {"near_upper_exit": 1, "trend:trailing_stop": 1, ...}
    }
}
```

#### `GET /api/analysis/trade-stats`

기간별 거래 통계 — 박스 + 추세추종 포지션 통합, 전략별 내역 포함.

**쿼리 파라미터**: `pair` (필수), `period` (daily|weekly|monthly|all, 기본 weekly)

**응답**:
```json
{
    "success": true,
    "stats": {
        "total_trades": 4, "valid_trades": 4,
        "wins": 3, "losses": 1, "unknown": 0,
        "win_rate": 75.0, "expected_value_pct": 0.0143,
        "total_pnl_jpy": 8750.0,
        "max_consecutive_losses": 1,
        "exit_reason_distribution": {...},
        "by_strategy": {
            "box_mean_reversion": {"trades": 2, "valid_trades": 2, "wins": 1, ...},
            "trend_following": {"trades": 2, "valid_trades": 2, "wins": 2, ...}
        }
    }
}
```

#### `GET /api/analysis/regime`

완성 캔들 기반 시장 체제 판단 (ranging / trending / unclear).

**쿼리 파라미터**: `pair` (필수), `timeframe` (1h|4h, 기본 4h), `lookback` (20~200, 기본 60)

#### `GET /api/analysis/trend-signal`

추세추종 진입/청산 시그널 종합 판단.

**쿼리 파라미터**: `pair` (필수), `timeframe` (1h|4h), `ema_period`, `atr_period`, `rsi_entry_low`, `rsi_entry_high`, `entry_price`

---

### 7. Monitoring

#### `GET /api/monitoring/report`

사만다 15분 보고용 — 서버가 시그널 계산 → 조건 판단 → telegram_text + memory_block 조립.
사만다는 이 응답의 `report.telegram_text`를 그대로 출력하면 된다.

**설계**: `solution-design/archive/MONITORING_REPORT_API.md`

**쿼리 파라미터**: `pair` (필수)

**응답**: `200`
```json
{
    "success": true,
    "generated_at": "2026-03-19T21:01:00+09:00",
    "report": {
        "telegram_text": "[CK] 21:01 | xrp_jpy 📉추세추종\n...",
        "memory_block": "## [21:01 JST] 🟢CK: xrp_jpy | ...\n..."
    },
    "raw": {
        "pair": "xrp_jpy",
        "trading_style": "trend_following",
        "current_price": 232.49,
        "signal": "exit_warning",
        "ema_slope_pct": -0.1358,
        "rsi14": 31.5,
        "position": null,
        "entry_conditions_met": false,
        "entry_blockers": ["EMA slope -0.14% → 양수 전환 필요", "..."]
    }
}
```

**에러 응답**: `404` (해당 pair 활성 전략 없음), `400` (미지원 trading_style), `503` (캔들 부족)

---

## 라우트 경로 호환성

현행 CK/BF 에이전트 호출 경로와 동일하게 유지:

| 기존 (CK) | 기존 (BF) | Trading Engine |
|-----------|-----------|----------------|
| `GET /api/system/health` | 동일 | 동일 |
| `GET /api/ck/accounts/balance` | `GET /api/bf/accounts/balance` | `GET /api/accounts/balance` |
| `POST /api/ck/exchange/orders` | `POST /api/bf/exchange/orders` | `POST /api/exchange/orders` |

> **참고**: 기존 CK/BF는 `/api/ck/`·`/api/bf/` prefix가 있었으나, Trading Engine은 `EXCHANGE` 환경변수로 구분하므로 prefix가 없다. 에이전트 TOOLS.md/SKILL.md 갱신 필요.

---

## 관련 문서

- [MONITORING.md](MONITORING.md) — `GET /api/system/health` 상세
- [TASK_SUPERVISOR.md](TASK_SUPERVISOR.md) — 태스크 관리

---

### 8. CFD

#### `GET /api/cfd/status`

CFD 실시간 상태 — 인메모리 포지션 + BitFlyer 증거금/keep_rate.

**Query**: `product_code` (default: `FX_BTC_JPY`)

```json
{
  "product_code": "FX_BTC_JPY",
  "is_running": true,
  "position": { "side": "buy", "entry_price": 15000000, "entry_amount": 0.01, "stop_loss_price": 14000000 },
  "collateral": { "collateral": 1000000, "open_position_pnl": 5000, "require_collateral": 75000, "keep_rate": 1333.3 },
  "task_health": {}
}
```

#### `GET /api/cfd/positions`

CFD 포지션 이력 (DB).

**Query**: `product_code` (default: `FX_BTC_JPY`), `status` (open/closed), `limit` (1-100, default: 20)

---

### 11. Performance (Phase 1-B)

#### `GET /api/performance`

종합 성과 메트릭. 추세추종 + 박스 포지션 통합 집계.

**Query**: `pair` (필수), `period` (7d|30d|90d|180d|365d|all, default: 30d), `strategy_type` (선택: trend_following|box_mean_reversion)

```json
{
  "success": true,
  "pair": "xrp_jpy",
  "period": "90d",
  "metrics": {
    "total_trades": 25, "valid_trades": 23, "wins": 14, "losses": 9, "unknown": 2,
    "win_rate": 60.9, "total_pnl_jpy": 12800.0, "total_return_pct": 12.8,
    "avg_win_pct": 3.2, "avg_loss_pct": -1.8, "expected_value_pct": 1.22,
    "sharpe_ratio": 1.1, "max_drawdown_pct": 6.3, "max_consecutive_losses": 3,
    "avg_holding_hours": 28.5,
    "monthly": [{"month": "2026-01", "trades": 8, "return_pct": 2.8, "avg_pct": 0.35}]
  },
  "by_strategy": {"trend_following": {...}, "box_mean_reversion": {...}}
}
```

#### `GET /api/performance/compare`

백테스트 vs 실전 괴리 비교. 활성(또는 최근 archived) 전략 파라미터로 동일 기간 백테스트 자동 실행.

**Query**: `pair` (필수), `period` (default: 90d), `strategy_type` (선택)

```json
{
  "success": true,
  "pair": "xrp_jpy",
  "period": "90d",
  "backtest": {"total_trades": 28, "win_rate": 60.7, "total_return_pct": 15.2, "sharpe_ratio": 1.3, "max_drawdown_pct": 5.8},
  "live": {"total_trades": 25, "win_rate": 60.9, "total_return_pct": 12.8, "sharpe_ratio": 1.1, "max_drawdown_pct": 6.3},
  "gap": {"return_gap": -2.4, "sharpe_gap": -0.2, "drawdown_gap": 0.5},
  "reliability_score": 84
}
```

---

### 12. Backtest (Phase 1-C)

#### `POST /api/backtest/run`

백테스트 실행. 실전과 동일한 `signals.py` 호출, 슬리피지/수수료 시뮬레이션 포함.

**Body**:
```json
{
  "pair": "xrp_jpy",
  "params": {"trailing_stop_atr_initial": 2.0, "entry_rsi_max": 65, ...},
  "days": 90,
  "timeframe": "4h",
  "initial_capital_jpy": 100000,
  "slippage_pct": 0.05,
  "fee_pct": 0.15
}
```

**응답**: `result` 필드에 BacktestResult (trades, win_rate, sharpe, drawdown, monthly 등)

#### `POST /api/backtest/grid`

파라미터 그리드 서치. 모든 조합으로 백테스트 실행 후 Sharpe ratio 기준 정렬.

**Body**:
```json
{
  "pair": "xrp_jpy",
  "base_params": {...},
  "param_grid": {"trailing_stop_atr_initial": [1.5, 2.0, 2.5], "entry_rsi_max": [60, 65, 70]},
  "days": 90,
  "top_n": 10
}
```

**응답**: `grid_search.results` (top_n개), `grid_search.best_params`, `grid_search.best_sharpe`
