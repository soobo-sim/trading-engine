# 라젠카 전략 엔진 — 트레이딩은 어떻게 이루어지는가

> 이 문서는 라젠카의 전략 시스템을 처음 접하는 사람을 위한 개요입니다.
> 각 전략의 상세 설계는 하단 링크를 참조하세요.

---

## 전체 구조 — 한눈에 보기

```
         시장 데이터
      (가격 · 뉴스 · 매크로)
              │
              ▼
  ┌───────────────────────┐
  │   전략 매니저           │  ← 자동 감시 루프
  │   (추세추종 / 박스권)    │     24시간 쉬지 않고 시장을 본다
  └───────────┬───────────┘
              │ 시그널 발생!
              ▼
  ┌───────────────────────┐
  │   AI 판단 레이어        │  ← 레이첼(Rachel) advisory
  │   (진입할까? 말까?      │     뉴스·매크로·맥락을 종합 판단
  │    크기는? 손절선은?)    │
  └───────────┬───────────┘
              │ 최종 결정
              ▼
  ┌───────────────────────┐
  │   안전장치 (Guardrails) │  ← AI도 무시 못하는 하드캡
  │   일일 손실 3%↑ → 정지  │     최대 사이즈 80%, SL 필수
  └───────────┬───────────┘
              │ 통과
              ▼
  ┌───────────────────────┐
  │   거래소 주문 실행       │  ← BitFlyer / GMO FX 어댑터
  │   market buy/sell      │
  └───────────────────────┘
```

---

## 두 가지 전략

라젠카는 시장 상황에 따라 **두 가지 전략**을 운용합니다.

```
       가격
        │
   ──╲──┼─────╱──────╲───╱──    ← 추세추종: "올라갈 때 올라타자"
        │                          방향이 명확할 때 사용
        │
   ─────┼───────────────────
   ═══  │  ═════════════════    ← 박스역추세: "바닥에서 사고, 천장에서 팔자"
   ═══  │  ═════════════════       가격이 일정 범위에서 왔다갔다할 때 사용
   ─────┼───────────────────
        │
```

| | 추세추종 (Trend Following) | 박스역추세 (Box Mean Reversion) |
|---|---|---|
| **언제?** | 강한 방향성이 있을 때 | 가격이 범위 안에서 횡보할 때 |
| **원칙** | "Let winners run" — 추세를 끝까지 탄다 | "바닥에서 사서 천장에서 판다" |
| **진입** | EMA 상승 + RSI 적정 → 매수 | 박스 하단 접근 → 매수, 상단 접근 → 매도 |
| **청산** | 추세 이탈 감지 → 전량 청산 | 반대편 도달 or 박스 붕괴 → 청산 |
| **손절** | 적응형 트레일링 스탑 | 고정 % 스탑로스 |
| **상세 설계** | [TREND_FOLLOWING.md] | [BOX_MEAN_REVERSION.md] |

---

## 1. 추세추종 — 주문이 들어가는 과정

### 감시 → 시그널 → 판단 → 주문

```
┌─────────────────────────────────────────────────────────────┐
│                     매 60초마다 반복                          │
│                                                              │
│  ① DB에서 최신 4시간 캔들 조회                                │
│     └→ EMA(20), RSI(14), ATR(14), BB Width 계산              │
│                                                              │
│  ② 시그널 판정                                               │
│     ├─ 가격 > EMA20, EMA 상승 중, RSI 40~65                  │
│     │  └→ "entry_ok" (진입 가능!)                            │
│     ├─ RSI > 65                                              │
│     │  └→ "wait_dip" (과매수, 좀 기다리자)                    │
│     ├─ 가격 < EMA20                                          │
│     │  └→ "exit_warning" (추세 이탈, 빠져나가자!)             │
│     └─ 그 외                                                 │
│        └→ "no_signal" (아무것도 안 함)                        │
│                                                              │
│  ③ AI 판단 (TRADING_MODE=rachel)                             │
│     └→ 오케스트레이터가 레이첼 advisory 조회                  │
│        ├─ advisory "entry_long" + signal "entry_ok"           │
│        │  └→ 둘 다 동의 → 진입 실행!                         │
│        ├─ advisory "exit"                                    │
│        │  └→ AI가 나가라고 하면 즉시 청산 (시그널 무관)        │
│        └─ advisory 없거나 만료                                │
│           └→ v1 룰 기반 판단으로 폴백                         │
│                                                              │
│  ④ 안전장치 체크                                              │
│     ├─ 일일 손실 3% 초과? → 거래 정지                         │
│     ├─ 일일 거래 5회 초과? → 거래 정지                         │
│     └─ 최대 포지션 80% 초과? → 크기 축소                      │
│                                                              │
│  ⑤ 거래소에 주문                                              │
│     └→ market_buy / market_sell 실행                          │
└─────────────────────────────────────────────────────────────┘
```

### 포지션 보유 중 — 추세를 언제까지 타는가

