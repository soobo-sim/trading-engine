"""
Trading Engine — FastAPI 엔트리포인트.

EXCHANGE 환경변수로 CoincheckAdapter / BitFlyerAdapter 자동 선택.
lifespan에서 전체 의존성 조립 + 활성 전략 자동 기동.

사용:
    EXCHANGE=coincheck uvicorn main:app --port 8000
    EXCHANGE=bitflyer  uvicorn main:app --port 8001
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.config
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


# ── 구조화된 로깅 ────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    """JSON 구조화 로그 포맷터."""

    def __init__(self, exchange: str = "unknown"):
        super().__init__()
        self.exchange = exchange

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "exchange": self.exchange,
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        # 구조화 필드: logger.info("msg", extra={"pair": "xrp_jpy"}) 등
        for key in ("pair", "strategy_id", "event", "order_id", "action"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val
        return json.dumps(log_entry, ensure_ascii=False)


def setup_logging(exchange: str) -> None:
    """전역 로깅 초기화. EXCHANGE 이름을 모든 로그에 포함."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(exchange=exchange))

    root = logging.getLogger()
    root.setLevel(level)
    # 기존 핸들러 제거 (uvicorn 기본 핸들러 중복 방지)
    root.handlers.clear()
    root.addHandler(handler)

    # uvicorn 과도한 access 로그 억제
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

from adapters.database.models import (
    StrategyTechnique,
    create_balance_entry_model,
    create_box_model,
    create_box_position_model,
    create_candle_model,
    create_cfd_position_model,
    create_insight_model,
    create_strategy_model,
    create_summary_model,
    create_trade_model,
    create_trend_position_model,
)
from adapters.database.session import create_db_engine, create_session_factory
from api.dependencies import AppState, ModelRegistry
from api.routes import system, trading, account, strategies, boxes, candles, techniques, analysis, monitoring, cfd, performance
from core.monitoring.health import HealthChecker
from core.strategy.box_mean_reversion import BoxMeanReversionManager
from core.strategy.cfd_trend_following import CfdTrendFollowingManager
from core.strategy.trend_following import TrendFollowingManager
from core.task.supervisor import TaskSupervisor

logger = logging.getLogger(__name__)

# ── 거래소별 설정 ────────────────────────────────────────────

_EXCHANGE_CONFIG = {
    "coincheck": {
        "prefix": "ck",
        "pair_column": "pair",
        "order_id_length": 25,
        "env_api_key": "COINCHECK_API_KEY",
        "env_api_secret": "COINCHECK_API_SECRET",
        "env_base_url": "COINCHECK_BASE_URL",
        "default_base_url": "https://coincheck.com",
    },
    "bitflyer": {
        "prefix": "bf",
        "pair_column": "product_code",
        "order_id_length": 40,
        "env_api_key": "BITFLYER_API_KEY",
        "env_api_secret": "BITFLYER_API_SECRET",
        "env_base_url": "BITFLYER_BASE_URL",
        "default_base_url": "https://api.bitflyer.com",
    },
    "gmofx": {
        "prefix": "gmo",
        "pair_column": "pair",
        "order_id_length": 40,
        "env_api_key": "GMOFX_API_KEY",
        "env_api_secret": "GMOFX_API_SECRET",
        "env_base_url": "GMOFX_BASE_URL",
        "default_base_url": "https://forex-api.coin.z.com",
    },
}


def _create_adapter(exchange: str):
    """EXCHANGE에 따라 올바른 어댑터 인스턴스 생성."""
    cfg = _EXCHANGE_CONFIG[exchange]
    api_key = os.environ.get(cfg["env_api_key"], "")
    api_secret = os.environ.get(cfg["env_api_secret"], "")
    base_url = os.environ.get(cfg["env_base_url"], cfg["default_base_url"])

    if exchange == "coincheck":
        from adapters.coincheck.client import CoincheckAdapter
        return CoincheckAdapter(api_key=api_key, api_secret=api_secret, base_url=base_url)
    elif exchange == "gmofx":
        from adapters.gmo_fx.client import GmoFxAdapter
        return GmoFxAdapter(api_key=api_key, api_secret=api_secret, base_url=base_url)
    else:
        from adapters.bitflyer.client import BitFlyerAdapter
        return BitFlyerAdapter(api_key=api_key, api_secret=api_secret, base_url=base_url)


