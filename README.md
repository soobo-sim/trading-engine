# Trading Engine

> 통합 트레이딩 엔진 — 거래소 어댑터 패턴 기반, 단일 코드베이스로 다중 거래소 지원

## 아키텍처

- **Hexagonal Architecture**: core(도메인) → adapters(외부) → api(프레젠테이션)
- **배포**: 동일 이미지, `EXCHANGE` 환경변수로 거래소 선택
- **설계 문서**: [`SYSTEM_REDESIGN.md`](../trader-common/solution-design/archive/SYSTEM_REDESIGN.md)

## 구조

```
trading-engine/
├── core/                    # 순수 도메인 로직 (프레임워크 무관)
│   ├── strategy/            # 전략 (trend_following, box_mean_reversion, signals)
│   ├── exchange/            # ExchangeAdapter Protocol + 공통 DTO
│   ├── task/                # TaskSupervisor (생명주기 관리)
│   └── monitoring/          # 헬스체크, 메트릭스
├── adapters/                # 외부 시스템 어댑터
│   ├── coincheck/           # Coincheck REST + WS
│   ├── bitflyer/            # BitFlyer REST + WS
│   └── database/            # SQLAlchemy ORM + 리포지토리
├── api/                     # FastAPI 프레젠테이션 계층
│   ├── routes/              # 라우트
│   ├── schemas/             # Pydantic 스키마
│   └── middleware/          # 에러 핸들링, 로깅
├── tests/
│   ├── unit/                # 순수 로직 테스트
│   └── integration/         # 어댑터 통합 테스트
├── main.py                  # 엔트리포인트
├── Dockerfile
└── docker-compose.yml
```

## 스택

- Python 3.12+ / FastAPI / uvicorn
- PostgreSQL 16 (기존 trader-postgres 공유)
- Docker Compose

## 관련 서비스

| 서비스 | 포트 | 역할 |
|--------|------|------|
| trading-engine (CK) | 8000 | Coincheck 자동매매 |
| trading-engine (BF) | 8001 | BitFlyer 자동매매 |
| coinmarket-data | 8002 | 마켓 데이터 수집 (별도 서비스) |
