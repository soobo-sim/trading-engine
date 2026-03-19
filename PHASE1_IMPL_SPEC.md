---
status: approved
created_at: 2026-03-17
author: Monica (Opus)
target: Sonnet
---

# Phase 1 구현 명세 — 어댑터 구현

> **Opus → Sonnet 인수인계 문서**
> Phase 0에서 확정된 인터페이스를 기반으로, Sonnet이 어댑터를 구현한다.

---

## 0. Phase 0 산출물 (이미 구현됨 — 읽기 전용)

| 파일 | 역할 | 핵심 사항 |
|------|------|-----------|
| `core/exchange/types.py` | DTO 정의 | Candle, Order, Balance, Ticker, Position, ExchangeConstraints + enums |
| `core/exchange/base.py` | ExchangeAdapter Protocol | 11개 async 메서드. **이 인터페이스를 그대로 구현해야 한다.** |
| `core/exchange/errors.py` | 표준 예외 | ExchangeError → OrderError, AuthenticationError, RateLimitError, ConnectionError |
| `core/task/supervisor.py` | TaskSupervisor | Phase 1에서는 사용하지 않음 (Phase 2에서 매니저 통합 시 사용) |
| `core/strategy/signals.py` | 시그널 함수 | Phase 1에서는 사용하지 않음 |
| `tests/fake_exchange.py` | 테스트 어댑터 | Protocol 준수 레퍼런스. 구현 시 참고할 것 |

**반드시 `core/exchange/base.py`의 ExchangeAdapter Protocol 정의를 먼저 읽은 후 작업을 시작할 것.**

---

## 1. 파일별 구현 명세

### 1.1 `adapters/coincheck/signer.py`

**책임**: Coincheck REST API HMAC-SHA256 서명 생성

```python
import hashlib
import hmac
import time


class CoincheckSigner:
    """Coincheck API 요청 서명."""

    def __init__(self, api_key: str, api_secret: str):
        self._api_key = api_key
        self._api_secret = api_secret

    def sign(self, url: str, body: str = "") -> dict[str, str]:
        """
        인증 헤더 생성.

        서명 대상: nonce + full_url + body
        nonce: Unix timestamp 밀리초 (str)

        Returns:
            {"ACCESS-KEY": ..., "ACCESS-NONCE": ..., "ACCESS-SIGNATURE": ...}
        """
        ...
```

**핵심 규칙**:
- nonce = `str(int(time.time() * 1000))` (밀리초)
- message = `nonce + url + body` (url은 full URL, 예: `https://coincheck.com/api/exchange/orders`)
- signature = `hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()`
- 헤더: `ACCESS-KEY`, `ACCESS-NONCE`, `ACCESS-SIGNATURE`

### 1.2 `adapters/coincheck/client.py`

**책임**: CoincheckAdapter — ExchangeAdapter Protocol 구현

```python
class CoincheckAdapter:
    """Coincheck 거래소 어댑터."""

    def __init__(self, api_key: str, api_secret: str, base_url: str):
        ...

    @property
    def exchange_name(self) -> str:
        return "coincheck"

    @property
    def constraints(self) -> ExchangeConstraints:
        return ExchangeConstraints(
            min_order_sizes={"xrp": 1.0, "btc": 0.001},
            rate_limit=(180, 60),  # 180 calls / 60 sec
        )
```

**메서드별 변환 규칙**:

| Protocol 메서드 | CK API 경로 | 변환 규칙 |
|----------------|-------------|-----------|
| `place_order()` | `POST /api/exchange/orders` | amount → `str(amount)`, price → `str(int(price))`. OrderType.MARKET_BUY 시 body에 `market_buy_amount`(JPY) 대신 **`amount` 사용** (XRP 수량). order_id: `str(response["id"])` |
| `cancel_order()` | `DELETE /api/exchange/orders/{id}` | — |
| `get_open_orders()` | `GET /api/exchange/orders/opens?pair={pair}` | 응답 `orders` 배열 → `list[Order]` |
| `get_order()` | `GET /api/exchange/orders/transactions_pagination?limit=100` | order_id로 필터링 필요 (CK에 단건 조회 API 없음) |
| `get_balance()` | `GET /api/accounts/balance` | 응답이 flat dict: `{"jpy": "1000000", "xrp": "50.5", ...}`. `{currency}_reserved` 필드로 available 계산: `available = float(data[currency]) - float(data.get(f"{currency}_reserved", 0))` |
| `get_ticker()` | `GET /api/ticker?pair={pair}` | Public API (서명 불필요) |
| `subscribe_trades()` | WS `wss://ws-api.coincheck.com/` | channel: `{pair}-trades` |
| `subscribe_executions()` | Private WS | 별도 URL: `COINCHECK_PRIVATE_WS_URL` 환경변수. 서명용: `COINCHECK_PRIVATE_WS_SIGN_URL` |
| `connect()` | — | httpx.AsyncClient 초기화 |
| `close()` | — | httpx.AsyncClient.aclose() + WS 종료 |