```
        가격
         │    ╱╲
         │   ╱  ╲  ← 스탑 타이트닝 (RSI 과열 감지)
         │  ╱    ╲    스탑라인을 바짝 올려서 이익 보호
         │ ╱  ↑   ╲
    진입 →╱   │    ╲→ 스탑로스 발동 = 청산
         │   │
         │   │ 적응형 트레일링 스탑
         │   │ (가격이 오르면 스탑도 따라 올라간다)
         │   │
    ─────┼───┘
         │
```

**적응형 트레일링 스탑** — 추세 상태에 따라 손절 간격이 바뀝니다:

| 추세 상태 | 스탑 거리 | 의미 |
|-----------|----------|------|
| 초기/가속 (추세 시작) | 현재가 - ATR × 2.0 (넓음) | 추세에 숨을 공간을 준다 |
| 성숙/과열 (RSI↑ or 기울기↓) | 현재가 - ATR × 1.2 (좁음) | 이익을 보호한다 |
| 스탑 타이트닝 발동 후 | 현재가 - ATR × 1.0 (바짝) | 곧 빠질 수 있다 |

> 스탑은 **한 방향으로만 움직입니다** (올라가기만 함). 가격이 내려도 스탑은 내려가지 않습니다.

### 두 개의 감시 태스크

```
┌─────────────────────────────┐  ┌──────────────────────────────┐
│  Task 1: CandleMonitor       │  │  Task 2: StopLossMonitor      │
│  (60초마다 DB 캔들 폴링)      │  │  (WebSocket 실시간 틱)         │
│                              │  │                               │
│  · 시그널 → AI → 진입/청산    │  │  · 매 체결마다 가격 확인        │
│  · 트레일링 스탑 갱신         │  │  · 스탑 이탈 → 즉시 market_sell │
│  · EMA 다이버전스 감지        │  │                               │
│                              │  │  "60초 기다릴 여유 없을 때의    │
│  "전략적 판단"               │  │   비상 브레이크"               │
└─────────────────────────────┘  └──────────────────────────────┘
```

> 상세: [TREND_FOLLOWING.md](../../../../trader-common/docs/specs/strategy/TREND_FOLLOWING.md)

---

## 2. 박스역추세 — 주문이 들어가는 과정

### 박스 감지

가격이 일정 범위를 반복 왕복하면 **박스(Box)**로 인식합니다.

```
    가격
     │
     │  ════════ 상단 (upper) ════════   ← 이 선에 3번 이상 닿아야 인정
     │  │                            │
     │  │       박스 내부              │  ← 가격이 이 범위 안에 머무는 구간
     │  │                            │
     │  ════════ 하단 (lower) ════════   ← 이 선에 3번 이상 닿아야 인정
     │
```

**감지 조건**: 4시간 캔들 기준, 지정된 기간의 고점/저점을 클러스터링하여 상/하단 경계를 결정합니다.

### 진입과 청산

```
    가격
     │
     │  ════ 상단 ════
     │  │    ↓ 매도 (숏)   ← near_upper: 상단 근처 도달 → 매도
     │  │                           (가격이 내려갈 거라 예상)
     │  │
     │  │    ↑ 매수 (롱)   ← near_lower: 하단 근처 도달 → 매수
     │  ════ 하단 ════              (가격이 올라갈 거라 예상)
     │
     │  ╳ 박스 붕괴!       ← 가격이 박스를 벗어나면
     │    → 즉시 손절            기존 로직이 깨졌으므로 즉시 청산
```

### 주문 경로 (WebSocket 틱 기반)

```
┌─────────────────────────────────────────────────────────────┐
│                  매 WebSocket 틱마다 반복                     │
│                                                              │
│  ① 현재가 → 박스 내 위치 판정                                 │
│     ├─ "near_lower" (하단 근처)                               │
│     ├─ "near_upper" (상단 근처)                               │
│     ├─ "inside" (박스 안, 경계에서 먼 곳)                      │
│     └─ "outside" (박스 바깥 → 붕괴 가능)                      │
│                                                              │
│  ② 상태 전환 시 (예: inside → near_lower)                     │
│     └→ 진입 판단                                             │
│                                                              │
│  ③ 주문 방식 (거래소별 분기)                                   │
│     ├─ BF 현물: market_buy → 이후 틱마다 SL% 체크             │
│     └─ GMO FX:  IFD-OCO 주문 (진입+손절+익절을 한 번에 설정)   │
│                                                              │
│  ④ 손절 체크 (매 틱)                                          │
│     └→ 진입가 대비 -1.5% 이상 하락? → 즉시 market_sell         │
│                                                              │
│  ⑤ 박스 무효화 체크 (60초마다 별도 태스크)                      │
│     ├─ 가격이 박스를 이탈했나?                                 │
│     ├─ 박스 폭이 수렴하고 있나? (삼각형)                       │
│     └→ 무효화 시 → 보유 포지션 강제 청산                       │
└─────────────────────────────────────────────────────────────┘
```

> 상세: [BOX_MEAN_REVERSION.md](../../../../trader-common/docs/specs/strategy/BOX_MEAN_REVERSION.md)

---

## 3. AI가 거래에 관여하는 방식

### 현재 작동 중 (TRADING_MODE=rachel)

