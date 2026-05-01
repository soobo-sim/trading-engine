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
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


# ── 구조화된 로깅 ────────────────────────────────────────────

JST = timezone(timedelta(hours=9))


class JSONFormatter(logging.Formatter):
    """JSON 구조화 로그 포맷터."""

    def __init__(self, exchange: str = "unknown"):
        super().__init__()
        self.exchange = exchange

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "ts": datetime.fromtimestamp(record.created, tz=JST).isoformat(),
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
    from logging.handlers import TimedRotatingFileHandler

    console_level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
    json_fmt = JSONFormatter(exchange=exchange)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # 루트는 DEBUG — 파일에 전부 남김
    # 기존 핸들러 제거 (uvicorn 기본 핸들러 중복 방지)
    root.handlers.clear()

    # 1) 콘솔: LOG_LEVEL 이상 (기본 INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, console_level_str, logging.INFO))
    console.setFormatter(json_fmt)
    root.addHandler(console)

    # 2) 파일: DEBUG 이상 (전체 활동 기록, 30일 보관)
    os.makedirs("logs", exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        filename=f"logs/{exchange}.log",
        when="midnight", interval=1, backupCount=30,
        encoding="utf-8", utc=False,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(json_fmt)
    root.addHandler(file_handler)

    # 노이즈 억제
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("websockets.client").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

from adapters.database.models import (
    AiJudgment,
    RachelAdvisory,
    StrategyTechnique,
    create_balance_entry_model,
    create_box_model,
    create_box_position_model,
    create_candle_model,
    create_insight_model,
    create_strategy_model,
    create_strategy_snapshot_model,
    create_switch_recommendation_model,
    create_summary_model,
    create_trade_model,
    create_trend_position_model,
)
from adapters.database.session import create_db_engine, create_session_factory
from adapters.database import lesson_model  # noqa: F401 — Lesson 테이블 Base 메타데이터 등록
from adapters.database import hypothesis_model  # noqa: F401 — Hypothesis 테이블 Base 메타데이터 등록
from adapters.database import owner_query_model  # noqa: F401 — OwnerQuery 테이블 Base 메타데이터 등록
from api.dependencies import AppState, ModelRegistry
from api.routes import system, trading, account, strategies, boxes, candles, techniques, analysis, monitoring, cfd, performance, wake_up_reviews, strategy_changes, strategy_analysis, paper_trades, strategy_scores, advisories, evolution
import core.shared.tunable_registry  # noqa: F401 — TunableCatalog 초기화 (side-effect import)
from core.punisher.notifications.switch_telegram import send_switch_recommendation_telegram
from core.punisher.monitoring.health import HealthChecker
from core.judge.analysis.event_filter import create_event_filter
from core.judge.analysis.intermarket import create_intermarket_client
from core.strategy.box_mean_reversion import GmoCoinBoxManager
from core.punisher.strategy.plugins.cfd_trend_following.manager import MarginTrendManager
from core.strategy.gmo_coin_trend import GmoCoinTrendManager
from core.strategy.registry import StrategyRegistry
from core.execution.regime_gate import RegimeGate
from core.strategy.snapshot_collector import SnapshotCollector
from core.strategy.switch_recommender import SwitchRecommender
from core.punisher.task.auto_reporter import create_auto_reporter
from core.punisher.task.supervisor import TaskSupervisor

logger = logging.getLogger(__name__)

# ── 거래소별 설정 ────────────────────────────────────────────

_EXCHANGE_CONFIG = {
    "gmo_coin": {
        "prefix": "gmoc",
        "pair_column": "pair",
        "order_id_length": 40,
        "env_api_key": "GMO_COIN_API_KEY",
        "env_api_secret": "GMO_COIN_API_SECRET",
        "env_base_url": "GMO_COIN_BASE_URL",
        "default_base_url": "https://api.coin.z.com",
    },
}


def _create_adapter(exchange: str):
    """GmoCoinAdapter 생성."""
    cfg = _EXCHANGE_CONFIG[exchange]
    api_key = os.environ.get(cfg["env_api_key"], "")
    api_secret = os.environ.get(cfg["env_api_secret"], "")
    base_url = os.environ.get(cfg["env_base_url"], cfg["default_base_url"])
    from adapters.gmo_coin.client import GmoCoinAdapter
    return GmoCoinAdapter(api_key=api_key, api_secret=api_secret, base_url=base_url)


def _create_models(prefix: str, pair_column: str, order_id_length: int) -> ModelRegistry:
    """프리픽스로 ORM 모델 인스턴스화."""
    return ModelRegistry(
        strategy=create_strategy_model(prefix),
        trade=create_trade_model(prefix, order_id_length=order_id_length, pair_column=pair_column),
        balance_entry=create_balance_entry_model(prefix),
        insight=create_insight_model(prefix),
        summary=create_summary_model(prefix),
        candle=create_candle_model(prefix, pair_column=pair_column),
        box=create_box_model(prefix, pair_column=pair_column),
        box_position=create_box_position_model(prefix, pair_column=pair_column, order_id_length=order_id_length),
        trend_position=create_trend_position_model(prefix, pair_column=pair_column, order_id_length=order_id_length),
        technique=StrategyTechnique,
        strategy_snapshot=create_strategy_snapshot_model(prefix),
        switch_recommendation=create_switch_recommendation_model(prefix),
    )


# ── lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """startup → yield → shutdown"""
    exchange = os.environ.get("EXCHANGE", "gmo_coin").lower()
    if exchange not in _EXCHANGE_CONFIG:
        raise ValueError(f"Unknown EXCHANGE: {exchange}. {list(_EXCHANGE_CONFIG.keys())}만 가능.")

    # 로깅 초기화 (exchange 이름 포함)
    setup_logging(exchange)

    # Telegram 로그 핸들러 초기화 (DEBUG→사만다, INFO→레이첼, WARNING+→Save Us)
    from core.logging.telegram_handlers import (
        setup_telegram_logging, shutdown_telegram_logging, seed_telegram_regime_state,
        seed_telegram_strategy_params,
    )
    await setup_telegram_logging(exchange)

    cfg = _EXCHANGE_CONFIG[exchange]
    prefix = cfg["prefix"]
    pair_column = cfg["pair_column"]

    logger.debug(f"Starting trading-engine: exchange={exchange}, prefix={prefix}")

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
    switch_recommender = SwitchRecommender(
        session_factory=session_factory,
        recommendation_model=models.switch_recommendation,
        on_recommendation=send_switch_recommendation_telegram,
    )
    snapshot_collector = SnapshotCollector(
        session_factory=session_factory,
        adapter=adapter,
        strategy_model=models.strategy,
        candle_model=models.candle,
        box_model=models.box,
        snapshot_model=models.strategy_snapshot,
        pair_column=pair_column,
        switch_recommender=switch_recommender,
    )
    trend_manager = GmoCoinTrendManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=models.candle,
        cfd_position_model=models.trend_position,
        pair_column=pair_column,
        snapshot_collector=snapshot_collector,
    )
    strategy_registry = StrategyRegistry()
    strategy_registry.register("trend_following", trend_manager)

    # 5.5-pre. GmoCoinBoxManager (박스역추세) + RegimeGate
    box_manager = GmoCoinBoxManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=session_factory,
        candle_model=models.candle,
        cfd_position_model=models.box_position,
        pair_column=pair_column,
        snapshot_collector=snapshot_collector,
    )
    strategy_registry.register("box_mean_reversion", box_manager)  # box_position은 gmoc_box_positions 사용

    # GMO Coin 단일 페어 btc_jpy → RegimeGate 1개 공유
    # TODO: 멀티 페어 지원 시 per-pair gate로 확장 필요
    from core.execution.regime_gate_persistence import load_regime_gate_state

    _regime_gate = RegimeGate("btc_jpy")
    _restored = await load_regime_gate_state(session_factory, _regime_gate)
    if _restored:
        logger.info(f"RegimeGate DB 복원 완료 (active={_regime_gate.active_strategy})")
        _gate_state = _regime_gate.to_dict()
        seed_telegram_regime_state(
            _gate_state.get("consecutive_regime"),
            _gate_state.get("consecutive_count", 0),
        )
    else:
        logger.info("RegimeGate DB 상태 없음 — warm-up 시작")
    trend_manager.set_regime_gate(_regime_gate)
    box_manager.set_regime_gate(_regime_gate)
    logger.debug("RegimeGate 초기화: trend_manager + box_manager 공유 (btc_jpy)")

    # 5.5. Execution Layer 조립 (TRADING_MODE 환경변수)
    trading_mode = os.environ.get("TRADING_MODE", "v1").lower()

    # 5.5-a. Approval Gate 조립 (TELEGRAM_APPROVAL / APPROVAL_MODE 환경변수)
    from core.execution.approval import AutoApprovalGate, TelegramApprovalGate

    _approval_gate = None
    _tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    _tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    _approval_env = os.environ.get("TELEGRAM_APPROVAL", "").lower()
    _approval_mode = os.environ.get("APPROVAL_MODE", "").lower()

    if _approval_env in ("true", "1", "yes") or _approval_mode in ("manual", "auto"):
        if _tg_token and _tg_chat:
            _timeout_sec = int(os.environ.get("TELEGRAM_APPROVAL_TIMEOUT", "300"))
            _tg_gate = TelegramApprovalGate(
                bot_token=_tg_token,
                chat_id=_tg_chat,
                timeout_sec=_timeout_sec,
            )
            if _approval_mode == "auto":
                _approval_gate = AutoApprovalGate(
                    telegram_gate=_tg_gate,
                    min_confidence=float(
                        os.environ.get("AUTO_APPROVAL_MIN_CONFIDENCE", "0.65")
                    ),
                    max_auto_size=float(
                        os.environ.get("AUTO_APPROVAL_MAX_SIZE", "0.40")
                    ),
                )
                # 실제 주문 성공 후 보고를 위해 매니저에 주입
                trend_manager.set_approval_gate(_approval_gate)
                box_manager.set_approval_gate(_approval_gate)
                logger.debug("Approval Gate: Phase B 자동 승인 (AutoApprovalGate)")
            else:
                _approval_gate = _tg_gate
                logger.debug("Approval Gate: Phase A 수동 승인 (TelegramApprovalGate)")
        else:
            logger.warning(
                "TELEGRAM_APPROVAL=true이나 TELEGRAM_BOT_TOKEN/CHAT_ID 미설정 "
                "— 승인 게이트 비활성화"
            )

    if trading_mode in ("v1", "rule_based"):
        from core.judge.decision.rule_based import RuleBasedDecision
        from core.execution.orchestrator import ExecutionOrchestrator
        from core.judge.safety.guardrails import AiGuardrails

        _guardrail = AiGuardrails(
            session_factory=session_factory,
            trade_model=models.trade,
            balance_model=models.balance_entry,
        )
        _orchestrator = ExecutionOrchestrator(
            decision_maker=RuleBasedDecision(),
            guardrail=_guardrail,
            session_factory=session_factory,
            judgment_model=AiJudgment,
            approval_gate=_approval_gate,
        )
        trend_manager.set_orchestrator(_orchestrator)
        box_manager.set_orchestrator(_orchestrator)
        logger.debug(f"Execution Layer 초기화: TRADING_MODE={trading_mode}")
    elif trading_mode in ("v2", "ai"):
        from core.judge.decision.ai_decision import AiDecision
        from core.judge.decision.llm_client import OpenAiLlmClient
        from core.execution.orchestrator import ExecutionOrchestrator
        from core.judge.safety.guardrails import AiGuardrails

        _openai_key = os.environ.get("OPENAI_API_KEY")
        if not _openai_key:
            raise RuntimeError("TRADING_MODE=v2 requires OPENAI_API_KEY")

        _llm = OpenAiLlmClient(
            api_key=_openai_key,
            default_model=os.environ.get("AI_DEFAULT_MODEL", "gpt-4o-mini"),
        )
        _ai_decision = AiDecision(
            llm_client=_llm,
            alice_model=os.environ.get("AI_ALICE_MODEL"),
            samantha_model=os.environ.get("AI_SAMANTHA_MODEL"),
            rachel_model=os.environ.get("AI_RACHEL_MODEL"),
        )
        _guardrail = AiGuardrails(
            session_factory=session_factory,
            trade_model=models.trade,
            balance_model=models.balance_entry,
        )
        _orchestrator = ExecutionOrchestrator(
            decision_maker=_ai_decision,
            guardrail=_guardrail,
            session_factory=session_factory,
            judgment_model=AiJudgment,
            approval_gate=_approval_gate,
        )
        trend_manager.set_orchestrator(_orchestrator)
        box_manager.set_orchestrator(_orchestrator)
        logger.debug(f"Execution Layer 초기화: TRADING_MODE={trading_mode} [DEPRECATED: v2/ai는 jit 모드로 전환 권장]")
    elif trading_mode == "jit":
        # JIT advisory: 룰엔진 판단 후 진입 직전 LLM 단발 자문
        from core.judge.jit_advisory import JITAdvisoryGate
        from core.judge.jit_advisory.client import JITAdvisoryClient
        from core.execution.orchestrator import ExecutionOrchestrator
        from core.judge.safety.guardrails import AiGuardrails

        _jit_url = os.environ.get("JIT_ADVISORY_URL", "http://host.docker.internal:18793/v1/responses")
        _jit_token = os.environ.get("JIT_ADVISORY_TOKEN", "")
        _jit_timeout = float(os.environ.get("JIT_TIMEOUT_SEC", "20"))

        _jit_client = JITAdvisoryClient(
            url=_jit_url,
            token=_jit_token,
            timeout_sec=_jit_timeout,
        )
        _jit_gate = JITAdvisoryGate(
            session_factory=session_factory,
            jit_client=_jit_client,
        )
        _guardrail = AiGuardrails(
            session_factory=session_factory,
            trade_model=models.trade,
            balance_model=models.balance_entry,
        )
        _orchestrator = ExecutionOrchestrator(
            decision_maker=_jit_gate,
            guardrail=_guardrail,
            session_factory=session_factory,
            judgment_model=AiJudgment,
            approval_gate=_approval_gate,
        )
        trend_manager.set_orchestrator(_orchestrator)
        box_manager.set_orchestrator(_orchestrator)
        logger.info(
            f"Execution Layer 초기화: TRADING_MODE=jit — "
            f"JIT advisory URL={_jit_url}, timeout={_jit_timeout}s"
        )
    else:
        logger.warning(
            f"TRADING_MODE={trading_mode!r} 미지원. 기본값 v1을 사용합니다."
        )
        from core.judge.decision.rule_based import RuleBasedDecision
        from core.execution.orchestrator import ExecutionOrchestrator
        from core.judge.safety.guardrails import AiGuardrails

        _guardrail = AiGuardrails(
            session_factory=session_factory,
            trade_model=models.trade,
            balance_model=models.balance_entry,
        )
        _orchestrator = ExecutionOrchestrator(
            decision_maker=RuleBasedDecision(),
            guardrail=_guardrail,
            session_factory=session_factory,
            judgment_model=AiJudgment,
            approval_gate=_approval_gate,
        )
        trend_manager.set_orchestrator(_orchestrator)
        box_manager.set_orchestrator(_orchestrator)

    # 5.6. Data Layer (DataHub v1.5)
    from core.data.hub import DataHub
    from adapters.database.models import WakeUpReview

    _trading_data_url = os.environ.get("TRADING_DATA_URL", "http://trading-data:8002")
    _data_hub = DataHub(
        session_factory=session_factory,
        adapter=adapter,
        candle_model=models.candle,
        pair_column=pair_column,
        positions=trend_manager._position,
        trading_data_url=_trading_data_url,
        lesson_model=WakeUpReview,
    )
    trend_manager.set_data_hub(_data_hub)
    box_manager.set_data_hub(_data_hub)
    logger.debug(f"DataHub v1.5 초기화: trading_data_url={_trading_data_url}")

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
        _strategy_start_count = 0   # 전략 기동 순서 카운터 (오프셋 계산용)
        _CANDLE_POLL_SEC = 60       # candle_monitor 폴링 주기
        _CANDLE_START_OFFSET_SEC = 30  # 전략 간 candle_monitor 기동 오프셋 (초)

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
                if style in ("cfd_trend_following", "trend_following"):
                    trend_manager.register_paper_pair(pair, strategy.id)
                proposed_count += 1
                logger.debug(
                    f"Paper trading 시작: strategy_id={strategy.id} pair={pair} style={style} "
                    f"({proposed_count}/{_PAPER_TRADING_HARDCAP})"
                )

            if not await strategy_registry.start_strategy(
                style, pair, start_params,
                initial_delay_sec=(_strategy_start_count * _CANDLE_START_OFFSET_SEC) % _CANDLE_POLL_SEC,
            ):
                logger.warning(f"미등록 전략 스타일: {style} (pair={pair})")
            else:
                _strategy_start_count += 1
                if style in ("cfd_trend_following", "trend_following"):
                    # 주요 전략 파라미터를 텔레그램 핸들러에 주입해 RSI 표시 범위를 동기화
                    # cfd_trend_following은 trend_following의 구식 이름 (하위 호환)
                    seed_telegram_strategy_params(params)
    except Exception as e:
        logger.warning(f"활성 전략 자동 기동 실패 (DB 없으면 정상): {e}")

    # 9. AutoReporter (환경변수로 ON/OFF)
    auto_reporter = create_auto_reporter(session_factory, state)
    if auto_reporter:
        await auto_reporter.start()

    # 10. PostAnalyzer (ENABLE_POST_ANALYSIS=true + OPENAI_API_KEY 필요)
    _enable_post_analysis = os.environ.get("ENABLE_POST_ANALYSIS", "").lower() in ("true", "1", "yes")
    if _enable_post_analysis:
        _openai_key_pa = os.environ.get("OPENAI_API_KEY")
        if _openai_key_pa:
            from core.judge.decision.llm_client import OpenAiLlmClient
            from core.punisher.learning.post_analyzer import PostAnalyzer
            _pa_llm = OpenAiLlmClient(
                api_key=_openai_key_pa,
                default_model=os.environ.get("POST_ANALYSIS_MODEL", "gpt-4o-mini"),
            )
            _post_analyzer = PostAnalyzer(
                llm_client=_pa_llm,
                session_factory=session_factory,
                judgment_model=AiJudgment,
            )
            trend_manager.set_post_analyzer(_post_analyzer)
            logger.debug("PostAnalyzer 초기화 완료 (ENABLE_POST_ANALYSIS=true)")
        else:
            logger.warning("ENABLE_POST_ANALYSIS=true이나 OPENAI_API_KEY 미설정 — PostAnalyzer 비활성화")

    # 11. EventDetector (ENABLE_EVENT_DETECTOR=true)
    _enable_event_detector = os.environ.get("ENABLE_EVENT_DETECTOR", "").lower() in ("true", "1", "yes")
    _event_detector = None
    _active_pairs: list[str] = []
    if _enable_event_detector and _data_hub is not None:
        _active_pairs: list[str] = []
        try:
            from sqlalchemy import select as sa_select
            async with session_factory() as _db:
                _strats = (await _db.execute(
                    sa_select(models.strategy).where(models.strategy.status == "active")
                )).scalars().all()
                for _s in _strats:
                    _p = (_s.parameters or {}).get("pair") or (_s.parameters or {}).get("product_code")
                    if _p:
                        _active_pairs.append(_p)
        except Exception as _e:
            logger.warning(f"EventDetector: 활성 pair 조회 실패 — {_e}")
        _advisory_base_url = os.environ.get("SELF_BASE_URL", f"http://localhost:{os.environ.get('PORT', '8001')}")
        from core.judge.monitoring.event_detector import EventDetector
        _event_detector = EventDetector(
            data_hub=_data_hub,
            advisory_base_url=_advisory_base_url,
            exchange=exchange,
            pairs=_active_pairs,
        )
        await _event_detector.start()
        logger.debug(f"EventDetector 시작 (pairs={_active_pairs})")

    # 12. DailyBriefing (ENABLE_DAILY_BRIEFING=true)
    _enable_daily_briefing = os.environ.get("ENABLE_DAILY_BRIEFING", "").lower() in ("true", "1", "yes")
    _daily_briefing = None
    if _enable_daily_briefing:
        _tg_token_db = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        _tg_chat_db = os.environ.get("TELEGRAM_CHAT_ID", "")
        if _tg_token_db and _tg_chat_db:
            from core.punisher.monitoring.daily_briefing import DailyBriefing
            _daily_briefing = DailyBriefing(
                session_factory=session_factory,
                trade_model=models.trade,
                pairs=_active_pairs,
                bot_token=_tg_token_db,
                chat_id=_tg_chat_db,
                adapter=adapter,
            )
            await _daily_briefing.start()
            logger.debug("DailyBriefing 시작 (09:00 JST 스케줄)")
        else:
            logger.warning("ENABLE_DAILY_BRIEFING=true이나 TELEGRAM_BOT_TOKEN/CHAT_ID 미설정 — 비활성화")

    logger.debug("Application startup complete")

    # === P6: CanaryMonitor 시작 ===
    from core.judge.evolution.canary_monitor import get_canary_monitor
    from adapters.database.hypothesis_model import Hypothesis
    from sqlalchemy import select as _sa_select
    _canary_monitor = get_canary_monitor()
    _canary_monitor._session_factory = session_factory  # session_factory 주입
    async with session_factory() as _cm_db:
        _active_canaries = (
            await _cm_db.execute(_sa_select(Hypothesis).where(Hypothesis.status == "canary"))
        ).scalars().all()
        for _ch in _active_canaries:
            await _canary_monitor._resolve_start_balance(_cm_db, _ch)
            logger.info("Canary 가설 복원: %s", _ch.id)
    await _canary_monitor.start()

    yield

    # === Shutdown ===
    logger.info("Shutting down trading-engine...")
    await _canary_monitor.stop()
    # Telegram 로그 핸들러 정리 (잔여 버퍼 전송)
    await shutdown_telegram_logging()
    if auto_reporter:
        await auto_reporter.stop()
    if _event_detector is not None:
        await _event_detector.stop()
    if _daily_briefing is not None:
        await _daily_briefing.stop()
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
app.include_router(strategy_scores.router)
app.include_router(advisories.router)
app.include_router(evolution.router)