**⚠️ 주의사항**:
1. `order_id`는 CK 내부적으로 **int**지만, 항상 `str()`로 변환하여 반환
2. `amount`/`rate` 필드는 CK API에 **문자열**로 전송해야 함
3. `pair`는 Protocol에서 소문자로 받으므로 그대로 전달 (CK도 소문자)
4. 잔고 응답에서 통화코드는 이미 소문자 (변환 불필요)

### 1.3 `adapters/bitflyer/signer.py`

**책임**: BitFlyer REST API HMAC-SHA256 서명 생성

```python
class BitFlyerSigner:
    """BitFlyer API 요청 서명."""

    def __init__(self, api_key: str, api_secret: str):
        self._api_key = api_key
        self._api_secret = api_secret

    def sign(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """
        인증 헤더 생성.

        서명 대상: timestamp + METHOD + path + body
        timestamp: Unix timestamp 초 (str)

        Returns:
            {"ACCESS-KEY": ..., "ACCESS-TIMESTAMP": ..., "ACCESS-SIGN": ...}
        """
        ...
```

**핵심 규칙**:
- timestamp = `str(int(time.time()))` (초, 밀리초 아님!)
- message = `timestamp + method.upper() + path + body` (path만, host 제외)
- signature = `hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()`
- 헤더: `ACCESS-KEY`, `ACCESS-TIMESTAMP`, `ACCESS-SIGN` (SIGNATURE 아님!)

### 1.4 `adapters/bitflyer/client.py`

**책임**: BitFlyerAdapter — ExchangeAdapter Protocol 구현

```python
class BitFlyerAdapter:
    """BitFlyer 거래소 어댑터."""

    def __init__(self, api_key: str, api_secret: str, base_url: str):
        ...

    @property
    def exchange_name(self) -> str:
        return "bitflyer"

    @property
    def constraints(self) -> ExchangeConstraints:
        return ExchangeConstraints(
            min_order_sizes={"xrp": 0.1, "btc": 0.001},
            rate_limit=(500, 300),  # 500 calls / 5 min
        )
```

**메서드별 변환 규칙**:

| Protocol 메서드 | BF API 경로 | 변환 규칙 |
|----------------|-------------|-----------|
| `place_order()` | `POST /v1/me/sendchildorder` | `pair` → `product_code` 대문자 변환 (xrp_jpy → XRP_JPY). `amount` → `size`. OrderType → `side` + `child_order_type` 분리: BUY+LIMIT, SELL+MARKET 등. 응답: `child_order_acceptance_id` → `order_id` |
| `cancel_order()` | `POST /v1/me/cancelchildorder` | body: `{"product_code": ..., "child_order_acceptance_id": order_id}` |
| `get_open_orders()` | `GET /v1/me/getchildorders?product_code={}&child_order_state=ACTIVE` | — |
| `get_order()` | `GET /v1/me/getchildorders?child_order_acceptance_id={id}` | 배열 반환 → 첫 번째 요소 |
| `get_balance()` | `GET /v1/me/getbalance` | 응답이 **배열**: `[{"currency_code": "JPY", "amount": ..., "available": ...}]` → dict 변환. `currency_code` 를 소문자로 변환: `"JPY" → "jpy"` |
| `get_ticker()` | `GET /v1/ticker?product_code={PAIR}` | Public API. pair 대문자 변환 |
| `subscribe_trades()` | WS `wss://ws.lightstream.bitflyer.com/json-rpc` | channel: `lightning_executions_{PRODUCT_CODE}` |
| `subscribe_executions()` | Private WS 동일 URL | channel: `child_order_events` |
| `connect()` / `close()` | — | httpx.AsyncClient 관리 |

