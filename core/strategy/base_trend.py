"""
BaseTrendManager — 추세추종 전략 공통 베이스 클래스.

MRO: CandleLoopMixin → JudgeMixin → ExecutionMixin → ABC

이 파일은 __init__()와 라이프사이클(start/stop/is_running) + 공유 유틸만 보유.

- 판단 도메인 (시그널·DTO) → core/strategy/_judge_mixin.py      [져지 소유]
- 실행 도메인 (주문·청산·학습) → core/strategy/_execution_mixin.py [퍼니셔 소유]
- 루프 골격 (캔들·스탑로스) → core/strategy/_candle_loop.py      [접합점·아키 조율]

서브클래스가 override해야 할 메서드 (abstract):
    - _detect_existing_position
    - _sync_position_state
    - _open_position
    - _close_position_impl  ← (구: _close_position)
    - _apply_stop_tightening
    - _record_open
    - _record_close
    - _get_entry_side (진입 시그널에서 side 결정)
    - _is_stop_triggered (스탑로스 방향 체크)

Paper Trading 지원:
    - register_paper_pair(pair, strategy_id): proposed pair 등록 → PaperExecutor 바인딩
    - _try_paper_entry(): 진입 전 paper 분기 (True 반환 시 실주문 스킵)
    - _close_position(): concrete wrapper — paper pair면 paper exit 처리 후 return
    - active pair는 _paper_executors에 없으므로 기존 동작 100% 유지
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.data.hub import IDataHub
from core.exchange.base import ExchangeAdapter
from core.exchange.types import Position
from core.execution.orchestrator import ExecutionOrchestrator
from core.task.supervisor import TaskSupervisor
from core.strategy._candle_loop import CandleLoopMixin
from core.strategy._judge_mixin import JudgeMixin
from core.strategy._execution_mixin import ExecutionMixin

logger = logging.getLogger(__name__)


class BaseTrendManager(CandleLoopMixin, JudgeMixin, ExecutionMixin, ABC):
    """추세추종 공통 베이스.

    MRO: CandleLoopMixin → JudgeMixin → ExecutionMixin → ABC

    이 파일은 __init__()와 라이프사이클(start/stop/is_running) +
    공유 유틸(register_paper_pair, set_orchestrator 등)만 보유한다.
    """

    # 서브클래스에서 설정
    _task_prefix: str = "trend"      # "trend" or "cfd"
    _log_prefix: str = "[TrendMgr]"  # "[TrendMgr]" or "[CfdMgr]"
    _supports_short: bool = False     # 숏 진입 지원 여부. CfdTrendFollowingManager만 True

    def __init__(
        self,
        adapter: ExchangeAdapter,
        supervisor: TaskSupervisor,
        session_factory: async_sessionmaker[AsyncSession],
        candle_model: Type,
        position_model: Type,
        pair_column: str = "pair",
        position_pair_column: Optional[str] = None,
        snapshot_collector: Optional[Any] = None,
    ) -> None:
        self._adapter = adapter
        self._supervisor = supervisor
        self._session_factory = session_factory
        self._candle_model = candle_model
        self._position_model = position_model
        self._pair_column = pair_column
        self._position_pair_column = position_pair_column or pair_column
        self._snapshot_collector: Optional[Any] = snapshot_collector  # P-1 트리거 훅

        # pair별 상태
        self._params: Dict[str, Dict] = {}
        self._position: Dict[str, Optional[Position]] = {}
        self._latest_price: Dict[str, float] = {}
        self._last_seen_open_time: Dict[str, Optional[str]] = {}
        self._ema_slope_history: Dict[str, List[float]] = {}
        self._ema_slope_last_key: Dict[str, Optional[str]] = {}

        # 스탑로스 실패 백오프
        self._close_fail_count: Dict[str, int] = {}
        self._close_fail_until: Dict[str, float] = {}
        # 정합성 검사 카운터 (30사이클=30분마다)
        self._sync_counter: Dict[str, int] = {}

        # Paper Trading — pair 레벨 분리 (active pair 영향 0)
        self._paper_executors: Dict[str, Any] = {}   # pair → PaperExecutor
        self._paper_positions: Dict[str, dict] = {}  # pair → {paper_trade_id, entry_price, direction}

        # Limit Order 대기 상태: pair → PendingLimitOrder
        self._pending_limit_orders: Dict[str, Any] = {}

        # 시그널 변경 감지용 (동일 시그널 반복 출력 억제)
        self._last_signal: Dict[str, str] = {}

        # Execution Layer 연결 (Step 4)
        self._orchestrator: Optional[ExecutionOrchestrator] = None
        # Data Layer 연결 (v1.5)
        self._data_hub: Optional[IDataHub] = None
        # 사후 분석 (ENABLE_POST_ANALYSIS=true 시 주입)
        self._post_analyzer: Optional[Any] = None
        # Regime Gate (듀얼 매니저 체제 전환)
        self._regime_gate: Optional[Any] = None  # RegimeGate | None

    # ──────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────

    async def start(self, pair: str, params: Dict) -> None:
        """pair에 대한 추세추종 태스크 2개 등록."""
        self._params[pair] = params
        self._last_seen_open_time[pair] = None
        self._latest_price.pop(pair, None)
        self._ema_slope_history[pair] = []
        self._ema_slope_last_key[pair] = None
        self._close_fail_count[pair] = 0
        self._close_fail_until[pair] = 0
        self._last_signal[pair] = ""

        pos = await self._detect_existing_position(pair)
        self._position[pair] = pos
        if pos:
            pos.db_record_id = await self._recover_db_position_id(pair)

        prefix = self._task_prefix
        await self._supervisor.register(
            f"{prefix}_candle:{pair}",
            lambda p=pair: self._candle_monitor(p),
            max_restarts=5,
        )
        await self._supervisor.register(
            f"{prefix}_stoploss:{pair}",
            lambda p=pair: self._stop_loss_monitor(p),
            max_restarts=5,
        )

        logger.debug(
            f"{self._log_prefix} {pair}: 추세추종 시작 "
            f"(position={'있음' if pos else '없음'}, exchange={self._adapter.exchange_name})"
        )

    async def stop(self, pair: str) -> None:
        """pair에 대한 태스크 종료."""
        prefix = self._task_prefix
        await self._supervisor.stop(f"{prefix}_candle:{pair}")
        await self._supervisor.stop(f"{prefix}_stoploss:{pair}")
        self._params.pop(pair, None)
        self._position.pop(pair, None)
        self._last_seen_open_time.pop(pair, None)
        self._latest_price.pop(pair, None)
        self._ema_slope_history.pop(pair, None)
        self._ema_slope_last_key.pop(pair, None)
        self._close_fail_count.pop(pair, None)
        self._close_fail_until.pop(pair, None)
        logger.debug(f"{self._log_prefix} {pair}: 추세추종 태스크 종료")

    async def stop_all(self) -> None:
        for p in list(self._params.keys()):
            await self.stop(p)
        logger.debug(f"{self._log_prefix} 전체 추세추종 인프라 종료")

    def is_running(self, pair: str) -> bool:
        prefix = self._task_prefix
        return (
            self._supervisor.is_running(f"{prefix}_candle:{pair}")
            or self._supervisor.is_running(f"{prefix}_stoploss:{pair}")
        )

    def running_pairs(self) -> list[str]:
        return [p for p in self._params if self.is_running(p)]

    def register_paper_pair(self, pair: str, strategy_id: int) -> None:
        """proposed pair에 PaperExecutor를 바인딩한다. active pair에는 호출하지 않는다."""
        from core.execution.executor import PaperExecutor
        self._paper_executors[pair] = PaperExecutor(self._session_factory, strategy_id)
        logger.debug(
            f"{self._log_prefix} {pair}: PaperExecutor 등록 (strategy_id={strategy_id})"
        )

    def unregister_paper_pair(self, pair: str) -> None:
        """Paper 등록 해제. 추천 승인/pair 전환 시 호출."""
        self._paper_executors.pop(pair, None)
        self._paper_positions.pop(pair, None)
        logger.debug(f"{self._log_prefix} {pair}: PaperExecutor 해제")

    def set_orchestrator(self, orchestrator: ExecutionOrchestrator) -> None:
        """ExecutionOrchestrator를 주입한다. main.py lifespan에서 호출."""
        self._orchestrator = orchestrator

    def set_data_hub(self, hub: IDataHub) -> None:
        """IDataHub를 주입한다. main.py lifespan에서 호출."""
        self._data_hub = hub

    def set_post_analyzer(self, analyzer: Any) -> None:
        """PostAnalyzer를 주입한다. ENABLE_POST_ANALYSIS=true 시 main.py에서 호출."""
        self._post_analyzer = analyzer

    def set_regime_gate(self, gate: Any) -> None:
        """RegimeGate를 주입한다. main.py lifespan에서 양쪽 매니저에 동일 인스턴스 주입."""
        self._regime_gate = gate

    def get_position(self, pair: str) -> Optional[Position]:
        return self._position.get(pair)

    def get_task_health(self) -> dict:
        prefix = self._task_prefix
        result: Dict[str, dict] = {}
        for pair in self._params:
            result[pair] = {
                "candle_monitor": self._supervisor.get_health().get(f"{prefix}_candle:{pair}", {}),
                "stop_loss_monitor": self._supervisor.get_health().get(f"{prefix}_stoploss:{pair}", {}),
            }
        return result

    # ──────────────────────────────────────────
    # DB 포지션 복원 (공통)
    # ──────────────────────────────────────────

    async def _recover_db_position_id(self, pair: str) -> Optional[int]:
        """열린 DB 포지션 레코드 ID + stop_loss_price 복원."""
        try:
            Model = self._position_model
            pair_col = getattr(Model, self._position_pair_column)
            async with self._session_factory() as db:
                result = await db.execute(
                    select(Model)
                    .where(pair_col == pair, Model.status == "open")
                    .order_by(Model.created_at.desc())
                    .limit(1)
                )
                rec = result.scalars().first()
                if rec:
                    # stop_loss_price 복원 — 재기동 직후 스탑 공백 방지
                    pos = self._position.get(pair)
                    if pos:
                        if hasattr(rec, "stop_loss_price") and rec.stop_loss_price is not None:
                            pos.stop_loss_price = float(rec.stop_loss_price)
                            logger.info(
                                f"{self._log_prefix} {pair}: DB 스탑 복원 ¥{rec.stop_loss_price:.0f}"
                            )
                        if pos.entry_price is None and hasattr(rec, "entry_price") and rec.entry_price is not None:
                            pos.entry_price = float(rec.entry_price)
                    logger.info(f"{self._log_prefix} {pair}: DB 포지션 레코드 복원 id={rec.id}")
                    return rec.id
        except Exception as e:
            logger.warning(f"{self._log_prefix} {pair}: DB 포지션 ID 복원 실패 — {e}")
        return None

    # ──────────────────────────────────────────
    # 공유 유틸 (기본 구현, 서브클래스 override 가능)
    # ──────────────────────────────────────────

    def _is_stop_triggered(self, pos: Position, price: float, stop_loss_price: float) -> bool:
        """스탑로스 발동 여부. 기본: 롱(price <= stop)."""
        return price <= stop_loss_price

    def _get_strategy_type(self) -> str:
        """이 매니저의 전략 타입 반환. RegimeGate.should_allow_entry() 인자로 사용.

        서브클래스가 override해야 한다.
        기본값 "trend_following" — override 없는 서브클래스는 RegimeGate 없이 동작하는
        기존 추세추종 매니저와 동일하게 취급된다.
        """
        return "trend_following"

    async def _pre_entry_checks(self, pair: str, side: str, params: Dict) -> bool:
        """진입 전 추가 검사. 서브클래스에서 오버라이드.

        Returns:
            True = 진입 허용, False = 차단
        """
        return True

    async def _add_to_position(
        self, pair: str, side: str, price: float,
        atr: Optional[float], params: Dict, *, result: Any = None
    ) -> None:
        """피라미딩 추가 매수. 서브클래스에서 구현.

        기본: WARNING 로그만 출력 (GmoCoinTrendManager에서 override).
        """
        logger.warning(
            f"{self._log_prefix} {pair}: _add_to_position 미구현 — 서브클래스 override 필요"
        )

    # ──────────────────────────────────────────
    # Abstract — 서브클래스 필수 구현
    # ──────────────────────────────────────────

    @abstractmethod
    async def _detect_existing_position(self, pair: str) -> Optional[Position]:
        """재시작 시 기존 포지션 감지."""
        ...

    @abstractmethod
    async def _sync_position_state(self, pair: str) -> None:
        """실잔고/실포지션과 인메모리 비교 → 갱신."""
        ...

    @abstractmethod
    async def _open_position(
        self, pair: str, side: str, price: float, atr: Optional[float], params: Dict,
        *, signal_data: dict | None = None
    ) -> None:
        """진입 주문 실행."""
        ...

    @abstractmethod
    async def _close_position_impl(self, pair: str, reason: str) -> None:
        """실거래소 청산 주문 실행. (구: _close_position)"""
        ...

    @abstractmethod
    async def _apply_stop_tightening(
        self, pair: str, current_price: float, atr: float, params: dict
    ) -> None:
        """스탑 타이트닝."""
        ...

    @abstractmethod
    async def _record_open(self, **kwargs) -> Optional[int]:
        """진입 DB 기록."""
        ...

    @abstractmethod
    async def _record_close(self, **kwargs) -> None:
        """청산 DB 기록."""
        ...