def _create_models(prefix: str, pair_column: str, order_id_length: int) -> ModelRegistry:
    """프리픽스로 ORM 모델 인스턴스화."""
    return ModelRegistry(
        strategy=create_strategy_model(prefix),
        trade=create_trade_model(prefix, order_id_length=order_id_length),
        balance_entry=create_balance_entry_model(prefix),
        insight=create_insight_model(prefix),
        summary=create_summary_model(prefix),
        candle=create_candle_model(prefix, pair_column=pair_column),
        box=create_box_model(prefix, pair_column=pair_column),
        box_position=create_box_position_model(prefix, pair_column=pair_column, order_id_length=order_id_length),
        trend_position=create_trend_position_model(prefix, order_id_length=order_id_length),
        cfd_position=create_cfd_position_model(prefix, pair_column=pair_column, order_id_length=order_id_length),
        technique=StrategyTechnique,
    )


# ── lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """startup → yield → shutdown"""
    exchange = os.environ.get("EXCHANGE", "coincheck").lower()
    if exchange not in _EXCHANGE_CONFIG:
        raise ValueError(f"Unknown EXCHANGE: {exchange}. {list(_EXCHANGE_CONFIG.keys())}만 가능.")

    # 로깅 초기화 (exchange 이름 포함)
    setup_logging(exchange)

    cfg = _EXCHANGE_CONFIG[exchange]
    prefix = cfg["prefix"]
    pair_column = cfg["pair_column"]

    logger.info(f"Starting trading-engine: exchange={exchange}, prefix={prefix}")

    # 1. DB
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise ValueError("DATABASE_URL 환경변수 필수")

    engine = create_db_engine(database_url)
    session_factory = create_session_factory(engine)

    # 2. Adapter
    adapter = _create_adapter(exchange)
    await adapter.connect()

    # 3. ORM Models
    models = _create_models(prefix, pair_column, cfg["order_id_length"])

    # 4. Supervisor
    supervisor = TaskSupervisor()

    # 5. Strategy Managers
    trend_manager = TrendFollowingManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=models.candle,
        trend_position_model=models.trend_position,
        pair_column=pair_column,
    )
    box_manager = BoxMeanReversionManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=models.candle,
        box_model=models.box,
        box_position_model=models.box_position,
        pair_column=pair_column,
    )
    cfd_manager = CfdTrendFollowingManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=models.candle,
        cfd_position_model=models.cfd_position,
        pair_column=pair_column,
    )

    # 6. Health Checker
    health_checker = HealthChecker(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        strategy_model=models.strategy,
        trend_position_model=models.trend_position,
        box_position_model=models.box_position,
        pair_column=pair_column,
    )

    # 7. AppState → app.state
    state = AppState(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        trend_manager=trend_manager,
        box_manager=box_manager,
        cfd_manager=cfd_manager,
        health_checker=health_checker,
        models=models,
        prefix=prefix,
        pair_column=pair_column,
    )
    app.state.app_state = state

    # 8. 활성 전략 자동 기동
    try:
        from sqlalchemy import select
        async with session_factory() as db:
            stmt = select(models.strategy).where(models.strategy.status == "active")
            result = await db.execute(stmt)
            active_strategies = result.scalars().all()

        for strategy in active_strategies:
            params = strategy.parameters or {}
            # CK: "pair" 키 (소문자 "xrp_jpy"), BF: "product_code" 키 (대문자 "BTC_JPY")
            # DB 쿼리에서 캔들 필터링 시 원본 case를 유지해야 함
            pair = params.get("pair") or params.get("product_code") or None
            if not pair:
                continue
            style = params.get("trading_style")
            if style == "box_mean_reversion":
                await box_manager.start(pair, params)
                logger.info(f"BoxMeanReversionManager 기동: pair={pair}")
            elif style == "trend_following":
                await trend_manager.start(pair, {**params, "strategy_id": strategy.id})
                logger.info(f"TrendFollowingManager 기동: pair={pair}")
            elif style == "cfd_trend_following":
                await cfd_manager.start(pair, {**params, "strategy_id": strategy.id})
                logger.info(f"CfdTrendFollowingManager 기동: pair={pair}")
    except Exception as e:
        logger.warning(f"활성 전략 자동 기동 실패 (DB 없으면 정상): {e}")

    logger.info("Application startup complete")
    yield

    # === Shutdown ===
    logger.info("Shutting down trading-engine...")
    await supervisor.stop_all()
    await adapter.close()
    await engine.dispose()
    logger.info("Shutdown complete")


# ── FastAPI app ──────────────────────────────────────────────

app = FastAPI(
    title="Trading Engine",
    description="거래소-무관 자동매매 시스템",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우트 등록
app.include_router(system.router)
app.include_router(trading.router)
app.include_router(account.router)
app.include_router(strategies.router)
app.include_router(boxes.router)
app.include_router(candles.router)
app.include_router(techniques.router)
app.include_router(analysis.router)
app.include_router(monitoring.router)
app.include_router(cfd.router)
app.include_router(performance.router)