**⚠️ 주의사항**:
1. `order_id`는 문자열 26자+ (`JRF20150707-050237-639234`). 그대로 반환
2. `pair` → `product_code` 변환: `pair.upper().replace("_", "_")` (이미 언더스코어이므로 `.upper()` 만)
3. 잔고 `currency_code` 대문자 → Balance에 담을 때 **소문자로** 변환
4. BF `cancel_order()`는 `DELETE`가 아닌 **`POST`**

### 1.5 `adapters/database/session.py`

**책임**: AsyncSession 팩토리 — 거래소 무관하게 동일 DB 접속

```python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def create_db_engine(database_url: str):
    return create_async_engine(
        database_url,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True,
    )

def create_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
```

기존 CK/BF `app/database.py` 와 동일한 설정. 변경 없이 옮기면 된다.

### 1.6 `adapters/database/models.py`

**책임**: ORM 모델 팩토리 — ck_/bf_ prefix를 파라미터로 추상화

**현재 구조**: CK와 BF 각각 `app/models/database.py`에 거의 동일한 ORM 모델이 있음.
차이점은 테이블명 prefix와 일부 컬럼 크기 (order_id String(25) vs String(40)).

**팩토리 패턴**:

```python
def create_trade_model(prefix: str, order_id_length: int = 40):
    """
    Trade ORM 모델 팩토리.

    create_trade_model("ck", 25) → CkTrade (table: ck_trades)
    create_trade_model("bf", 40) → BfTrade (table: bf_trades)
    """
    class Trade(Base):
        __tablename__ = f"{prefix}_trades"
        id = Column(Integer, primary_key=True)
        order_id = Column(String(order_id_length), unique=True)
        # ... 나머지 공통 컬럼
    Trade.__name__ = f"{prefix.capitalize()}Trade"
    return Trade
```

**구현할 팩토리 함수**:
- `create_trade_model(prefix, order_id_length)`
- `create_strategy_model(prefix)`
- `create_balance_entry_model(prefix)`
- `create_insight_model(prefix)`
- `create_summary_model(prefix)`
- `create_candle_model(prefix)` — CK: `pair` 컬럼, BF: `product_code` 컬럼 (이 차이는 파라미터로)
- `create_box_model(prefix)`
- `create_box_position_model(prefix)`
- `create_trend_position_model(prefix)`
- `StrategyTechnique` — 공유 테이블, 팩토리 아님 (직접 정의)

**주의**: 기존 DB 테이블은 변경하지 않는다. prefix + 컬럼 크기만 파라미터화.

---

## 2. 테스트 시나리오 목록

Sonnet이 구현할 테스트. `tests/unit/` 과 `tests/integration/` 에 배치.

### 2.1 Signer 테스트 (`tests/unit/test_signers.py`)

| # | 테스트 | 검증 |
|---|--------|------|
| 1 | CK signer — 정상 서명 생성 | 헤더 키 3개 존재, signature hex 형식 |
| 2 | CK signer — body 있는 POST 요청 | message = nonce + url + body 포함 확인 |
| 3 | CK signer — body 없는 GET 요청 | message = nonce + url 만 |
| 4 | BF signer — 정상 서명 생성 | 헤더 키 3개 존재, ACCESS-SIGN (SIGNATURE 아님) |
| 5 | BF signer — POST 요청 | message = timestamp + POST + path + body |
| 6 | BF signer — GET 요청 + 쿼리스트링 | path에 쿼리스트링 포함 확인 |
| 7 | CK vs BF — nonce 단위 차이 | CK 밀리초(13자리), BF 초(10자리) |

### 2.2 어댑터 테스트 (`tests/unit/test_coincheck_adapter.py`, `test_bitflyer_adapter.py`)

httpx 응답을 mock하여 테스트. 실제 API 호출 없음.

| # | 테스트 | 검증 |
|---|--------|------|
| 1 | place_order → Order DTO 변환 | order_id가 str, 필드 매핑 정확 |
| 2 | place_order market_buy | CK: market_buy_amount vs BF: MARKET+BUY |
| 3 | cancel_order 성공 | True 반환 |
| 4 | cancel_order 실패 (404) | False 또는 OrderError |
| 5 | get_balance → Balance DTO 변환 | CK: flat dict → Balance, BF: array → Balance |
| 6 | get_balance 통화 소문자 통일 | BF "JPY" → balance.get("jpy") 정상 |
| 7 | get_ticker → Ticker DTO 변환 | — |
| 8 | get_open_orders | list[Order] 반환 |
| 9 | Protocol 준수 확인 | `isinstance(adapter, ExchangeAdapter)` |
| 10 | pair 변환 (BF only) | "xrp_jpy" → "XRP_JPY" product_code 변환 |

