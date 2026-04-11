"""
BaseTrendManager — 추세추종 전략 공통 베이스 클래스.

현물(TrendFollowingManager)과 CFD(CfdTrendFollowingManager)가 공유하는
태스크 관리·시그널 계산·스탑로스 모니터·캔들 모니터 골격을 정의한다.

서브클래스가 override해야 할 메서드 (abstract):
    - _detect_existing_position
    - _sync_position_state
    - _open_position
    - _close_position_impl  ← (구: _close_position)
    - _apply_stop_tightening
    - _record_open
    - _record_close
    - _on_candle_extra_checks (보유시간/keep_rate 등)
    - _get_entry_side (진입 시그널에서 side 결정)
    - _is_stop_triggered (스탑로스 방향 체크)

Paper Trading 지원:
    - register_paper_pair(pair, strategy_id): proposed pair 등록 → PaperExecutor 바인딩
    - _try_paper_entry(): 진입 전 paper 분기 (True 반환 시 실주문 스킵)
    - _close_position(): concrete wrapper — paper pair면 paper exit 처리 후 return
    - active pair는 _paper_executors에 없으므로 기존 동작 100% 유지
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.data.dto import PositionDTO, SignalSnapshot
from core.data.hub import IDataHub
from core.exchange.base import ExchangeAdapter
from core.exchange.types import OrderType, Position
from core.execution.orchestrator import ExecutionOrchestrator
from core.strategy.signals import (
    compute_adaptive_trailing_mult,
    compute_trend_signal,
    detect_bearish_divergences,
)
from core.analysis.session_filter import is_allowed_session, is_london_open_blackout
from core.task.supervisor import TaskSupervisor

logger = logging.getLogger(__name__)

_CANDLE_POLL_INTERVAL = 60  # 초


class BaseTrendManager(ABC):
    """추세추종 공통 베이스. 서브클래스에서 거래소/상품별 로직을 구현한다."""

    # 서브클래스에서 설정
    _task_prefix: str = "trend"      # "trend" or "cfd"
    _log_prefix: str = "[TrendMgr]"  # "[TrendMgr]" or "[CfdMgr]"

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
        # 정합성 검사 카운터 (5사이클=5분마다)
        self._sync_counter: Dict[str, int] = {}

        # Paper Trading — pair 레벨 분리 (active pair 영향 0)
        self._paper_executors: Dict[str, Any] = {}   # pair → PaperExecutor
        self._paper_positions: Dict[str, dict] = {}  # pair → {paper_trade_id, entry_price, direction}

        # Limit Order 대기 상태: pair → PendingLimitOrder
        self._pending_limit_orders: Dict[str, Any] = {}

        # Execution Layer 연결 (Step 4)
        self._orchestrator: Optional[ExecutionOrchestrator] = None
        # Data Layer 연결 (v1.5)
        self._data_hub: Optional[IDataHub] = None
        # 사후 분석 (ENABLE_POST_ANALYSIS=true 시 주입)
        self._post_analyzer: Optional[Any] = None

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

    async def _try_paper_entry(
        self, pair: str, direction: str, current_price: float,
        atr: Optional[float], params: Dict,
    ) -> bool:
        """Paper 모드 진입 처리. paper pair가 아니면 False 반환(실주문 진행).

        True 반환 시 _open_position 호출 스킵.
        인메모리 Position을 생성해 stop_loss_monitor가 동작하도록 유지한다.
        """
        paper_exec = self._paper_executors.get(pair)
        if paper_exec is None:
            return False

        strategy_id = params.get("strategy_id", 0)
        paper_id = await paper_exec.record_paper_entry(
            strategy_id=strategy_id,
            pair=pair,
            direction=direction,
            entry_price=current_price,
        )
        if paper_id is None:
            return False  # 기록 실패 시 실주문 진행 (안전 방향)

        # 인메모리 포지션 생성 (스탑로스 모니터 유지 + 트레일링 스탑 동작)
        atr_mult = float(params.get("atr_multiplier_stop", 2.0))
        if direction == "sell":
            initial_sl = round(current_price + atr * atr_mult, 6) if atr else None
        else:
            initial_sl = round(current_price - atr * atr_mult, 6) if atr else None
        self._position[pair] = Position(
            pair=pair,
            entry_price=current_price,
            entry_amount=0.0,  # paper: 실수량 없음
            stop_loss_price=initial_sl,
        )
        self._paper_positions[pair] = {
            "paper_trade_id": paper_id,
            "entry_price": current_price,
            "direction": direction,
        }
        logger.info(
            f"{self._log_prefix} {pair}: Paper 진입 기록 id={paper_id} "
            f"direction={direction} price={current_price}"
        )
        return True

    def set_orchestrator(self, orchestrator: ExecutionOrchestrator) -> None:
        """ExecutionOrchestrator를 주입한다. main.py lifespan에서 호출."""
        self._orchestrator = orchestrator

    def set_data_hub(self, hub: IDataHub) -> None:
        """IDataHub를 주입한다. main.py lifespan에서 호출."""
        self._data_hub = hub

    def set_post_analyzer(self, analyzer: Any) -> None:
        """PostAnalyzer를 주입한다. ENABLE_POST_ANALYSIS=true 시 main.py에서 호출."""
        self._post_analyzer = analyzer

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
    # 시그널 계산 (공통)
    # ──────────────────────────────────────────

    async def _compute_signal(
        self, pair: str, timeframe: str,
        entry_price: Optional[float] = None,
        params: Optional[dict] = None,
        side: Optional[str] = None,
        include_incomplete: bool = False,
    ) -> Optional[dict]:
        ema_period, atr_period = 20, 14
        limit = max(ema_period * 2, atr_period + 1, int((params or {}).get("divergence_lookback", 40)))

        CandleModel = self._candle_model
        pair_col = getattr(CandleModel, self._pair_column)

        async with self._session_factory() as db:
            result = await db.execute(
                select(CandleModel)
                .where(
                    and_(
                        pair_col == pair,
                        CandleModel.timeframe == timeframe,
                        CandleModel.is_complete == True,  # noqa: E712
                    )
                )
                .order_by(CandleModel.open_time.desc())
                .limit(limit)
            )
            candles = list(reversed(result.scalars().all()))

        # include_incomplete=True: 미완성 캔들 1개를 추가로 조회해 맨 끝에 붙인다
        incomplete_candle = None
        if include_incomplete:
            async with self._session_factory() as db:
                result = await db.execute(
                    select(CandleModel)
                    .where(
                        and_(
                            pair_col == pair,
                            CandleModel.timeframe == timeframe,
                            CandleModel.is_complete == False,  # noqa: E712
                        )
                    )
                    .order_by(CandleModel.open_time.desc())
                    .limit(1)
                )
                incomplete_candle = result.scalars().first()
            if incomplete_candle is not None:
                candles = candles + [incomplete_candle]

        if len(candles) < ema_period + 1:
            logger.debug(f"{self._log_prefix} {pair}: 캔들 부족 ({len(candles)}개)")
            return None

        kwargs: dict[str, Any] = {"params": params or {}, "entry_price": entry_price}
        if side is not None:
            kwargs["side"] = side
        result = compute_trend_signal(candles, **kwargs)
        if result is not None:
            result["latest_candle_open_time"] = str(candles[-1].open_time)
            result["candles"] = candles
            if include_incomplete and incomplete_candle is not None:
                result["has_incomplete"] = True
                result["incomplete_candle"] = incomplete_candle
        return result

    # ──────────────────────────────────────────
    # 트레일링 스탑 DB 갱신 (공통)
    # ──────────────────────────────────────────

    async def _update_trailing_stop_in_db(self, pair: str, stop_loss_price: float) -> None:
        try:
            pos = self._position.get(pair)
            if pos is None or pos.db_record_id is None:
                return
            Model = self._position_model
            async with self._session_factory() as db:
                result = await db.execute(
                    select(Model).where(Model.id == pos.db_record_id)
                )
                rec = result.scalars().first()
                if rec:
                    rec.stop_loss_price = stop_loss_price
                    await db.commit()
        except Exception as e:
            logger.warning(f"{self._log_prefix} {pair}: DB 트레일링 스탑 갱신 실패 — {e}")

    # ──────────────────────────────────────────
    # Task 2: 스탑로스 모니터 (공통 골격)
    # ──────────────────────────────────────────

    async def _stop_loss_monitor(self, pair: str) -> None:
        """WS 실시간 체결가 → 스탑로스 이탈 시 청산."""
        price_queue: asyncio.Queue[float] = asyncio.Queue()

        async def _on_trade(price: float, amount: float) -> None:
            await price_queue.put(price)

        await self._adapter.subscribe_trades(pair, _on_trade)

        try:
            while True:
                try:
                    price = await asyncio.wait_for(price_queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    continue

                pos = self._position.get(pair)
                if pos is None:
                    continue

                stop_loss_price = pos.stop_loss_price
                if stop_loss_price is None:
                    continue

                self._latest_price[pair] = price

                if not self._is_stop_triggered(pos, price, stop_loss_price):
                    continue

                cooldown_until = self._close_fail_until.get(pair, 0)
                if time.time() < cooldown_until:
                    continue

                logger.info(
                    f"{self._log_prefix} {pair}: 하드 스탑로스 발동 "
                    f"현재가 ¥{price} / 스탑 ¥{stop_loss_price}"
                )
                await self._close_position(pair, "stop_loss")

                if self._position.get(pair) is None:
                    self._close_fail_count[pair] = 0
                    self._close_fail_until[pair] = 0
                else:
                    fail_count = self._close_fail_count.get(pair, 0) + 1
                    self._close_fail_count[pair] = fail_count
                    if fail_count % 5 == 0:
                        self._close_fail_until[pair] = time.time() + 60
                        logger.warning(
                            f"{self._log_prefix} {pair}: 청산 {fail_count}회 실패 — 60초 쿨다운"
                        )
        except asyncio.CancelledError:
            raise

    # ──────────────────────────────────────────
    # Task 1: 캔들 모니터 (공통 골격)
    # ──────────────────────────────────────────

    async def _candle_monitor(self, pair: str) -> None:
        """60초마다 시그널 재계산 → 진입/청산/트레일링."""
        while True:
            await asyncio.sleep(_CANDLE_POLL_INTERVAL)

            params = self._params.get(pair, {})
            basis_tf = params.get("basis_timeframe", "4h")
            pos = self._position.get(pair)
            entry_price = pos.entry_price if pos else None

            # 5사이클(5분)마다 정합성 검사
            if pos is not None:
                cnt = self._sync_counter.get(pair, 0) + 1
                self._sync_counter[pair] = cnt
                # Paper pair는 실잔고 조회 스킵 (entry_amount=0 → ZeroDivisionError 방지)
                if cnt % 5 == 0 and pair not in self._paper_executors:
                    await self._sync_position_state(pair)

            # 서브클래스 추가 체크 (keep_rate, 보유시간 등)
            should_continue = await self._on_candle_extra_checks(pair, params)
            if not should_continue:
                continue
            # 추가 체크로 포지션이 청산됐을 수 있음
            pos = self._position.get(pair)
            entry_price = pos.entry_price if pos else None

            try:
                signal_data = await self._compute_signal(
                    pair, basis_tf,
                    entry_price=entry_price,
                    params=params,
                    side=pos.extra.get("side") if pos else None,
                )
            except Exception as e:
                logger.warning(f"{self._log_prefix} {pair}: 시그널 계산 실패 — {e}")
                continue

            if signal_data is None:
                continue

            signal = signal_data["signal"]
            current_price = signal_data["current_price"]
            atr = signal_data.get("atr")
            ema = signal_data.get("ema")
            ema_slope_pct = signal_data.get("ema_slope_pct")
            rsi = signal_data.get("rsi")
            exit_signal = signal_data.get("exit_signal", {})
            exit_action = exit_signal.get("action", "hold")
            latest_candle_key = signal_data.get("latest_candle_open_time")

            # 서브클래스 시그널 후처리 (RSI 캐시, exit_warning 보정 등)
            signal = self._on_signal_computed(pair, signal, signal_data, pos)

            # 실시간 가격으로 exit_warning 보정
            realtime_price = self._latest_price.get(pair)
            if realtime_price is not None and ema is not None:
                signal = self._check_exit_warning(pair, signal, realtime_price, ema, pos)

            # 시그널 로그: hold=DEBUG, 그 외(매매 이벤트)=INFO
            _sig_log = logger.debug if signal == "hold" else logger.info
            _sig_log(
                f"{self._log_prefix} {pair}: signal={signal} exit={exit_action} "
                f"price={current_price} pos={'있음' if pos else '없음'}"
            )

            # ── Pending Limit Order 체결 확인 ──
            pending = self._pending_limit_orders.get(pair)
            if pending is not None:
                pl_continue = await self._check_pending_limit_order(pair, pending, signal, params)
                if pl_continue:
                    continue

            # ── EMA 기울기 이력 + 다이버전스 ──
            if latest_candle_key != self._ema_slope_last_key.get(pair):
                # 새 4H 캔들 완성: 프리뷰 진입 검증
                cur_pos = self._position.get(pair)
                if cur_pos is not None and cur_pos.extra.get("preview_entry"):
                    self._ema_slope_last_key[pair] = latest_candle_key
                    if signal == "entry_ok":
                        cur_pos.extra.pop("preview_entry")
                        logger.info(
                            f"{self._log_prefix} {pair}: 프리뷰 진입 확인 — signal=entry_ok → 정상 포지션 전환"
                        )
                    else:
                        logger.warning(
                            f"{self._log_prefix} {pair}: 프리뷰 오판 보호 — signal={signal} → 즉시 청산"
                        )
                        await self._close_position(pair, "preview_invalidated")
                        continue

                slope_history = self._ema_slope_history.setdefault(pair, [])
                slope_history.append(ema_slope_pct)
                if len(slope_history) > 3:
                    slope_history.pop(0)
                self._ema_slope_last_key[pair] = latest_candle_key

                if (
                    len(slope_history) == 3
                    and all(s is not None for s in slope_history)
                    and slope_history[0] > slope_history[1] > slope_history[2]
                    and pos is not None
                    and not pos.stop_tightened
                    and atr is not None
                ):
                    logger.info(
                        f"{self._log_prefix} {pair}: EMA 기울기 3캔들 연속 하락 → 스탑 타이트닝"
                    )
                    await self._apply_stop_tightening(pair, current_price, atr, params)

                if (
                    pos is not None
                    and params.get("divergence_enabled", True)
                    and not pos.stop_tightened
                    and atr is not None
                ):
                    div_candles = signal_data.get("candles", [])
                    rsi_series = signal_data.get("rsi_series", [])
                    if div_candles and rsi_series:
                        div = detect_bearish_divergences(div_candles, rsi_series, params)
                        if div["both"] or div["rsi_divergence"] or div["volume_divergence"]:
                            logger.info(
                                f"{self._log_prefix} {pair}: 다이버전스 감지 → 스탑 타이트닝"
                            )
                            await self._apply_stop_tightening(pair, current_price, atr, params)

            # ── 오케스트레이터 위임 ──
            if self._orchestrator is None:
                logger.error(
                    f"{self._log_prefix} {pair}: _orchestrator 미설정 "
                    "— set_orchestrator() 필요. 이번 사이클 스킵."
                )
                continue
            snapshot = await self._build_signal_snapshot(pair, signal_data, params, pos)
            result = await self._orchestrator.process(snapshot)
            should_continue = await self._handle_execution_result(
                pair, result, snapshot, signal_data, params
            )
            if should_continue:
                continue

            # ── 프리뷰 진입 시도 ──
            # 조건: 포지션 없음 + pending 없음 + 정규 시그널 entry_ok/sell 아님 + opt-in
            if (
                self._position.get(pair) is None
                and pair not in self._pending_limit_orders
                and signal not in ("entry_ok", "entry_sell")
                and params.get("preview_entry_enabled", False)
            ):
                await self._try_preview_entry(pair, basis_tf, params)

    # ──────────────────────────────────────────
    # 서브클래스 hook (기본 구현 제공)
    # ──────────────────────────────────────────

    async def _on_candle_extra_checks(self, pair: str, params: Dict) -> bool:
        """캔들 시그널 계산 전 추가 체크. False 반환 시 이번 사이클 스킵.

        기본: 아무것도 하지 않음 (True). CFD에서 keep_rate/보유시간 체크.
        """
        return True

    def _on_signal_computed(
        self, pair: str, signal: str, signal_data: dict, pos: Optional[Position]
    ) -> str:
        """시그널 계산 후 후처리 hook. 기본: pass-through."""
        return signal

    def _check_exit_warning(
        self, pair: str, signal: str, realtime_price: float, ema: float, pos: Position
    ) -> str:
        """실시간 가격으로 exit_warning 보정. 서브클래스에서 양방향 지원 가능."""
        if realtime_price < ema and signal != "exit_warning":
            logger.info(
                f"{self._log_prefix} {pair}: 실시간 가격 ¥{realtime_price} < EMA20 ¥{ema:.4f} "
                f"→ exit_warning 즉각 보정"
            )
            return "exit_warning"
        return signal

    async def _update_trailing_stop(
        self, pair: str, pos: Position, current_price: float,
        atr: float, ema_slope_pct: Optional[float], rsi: Optional[float], params: Dict
    ) -> None:
        """적응형 트레일링 스탑 ratchet. 서브클래스에서 양방향 지원 가능."""
        if pos.stop_tightened:
            mult = float(params.get("tighten_stop_atr", 1.0))
        else:
            mult = compute_adaptive_trailing_mult(ema_slope_pct, rsi, params)
        new_sl = round(current_price - atr * mult, 6)
        current_sl = pos.stop_loss_price
        if current_sl is None or new_sl > current_sl:
            pos.stop_loss_price = new_sl
            await self._update_trailing_stop_in_db(pair, new_sl)
            logger.info(
                f"{self._log_prefix} {pair}: 트레일링 스탑 갱신 "
                f"¥{current_sl} → ¥{new_sl} "
                f"(x{mult:.1f} {'tight' if pos.stop_tightened else 'adaptive'})"
            )

    # ──────────────────────────────────────────
    # Execution Layer 연동 (Step 4)
    # ──────────────────────────────────────────

    async def _build_signal_snapshot(
        self,
        pair: str,
        signal_data: dict,
        params: dict,
        pos: Optional[Position],
        is_preview: bool = False,
    ) -> SignalSnapshot:
        """signal_data dict + 현재 상태 → SignalSnapshot DTO."""
        pos_dto: Optional[PositionDTO] = None
        if pos is not None:
            pos_dto = PositionDTO(
                pair=pos.pair,
                entry_price=pos.entry_price,
                entry_amount=pos.entry_amount,
                stop_loss_price=pos.stop_loss_price,
                stop_tightened=pos.stop_tightened,
                extra=dict(pos.extra),
            )

        candles_raw = signal_data.get("candles") or []
        rsi_series_raw = signal_data.get("rsi_series") or []

        # ── v1.5: DataHub에서 매크로/이벤트/교훈 조회 ──
        macro = None
        upcoming_events = None
        relevant_lessons = None
        news = None
        sentiment = None
        if self._data_hub is not None:
            try:
                macro = await self._data_hub.get_macro_snapshot()
            except Exception as e:
                logger.debug(f"{self._log_prefix} {pair}: DataHub macro 조회 실패: {e}")
            try:
                upcoming_events = await self._data_hub.get_upcoming_events()
            except Exception as e:
                logger.debug(f"{self._log_prefix} {pair}: DataHub events 조회 실패: {e}")
            try:
                relevant_lessons = await self._data_hub.get_lessons(
                    pair, signal_data["signal"]
                )
            except Exception as e:
                logger.debug(f"{self._log_prefix} {pair}: DataHub lessons 조회 실패: {e}")
            try:
                news = await self._data_hub.get_news_summary(pair)
            except Exception as e:
                logger.debug(f"{self._log_prefix} {pair}: DataHub news 조회 실패: {e}")
            try:
                sentiment = await self._data_hub.get_sentiment()
            except Exception as e:
                logger.debug(f"{self._log_prefix} {pair}: DataHub sentiment 조회 실패: {e}")

        return SignalSnapshot(
            pair=pair,
            exchange=self._adapter.exchange_name,
            timestamp=datetime.now(timezone.utc),
            signal=signal_data["signal"],
            current_price=signal_data["current_price"],
            exit_signal=signal_data.get("exit_signal", {}),
            ema=signal_data.get("ema"),
            ema_slope_pct=signal_data.get("ema_slope_pct"),
            rsi=signal_data.get("rsi"),
            atr=signal_data.get("atr"),
            stop_loss_price=signal_data.get("stop_loss_price"),
            position=pos_dto,
            candles=tuple(candles_raw) if candles_raw else None,
            rsi_series=tuple(rsi_series_raw) if rsi_series_raw else None,
            params=params,
            macro=macro,
            upcoming_events=upcoming_events,
            relevant_lessons=relevant_lessons,
            news=news,
            sentiment=sentiment,
            is_preview=is_preview,
        )

    async def _handle_execution_result(
        self,
        pair: str,
        result: Any,             # ExecutionResult — Any로 선언해 순환 import 방지
        snapshot: SignalSnapshot,
        signal_data: dict,
        params: dict,
    ) -> bool:
        """ExecutionResult → 실제 실행.

        Returns:
            True  — 호출자(_candle_monitor)가 continue해야 하는 경우 (청산 후)
            False — 다음 로직 불필요 (hold / entry 등)
        """
        action = result.action
        pos = self._position.get(pair)
        current_price = snapshot.current_price
        atr = snapshot.atr
        ema_slope_pct = snapshot.ema_slope_pct
        rsi = snapshot.rsi

        if action == "exit":
            trigger = result.decision.trigger if result.decision else "orchestrator_exit"
            logger.info(
                f"{self._log_prefix} {pair}: {trigger} @ ¥{current_price} → 전량 청산"
            )
            await self._close_position(pair, trigger)
            return True  # continue

        if action == "tighten_stop":
            if pos is not None and not pos.stop_tightened and atr:
                await self._apply_stop_tightening(pair, current_price, atr, params)
            return False

        if action == "entry_long":
            is_preview = getattr(snapshot, "is_preview", False)
            entry_signal = "entry_preview" if is_preview else "entry_ok"
            await self._on_entry_signal(
                pair, entry_signal, current_price, atr, params, signal_data
            )
            # 진입 성공 시 학습 루프 연결: judgment_id / entry_time / confidence 기록
            new_pos = self._position.get(pair)
            if new_pos is not None and result.judgment_id is not None:
                new_pos.extra["judgment_id"] = result.judgment_id
                new_pos.extra["entry_time"] = datetime.now(timezone.utc).isoformat()
                if result.decision is not None:
                    new_pos.extra["confidence"] = result.decision.confidence
                    new_pos.extra["side"] = new_pos.extra.get("side", "long")
            if is_preview and new_pos is not None:
                new_pos.extra["preview_entry"] = True
            return False

        if action == "entry_short":
            is_preview = getattr(snapshot, "is_preview", False)
            entry_signal = "entry_preview" if is_preview else "entry_sell"
            await self._on_entry_signal(
                pair, entry_signal, current_price, atr, params, signal_data
            )
            # 진입 성공 시 학습 루프 연결: judgment_id / entry_time / confidence 기록
            new_pos = self._position.get(pair)
            if new_pos is not None and result.judgment_id is not None:
                new_pos.extra["judgment_id"] = result.judgment_id
                new_pos.extra["entry_time"] = datetime.now(timezone.utc).isoformat()
                if result.decision is not None:
                    new_pos.extra["confidence"] = result.decision.confidence
                    new_pos.extra["side"] = new_pos.extra.get("side", "short")
            if is_preview and new_pos is not None:
                new_pos.extra["preview_entry"] = True
            return False

        if action == "blocked":
            logger.info(
                f"{self._log_prefix} {pair}: 진입 차단 — {result.reason}"
            )
            return False

        if action == "adjust_risk":
            # 레이첼 advisory adjust_risk — 포지션 리스크 파라미터 동적 재조정
            adjustments: dict = (
                result.decision.meta.get("adjustments", {}) if result.decision else {}
            )
            if adjustments:
                # force_exit 처리: true이면 즉시 전량 청산 (설계서 §7-3, §7-4)
                if adjustments.get("force_exit", False):
                    logger.warning(
                        f"{self._log_prefix} {pair}: adjust_risk force_exit=true → 즉시 청산"
                    )
                    await self._close_position(pair, "advisory_force_exit")
                    return False

                _adjustable_keys = {
                    "stop_loss_pct",
                    "trailing_stop_atr_initial",
                    "trailing_stop_atr_mature",
                    "tighten_stop_atr",
                    "ema_slope_weak_threshold",
                }
                applied = {}
                for k, v in adjustments.items():
                    if k in _adjustable_keys:
                        params[k] = v
                        applied[k] = v
                if applied:
                    logger.info(
                        f"{self._log_prefix} {pair}: adjust_risk 적용 — {applied}"
                    )
                    # 새로운 SL이 있으면 즉시 갱신
                    if pos is not None and atr is not None and "stop_loss_pct" in applied:
                        sl_pct = float(applied["stop_loss_pct"])
                        new_sl = round(current_price * (1.0 - sl_pct / 100.0), 6)
                        if pos.stop_loss_price is None or new_sl > pos.stop_loss_price:
                            pos.stop_loss_price = new_sl
                            await self._update_trailing_stop_in_db(pair, new_sl)
                            logger.info(
                                f"{self._log_prefix} {pair}: adjust_risk SL 갱신 → ¥{new_sl}"
                            )
                # 서브클래스 훅 (GMO FX IFD-OCO 주문 변경 등)
                await self._on_adjust_risk_hook(pair, adjustments, params)
            return False

        # hold: 트레일링 스탑 (포지션 있을 때만)
        if pos is not None and atr is not None:
            await self._update_trailing_stop(
                pair, pos, current_price, atr, ema_slope_pct, rsi, params
            )
        return False

    async def _on_entry_signal(
        self, pair: str, signal: str, current_price: float,
        atr: Optional[float], params: Dict, signal_data: dict
    ) -> None:
        """진입 시그널 처리.

        signal:
          "entry_ok"      — 4H 완성 시그널, 로직: entry_mode에 따라 market 또는 limit
          "entry_preview" — 미완성 캔들 프리뷰 시그널, 동일하게 entry_mode 디스패치
          "entry_sell"    — 숏 진입 (CFD 전용)
        """
        is_long_entry = signal in ("entry_ok", "entry_preview")
        is_short_entry = signal == "entry_sell"

        if not (is_long_entry or is_short_entry):
            return

        # ── FX 전용 세션 필터 ────────────────────────────────────
        is_fx = getattr(self._adapter, "is_margin_trading", False)
        if is_fx and not is_allowed_session(params):
            logger.debug(f"{self._log_prefix} {pair}: 세션 필터 — 세션 차단")
            return
        if is_fx and is_london_open_blackout(params):
            logger.debug(f"{self._log_prefix} {pair}: 런던 오픈 블랙아웃 — 진입 대기")
            return

        direction = "long" if is_long_entry else "short"
        logger.info(
            f"{self._log_prefix} {pair}: {signal} @ ¥{current_price} → 진입 시도 (dir={direction})"
        )

        # Paper pair는 실주문 스킵
        if await self._try_paper_entry(pair, direction, current_price, atr, params):
            return

        # ── entry_mode 디스패치: market / limit / limit_then_market ──
        entry_mode = str(params.get("entry_mode", "market"))
        is_preview_signal = (signal == "entry_preview")

        if entry_mode in ("limit", "limit_then_market"):
            pending = await self._open_position_limit(
                pair, current_price, atr, params,
                signal_data=signal_data,
                is_preview=is_preview_signal,
            )
            if pending is not None:
                self._pending_limit_orders[pair] = pending
                logger.info(
                    f"{self._log_prefix} {pair}: limit order 등록 — "
                    f"order_id={pending.order_id} price=¥{pending.limit_price:.0f}"
                )
                return
            # limit 실패
            if entry_mode == "limit":
                logger.info(f"{self._log_prefix} {pair}: limit 진입 실패 — market fallback 없음")
                return
            logger.info(f"{self._log_prefix} {pair}: limit 실패 — market fallback")

        # market order (default 또는 limit_then_market fallback)
        await self._open_position(pair, current_price, atr, params, signal_data=signal_data)

    def _is_stop_triggered(self, pos: Position, price: float, stop_loss_price: float) -> bool:
        """스탑로스 발동 여부. 기본: 롱(price <= stop)."""
        return price <= stop_loss_price

    async def _try_preview_entry(self, pair: str, basis_tf: str, params: dict) -> None:
        """미완성 캔들 포함 프리뷰 시그널 계산 → entry_preview 시 오케스트레이터 위임.

        조건:
          - preview_entry_enabled=True (opt-in)
          - 미완성 캔들 진행률 ≥ 50% (noise 제거)
          - tick_count ≥ preview_min_tick_count (유동성 확인)
          - 직전 완성 캔들 시그널이 entry_ok/entry_sell 아님 (이미 진입됐거나 청산 중 아님)
        """
        if self._orchestrator is None:
            return

        try:
            preview_data = await self._compute_signal(
                pair, basis_tf, params=params, include_incomplete=True,
            )
        except Exception as e:
            logger.debug(f"{self._log_prefix} {pair}: 프리뷰 시그널 계산 실패 — {e}")
            return

        if not preview_data or not preview_data.get("has_incomplete"):
            return

        if preview_data.get("signal") != "entry_ok":
            return

        # 미완성 캔들 필터
        incomplete = preview_data.get("incomplete_candle")
        if incomplete is not None:
            min_tick = int(params.get("preview_min_tick_count", 3))
            tick_count = getattr(incomplete, "tick_count", None)
            if tick_count is not None and tick_count < min_tick:
                logger.debug(
                    f"{self._log_prefix} {pair}: 프리뷰 스킵 — tick_count={tick_count} < {min_tick}"
                )
                return

            # 진행률 50% 이상 체크
            open_time = getattr(incomplete, "open_time", None)
            close_time = getattr(incomplete, "close_time", None)
            if open_time is not None and close_time is not None:
                now = datetime.now(timezone.utc)
                total = (close_time - open_time).total_seconds()
                elapsed = (now - open_time).total_seconds()
                if total > 0 and elapsed / total < 0.5:
                    logger.debug(
                        f"{self._log_prefix} {pair}: 프리뷰 스킵 — "
                        f"캔들 진행률 {elapsed/total*100:.0f}% < 50%"
                    )
                    return

        # 프리뷰 시그널로 snapshot 구성: signal을 "entry_preview"로 변경
        preview_signal_data = {**preview_data, "signal": "entry_preview"}
        snapshot = await self._build_signal_snapshot(
            pair, preview_signal_data, params, None, is_preview=True
        )
        result = await self._orchestrator.process(snapshot)
        await self._handle_execution_result(pair, result, snapshot, preview_signal_data, params)

    async def _check_pending_limit_order(
        self, pair: str, pending: Any, current_signal: str, params: dict
    ) -> bool:
        """Pending limit order 상태 확인.

        Returns:
            True  → _candle_monitor가 continue해야 함 (대기 중 또는 포지션 등록 완료)
            False → pending 제거됨, 정규 흐름 재시도 가능
        """
        from core.exchange.types import OrderStatus

        elapsed = time.time() - pending.placed_at
        limit_timeout_sec = float(params.get("limit_timeout_sec", 300))

        try:
            order = await self._adapter.get_order(pending.order_id, pending.pair)
        except Exception as e:
            logger.warning(f"{self._log_prefix} {pair}: limit order 조회 실패 — {e}")
            return True  # 다음 사이클에서 재시도

        if order is None or order.status == OrderStatus.CANCELLED:
            logger.info(f"{self._log_prefix} {pair}: limit order 취소됨 → pending 제거")
            del self._pending_limit_orders[pair]
            return False

        if order.status == OrderStatus.COMPLETED:
            logger.info(
                f"{self._log_prefix} {pair}: limit order 체결 완료 "
                f"order_id={pending.order_id} price=¥{order.price}"
            )
            await self._finalize_limit_entry(pair, order, pending)
            del self._pending_limit_orders[pair]
            return True  # 포지션 등록 완료 → continue

        # OPEN 상태: 시그널 변경 감지 → 즉시 취소 (추세 이탈 보호)
        if current_signal not in ("entry_ok", "entry_preview"):
            logger.info(
                f"{self._log_prefix} {pair}: 시그널 변경 ({current_signal}) → limit order 취소"
            )
            try:
                await self._adapter.cancel_order(pending.order_id, pending.pair)
            except Exception as e:
                logger.warning(f"{self._log_prefix} {pair}: limit order 취소 실패 — {e}")
            del self._pending_limit_orders[pair]
            return False

        # 타임아웃 체크
        if elapsed > limit_timeout_sec:
            logger.info(
                f"{self._log_prefix} {pair}: limit order 타임아웃 ({elapsed:.0f}초) — 취소"
            )
            try:
                await self._adapter.cancel_order(pending.order_id, pending.pair)
            except Exception as e:
                logger.warning(f"{self._log_prefix} {pair}: limit order 타임아웃 취소 실패 — {e}")
            del self._pending_limit_orders[pair]
            # 타임아웃 후 시그널이 여전히 entry_ok면 다음 사이클에서 market fallback 시도
            return False

        # 대기 중
        logger.debug(
            f"{self._log_prefix} {pair}: limit order 대기 중 "
            f"order_id={pending.order_id} elapsed={elapsed:.0f}s"
        )
        return True

    async def _open_position_limit(
        self, pair: str, price: float, atr: Optional[float], params: Dict,
        *, signal_data: dict | None = None, is_preview: bool = False,
    ) -> Optional[Any]:
        """Limit order 진입 시도. None 반환 → market fallback.

        기본: 미지원 (None). 서브클래스(TrendFollowingManager 등)에서 override.
        """
        return None

    async def _finalize_limit_entry(self, pair: str, order: Any, pending: Any) -> None:
        """Limit order 체결 후 포지션 등록. 서브클래스에서 override.

        기본: 로그만 출력. 서브클래스(TrendFollowingManager)에서 실제 포지션 등록.
        """
        logger.info(f"{self._log_prefix} {pair}: limit 체결 — 서브클래스 미구현, 기본 처리 스킵")

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
    async def _open_position(self, pair: str, price: float, atr: Optional[float], params: Dict, *, signal_data: dict | None = None) -> None:
        """진입 주문 실행."""
        ...

    async def _close_position(self, pair: str, reason: str) -> None:
        """청산 wrapper. paper pair면 paper exit 처리 후 return. 그 외 _close_position_impl 위임."""
        paper_info = self._paper_positions.pop(pair, None)
        paper_exec = self._paper_executors.get(pair)
        if paper_exec is not None and paper_info is not None:
            exit_price = self._latest_price.get(pair, paper_info["entry_price"])
            try:
                await paper_exec.record_paper_exit(
                    paper_trade_id=paper_info["paper_trade_id"],
                    exit_price=exit_price,
                    exit_reason=reason,
                    entry_price=paper_info["entry_price"],
                    invest_jpy=100_000.0,  # 가상 투입금: PnL% 계산용
                    direction=paper_info["direction"],
                )
            except Exception as e:
                logger.error(f"{self._log_prefix} {pair}: Paper exit 기록 실패 — {e}")
            self._position[pair] = None
            logger.info(
                f"{self._log_prefix} {pair}: Paper 청산 기록 reason={reason} exit_price={exit_price}"
            )
            return

        # 학습 루프: impl 호출 전에 Position 정보 보존 (impl 내에서 position=None 처리됨)
        pos_before = self._position.get(pair)
        judgment_id: int | None = pos_before.extra.get("judgment_id") if pos_before else None
        entry_price_before = pos_before.entry_price if pos_before else None
        entry_amount_before = pos_before.entry_amount if pos_before else 0.0
        entry_time_str = pos_before.extra.get("entry_time") if pos_before else None
        confidence_before: float = pos_before.extra.get("confidence", 0.5) if pos_before else 0.5
        side_before = pos_before.extra.get("side", "long") if pos_before else "long"

        await self._close_position_impl(pair, reason)

        # 학습 루프: outcome backfill
        if judgment_id is not None and entry_price_before is not None:
            exit_price = self._latest_price.get(pair, entry_price_before)
            if side_before == "short":
                pnl = (entry_price_before - exit_price) * entry_amount_before
                pnl_pct = (entry_price_before - exit_price) / entry_price_before * 100 if entry_price_before > 0 else 0.0
            else:
                pnl = (exit_price - entry_price_before) * entry_amount_before
                pnl_pct = (exit_price - entry_price_before) / entry_price_before * 100 if entry_price_before > 0 else 0.0
            try:
                from datetime import datetime as _dt
                entry_time = _dt.fromisoformat(entry_time_str) if entry_time_str else datetime.now(timezone.utc)
            except (ValueError, TypeError):
                entry_time = datetime.now(timezone.utc)
            import asyncio as _asyncio
            _asyncio.create_task(
                self._update_judgment_outcome(
                    pair, judgment_id, pnl, pnl_pct, entry_time, confidence_before
                )
            )

        # T1 트리거: real 청산 완료 직후 全 전략 Score 스냅샷 수집 (P-1)
        if self._snapshot_collector is not None:
            import asyncio
            asyncio.create_task(
                self._snapshot_collector.collect_all_snapshots("T1_position_close", pair)
            )

    async def _update_judgment_outcome(
        self,
        pair: str,
        judgment_id: int,
        realized_pnl: float,
        realized_pnl_pct: float,
        entry_time: datetime,
        confidence: float,
    ) -> None:
        """ai_judgments 결과 컬럼 UPDATE (학습 루프 Stage 1).

        거래 청산 직후 asyncio.create_task()로 비동기 실행.
        실패해도 WARNING만 — 거래 흐름 블록하지 않는다.
        """
        if self._session_factory is None:
            return
        # judgment_model은 orchestrator에만 주입됨 → SessionFactory만으로 update
        try:
            from sqlalchemy import update as sa_update
            from adapters.database.models import AiJudgment  # 공유 테이블
            outcome = "win" if realized_pnl >= 0 else "loss"
            now = datetime.now(timezone.utc)
            hold_hours = (now - entry_time).total_seconds() / 3600
            # 확신도 오차: |confidence - (1.0 if win else 0.0)|
            confidence_error = abs(confidence - (1.0 if outcome == "win" else 0.0))
            async with self._session_factory() as session:
                await session.execute(
                    sa_update(AiJudgment)
                    .where(AiJudgment.id == judgment_id)
                    .values(
                        outcome=outcome,
                        realized_pnl=round(realized_pnl, 2),
                        hold_duration_hours=round(hold_hours, 4),
                        confidence_error=round(confidence_error, 4),
                        updated_at=now,
                    )
                )
                await session.commit()
            logger.debug(
                f"{self._log_prefix} {pair}: ai_judgments[{judgment_id}] outcome={outcome} "
                f"pnl={realized_pnl:.2f} ({realized_pnl_pct:.2f}%) hold={hold_hours:.1f}h"
            )
        except Exception as e:
            logger.warning(f"{self._log_prefix} {pair}: ai_judgments outcome 업데이트 실패 — {e}")

        # 사후 분석 (ENABLE_POST_ANALYSIS=true + PostAnalyzer 주입 시)
        if self._post_analyzer is not None:
            try:
                await self._post_analyzer.analyze(
                    judgment_id=judgment_id,
                    outcome=outcome,
                    realized_pnl=realized_pnl,
                    hold_duration_hours=hold_hours,
                )
            except Exception as e:
                logger.warning(f"{self._log_prefix} {pair}: 사후 분석 실패 (무시) — {e}")

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

    async def _on_adjust_risk_hook(
        self, pair: str, adjustments: dict, params: dict
    ) -> None:
        """adjust_risk 실행 후 서브클래스별 추가 처리 hook.

        기본 구현은 no-op. 서브클래스에서 override:
          - CfdTrendFollowingManager: GMO FX IFD-OCO 주문 변경
        """

    @abstractmethod
    async def _record_open(self, **kwargs) -> Optional[int]:
        """진입 DB 기록."""
        ...

    @abstractmethod
    async def _record_close(self, **kwargs) -> None:
        """청산 DB 기록."""
        ...
