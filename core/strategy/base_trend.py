"""
BaseTrendManager — 추세추종 전략 공통 베이스 클래스.

현물(TrendFollowingManager)과 CFD(CfdTrendFollowingManager)가 공유하는
태스크 관리·시그널 계산·스탑로스 모니터·캔들 모니터 골격을 정의한다.

서브클래스가 override해야 할 메서드 (abstract):
    - _detect_existing_position
    - _sync_position_state
    - _open_position
    - _close_position
    - _apply_stop_tightening
    - _record_open
    - _record_close
    - _on_candle_extra_checks (보유시간/keep_rate 등)
    - _get_entry_side (진입 시그널에서 side 결정)
    - _is_stop_triggered (스탑로스 방향 체크)
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

from core.exchange.base import ExchangeAdapter
from core.exchange.types import OrderType, Position
from core.strategy.signals import (
    compute_adaptive_trailing_mult,
    compute_trend_signal,
    detect_bearish_divergences,
)
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
    ) -> None:
        self._adapter = adapter
        self._supervisor = supervisor
        self._session_factory = session_factory
        self._candle_model = candle_model
        self._position_model = position_model
        self._pair_column = pair_column
        self._position_pair_column = position_pair_column or pair_column

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

        logger.info(
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
        logger.info(f"{self._log_prefix} {pair}: 추세추종 태스크 종료")

    async def stop_all(self) -> None:
        for p in list(self._params.keys()):
            await self.stop(p)
        logger.info(f"{self._log_prefix} 전체 추세추종 인프라 종료")

    def is_running(self, pair: str) -> bool:
        prefix = self._task_prefix
        return (
            self._supervisor.is_running(f"{prefix}_candle:{pair}")
            or self._supervisor.is_running(f"{prefix}_stoploss:{pair}")
        )

    def running_pairs(self) -> list[str]:
        return [p for p in self._params if self.is_running(p)]

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
                if cnt % 5 == 0:
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

            logger.debug(
                f"{self._log_prefix} {pair}: signal={signal} exit={exit_action} "
                f"price={current_price} pos={'있음' if pos else '없음'}"
            )

            # ── EMA 기울기 이력 + 다이버전스 ──
            if latest_candle_key != self._ema_slope_last_key.get(pair):
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

            # ── 포지션 있을 때: 청산 우선순위 ──
            if pos is not None:
                if signal == "exit_warning":
                    logger.info(f"{self._log_prefix} {pair}: exit_warning @ ¥{current_price} → 전량 청산")
                    await self._close_position(pair, "exit_warning")
                    continue

                if exit_action == "full_exit":
                    triggers = exit_signal.get("triggers", {})
                    reason_code = (
                        "full_exit_ema_slope" if triggers.get("ema_slope_negative")
                        else "full_exit_rsi_breakdown" if triggers.get("rsi_breakdown")
                        else "full_exit"
                    )
                    logger.info(
                        f"{self._log_prefix} {pair}: {reason_code} @ ¥{current_price} → 전량 청산"
                    )
                    await self._close_position(pair, reason_code)
                    continue

                if exit_action == "tighten_stop" and not pos.stop_tightened:
                    if atr:
                        await self._apply_stop_tightening(pair, current_price, atr, params)

                # 적응형 트레일링 스탑
                if atr is not None:
                    await self._update_trailing_stop(pair, pos, current_price, atr, ema_slope_pct, rsi, params)

            # ── 포지션 없을 때: 진입 ──
            else:
                await self._on_entry_signal(pair, signal, current_price, atr, params, signal_data)

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

    async def _on_entry_signal(
        self, pair: str, signal: str, current_price: float,
        atr: Optional[float], params: Dict, signal_data: dict
    ) -> None:
        """진입 시그널 처리. 기본: entry_ok → buy 진입."""
        if signal == "entry_ok":
            logger.info(f"{self._log_prefix} {pair}: entry_ok @ ¥{current_price} → 진입 시도")
            await self._open_position(pair, current_price, atr, params)

    def _is_stop_triggered(self, pos: Position, price: float, stop_loss_price: float) -> bool:
        """스탑로스 발동 여부. 기본: 롱(price <= stop)."""
        return price <= stop_loss_price

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
    async def _open_position(self, pair: str, price: float, atr: Optional[float], params: Dict) -> None:
        """진입 주문 실행."""
        ...

    @abstractmethod
    async def _close_position(self, pair: str, reason: str) -> None:
        """청산 주문 실행."""
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
