"""
TrendFollowingManager — 거래소-무관 추세추종 전략 통합 매니저.

CK/BF 매니저를 단일 구현으로 통합. ExchangeAdapter Protocol에만 의존한다.
거래소 고유 로직(서명, 주문 포맷, WS 메시지 형식)은 어댑터가 처리.

아키텍처:
    main.py (EXCHANGE 환경변수)
      → ExchangeAdapter (CK or BF)
      → TrendFollowingManager (이 클래스)
        → TaskSupervisor (태스크 생명주기)
        → signals.py (기술적 지표 계산)
        → ORM models (DB 기록)

태스크 구성 (pair당 2개):
    1. CandleMonitor  — 60초 폴링, 시그널 계산 → 진입/청산/트레일링
    2. StopLossMonitor — WS 틱 기반 하드 스탑

통합된 버그 수정:
    - BUG-001: partial_exit race condition → optimistic flag + rollback
    - BUG-002: min_coin_size 기본값 → params에서 읽음
    - BUG-003: dust 잔고 → min_coin_size 미만 시 포지션 없음 취급 + DB 종료
    - BUG-004: 매도 수수료 차감 → sell_amount = available / (1 + fee_rate)
    - BUG-005: 스탑로스 무한 재시도 → 5회 실패마다 60초 쿨다운
    - BUG-006: 잔고-포지션 괴리 → 5분마다 실잔고 대비 정합성 검사 + 인메모리 갱신
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
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


class TrendFollowingManager:
    """
    거래소-무관 추세추종 전략 매니저.

    생성 시 의존성을 주입받는다:
        - adapter: ExchangeAdapter (CK or BF)
        - supervisor: TaskSupervisor (태스크 관리)
        - session_factory: async_sessionmaker (DB 접근)
        - CandleModel: ORM 캔들 모델 클래스
        - TrendPositionModel: ORM 포지션 모델 클래스
        - pair_column: 캔들 모델의 페어 컬럼명 ("pair" or "product_code")
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        supervisor: TaskSupervisor,
        session_factory: async_sessionmaker[AsyncSession],
        candle_model: Type,
        trend_position_model: Type,
        pair_column: str = "pair",
    ) -> None:
        self._adapter = adapter
        self._supervisor = supervisor
        self._session_factory = session_factory
        self._candle_model = candle_model
        self._trend_position_model = trend_position_model
        self._pair_column = pair_column

        # pair별 상태
        self._params: Dict[str, Dict] = {}
        self._position: Dict[str, Optional[Position]] = {}
        self._latest_price: Dict[str, float] = {}
        self._last_seen_open_time: Dict[str, Optional[str]] = {}
        self._ema_slope_history: Dict[str, List[float]] = {}
        self._ema_slope_last_key: Dict[str, Optional[str]] = {}

        # BUG-005: 스탑로스 청산 실패 백오프
        self._close_fail_count: Dict[str, int] = {}
        self._close_fail_until: Dict[str, float] = {}
        # BUG-006: 잔고-포지션 정합성 검사 카운터 (5사이클=5분마다)
        self._balance_sync_counter: Dict[str, int] = {}

    # ──────────────────────────────────────────
    # 시작 / 종료
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

        # 기존 포지션 복원
        pos = await self._detect_existing_position(pair)
        self._position[pair] = pos
        if pos:
            pos.db_record_id = await self._recover_db_position_id(pair)

        await self._supervisor.register(
            f"trend_candle:{pair}",
            lambda p=pair: self._candle_monitor(p),
            max_restarts=5,
        )
        await self._supervisor.register(
            f"trend_stoploss:{pair}",
            lambda p=pair: self._stop_loss_monitor(p),
            max_restarts=5,
        )

        logger.info(
            f"[TrendMgr] {pair}: 추세추종 시작 "
            f"(position={'있음' if pos else '없음'}, exchange={self._adapter.exchange_name})"
        )

    async def stop(self, pair: str) -> None:
        """pair에 대한 추세추종 태스크 종료."""
        await self._supervisor.stop(f"trend_candle:{pair}")
        await self._supervisor.stop(f"trend_stoploss:{pair}")
        self._params.pop(pair, None)
        self._position.pop(pair, None)
        self._last_seen_open_time.pop(pair, None)
        self._latest_price.pop(pair, None)
        self._ema_slope_history.pop(pair, None)
        self._ema_slope_last_key.pop(pair, None)
        self._close_fail_count.pop(pair, None)
        self._close_fail_until.pop(pair, None)
        logger.info(f"[TrendMgr] {pair}: 추세추종 태스크 종료")

    async def stop_all(self) -> None:
        """모든 pair 태스크 종료."""
        for pair in list(self._params.keys()):
            await self.stop(pair)
        logger.info("[TrendMgr] 전체 추세추종 인프라 종료")

    def is_running(self, pair: str) -> bool:
        return (
            self._supervisor.is_running(f"trend_candle:{pair}")
            or self._supervisor.is_running(f"trend_stoploss:{pair}")
        )

    def running_pairs(self) -> list[str]:
        return [p for p in self._params if self.is_running(p)]

    def get_position(self, pair: str) -> Optional[Position]:
        return self._position.get(pair)

    def get_task_health(self) -> dict:
        """pair별 태스크 헬스 리포트."""
        result: Dict[str, dict] = {}
        for pair in self._params:
            result[pair] = {
                "candle_monitor": self._supervisor.get_health().get(f"trend_candle:{pair}", {}),
                "stop_loss_monitor": self._supervisor.get_health().get(f"trend_stoploss:{pair}", {}),
            }
        return result

    # ──────────────────────────────────────────
    # 재시작 시 포지션 복원
    # ──────────────────────────────────────────

    async def _detect_existing_position(self, pair: str) -> Optional[Position]:
        """서버 재시작 시 잔고 확인으로 기존 포지션 감지.

        BUG-003: min_coin_size 미만 잔고는 dust로 간주 → 포지션 없음.
        """
        try:
            balance = await self._adapter.get_balance()
            currency = pair.split("_")[0]
            amount = balance.get_available(currency)
            min_size = float(self._params.get(pair, {}).get("min_coin_size", 0.001))

            if amount >= min_size:
                logger.info(
                    f"[TrendMgr] {pair}: 기존 포지션 감지 "
                    f"({currency} {amount:.6f}개) — 스탑로스 감시 재개"
                )
                return Position(
                    pair=pair,
                    entry_price=None,
                    entry_amount=amount,
                    stop_loss_price=None,
                )
            if amount > 0:
                logger.info(
                    f"[TrendMgr] {pair}: dust 잔고 무시 "
                    f"({currency} {amount:.6f}개 < min_coin_size {min_size}) — 포지션 없음"
                )
        except Exception as e:
            logger.warning(f"[TrendMgr] {pair}: 포지션 복원 실패 — {e}")
        return None

    async def _recover_db_position_id(self, pair: str) -> Optional[int]:
        """재시작 시 열린 DB 포지션 레코드 ID 복원."""
        try:
            Model = self._trend_position_model
            async with self._session_factory() as db:
                result = await db.execute(
                    select(Model)
                    .where(
                        Model.pair == pair,
                        Model.status == "open",
                    )
                    .order_by(Model.created_at.desc())
                    .limit(1)
                )
                rec = result.scalars().first()
                if rec:
                    logger.info(f"[TrendMgr] {pair}: DB 포지션 레코드 복원 id={rec.id}")
                    return rec.id
        except Exception as e:
            logger.warning(f"[TrendMgr] {pair}: DB 포지션 ID 복원 실패 — {e}")
        return None

    # ──────────────────────────────────────────
    # BUG-006: 잔고-포지션 정합성 검사
    # ──────────────────────────────────────────

    async def _sync_position_balance(self, pair: str) -> None:
        """실시간 잔고와 인메모리 entry_amount 비교. 괴리 시 인메모리 갱신 + 경고.

        외부 매매(수동, 테스트, 다른 버그)로 인한 잔고-포지션 불일치를 감지.
        청산 시에는 항상 실시간 잔고를 사용하므로 실행 안전성에는 영향 없으나,
        로그/PnL 추적 정확도를 위해 인메모리 값을 동기화한다.
        """
        pos = self._position.get(pair)
        if pos is None:
            return
        try:
            balance = await self._adapter.get_balance()
            currency = pair.split("_")[0].lower()
            real_available = balance.get_available(currency)
            mem_amount = pos.entry_amount
            if mem_amount <= 0:
                return
            drift_pct = abs(real_available - mem_amount) / mem_amount * 100
            if drift_pct > 1.0:
                logger.warning(
                    f"[TrendMgr] {pair}: 잔고-포지션 괴리 감지 "
                    f"(인메모리 {mem_amount:.8f} vs 실잔고 {real_available:.8f}, "
                    f"차이 {drift_pct:.1f}%) → 인메모리 갱신"
                )
                self._position[pair] = Position(
                    pair=pos.pair,
                    entry_price=pos.entry_price,
                    entry_amount=real_available,
                    stop_loss_price=pos.stop_loss_price,
                    db_record_id=pos.db_record_id,
                    stop_tightened=pos.stop_tightened,
                    extra=pos.extra,
                )
        except Exception as e:
            logger.debug(f"[TrendMgr] {pair}: 잔고 동기화 실패 — {e}")

    # ──────────────────────────────────────────
    # Task 1: 캔들 모니터 (진입 / EMA 이탈 청산)
    # ──────────────────────────────────────────

    async def _candle_monitor(self, pair: str) -> None:
        """
        60초마다 DB에서 최신 완성 캔들 조회.
        새 캔들 감지 시 트렌드 시그널 재계산 → 진입/청산 결정.

        청산 우선순위:
        1. 가격 < EMA20 (exit_warning)      → 전량 청산
        2. exit_signal.full_exit            → 전량 청산
        3. exit_signal.tighten_stop         → 스탑 타이트닝
        4. EMA 기울기 3캔들 연속 하락         → 스탑 타이트닝 (Phase 2)
        5. 적응형 트레일링 스탑 ratchet-up    (초기 2.0 / 성숙 1.2 / 타이트닝 1.0)
        6. entry_ok + 포지션 없음            → 진입
        """
        while True:
            await asyncio.sleep(_CANDLE_POLL_INTERVAL)

            params = self._params.get(pair, {})
            basis_tf = params.get("basis_timeframe", "4h")
            pos = self._position.get(pair)
            entry_price = pos.entry_price if pos else None

            # BUG-006: 5사이클(5분)마다 잔고-포지션 정합성 검사
            if pos is not None:
                cnt = self._balance_sync_counter.get(pair, 0) + 1
                self._balance_sync_counter[pair] = cnt
                if cnt % 5 == 0:
                    await self._sync_position_balance(pair)

            try:
                signal_data = await self._compute_signal(
                    pair, basis_tf,
                    entry_price=entry_price,
                    params=params,
                )
            except Exception as e:
                logger.warning(f"[TrendMgr] {pair}: 시그널 계산 실패 — {e}")
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

            # 실시간 가격으로 exit_warning 조기 발동 보정
            realtime_price = self._latest_price.get(pair)
            if realtime_price is not None and ema is not None and realtime_price < ema:
                if signal != "exit_warning":
                    logger.info(
                        f"[TrendMgr] {pair}: 실시간 가격 ¥{realtime_price} < EMA20 ¥{ema:.4f} "
                        f"→ exit_warning 즉각 보정 (4H signal={signal})"
                    )
                signal = "exit_warning"

            logger.debug(
                f"[TrendMgr] {pair}: signal={signal} exit={exit_action} "
                f"price={current_price} pos={'있음' if pos else '없음'}"
            )

            # ── Phase 2: EMA 기울기 이력 갱신 (새 캔들 도착 시만) ──
            if latest_candle_key != self._ema_slope_last_key.get(pair):
                slope_history = self._ema_slope_history.setdefault(pair, [])
                slope_history.append(ema_slope_pct)
                if len(slope_history) > 3:
                    slope_history.pop(0)
                self._ema_slope_last_key[pair] = latest_candle_key

                # EMA 기울기 3캔들 연속 하락 → 스탑 타이트닝
                if (
                    len(slope_history) == 3
                    and all(s is not None for s in slope_history)
                    and slope_history[0] > slope_history[1] > slope_history[2]
                    and pos is not None
                    and not pos.stop_tightened
                    and atr is not None
                ):
                    logger.info(
                        f"[TrendMgr] {pair}: EMA 기울기 3캔들 연속 하락 "
                        f"({slope_history[0]:.4f}%→{slope_history[1]:.4f}%→{slope_history[2]:.4f}%) "
                        f"→ 스탑 타이트닝 (Phase 2)"
                    )
                    await self._apply_stop_tightening(pair, current_price, atr, params)

                # ── Phase 3: RSI + 볼륨 베어리시 다이버전스 감지 ──
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
                        if div["both"]:
                            logger.info(
                                f"[TrendMgr] {pair}: RSI+볼륨 이중 다이버전스 감지 "
                                f"→ 스탑 타이트닝 (Phase 3 높은 신뢰도)"
                            )
                            await self._apply_stop_tightening(pair, current_price, atr, params)
                        elif div["rsi_divergence"]:
                            logger.info(
                                f"[TrendMgr] {pair}: RSI 다이버전스 감지 → 스탑 타이트닝 (Phase 3)"
                            )
                            await self._apply_stop_tightening(pair, current_price, atr, params)
                        elif div["volume_divergence"]:
                            logger.info(
                                f"[TrendMgr] {pair}: 볼륨 다이버전스 감지 → 스탑 타이트닝 (Phase 3)"
                            )
                            await self._apply_stop_tightening(pair, current_price, atr, params)

            # ── 포지션 있을 때: 청산 우선순위 체크 ──
            if pos is not None:

                # 1단계: 전량 청산
                if signal == "exit_warning":
                    logger.info(f"[TrendMgr] {pair}: exit_warning @ ¥{current_price} → 전량 청산")
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
                        f"[TrendMgr] {pair}: {reason_code} @ ¥{current_price} "
                        f"— {exit_signal.get('reason', '')}"
                    )
                    await self._close_position(pair, reason_code)
                    continue

                # 2단계: 스탑 타이트닝
                if exit_action == "tighten_stop" and not pos.stop_tightened:
                    if atr:
                        logger.info(
                            f"[TrendMgr] {pair}: tighten_stop @ ¥{current_price} "
                            f"— {exit_signal.get('reason', '')}"
                        )
                        await self._apply_stop_tightening(pair, current_price, atr, params)

                # 적응형 트레일링 스탑 ratchet-up
                if atr is not None:
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
                            f"[TrendMgr] {pair}: 트레일링 스탑 갱신 "
                            f"¥{current_sl} → ¥{new_sl} "
                            f"(x{mult:.1f} {'tight' if pos.stop_tightened else 'adaptive'})"
                        )

            # ── 포지션 없을 때: 진입 ──
            elif signal == "entry_ok":
                logger.info(f"[TrendMgr] {pair}: entry_ok @ ¥{current_price} → 진입 시도")
                await self._open_position(pair, current_price, atr, params)

    # ──────────────────────────────────────────
    # Task 2: 스탑로스 모니터 (WS 틱 기반 하드 스탑)
    # ──────────────────────────────────────────

    async def _stop_loss_monitor(self, pair: str) -> None:
        """
        WS 실시간 체결가 구독 → 스탑로스 이탈 시 즉시 청산.
        ExchangeAdapter.subscribe_trades(pair, callback) 사용.
        """
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

                # 실시간 가격 캐시 갱신 (캔들 모니터 exit_warning 보정용)
                self._latest_price[pair] = price

                if price <= stop_loss_price:
                    # BUG-005: 청산 실패 백오프
                    cooldown_until = self._close_fail_until.get(pair, 0)
                    if time.time() < cooldown_until:
                        continue

                    logger.info(
                        f"[TrendMgr] {pair}: 하드 스탑로스 발동 "
                        f"현재가 ¥{price} ≤ 스탑로스 ¥{stop_loss_price}"
                    )
                    await self._close_position(pair, "stop_loss")

                    # 청산 성공 여부 확인
                    if self._position.get(pair) is None:
                        self._close_fail_count[pair] = 0
                        self._close_fail_until[pair] = 0
                    else:
                        fail_count = self._close_fail_count.get(pair, 0) + 1
                        self._close_fail_count[pair] = fail_count
                        if fail_count % 5 == 0:
                            self._close_fail_until[pair] = time.time() + 60
                            logger.warning(
                                f"[TrendMgr] {pair}: 청산 {fail_count}회 연속 실패 "
                                f"— 60초 쿨다운 후 재시도"
                            )
        except asyncio.CancelledError:
            raise

    # ──────────────────────────────────────────
    # 시그널 계산 (DB 직접 조회)
    # ──────────────────────────────────────────

    async def _compute_signal(
        self, pair: str, timeframe: str,
        entry_price: Optional[float] = None,
        params: Optional[dict] = None,
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
            logger.debug(f"[TrendMgr] {pair}: 캔들 부족 ({len(candles)}개)")
            return None

        result = compute_trend_signal(candles, params=params or {}, entry_price=entry_price)
        if result is not None:
            result["latest_candle_open_time"] = str(candles[-1].open_time)
            result["candles"] = candles
        return result

    # ──────────────────────────────────────────
    # 진입
    # ──────────────────────────────────────────

    async def _open_position(
        self, pair: str, price: float, atr: Optional[float], params: Dict
    ) -> None:
        """market_buy 자동 진입 + 인메모리 포지션 기록."""
        try:
            balance = await self._adapter.get_balance()
            jpy_available = balance.get_available("jpy")
            position_size_pct = float(params.get("position_size_pct", 10.0))
            invest_jpy = jpy_available * position_size_pct / 100
            min_jpy = float(params.get("min_order_jpy", 500))

            if invest_jpy < min_jpy:
                logger.info(
                    f"[TrendMgr] {pair}: 투입 JPY({invest_jpy:.0f}) < {min_jpy:.0f}, 진입 스킵"
                )
                return

            # 슬리피지 pre-check
            max_slippage_pct = float(params.get("max_slippage_pct", 0.3))
            try:
                ticker = await self._adapter.get_ticker(pair)
                if ticker.ask > 0:
                    expected_slippage = (ticker.ask - price) / price * 100
                    if expected_slippage > max_slippage_pct:
                        logger.warning(
                            f"[TrendMgr] {pair}: 슬리피지 초과 "
                            f"ask=¥{ticker.ask} price=¥{price} "
                            f"slippage={expected_slippage:.3f}% > {max_slippage_pct}%, 진입 스킵"
                        )
                        return
            except Exception as e:
                logger.warning(f"[TrendMgr] {pair}: 시세 조회 실패 (슬리피지 체크 스킵) — {e}")

            # Protocol: MARKET_BUY amount = JPY 금액
            order = await self._adapter.place_order(
                order_type=OrderType.MARKET_BUY,
                pair=pair,
                amount=round(invest_jpy, 0),
            )

            exec_price = order.price or price
            exec_amount = order.amount
            if exec_amount == 0 and exec_price > 0:
                exec_amount = round(invest_jpy / exec_price, 8)

            # 초기 스탑로스
            atr_mult = float(params.get("atr_multiplier_stop", 2.0))
            initial_sl = round(exec_price - atr * atr_mult, 6) if atr else None

            pos = Position(
                pair=pair,
                entry_price=exec_price,
                entry_amount=exec_amount,
                stop_loss_price=initial_sl,
            )
            self._position[pair] = pos

            pos.db_record_id = await self._record_open(
                pair=pair,
                order_id=order.order_id,
                price=exec_price,
                amount=exec_amount,
                invest_jpy=invest_jpy,
                stop_loss_price=initial_sl,
                strategy_id=params.get("strategy_id"),
            )

            logger.info(
                f"[TrendMgr] {pair}: 진입 완료 "
                f"order_id={order.order_id} price=¥{exec_price} amount={exec_amount} "
                f"stop_loss=¥{initial_sl}"
            )
        except Exception as e:
            logger.error(f"[TrendMgr] {pair}: 진입 주문 오류 — {e}", exc_info=True)

    # ──────────────────────────────────────────
    # 청산
    # ──────────────────────────────────────────

    async def _close_position(self, pair: str, reason: str) -> None:
        """market_sell 자동 청산 + 포지션 클리어.

        BUG-003: dust 잔고 → DB 레코드도 종료.
        BUG-004: 매도 수수료 차감 → available / (1 + fee_rate).
        """
        try:
            balance = await self._adapter.get_balance()
            currency = pair.split("_")[0]
            coin_available = balance.get_available(currency)
            min_size = float(self._params.get(pair, {}).get("min_coin_size", 0.001))

            # BUG-004: 매도 수수료 차감 (BF는 매도 통화에서 차감)
            fee_rate = float(self._params.get(pair, {}).get("trading_fee_rate", 0.002))
            sell_amount = math.floor(coin_available / (1 + fee_rate) * 1e8) / 1e8

            if sell_amount < min_size:
                logger.warning(
                    f"[TrendMgr] {pair}: 청산 시도했지만 잔고 부족 "
                    f"({coin_available} < {min_size}) — 포지션 클리어"
                )
                prev_pos = self._position.get(pair)
                prev_db_id = prev_pos.db_record_id if prev_pos else None
                self._position[pair] = None
                # BUG-003: DB 오픈 레코드도 dust로 종료
                if prev_db_id:
                    await self._record_close(
                        db_record_id=prev_db_id,
                        pair=pair,
                        order_id="",
                        price=0.0,
                        amount=coin_available,
                        reason="dust_position_cleared",
                        entry_price=prev_pos.entry_price if prev_pos else None,
                    )
                return

            order = await self._adapter.place_order(
                order_type=OrderType.MARKET_SELL,
                pair=pair,
                amount=sell_amount,
            )

            prev_pos = self._position.get(pair)
            prev_db_id = prev_pos.db_record_id if prev_pos else None
            self._position[pair] = None

            exec_price = order.price or 0
            # BUG-008: market_sell 응답에 체결가 없을 수 있음 → ticker 현재가로 대체
            if exec_price == 0:
                try:
                    ticker = await self._adapter.get_ticker(pair)
                    exec_price = ticker.last
                    logger.warning(f"[TrendMgr] {pair}: 체결가 미반환, ticker last={exec_price}로 대체")
                except Exception as te:
                    logger.warning(f"[TrendMgr] {pair}: ticker 조회도 실패 — {te}")
            await self._record_close(
                db_record_id=prev_db_id,
                pair=pair,
                order_id=order.order_id,
                price=exec_price,
                amount=sell_amount,
                reason=reason,
                entry_price=prev_pos.entry_price if prev_pos else None,
            )

            logger.info(
                f"[TrendMgr] {pair}: 청산 완료 "
                f"reason={reason} order_id={order.order_id} amount={sell_amount}"
            )

            # BUG-009: 청산 후 dust 잔고 감지 로깅
            try:
                balance_after = await self._adapter.get_balance()
                dust = balance_after.get_available(currency)
                if 0 < dust < min_size:
                    logger.info(
                        f"[TrendMgr] {pair}: 청산 후 dust 잔고 감지 "
                        f"({currency} {dust:.8f} < min_size {min_size}) — 매도 불가 수량, 다음 진입 시 포함됨"
                    )
            except Exception as de:
                logger.debug(f"[TrendMgr] {pair}: dust 확인 실패 — {de}")
        except Exception as e:
            logger.error(f"[TrendMgr] {pair}: 청산 주문 오류 — {e}", exc_info=True)

    async def _apply_stop_tightening(
        self, pair: str, current_price: float, atr: float, params: dict
    ) -> None:
        """스탑을 타이트하게 조정하고 stop_tightened 플래그 설정."""
        pos = self._position.get(pair)
        if pos is None:
            return
        tighten_mult = float(params.get("tighten_stop_atr", 1.0))
        new_sl = round(current_price - atr * tighten_mult, 6)
        current_sl = pos.stop_loss_price
        if current_sl is None or new_sl > current_sl:
            pos.stop_loss_price = new_sl
            await self._update_trailing_stop_in_db(pair, new_sl)
        pos.stop_tightened = True
        logger.info(
            f"[TrendMgr] {pair}: 스탑 타이트닝 ¥{current_sl} → ¥{new_sl} (x{tighten_mult})"
        )

    # ──────────────────────────────────────────
    # DB 기록 헬퍼
    # ──────────────────────────────────────────

    async def _record_open(
        self,
        pair: str,
        order_id: str,
        price: float,
        amount: float,
        invest_jpy: float,
        stop_loss_price: Optional[float],
        strategy_id: Optional[int],
    ) -> Optional[int]:
        """진입 시 trend_positions 레코드 생성."""
        try:
            Model = self._trend_position_model
            async with self._session_factory() as db:
                rec = Model(
                    pair=pair,
                    strategy_id=strategy_id,
                    entry_order_id=order_id,
                    entry_price=price,
                    entry_amount=amount,
                    entry_jpy=round(invest_jpy, 2),
                    stop_loss_price=stop_loss_price,
                    status="open",
                )
                db.add(rec)
                await db.commit()
                await db.refresh(rec)
                logger.debug(f"[TrendMgr] {pair}: DB 포지션 기록 id={rec.id}")
                return rec.id
        except Exception as e:
            logger.error(f"[TrendMgr] {pair}: DB 진입 기록 실패 — {e}", exc_info=True)
            return None

    async def _record_close(
        self,
        db_record_id: Optional[int],
        pair: str,
        order_id: str,
        price: float,
        amount: float,
        reason: str,
        entry_price: Optional[float],
    ) -> None:
        """청산 시 열린 trend_positions 레코드 업데이트."""
        try:
            if db_record_id is None:
                return

            exit_jpy = round(price * amount, 2) if price and amount else None
            pnl_jpy: Optional[float] = None
            pnl_pct: Optional[float] = None
            if entry_price and entry_price > 0 and price > 0:
                pnl_jpy = round((price - entry_price) * amount, 2)
                pnl_pct = round((price - entry_price) / entry_price * 100, 4)

            Model = self._trend_position_model
            async with self._session_factory() as db:
                result = await db.execute(
                    select(Model).where(Model.id == db_record_id)
                )
                rec = result.scalars().first()
                if rec is None:
                    return
                rec.exit_order_id = order_id
                rec.exit_price = price
                rec.exit_amount = amount
                rec.exit_jpy = exit_jpy
                rec.exit_reason = reason
                rec.realized_pnl_jpy = pnl_jpy
                rec.realized_pnl_pct = pnl_pct
                rec.status = "closed"
                rec.closed_at = datetime.now(timezone.utc)
                await db.commit()
                logger.debug(
                    f"[TrendMgr] {pair}: DB 청산 기록 id={db_record_id} "
                    f"pnl=¥{pnl_jpy} ({pnl_pct}%)"
                )
        except Exception as e:
            logger.error(f"[TrendMgr] {pair}: DB 청산 기록 실패 — {e}", exc_info=True)

    async def _update_trailing_stop_in_db(self, pair: str, stop_loss_price: float) -> None:
        """열린 포지션 레코드의 stop_loss_price 컬럼 갱신."""
        try:
            pos = self._position.get(pair)
            if pos is None or pos.db_record_id is None:
                return
            Model = self._trend_position_model
            async with self._session_factory() as db:
                result = await db.execute(
                    select(Model).where(Model.id == pos.db_record_id)
                )
                rec = result.scalars().first()
                if rec:
                    rec.stop_loss_price = stop_loss_price
                    await db.commit()
        except Exception as e:
            logger.warning(f"[TrendMgr] {pair}: DB 트레일링 스탑 갱신 실패 — {e}")

    async def _record_partial_close(
        self,
        pair: str,
        order_id: str,
        price: float,
        amount: float,
        reason: str,
    ) -> None:
        """부분 청산 시 trend_positions 누적 컬럼 갱신."""
        try:
            pos = self._position.get(pair)
            if pos is None or pos.db_record_id is None:
                return
            partial_jpy = round(price * amount, 2) if price and amount else 0.0
            Model = self._trend_position_model
            async with self._session_factory() as db:
                result = await db.execute(
                    select(Model).where(Model.id == pos.db_record_id)
                )
                rec = result.scalars().first()
                if rec is None:
                    return
                rec.partial_exit_count = (rec.partial_exit_count or 0) + 1
                rec.partial_exit_amount = round(
                    float(rec.partial_exit_amount or 0) + amount, 8
                )
                rec.partial_exit_jpy = round(
                    float(rec.partial_exit_jpy or 0) + partial_jpy, 2
                )
                existing = rec.partial_exit_reasons or ""
                rec.partial_exit_reasons = f"{existing},{reason}" if existing else reason
                await db.commit()
        except Exception as e:
            logger.warning(f"[TrendMgr] {pair}: 부분 청산 DB 기록 실패 — {e}")
