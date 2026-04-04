"""
Trading Engine — FastAPI 엔트리포인트.

EXCHANGE 환경변수로 BitFlyerAdapter / GmoFxAdapter 자동 선택.
lifespan에서 전체 의존성 조립 + 활성 전략 자동 기동.

사용:
    EXCHANGE=bitflyer  uvicorn main:app --port 8001
    EXCHANGE=gmofx     uvicorn main:app --port 8003
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
from api.routes import system, trading, account, strategies, boxes, candles, techniques, analysis, monitoring, cfd, performance, wake_up_reviews, strategy_changes, strategy_analysis, paper_trades
from core.monitoring.health import HealthChecker
from core.analysis.event_filter import create_event_filter
from core.analysis.intermarket import create_intermarket_client
from core.execution.executor import PaperExecutor
from core.strategy.box_mean_reversion import BoxMeanReversionManager
from core.strategy.cfd_trend_following import CfdTrendFollowingManager
from core.strategy.trend_following import TrendFollowingManager
from core.strategy.registry import StrategyRegistry
from core.task.auto_reporter import create_auto_reporter
from core.task.supervisor import TaskSupervisor

logger = logging.getLogger(__name__)

# ── 거래소별 설정 ────────────────────────────────────────────

_EXCHANGE_CONFIG = {
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

    if exchange == "gmofx":
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
    exchange = os.environ.get("EXCHANGE", "bitflyer").lower()
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

    # 5. Strategy Managers + Registry
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
        event_filter=create_event_filter(),
        intermarket_client=create_intermarket_client(),
    )
    cfd_manager = CfdTrendFollowingManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=models.candle,
        cfd_position_model=models.cfd_position,
        pair_column=pair_column,
    )

    strategy_registry = StrategyRegistry()
    strategy_registry.register("trend_following", trend_manager)
    strategy_registry.register("box_mean_reversion", box_manager)
    strategy_registry.register("cfd_trend_following", cfd_manager)

    # 6. Health Checker
    health_checker = HealthChecker(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        strategy_model=models.strategy,
        trend_position_model=models.trend_position,
        box_position_model=models.box_position,
        pair_column=pair_column,
        trend_manager=trend_manager,
        box_model=models.box,
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
        strategy_registry=strategy_registry,
    )
    app.state.app_state = state

    # 8. 활성 + Proposed 전략 자동 기동
    try:
        from sqlalchemy import select, or_
        async with session_factory() as db:
            stmt = select(models.strategy).where(
                or_(models.strategy.status == "active", models.strategy.status == "proposed")
            )
            result = await db.execute(stmt)
            all_strategies = result.scalars().all()

        proposed_count = 0
        _PAPER_TRADING_HARDCAP = 3  # proposed 동시 실행 최대 수

        for strategy in all_strategies:
            params = strategy.parameters or {}
            # CK: "pair" 키 (소문자 "xrp_jpy"), BF: "product_code" 키 (대문자 "BTC_JPY")
            # DB 캔들 pair 컬럼과 대소문자가 일치해야 한다
            pair = params.get("pair") or params.get("product_code") or None
            if not pair:
                continue
            pair = state.normalize_pair(pair)
            style = params.get("trading_style")
            start_params = {**params, "strategy_id": strategy.id}

            is_proposed = strategy.status == "proposed"
            if is_proposed:
                if proposed_count >= _PAPER_TRADING_HARDCAP:
                    logger.warning(
                        f"Paper trading 하드캡({_PAPER_TRADING_HARDCAP}) 초과 — "
                        f"strategy_id={strategy.id} ({pair}) 기동 스킵"
                    )
                    continue
                # 전략 스타일별 매니저에 PaperExecutor 바인딩 (pair 레벨 분리)
                if style == "box_mean_reversion":
                    box_manager._executor = PaperExecutor(session_factory, strategy.id)
                elif style == "trend_following":
                    trend_manager.register_paper_pair(pair, strategy.id)
                elif style == "cfd_trend_following":
                    cfd_manager.register_paper_pair(pair, strategy.id)
                proposed_count += 1
                logger.info(
                    f"Paper trading 시작: strategy_id={strategy.id} pair={pair} style={style} "
                    f"({proposed_count}/{_PAPER_TRADING_HARDCAP})"
                )

            if not await strategy_registry.start_strategy(style, pair, start_params):
                logger.warning(f"미등록 전략 스타일: {style} (pair={pair})")
    except Exception as e:
        logger.warning(f"활성 전략 자동 기동 실패 (DB 없으면 정상): {e}")

    # 9. AutoReporter (환경변수로 ON/OFF)
    auto_reporter = create_auto_reporter(session_factory, state)
    if auto_reporter:
        await auto_reporter.start()

    logger.info("Application startup complete")
    yield

    # === Shutdown ===
    logger.info("Shutting down trading-engine...")
    if auto_reporter:
        await auto_reporter.stop()
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
app.include_router(wake_up_reviews.router)
app.include_router(strategy_changes.router)
app.include_router(strategy_analysis.router)
app.include_router(paper_trades.router)
