# TaskSupervisor — 태스크 생명주기 관리

> 최종 업데이트: 2026-03-17 (trading-engine Phase 2)
> 소스: `core/task/supervisor.py`

---

## 1. 개요

`TaskSupervisor`는 asyncio 태스크의 **등록 · 감시 · 재시작 · 종료**를 중앙에서 관리한다.

기존 CK/BF 매니저에서 발생한 문제:

| 문제 | 원인 | TaskSupervisor 해결 |
|------|------|---------------------|
| 태스크 중복 생성 | `start()` 동시 호출 시 race condition | `asyncio.Lock` 기반 등록 — 동일 이름 중복 불가 |
| 태스크 조용한 사망 | 예외 발생 시 로그만 남고 끝남 | 예외 → 자동 재시작 (exponential backoff) |
| 재시작 로직 없음 | 수동 복구 필요 | `max_restarts` 횟수 내 자동 복구 |
| 헬스 정보 부족 | alive만 체크 | 구조화된 리포트 (restarts, last_error, started_at) |

---

## 2. 아키텍처

```
┌─────────────────────────────────────────────────────┐
│                    TaskSupervisor                     │
│                 (asyncio.Lock 보호)                    │
│                                                      │
│  _tasks: Dict[name → TaskInfo]                       │
│  ┌──────────────────────────────────────────────┐    │
│  │ TaskInfo                                      │    │
│  │  name: str        (예: "trend_candle:xrp_jpy")│    │
│  │  task: asyncio.Task                           │    │
│  │  restart_count: int                           │    │
│  │  max_restarts: int  (기본 5)                   │    │
│  │  auto_restart: bool (기본 True)                │    │
│  │  last_error: Optional[str]                    │    │
│  │  started_at: datetime                         │    │
│  └──────────────────────────────────────────────┘    │
│                                                      │
│  API                                                 │
│  ├── register(name, coro_factory, max_restarts)      │
│  ├── stop(name) / stop_group(prefix) / stop_all()   │
│  └── get_health() → {name: {alive, restarts, ...}}  │
└─────────────────────────────────────────────────────┘
```

---

## 3. 주요 API

### register()

```python
await supervisor.register(
    name="trend_candle:xrp_jpy",
    coro_factory=lambda: manager._candle_monitor("xrp_jpy"),
    max_restarts=5,
    auto_restart=True,
)
```

- `coro_factory`: 호출할 때마다 **새 coroutine**을 반환하는 callable (재시작 시 필요)
- 동일 이름이 이미 실행 중이면 **기존 태스크 중지 후 교체**
- Lock 보호로 동시 호출 안전

### stop / stop_group / stop_all

```python
await supervisor.stop("trend_candle:xrp_jpy")       # 단일 태스크
await supervisor.stop_group("xrp_jpy")               # pair 관련 모든 태스크
await supervisor.stop_all()                           # 전체 graceful shutdown
```

### get_health()

```python
{
    "trend_candle:xrp_jpy": {
        "alive": True,
        "restarts": 0,
        "max_restarts": 5,
        "started_at": "2026-03-17T00:00:00+00:00"
    },
    "trend_stoploss:xrp_jpy": {
        "alive": False,
        "restarts": 5,
        "max_restarts": 5,
        "last_error": "ConnectionError: WS disconnect",
        "last_error_at": "2026-03-17T01:00:00+00:00",
        "final_exception": "ConnectionError('WS disconnect')"
    }
}
```

---

## 4. 자동 재시작 (Exponential Backoff)

```
예외 발생
  └── restarts < max_restarts?
        ├── Yes → sleep(backoff) → coro_factory() 재실행
        │         backoff = min(backoff × 2, 60초) 증가
        └── No  → 포기, 태스크 최종 종료
                  get_health()에 final_exception 기록
```

- 초기 backoff: 1초
- 최대 backoff: 60초
- 정상 종료 (coroutine return) → 재시작 안 함
- `CancelledError` → 재시작 안 함 (의도적 중지)

---

## 5. 태스크 네이밍 컨벤션

```
{strategy_type}_{task_type}:{pair}
```

| 예시 | 전략 | 태스크 |
|------|------|--------|
| `trend_candle:xrp_jpy` | 추세추종 | CandleMonitor |
| `trend_stoploss:xrp_jpy` | 추세추종 | StopLossMonitor |
| `box_monitor:xrp_jpy` | 박스권 | BoxMonitor |
| `box_entry:xrp_jpy` | 박스권 | EntryMonitor |

---

## 6. 사용처

| 컴포넌트 | 사용 방식 |
|----------|----------|
| `TrendFollowingManager.start()` | 2개 태스크 등록 (candle + stoploss) |
| `BoxMeanReversionManager.start()` | 2개 태스크 등록 (box_monitor + entry) |
| `HealthChecker.check()` | `supervisor.get_health()` 호출 |
| `main.py` lifespan shutdown | `supervisor.stop_all()` 호출 |

---

## 7. 관련 문서

- [MONITORING.md](MONITORING.md) — HealthChecker (TaskSupervisor 헬스 데이터 소비자)
- [TREND_FOLLOWING.md](TREND_FOLLOWING.md) — 추세추종 전략
- [BOX_MEAN_REVERSION.md](BOX_MEAN_REVERSION.md) — 박스권 전략
