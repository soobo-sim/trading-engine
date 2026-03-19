# HealthChecker — 비즈니스 수준 헬스 모니터링

> 최종 업데이트: 2026-03-17 (trading-engine Phase 2)
> 소스: `core/monitoring/health.py`

---

## 1. 개요

`HealthChecker`는 **비즈니스 로직 수준**의 헬스 체크를 수행한다.

기존 CK/BF `GET /api/system/health`는 WS 연결 + 태스크 생존만 확인했다.
HealthChecker는 이에 더해 포지션-잔고 정합성, 활성 전략 상태를 검사한다.

| 레벨 | 기존 | HealthChecker |
|------|------|---------------|
| WS 연결 | ✅ | ✅ |
| 태스크 alive | ✅ | ✅ (TaskSupervisor 경유) |
| 태스크 재시작 이력 | ❌ | ✅ |
| 포지션-잔고 정합성 | ❌ | ✅ (1% 임계치) |
| 활성 전략 상태 | ❌ | ✅ |

---

## 2. 아키텍처

```
GET /api/system/health
         │
         ▼
┌──────────────────────────────────────────────────┐
│                  HealthChecker                    │
│                                                  │
│  입력 (DI 주입)                                    │
│  ├── ExchangeAdapter   → WS 상태, 잔고 조회        │
│  ├── TaskSupervisor    → 태스크 헬스 리포트          │
│  ├── session_factory   → DB 조회 (전략, 포지션)     │
│  └── ORM 모델         → Strategy, TrendPosition,  │
│                          BoxPosition               │
│                                                  │
│  출력: HealthReport                                │
│  ├── healthy: bool      (issues 없으면 True)       │
│  ├── ws_connected: bool                           │
│  ├── tasks: Dict        (get_health())            │
│  ├── active_strategies: List[Dict]                │
│  ├── position_balance: List[Dict]                 │
│  └── issues: List[str]  (문제 설명 목록)            │
└──────────────────────────────────────────────────┘
```

---

## 3. 검사 항목

### 3.1 WS 연결 상태

```python
ws_ok = adapter.is_ws_connected()
if not ws_ok:
    issues.append("WebSocket disconnected")
```

### 3.2 태스크 헬스

```python
tasks = supervisor.get_health()
for name, info in tasks.items():
    if not info["alive"]:
        issues.append(f"Task {name}: dead")
```

- `alive=False` → 태스크 사망 → issue 추가
- `restarts > 0` → 재시작 이력 있음 (정보 레벨, issue 아님)

### 3.3 활성 전략 상태

DB에서 `status="active"` 전략을 조회하여 응답에 포함.

### 3.4 포지션-잔고 정합성 (BUG-006)

```
DB에서 open 포지션 조회 → entry_amount 확인
거래소에서 실잔고 조회 → 해당 통화 available 확인

차이율 = |entry_amount - available| / entry_amount × 100

차이율 > 1% → issue 추가 ("Position-balance mismatch: {pair}")
```

- TrendPosition (open) + BoxPosition (open) 양쪽 검사
- 포지션이 없으면 스킵

---

## 4. HealthReport 구조

```python
@dataclass
class HealthReport:
    healthy: bool           # issues가 비어있으면 True
    checked_at: str         # ISO 8601 타임스탬프
    issues: list[str]       # 발견된 문제 목록
    ws_connected: bool
    tasks: dict[str, dict]  # TaskSupervisor.get_health() 결과
    active_strategies: list[dict]
    position_balance: list[dict]
```

### 응답 예시

`200 OK` (healthy):
```json
{
    "healthy": true,
    "checked_at": "2026-03-17T12:00:00+00:00",
    "issues": [],
    "ws_connected": true,
    "tasks": {
        "trend_candle:xrp_jpy": {"alive": true, "restarts": 0, ...},
        "trend_stoploss:xrp_jpy": {"alive": true, "restarts": 0, ...}
    },
    "active_strategies": [
        {"id": 21, "name": "XRP 추세추종 v2", "trading_style": "trend_following"}
    ],
    "position_balance": [
        {"pair": "xrp_jpy", "db_amount": 100.0, "exchange_amount": 100.0, "diff_pct": 0.0}
    ]
}
```

`503 Service Unavailable` (unhealthy):
```json
{
    "healthy": false,
    "issues": ["WebSocket disconnected", "Task trend_stoploss:xrp_jpy: dead"],
    ...
}
```

---

## 5. HTTP 상태 코드

| HealthReport.healthy | HTTP Status |
|---------------------|-------------|
| `True` | `200 OK` |
| `False` | `503 Service Unavailable` |

---

## 6. 관련 문서

- [TASK_SUPERVISOR.md](TASK_SUPERVISOR.md) — TaskSupervisor (헬스 데이터 소스)
- [API_CATALOG.md](API_CATALOG.md) — `GET /api/system/health` 엔드포인트 명세