```
                 레이첼 (OpenClaw 에이전트)
                 뉴스·매크로·기술적 분석 종합
                          │
                          ▼
              POST /api/advisories
              {
                action: "entry_long",      ← 무엇을 할지
                confidence: 0.7,           ← 얼마나 확신하는지
                size_pct: 0.55,            ← 자본의 몇 %를 투입할지
                stop_loss: 209.50,         ← 손절 가격
                reasoning: "BOJ 완화 기조 유지 + 추세 정렬"
              }
                          │
                          ▼
              rachel_advisories 테이블 (DB 저장)
                          │
                          │  (매 60초마다 조회됨)
                          ▼
  ┌───────────────────────────────────────────┐
  │         RachelAdvisoryDecision             │
  │                                            │
  │  advisory 액션    ×    실시간 시그널         │
  │  ─────────────   ─── ────────────          │
  │  entry_long      AND  entry_ok   → 진입!   │
  │  entry_long      AND  wait_dip   → 대기    │
  │  exit            AND  (무관)     → 청산!   │
  │  hold            AND  (무관)     → 대기    │
  │  (없음/만료)      →    v1 룰 기반 폴백      │
  └───────────────────────────────────────────┘
```

**핵심 원칙**:
- **진입**: AI와 시그널이 **둘 다 동의**해야 실행 (안전)
- **청산**: AI **또는** 시그널 **어느 한쪽**이라도 요구하면 실행 (보수적)
- **AI 장애**: advisory가 없거나 만료되면 기존 룰 기반(v1)으로 자동 폴백

### 설계 완료 / 미구현 (Stage 2)

| 기능 | 설명 | 적용 대상 |
|------|------|----------|
| **EventDetector** | 가격 ±2%, 센티먼트 급변 등 감지 → Rachel에 판단 요청 | 추세추종 + 박스 |
| **adjust_risk** | 보유 중 SL/TP를 매크로 맥락 기반으로 동적 재조정 | 추세추종 + 박스 |
| **박스 advisory 통합** | 박스전략에도 AI 사전 판단 적용 (현재 100% 룰 기반) | 박스역추세 |

---

## 4. 소스 파일 가이드

```
core/strategy/
│
├── README.md              ← 지금 읽고 있는 문서
│
├── base.py                ← IStrategy Protocol (4메서드: start/stop/is_running/running_pairs)
├── registry.py            ← StrategyRegistry — trading_style 이름으로 전략 매니저 검색
│
├── base_trend.py          ← 추세추종 공통 베이스 (캔들 모니터 + 스탑로스 모니터)
├── signals.py             ← 시그널 계산 함수 (EMA, RSI, ATR, BB, 다이버전스)
├── box_signals.py         ← 박스 시그널 순수 함수 (박스 내 위치 판정, 무효화 체크)
│
├── trend_following.py     ← re-export stub (실제: plugins/trend_following/manager.py)
├── box_mean_reversion.py  ← re-export stub (실제: plugins/box_mean_reversion/manager.py)
├── cfd_trend_following.py ← re-export stub (실제: plugins/cfd_trend_following/manager.py)
│
├── scoring.py             ← 전략 성과 스코어링
├── switch_recommender.py  ← 전략 전환 추천
├── snapshot_collector.py  ← 시그널 스냅샷 수집
│
└── plugins/               ← 전략별 매니저 구현
    ├── trend_following/
    │   └── manager.py     ← TrendFollowingManager (현물 추세추종)
    ├── box_mean_reversion/
    │   └── manager.py     ← BoxMeanReversionManager (박스역추세)
    └── cfd_trend_following/
        └── manager.py     ← CfdTrendFollowingManager (CFD 추세추종)
```

### 새 전략 추가하기

1. `plugins/` 아래에 폴더 생성
2. `manager.py` 에서 `IStrategy` Protocol 구현
3. `main.py`의 lifespan에서 `registry.register("전략이름", manager)` 1줄 추가
4. 끝 — 다른 파일은 수정할 필요 없음

---

## 5. 상세 설계 문서

| 문서 | 내용 |
|------|------|
| [추세추종 전략 (TREND_FOLLOWING.md)](../../../../trader-common/docs/specs/strategy/TREND_FOLLOWING.md) | 시그널 계산 · 트레일링 스탑 · 다이버전스 감지 · 파라미터 전체 목록 |
| [박스역추세 전략 (BOX_MEAN_REVERSION.md)](../../../../trader-common/docs/specs/strategy/BOX_MEAN_REVERSION.md) | 박스 감지 알고리즘 · IFD-OCO · 안전장치 · FX 실행 모델 차이 |
| [AI-Native 시스템 (AI_NATIVE_TRADING_SYSTEM.md)](../../../../trader-common/docs/specs/ai-native/AI_NATIVE_TRADING_SYSTEM.md) | 전체 AI 아키텍처 · EventDetector · adjust_risk · 안전장치 |
| [판단 엔진 (02_JUDGMENT_ENGINE.md)](../../../../trader-common/docs/specs/ai-native/02_JUDGMENT_ENGINE.md) | Rachel advisory 결합 규칙 · 트리거 종류 · 확신도 모델 |