### 2.3 ORM 팩토리 테스트 (`tests/unit/test_models.py`)

| # | 테스트 | 검증 |
|---|--------|------|
| 1 | create_trade_model("ck", 25) | __tablename__ == "ck_trades", order_id 길이 25 |
| 2 | create_trade_model("bf", 40) | __tablename__ == "bf_trades", order_id 길이 40 |
| 3 | 모든 팩토리 함수 호출 | 에러 없이 모델 클래스 반환 |
| 4 | StrategyTechnique 공유 모델 | __tablename__ == "strategy_techniques", prefix 없음 |

---

## 3. 환경 변수

어댑터가 필요로 하는 환경변수 (기존과 동일):

```env
# 공통
DATABASE_URL=postgresql+asyncpg://...
EXCHANGE=coincheck   # or bitflyer — main.py에서 어댑터 선택에 사용

# Coincheck
COINCHECK_API_KEY=...
COINCHECK_API_SECRET=...
COINCHECK_BASE_URL=https://coincheck.com
COINCHECK_WS_URL=wss://ws-api.coincheck.com
COINCHECK_PRIVATE_WS_URL=...
COINCHECK_PRIVATE_WS_SIGN_URL=...

# BitFlyer
BITFLYER_API_KEY=...
BITFLYER_API_SECRET=...
BITFLYER_BASE_URL=https://api.bitflyer.com
BITFLYER_WS_URL=wss://ws.lightstream.bitflyer.com/json-rpc
```

---

## 4. 구현 순서 (권장)

1. **Signer 2개** → 테스트 → (가장 독립적, 의존성 없음)
2. **CoincheckAdapter** → 테스트 → (CK 먼저, 결정사항 #4)
3. **BitFlyerAdapter** → 테스트 → (CK 패턴 참고)
4. **database/session.py** → (기존 코드 거의 복사)
5. **database/models.py** + 팩토리 → 테스트
6. **[문서] solution-design/ frontmatter 일괄 추가** (§13.3-A)
7. **[문서] API_CATALOG.md 초안** (§13.3-B)
8. **[문서] STRATEGE_DESIGN.md → STRATEGY_DESIGN.md 파일명 수정**

---

## 5. 참조 파일

Sonnet이 구현 시 읽어야 할 기존 코드:

| 참조 대상 | 파일 경로 | 읽는 이유 |
|-----------|-----------|-----------|
| CK 서명 | `coincheck-trader/app/services/coincheck/base_client.py` | `_generate_signature()` 로직 복사 |
| CK 주문 | `coincheck-trader/app/services/coincheck/order_client.py` | API 경로, 필드 매핑 |
| CK 잔고 | `coincheck-trader/app/services/coincheck/balance_client.py` | 응답 파싱 |
| CK ORM | `coincheck-trader/app/models/database.py` | 테이블 스키마 |
| BF 서명 | `bitflyer-trader/app/services/bitflyer/base_client.py` | `_get_auth_headers()` 로직 복사 |
| BF 주문 | `bitflyer-trader/app/services/bitflyer/order_client.py` | API 경로, 필드 매핑 |
| BF 잔고 | `bitflyer-trader/app/services/bitflyer/balance_client.py` | 응답 파싱 |
| BF ORM | `bitflyer-trader/app/models/database.py` | 테이블 스키마 |
| Protocol | `trading-engine/core/exchange/base.py` | **반드시 검증** |
| DTO 타입 | `trading-engine/core/exchange/types.py` | 반환 타입 참조 |
| 예외 | `trading-engine/core/exchange/errors.py` | 에러 매핑 |
| 레퍼런스 | `trading-engine/tests/fake_exchange.py` | Protocol 구현 예시 |

---

## 6. 금지사항

- `core/` 파일을 수정하지 말 것 — Phase 0 산출물은 동결
- 하드코딩 금지 — 모든 URL, 키, 포트는 환경변수 또는 생성자 인자
- `asyncio.create_task()` 직접 사용 금지 — Phase 1에서는 태스크 생성이 없음
- 거래소 고유 예외를 core/ 밖으로 전파하지 말 것 — `errors.py`의 표준 예외로 래핑
