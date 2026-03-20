"""
BoxMeanReversionManager — 거래소-무관 박스권 역추세 전략 통합 매니저.

CK/BF 박스 매니저+BoxService+BoxPositionService를 단일 구현으로 통합.
ExchangeAdapter Protocol에만 의존한다.

아키텍처:
    main.py (EXCHANGE 환경변수)
      → ExchangeAdapter (CK or BF)
      → BoxMeanReversionManager (이 클래스)
        → TaskSupervisor (태스크 생명주기)
        → ORM models (DB 기록)

태스크 구성 (pair당 2개):
    1. BoxMonitor   — 60초 폴링, DB 캔들 조회 → 박스 감지/유효성 검사
    2. EntryMonitor — WS 틱 기반, 박스 경계 진입/청산

박스 감지 알고리즘:
    - lookback 캔들의 몸통(open/close) 고점/저점 클러스터링
    - tolerance_pct 이내 가격을 하나의 클러스터로 묶음
    - min_touches 이상 반복된 최대 클러스터를 상/하단으로 확정
    - 최소 박스 폭: tolerance×2 + fee×2 (수수료 커버 보장)

안전장치:
    - 1-box-1-position: open 포지션 있으면 진입 스킵
    - 중복 발동 방지: prev_box_state 추적
    - 박스 무효화 시 자동 손절 (market_sell)
    - 수렴 삼각형 감지 → 박스 무효화
    - 최소 주문 금액/수량 체크
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Type

from sqlalchemy import and_, desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.exchange.base import ExchangeAdapter
from core.exchange.types import OrderType
from core.task.supervisor import TaskSupervisor

logger = logging.getLogger(__name__)

_BOX_MONITOR_INTERVAL = 60  # 초


class BoxMeanReversionManager:
    """
    거래소-무관 박스권 역추세 전략 매니저.

    생성 시 의존성을 주입받는다:
        - adapter: ExchangeAdapter (CK or BF)
        - supervisor: TaskSupervisor (태스크 관리)
        - session_factory: async_sessionmaker (DB 접근)
        - candle_model: ORM 캔들 모델 클래스
        - box_model: ORM 박스 모델 클래스
        - box_position_model: ORM 박스 포지션 모델 클래스
        - pair_column: 캔들/박스 모델의 페어 컬럼명 ("pair" or "product_code")
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        supervisor: TaskSupervisor,
        session_factory: async_sessionmaker[AsyncSession],
        candle_model: Type,
        box_model: Type,
        box_position_model: Type,
        pair_column: str = "pair",
    ) -> None:
        self._adapter = adapter
        self._supervisor = supervisor
        self._session_factory = session_factory
        self._candle_model = candle_model
        self._box_model = box_model
        self._box_position_model = box_position_model
        self._pair_column = pair_column

        # pair별 상태
        self._params: Dict[str, Dict] = {}
        self._last_seen_open_time: Dict[str, Optional[str]] = {}
        self._prev_box_state: Dict[str, Optional[str]] = {}

    # ──────────────────────────────────────────
    # 시작 / 종료
    # ──────────────────────────────────────────

    async def start(self, pair: str, params: Dict) -> None:
        """pair에 대한 박스 역추세 태스크 2개 등록."""
        self._params[pair] = params
        self._last_seen_open_time[pair] = None
        self._prev_box_state[pair] = None

        # 기존 태스크 정리
        await self._supervisor.stop_tasks(pair)

        self._supervisor.register(
            f"box_monitor:{pair}",
            lambda p=pair: self._box_monitor(p),
        )
        self._supervisor.register(
            f"box_entry:{pair}",
            lambda p=pair: self._entry_monitor(p),
        )
        await self._supervisor.start_all()
        logger.info(f"[BoxMgr] {pair}: 박스 인프라 태스크 2개 시작")

    async def stop(self, pair: str) -> None:
        """pair에 대한 태스크 종료."""
        await self._supervisor.stop_tasks(pair)
        self._params.pop(pair, None)
        self._last_seen_open_time.pop(pair, None)
        self._prev_box_state.pop(pair, None)
        logger.info(f"[BoxMgr] {pair}: 박스 인프라 태스크 종료")

    # ──────────────────────────────────────────
    # Task 1: 박스 감지/유효성 모니터 (DB 캔들 폴링)
    # ──────────────────────────────────────────

    async def _box_monitor(self, pair: str) -> None:
        """
        60초마다 DB에서 최신 완성 캔들 open_time 조회.
        새 캔들 감지 시:
          1. validate_active_box → 무효화 시 자동 손절
          2. detect_and_create_box → 신규 박스 감지
        """
        while True:
            await asyncio.sleep(_BOX_MONITOR_INTERVAL)

            params = self._params.get(pair, {})
            basis_tf = params.get("basis_timeframe", "4h")

            try:
                open_time = await self._get_latest_candle_open_time(pair, basis_tf)
            except Exception as e:
                logger.warning(f"[BoxMgr] {pair}: 캔들 조회 실패 — {e}")
                continue

            if open_time is None:
                continue

            last_seen = self._last_seen_open_time.get(pair)
            if open_time == last_seen:
                continue

            logger.info(f"[BoxMgr] {pair}: 새 {basis_tf} 캔들 감지 open_time={open_time}")
            self._last_seen_open_time[pair] = open_time

            # 유효성 검사 (항상)
            reason = await self._validate_active_box(pair, params)
            if reason:
                logger.info(f"[BoxMgr] {pair}: 박스 무효화 ({reason})")
                pos = await self._get_open_position(pair)
                if pos:
                    await self._close_position_market(pair, pos, reason)

            # 신규 박스 감지
            box = await self._detect_and_create_box(pair, params)
            if box:
                logger.info(
                    f"[BoxMgr] {pair}: 신규 박스 id={box.id} "
                    f"상단={box.upper_bound} 하단={box.lower_bound}"
                )

    # ──────────────────────────────────────────
    # Task 2: 진입/청산 모니터 (WS 틱 기반)
    # ──────────────────────────────────────────

    async def _entry_monitor(self, pair: str) -> None:
        """
        WS 틱마다 is_price_in_box() 체크 → 자동 진입/청산.

        진입: active box 존재 + position 없음 + near_lower로 전환
        청산: open position 존재 + near_upper로 전환
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

                box_state = await self._is_price_in_box(pair, price)
                params = self._params.get(pair, {})
                prev_state = self._prev_box_state.get(pair)

                # ── 진입: near_lower로 새로 진입했을 때 ──
                if (
                    box_state == "near_lower"
                    and prev_state != "near_lower"
                    and not await self._has_open_position(pair)
                ):
                    box = await self._get_active_box(pair)
                    if box:
                        await self._open_position_market(pair, box, price, params)

                # ── 청산: near_upper로 새로 진입했을 때 ──
                elif (
                    box_state == "near_upper"
                    and prev_state != "near_upper"
                ):
                    pos = await self._get_open_position(pair)
                    if pos:
                        await self._close_position_market(pair, pos, "near_upper_exit")

                self._prev_box_state[pair] = box_state

        except asyncio.CancelledError:
            raise

    # ──────────────────────────────────────────
    # 박스 감지
    # ──────────────────────────────────────────

    async def _detect_and_create_box(
        self, pair: str, params: Dict[str, Any],
    ) -> Optional[Any]:
        """
        완성된 4H 캔들에서 박스 감지 → DB INSERT.
        이미 active 박스가 있으면 스킵.
        """
        tolerance_pct = float(params.get("box_tolerance_pct", 0.5))
        min_touches = int(params.get("box_min_touches", 3))
        lookback = int(params.get("box_lookback_candles", 60))
        basis_tf = params.get("basis_timeframe", "4h")

        existing = await self._get_active_box(pair)
        if existing:
            logger.debug(f"[BoxMgr] {pair}: active 박스 이미 존재 (id={existing.id}), 감지 스킵")
            return None

        candles = await self._get_completed_candles(pair, basis_tf, lookback)
        if len(candles) < min_touches * 2:
            logger.info(f"[BoxMgr] {pair}: 캔들 부족 ({len(candles)}개 < {min_touches * 2})")
            return None

        upper, upper_count = self._find_cluster(
            [self._candle_high(c) for c in candles],
            tolerance_pct, min_touches, mode="high",
        )
        lower, lower_count = self._find_cluster(
            [self._candle_low(c) for c in candles],
            tolerance_pct, min_touches, mode="low",
        )

        if upper is None or lower is None:
            logger.info(f"[BoxMgr] {pair}: 박스 불형성 (upper={upper}, lower={lower})")
            return None

        if upper <= lower:
            logger.info(f"[BoxMgr] {pair}: 상단 ≤ 하단, 박스 무효")
            return None

        # 최소 박스 폭: tolerance×2 + fee×2
        fee_rate_pct = float(params.get("fee_rate_pct", 0.15))
        width_pct = (upper - lower) / lower * 100
        min_width_pct = tolerance_pct * 2 + fee_rate_pct * 2
        if params.get("box_min_width_pct"):
            min_width_pct = max(min_width_pct, float(params["box_min_width_pct"]))

        if width_pct < min_width_pct:
            logger.info(
                f"[BoxMgr] {pair}: 박스 폭 너무 좁음 "
                f"({width_pct:.2f}% < min {min_width_pct:.2f}%)"
            )
            return None

        # DB INSERT
        BoxModel = self._box_model
        pair_col = self._pair_column
        box = BoxModel()
        setattr(box, pair_col, pair)
        box.upper_bound = Decimal(str(upper))
        box.lower_bound = Decimal(str(lower))
        box.upper_touch_count = upper_count
        box.lower_touch_count = lower_count
        box.tolerance_pct = Decimal(str(tolerance_pct))
        box.basis_timeframe = basis_tf
        box.status = "active"
        box.detected_from_candle_count = len(candles)
        box.detected_at_candle_open_time = (
            candles[-1].open_time if candles else None
        )
        box.created_at = datetime.now(timezone.utc)

        async with self._session_factory() as db:
            db.add(box)
            await db.commit()
            await db.refresh(box)

        logger.info(
            f"[BoxMgr] 박스 생성: {pair} "
            f"상단={upper:.8f}({upper_count}회) 하단={lower:.8f}({lower_count}회) "
            f"폭={width_pct:.2f}%"
        )
        return box

    # ──────────────────────────────────────────
    # 박스 유효성 검사
    # ──────────────────────────────────────────

    async def _validate_active_box(
        self, pair: str, params: Dict[str, Any],
    ) -> Optional[str]:
        """
        활성 박스의 유효성을 최신 완성 캔들 종가로 검사.
        이탈 시 invalidated 처리하고 이유 반환. 유효하면 None.
        """
        box = await self._get_active_box(pair)
        if not box:
            return None

        tolerance_pct = float(box.tolerance_pct)
        basis_tf = box.basis_timeframe or params.get("basis_timeframe", "4h")

        candles = await self._get_completed_candles(pair, basis_tf, limit=1)
        if not candles:
            return None

        close = float(candles[-1].close)
        upper = float(box.upper_bound)
        lower = float(box.lower_bound)
        tol = tolerance_pct / 100

        reason: Optional[str] = None

        if close < lower * (1 - tol):
            reason = "4h_close_below_lower"
        elif close > upper * (1 + tol):
            reason = "4h_close_above_upper"
        else:
            reason = await self._check_converging_triangle(pair, box, params)

        if reason:
            await self._invalidate_box(box.id, reason)
            logger.info(
                f"[BoxMgr] 박스 무효화: {pair} id={box.id} reason={reason} "
                f"close={close} upper={upper} lower={lower}"
            )

        return reason

    async def _check_converging_triangle(
        self, pair: str, box: Any, params: Dict[str, Any],
    ) -> Optional[str]:
        """수렴 삼각형 감지: 고점 하락 + 저점 상승이면 'converging_triangle'."""
        lookback = min(int(params.get("box_lookback_candles", 60)), 20)
        basis_tf = params.get("basis_timeframe", "4h")
        candles = await self._get_completed_candles(pair, basis_tf, lookback)
        if len(candles) < 8:
            return None

        highs = [self._candle_high(c) for c in candles]
        lows = [self._candle_low(c) for c in candles]

        xs = list(range(len(highs)))
        high_slope = self._linear_slope(xs, highs)
        low_slope = self._linear_slope(xs, lows)

        if high_slope < -1e-6 and low_slope > 1e-6:
            return "converging_triangle"
        return None

    # ──────────────────────────────────────────
    # 박스 가격 판정
    # ──────────────────────────────────────────

    async def _is_price_in_box(self, pair: str, price: float) -> Optional[str]:
        """
        현재 가격이 박스 어느 구간에 있는지 반환.
        "near_lower" | "near_upper" | "middle" | "outside" | None(박스 없음)
        """
        box = await self._get_active_box(pair)
        if not box:
            return None

        upper = float(box.upper_bound)
        lower = float(box.lower_bound)
        tol = float(box.tolerance_pct) / 100

        if price <= lower * (1 + tol):
            return "near_lower"
        elif price >= upper * (1 - tol):
            return "near_upper"
        elif lower * (1 - tol) <= price <= upper * (1 + tol):
            return "middle"
        else:
            return "outside"

    # ──────────────────────────────────────────
    # 주문 실행
    # ──────────────────────────────────────────

    async def _open_position_market(
        self, pair: str, box: Any, price: float, params: Dict[str, Any],
    ) -> None:
        """market_buy 자동 진입 + DB 포지션 기록."""
        try:
            balance = await self._adapter.get_balance()
            jpy_available = balance.get_available("jpy")
            position_size_pct = float(params.get("position_size_pct", 10.0))
            invest_jpy = jpy_available * position_size_pct / 100

            min_jpy = float(params.get("min_order_jpy", 500))
            if invest_jpy < min_jpy:
                logger.info(
                    f"[BoxMgr] {pair}: 투입 JPY({invest_jpy:.0f}) < "
                    f"{min_jpy:.0f}, 진입 스킵"
                )
                return

            if price <= 0:
                logger.warning(f"[BoxMgr] {pair}: 현재가 0, 진입 스킵")
                return

            # MARKET_BUY: amount=JPY (adapter가 내부적으로 코인 수량 변환)
            order = await self._adapter.place_order(
                OrderType.MARKET_BUY, pair, invest_jpy,
            )

            exec_price = order.price or price
            exec_amount = order.amount
            if exec_amount == 0 and exec_price > 0:
                exec_amount = invest_jpy / exec_price

            await self._record_open_position(
                pair=pair,
                box_id=box.id,
                entry_order_id=order.order_id,
                entry_price=exec_price,
                entry_amount=exec_amount,
                entry_jpy=invest_jpy,
            )
            logger.info(
                f"[BoxMgr] {pair}: 자동 진입 완료 "
                f"order_id={order.order_id} price={exec_price} "
                f"amount={exec_amount}"
            )
        except Exception as e:
            logger.error(f"[BoxMgr] {pair}: 진입 주문 오류 — {e}", exc_info=True)

    async def _close_position_market(
        self, pair: str, pos: Any, reason: str,
    ) -> None:
        """market_sell 자동 청산 + DB 포지션 기록."""
        try:
            balance = await self._adapter.get_balance()
            currency = pair.split("_")[0].lower()
            coin_available = balance.get_available(currency)

            min_size = float(self._params.get(pair, {}).get("min_coin_size", 0.001))
            if coin_available < min_size:
                logger.warning(
                    f"[BoxMgr] {pair}: 청산 시도했지만 {currency} 잔고 부족 "
                    f"({coin_available} < {min_size})"
                )
                return

            # 수수료 차감 (BUG-004 교훈)
            fee_rate = float(self._params.get(pair, {}).get("trading_fee_rate", 0.002))
            sell_amount = math.floor(coin_available / (1 + fee_rate) * 1e8) / 1e8

            order = await self._adapter.place_order(
                OrderType.MARKET_SELL, pair, sell_amount,
            )

            exec_price = order.price or 0.0
            # BUG-008: market_sell 응답에 체결가 없을 수 있음 → ticker 현재가로 대체
            if exec_price == 0:
                try:
                    ticker = await self._adapter.get_ticker(pair)
                    exec_price = ticker.last
                    logger.warning(f"[BoxMgr] {pair}: 체결가 미반환, ticker last={exec_price}로 대체")
                except Exception as te:
                    logger.warning(f"[BoxMgr] {pair}: ticker 조회도 실패 — {te}")
            exec_amount = order.amount or sell_amount

            await self._record_close_position(
                pair=pair,
                exit_order_id=order.order_id,
                exit_price=exec_price,
                exit_amount=exec_amount,
                exit_reason=reason,
            )
            logger.info(
                f"[BoxMgr] {pair}: 자동 청산 완료 "
                f"reason={reason} order_id={order.order_id} "
                f"price={exec_price}"
            )

            # BUG-009: 청산 후 dust 잔고 감지 로깅
            try:
                balance_after = await self._adapter.get_balance()
                currency_lower = pair.split("_")[0].lower()
                dust = balance_after.get_available(currency_lower)
                if 0 < dust < min_size:
                    logger.info(
                        f"[BoxMgr] {pair}: 청산 후 dust 잔고 감지 "
                        f"({currency_lower} {dust:.8f} < min_size {min_size}) — 매도 불가 수량, 다음 진입 시 포함됨"
                    )
            except Exception as de:
                logger.debug(f"[BoxMgr] {pair}: dust 확인 실패 — {de}")
        except Exception as e:
            logger.error(f"[BoxMgr] {pair}: 청산 주문 오류 — {e}", exc_info=True)

    # ──────────────────────────────────────────
    # DB: 포지션 기록
    # ──────────────────────────────────────────

    async def _record_open_position(
        self,
        pair: str,
        box_id: Optional[int],
        entry_order_id: str,
        entry_price: float,
        entry_amount: float,
        entry_jpy: Optional[float] = None,
    ) -> Any:
        """진입 주문 직후 포지션 기록. open 이미 존재 시 경고 → 기존 반환."""
        existing = await self._get_open_position(pair)
        if existing:
            logger.warning(
                f"[BoxMgr] {pair}: open 포지션 이미 존재 (id={existing.id}), 새 진입 무시"
            )
            return existing

        PosModel = self._box_position_model
        pair_col = self._pair_column
        pos = PosModel()
        setattr(pos, pair_col, pair)
        pos.box_id = box_id
        pos.side = "buy"
        pos.entry_order_id = str(entry_order_id)
        pos.entry_price = Decimal(str(entry_price))
        pos.entry_amount = Decimal(str(entry_amount))
        pos.entry_jpy = Decimal(str(entry_jpy)) if entry_jpy is not None else None
        pos.status = "open"
        pos.created_at = datetime.now(timezone.utc)

        async with self._session_factory() as db:
            db.add(pos)
            await db.commit()
            await db.refresh(pos)

        logger.info(
            f"[BoxMgr] 진입 기록: {pair} "
            f"order_id={entry_order_id} price={entry_price} amount={entry_amount}"
        )
        return pos

    async def _record_close_position(
        self,
        pair: str,
        exit_order_id: str,
        exit_price: float,
        exit_amount: float,
        exit_reason: str,
        exit_jpy: Optional[float] = None,
    ) -> Optional[Any]:
        """청산 주문 직후 포지션 closed 처리. realized_pnl 자동 계산."""
        pos = await self._get_open_position(pair)
        if not pos:
            logger.warning(f"[BoxMgr] {pair}: close 호출했지만 open 포지션 없음")
            return None

        ep = float(pos.entry_price)
        ea = float(pos.entry_amount)
        pnl_jpy = (exit_price - ep) * min(exit_amount, ea)
        pnl_pct = (exit_price - ep) / ep * 100 if ep > 0 else 0.0

        if exit_jpy is None:
            exit_jpy = exit_price * exit_amount

        PosModel = self._box_position_model
        async with self._session_factory() as db:
            await db.execute(
                update(PosModel)
                .where(PosModel.id == pos.id)
                .values(
                    exit_order_id=str(exit_order_id),
                    exit_price=Decimal(str(exit_price)),
                    exit_amount=Decimal(str(exit_amount)),
                    exit_jpy=Decimal(str(exit_jpy)),
                    exit_reason=exit_reason,
                    realized_pnl_jpy=Decimal(str(round(pnl_jpy, 2))),
                    realized_pnl_pct=Decimal(str(round(pnl_pct, 4))),
                    status="closed",
                    closed_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()

        logger.info(
            f"[BoxMgr] 청산 기록: {pair} "
            f"reason={exit_reason} pnl={pnl_jpy:+.2f}JPY ({pnl_pct:+.2f}%)"
        )
        return pos

    # ──────────────────────────────────────────
    # DB: 박스 조회/관리
    # ──────────────────────────────────────────

    async def _get_active_box(self, pair: str) -> Optional[Any]:
        """현재 active 박스 반환."""
        BoxModel = self._box_model
        pair_col_attr = getattr(BoxModel, self._pair_column)
        async with self._session_factory() as db:
            result = await db.execute(
                select(BoxModel)
                .where(and_(pair_col_attr == pair, BoxModel.status == "active"))
                .order_by(desc(BoxModel.created_at))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def _invalidate_box(self, box_id: int, reason: str) -> None:
        """박스를 invalidated 처리."""
        BoxModel = self._box_model
        async with self._session_factory() as db:
            await db.execute(
                update(BoxModel)
                .where(BoxModel.id == box_id)
                .values(
                    status="invalidated",
                    invalidation_reason=reason,
                    invalidated_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()

    # ──────────────────────────────────────────
    # DB: 포지션 조회
    # ──────────────────────────────────────────

    async def _get_open_position(self, pair: str) -> Optional[Any]:
        """현재 open 포지션 반환."""
        PosModel = self._box_position_model
        pair_col_attr = getattr(PosModel, self._pair_column)
        async with self._session_factory() as db:
            result = await db.execute(
                select(PosModel)
                .where(and_(pair_col_attr == pair, PosModel.status == "open"))
                .order_by(desc(PosModel.created_at))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def _has_open_position(self, pair: str) -> bool:
        """open 포지션 존재 여부."""
        return (await self._get_open_position(pair)) is not None

    # ──────────────────────────────────────────
    # DB: 캔들 조회
    # ──────────────────────────────────────────

    async def _get_latest_candle_open_time(
        self, pair: str, timeframe: str,
    ) -> Optional[str]:
        """최신 완성 캔들의 open_time(ISO) 반환."""
        CandleModel = self._candle_model
        pair_col_attr = getattr(CandleModel, self._pair_column)
        async with self._session_factory() as db:
            result = await db.execute(
                select(CandleModel.open_time)
                .where(and_(
                    pair_col_attr == pair,
                    CandleModel.timeframe == timeframe,
                    CandleModel.is_complete == True,
                ))
                .order_by(desc(CandleModel.open_time))
                .limit(1)
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return row.isoformat() if hasattr(row, "isoformat") else str(row)

    async def _get_completed_candles(
        self, pair: str, timeframe: str, limit: int = 60,
    ) -> List[Any]:
        """완성된 캔들 최근 limit개 반환 (시간 오름차순)."""
        CandleModel = self._candle_model
        pair_col_attr = getattr(CandleModel, self._pair_column)
        async with self._session_factory() as db:
            result = await db.execute(
                select(CandleModel)
                .where(and_(
                    pair_col_attr == pair,
                    CandleModel.timeframe == timeframe,
                    CandleModel.is_complete == True,
                ))
                .order_by(desc(CandleModel.open_time))
                .limit(limit)
            )
            candles = list(result.scalars().all())
        candles.reverse()  # 시간 오름차순
        return candles

    # ──────────────────────────────────────────
    # 순수 헬퍼 (static)
    # ──────────────────────────────────────────

    @staticmethod
    def _candle_high(candle: Any) -> float:
        """몸통 고점 (max(open, close)) — 꼬리 배제 1차 기준."""
        return float(max(candle.open, candle.close))

    @staticmethod
    def _candle_low(candle: Any) -> float:
        """몸통 저점 (min(open, close)) — 꼬리 배제 1차 기준."""
        return float(min(candle.open, candle.close))

    @staticmethod
    def _find_cluster(
        prices: list[float],
        tolerance_pct: float,
        min_touches: int,
        mode: str,
    ) -> tuple[Optional[float], int]:
        """
        tolerance_pct 이내 가격들을 클러스터로 묶어 최다 빈도 클러스터 반환.
        mode="high" → 높은 쪽 우선, mode="low" → 낮은 쪽 우선.
        """
        if not prices:
            return None, 0

        tol = tolerance_pct / 100
        sorted_prices = sorted(prices, reverse=(mode == "high"))
        clusters: list[list[float]] = []

        for p in sorted_prices:
            placed = False
            for cluster in clusters:
                center = sum(cluster) / len(cluster)
                if abs(p - center) / center <= tol:
                    cluster.append(p)
                    placed = True
                    break
            if not placed:
                clusters.append([p])

        best_cluster = max(clusters, key=len)
        if len(best_cluster) < min_touches:
            return None, 0

        return sum(best_cluster) / len(best_cluster), len(best_cluster)

    @staticmethod
    def _linear_slope(xs: list[int], ys: list[float]) -> float:
        """간이 선형 회귀 기울기."""
        n = len(xs)
        if n < 2:
            return 0.0
        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sum_x2 = sum(x * x for x in xs)
        denom = n * sum_x2 - sum_x ** 2
        if denom == 0:
            return 0.0
        return (n * sum_xy - sum_x * sum_y) / denom
