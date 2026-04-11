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
from core.exchange.session import is_fx_market_open, should_close_for_weekend, minutes_until_market_close
from core.exchange.types import OrderType
from core.task.supervisor import TaskSupervisor
from core.analysis.box_detector import find_cluster, find_cluster_percentile
from core.analysis.session_filter import is_allowed_session, is_london_open_blackout
from core.analysis.event_filter import EventFilter
from core.analysis.intermarket import IntermarketClient
from core.strategy.box_signals import classify_price_in_box, check_box_invalidation, linear_slope
from core.execution.executor import IExecutor, PaperExecutor, RealExecutor

logger = logging.getLogger(__name__)

_BOX_MONITOR_INTERVAL = 60  # 초

# 무효화 후 신규 박스 재감지 금지 기간 (4H 캔들 수 = 32시간)
_BOX_COOLDOWN_CANDLES = 8


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
        event_filter: Optional[EventFilter] = None,
        executor: Optional[IExecutor] = None,
        intermarket_client: Optional[IntermarketClient] = None,
        snapshot_collector: Optional[Any] = None,
    ) -> None:
        self._adapter = adapter
        self._supervisor = supervisor
        self._session_factory = session_factory
        self._candle_model = candle_model
        self._box_model = box_model
        self._box_position_model = box_position_model
        self._pair_column = pair_column
        self._event_filter: Optional[EventFilter] = event_filter
        self._intermarket_client: Optional[IntermarketClient] = intermarket_client
        self._executor: IExecutor = executor if executor is not None else RealExecutor()
        self._snapshot_collector: Optional[Any] = snapshot_collector  # P-1 트리거 훅
        # pair → PaperExecutor (proposed 전략 전용). active 전략은 이 dict에 없음.
        self._paper_executors: Dict[str, PaperExecutor] = {}
        # pair → 거래소 SL 주문 ID (None = 미등록 or 취소됨)
        self._exchange_sl_orders: Dict[str, Optional[str]] = {}
        # pair → IFD-OCO rootOrderId (None = 미활성)
        self._ifdoco_orders: Dict[str, Optional[str]] = {}
        # pair → IFD-OCO 메타 (pending 상태 캐싱. 서버 재시작 시 소실 → poll에서 API 재취득)
        self._ifdoco_meta: Dict[str, Optional[Dict]] = {}

        # pair → strategy_id (None = active 전략, N = paper/proposed 전략)
        # _get_active_box / _detect_and_create_box에서 박스 격리에 사용
        self._strategy_id_map: Dict[str, Optional[int]] = {}

        # pair별 상태
        self._params: Dict[str, Dict] = {}
        self._last_seen_open_time: Dict[str, Optional[str]] = {}
        self._prev_box_state: Dict[str, Optional[str]] = {}
        # tick마다 DB 조회 최소화를 위한 포지션 캐시
        # pair not in dict → cold (첫 DB 조회 필요)
        # dict[pair] is None → 포지션 없음 (확인됨)
        # dict[pair] is not None → 포지션 있음 (ORM 객체)
        self._cached_position: Dict[str, Optional[Any]] = {}
        # 박스 무효화 후 쿨다운 추적 (pair → 무효화 UTC 시각)
        self._last_invalidation_time: Dict[str, Optional[datetime]] = {}

    # ──────────────────────────────────────────
    # 시작 / 종료
    # ──────────────────────────────────────────

    def register_paper_pair(self, pair: str, strategy_id: int) -> None:
        """proposed 전략의 pair를 Paper 실행으로 등록. 해당 pair에만 PaperExecutor 바인딩."""
        self._paper_executors[pair] = PaperExecutor(self._session_factory, strategy_id)
        logger.debug(f"[BoxMgr] {pair}: PaperExecutor 등록 strategy_id={strategy_id}")

    def unregister_paper_pair(self, pair: str) -> None:
        """Paper 등록 해제. 추천 승인/pair 전환 시 호출."""
        self._paper_executors.pop(pair, None)
        logger.debug(f"[BoxMgr] {pair}: PaperExecutor 해제")

    def _get_executor(self, pair: str) -> IExecutor:
        """pair에 해당하는 executor 반환. paper pair면 PaperExecutor, 아니면 공유 executor."""
        return self._paper_executors.get(pair, self._executor)

    async def start(self, pair: str, params: Dict) -> None:
        """pair에 대한 박스 역추세 태스크 2개 등록."""
        self._params[pair] = params
        self._strategy_id_map[pair] = params.get("strategy_id")
        self._last_seen_open_time[pair] = None

        # 재시작 시 prev_state 초기화
        # 포지션 있으면 현재 상태 유지 (중복 청산 방지)
        # 포지션 없으면 None으로 시작 → 이미 near_lower에 있으면 즉시 진입 가능
        try:
            has_pos = await self._has_open_position(pair)
            if has_pos:
                ticker = await self._adapter.get_ticker(pair)
                current_price = ticker.last
                if current_price:
                    self._prev_box_state[pair] = await self._is_price_in_box(pair, float(current_price))
                else:
                    self._prev_box_state[pair] = None
            else:
                self._prev_box_state[pair] = None
        except Exception:
            self._prev_box_state[pair] = None

        # 재시작 시 거래소 SL 주문 ID DB에서 복원 (인메모리 소실 방지)
        try:
            async with self._session_factory() as session:
                BoxPos = self._box_position_model
                pair_col = getattr(BoxPos, self._pair_column)
                result = await session.execute(
                    select(BoxPos.exchange_sl_order_id)
                    .where(
                        pair_col == pair,
                        BoxPos.status == "open",
                        BoxPos.exchange_sl_status == "registered",
                    )
                )
                sl_id = result.scalar_one_or_none()
                if sl_id:
                    self._exchange_sl_orders[pair] = sl_id
                    logger.info(
                        f"[BoxMgr] {pair}: DB에서 거래소 SL 복원 (order_id={sl_id})"
                    )
        except Exception as e:
            logger.warning(f"[BoxMgr] {pair}: 거래소 SL 복원 실패 — {e}")

        # IFD-OCO first_filled 복원 (first_filled 상태에서만 DB에 포지션 row 존재)
        try:
            async with self._session_factory() as session:
                BoxPos = self._box_position_model
                pair_col = getattr(BoxPos, self._pair_column)
                result = await session.execute(
                    select(BoxPos.ifdoco_root_order_id)
                    .where(
                        pair_col == pair,
                        BoxPos.status == "open",
                        BoxPos.ifdoco_status == "first_filled",
                    )
                )
                root_id = result.scalar_one_or_none()
                if root_id:
                    self._ifdoco_orders[pair] = str(root_id)
                    logger.info(
                        f"[BoxMgr] {pair}: DB에서 IFD-OCO 복원 (root_id={root_id})"
                    )
        except Exception as e:
            logger.warning(f"[BoxMgr] {pair}: IFD-OCO 복원 실패 — {e}")

        # 기존 태스크 정리
        await self._supervisor.stop_group(pair)

        await self._supervisor.register(
            f"box_monitor:{pair}",
            lambda p=pair: self._box_monitor(p),
        )
        await self._supervisor.register(
            f"box_entry:{pair}",
            lambda p=pair: self._entry_monitor(p),
        )
        logger.debug(f"[BoxMgr] {pair}: 박스 인프라 태스크 2개 시작")

    async def stop(self, pair: str) -> None:
        """pair에 대한 태스크 종료."""
        await self._supervisor.stop_group(pair)
        self._params.pop(pair, None)
        self._strategy_id_map.pop(pair, None)
        self._last_seen_open_time.pop(pair, None)
        self._prev_box_state.pop(pair, None)
        self._cached_position.pop(pair, None)
        self._last_invalidation_time.pop(pair, None)
        self._paper_executors.pop(pair, None)
        self._exchange_sl_orders.pop(pair, None)
        self._ifdoco_orders.pop(pair, None)
        self._ifdoco_meta.pop(pair, None)
        logger.debug(f"[BoxMgr] {pair}: 박스 인프라 태스크 종료")

    def is_running(self, pair: str) -> bool:
        """pair에 대한 박스 전략 태스크가 실행 중인지 확인."""
        return (
            self._supervisor.is_running(f"box_monitor:{pair}")
            or self._supervisor.is_running(f"box_entry:{pair}")
        )

    def running_pairs(self) -> list[str]:
        """현재 실행 중인 pair 목록 반환."""
        return [p for p in self._params if self.is_running(p)]

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
        # ── 기동 시 즉시 validate (재시작 후 stale 박스 정리) ──
        try:
            params = self._params.get(pair, {})
            existing_box = await self._get_active_box(pair)
            if existing_box:
                reason = await self._validate_active_box(pair, params)
                if reason:
                    logger.info(f"[BoxMgr] {pair}: 기동 시 기존 박스 무효화 ({reason})")
                    pos = await self._get_open_position(pair)
                    if pos:
                        await self._close_position_market(pair, pos, reason)
                else:
                    logger.info(f"[BoxMgr] {pair}: 기동 시 기존 박스 유효 확인 (id={existing_box.id})")
        except Exception as e:
            logger.warning(f"[BoxMgr] {pair}: 기동 시 validate 실패 — {e}")

        while True:
            await asyncio.sleep(_BOX_MONITOR_INTERVAL)
            await self._run_one_box_monitor_cycle(pair)

    async def _run_one_box_monitor_cycle(self, pair: str) -> None:
        """_box_monitor 한 사이클 실행 (테스트·리팩토링 공유용)."""
        params = self._params.get(pair, {})
        basis_tf = params.get("basis_timeframe", "4h")
        is_fx = getattr(self._adapter, "is_margin_trading", False)

        # ── 거래소 SL 체결 감지 + 동기화 (FX 전용) ──────────────────
        if is_fx:
            await self._sync_exchange_sl_status(pair)

        # ── IFD-OCO 상태 폴링 (FX 전용) ─────────────────────────────
        if is_fx:
            await self._poll_ifdoco_status(pair)

        # ── 주말 자동 청산 (FX 전용) ───────────────────────────────
        weekend_close_enabled = params.get("weekend_close", True)
        if is_fx and should_close_for_weekend():
            if weekend_close_enabled:
                pos = await self._get_open_position(pair)
                if pos:
                    mins = minutes_until_market_close()
                    logger.warning(
                        f"[BoxMgr] {pair}: 주말 마감 임박 (잔여 {mins}분) → 자동 청산"
                    )
                    await self._close_position_market(pair, pos, "weekend_close")
            return  # 주말에는 캔들/박스 감지 불필요 (weekend_close 여부 무관)

        # ── FX 시장 휴장 시 스킵 ──────────────────────
        if is_fx and not is_fx_market_open():
            return

        try:
            open_time = await self._get_latest_candle_open_time(pair, basis_tf)
        except Exception as e:
            logger.warning(f"[BoxMgr] {pair}: 캔들 조회 실패 — {e}")
            return

        if open_time is None:
            return

        last_seen = self._last_seen_open_time.get(pair)
        if open_time == last_seen:
            return

        logger.info(f"[BoxMgr] {pair}: 새 {basis_tf} 캔들 감지 open_time={open_time}")
        self._last_seen_open_time[pair] = open_time

        # 유효성 검사 (항상)
        reason = await self._validate_active_box(pair, params)
        if reason:
            logger.info(f"[BoxMgr] {pair}: 박스 무효화 ({reason})")
            # IFD-OCO pending(DB pos 없음)이면 거래소 주문만 취소
            if self._ifdoco_orders.get(pair) is not None:
                await self._cancel_active_ifdoco(pair)
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

        # T2 트리거: 새 4H봉 + 무포지션 → 全 전략 Score 스냅샷 수집 (P-1)
        if self._snapshot_collector is not None:
            pos = await self._get_open_position(pair)
            if pos is None:
                asyncio.create_task(
                    self._snapshot_collector.collect_all_snapshots(
                        "T2_candle_close", pair
                    )
                )

    # ──────────────────────────────────────────
    # Task 2: 진입/청산 모니터 (WS 틱 기반)
    # ──────────────────────────────────────────

    async def _entry_monitor(self, pair: str) -> None:
        """
        WS 틱마다 is_price_in_box() 체크 → 자동 진입/청산. 양방향 대응.

        direction_mode:
          "long_only" (기본): near_lower 롱 진입, near_upper 롱 청산
          "both": near_lower 롱 진입(+숏 청산), near_upper 숏 진입(+롱 청산)
          "short_only": near_upper 숏 진입, near_lower 숏 청산

        is_margin_trading=False(현물) 시 direction_mode 무시하고 long_only 강제.
        """
        price_queue: asyncio.Queue[float] = asyncio.Queue()

        async def _on_trade(price: float, amount: float) -> None:
            await price_queue.put(price)

        # subscribe_tradesは永久ループなのでバックグラウンドタスクで実行
        asyncio.create_task(self._adapter.subscribe_trades(pair, _on_trade))

        try:
            while True:
                try:
                    price = await asyncio.wait_for(price_queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    continue

                box_state = await self._is_price_in_box(pair, price)
                params = self._params.get(pair, {})
                prev_state = self._prev_box_state.get(pair)

                # ── FX: 주말/휴장 시 진입 차단 ─────────────
                is_fx = getattr(self._adapter, "is_margin_trading", False)
                if is_fx and (should_close_for_weekend() or not is_fx_market_open()):
                    self._prev_box_state[pair] = box_state
                    continue

                # ── 세션 필터 (FX 전용) ─────────────────────
                if is_fx and not is_allowed_session(params):
                    self._prev_box_state[pair] = box_state
                    continue
                if is_fx and is_london_open_blackout(params):
                    logger.debug(f"[BoxMgr] {pair}: 런던 오픈 블랙아웃 차단")
                    self._prev_box_state[pair] = box_state
                    continue

                # ── 이벤트 차단 (FX 전용, near_lower/upper 시간에만 확인) ──
                if is_fx and self._event_filter and box_state in ("near_lower", "near_upper"):
                    try:
                        blocked, reason = await self._event_filter.is_event_blackout(pair, params)
                        if blocked:
                            logger.info(f"[BoxMgr] {pair}: 이벤트 차단 — {reason}")
                            self._prev_box_state[pair] = box_state
                            continue
                    except Exception as e:
                        logger.debug(f"[BoxMgr] {pair}: 이벤트 필터 오류 ({e}) — 무시")

                # ── 매크로 스트레스 차단 (FX 전용) ─────────────────────────
                if is_fx and self._intermarket_client and box_state in ("near_lower", "near_upper"):
                    try:
                        if await self._intermarket_client.is_macro_stress(params):
                            logger.info(f"[BoxMgr] {pair}: 매크로 스트레스(VIX) 차단")
                            self._prev_box_state[pair] = box_state
                            continue
                    except Exception as e:
                        logger.debug(f"[BoxMgr] {pair}: 인터마켓 스트레스 오류 ({e}) — 무시")

                # ── direction_mode 결정: 현물이면 무조건 long_only ──
                if not is_fx:
                    direction_mode = "long_only"
                else:
                    direction_mode = params.get("direction_mode", "long_only")

                # ── IFD-OCO 경로 (use_ifdoco=True && FX 시) ──────────────
                use_ifdoco = params.get("use_ifdoco", False) and is_fx
                if use_ifdoco:
                    # IFD-OCO 이미 활성이면 틱 로직 전부 스킵 (거래소가 처리 중)
                    if self._ifdoco_orders.get(pair) is not None:
                        self._prev_box_state[pair] = box_state
                        continue

                    # near_lower → 롱 IFD-OCO
                    if box_state == "near_lower" and prev_state != "near_lower":
                        if direction_mode in ("long_only", "both"):
                            box = await self._get_active_box(pair)
                            if box:
                                entry_params = await self._apply_bias(pair, params, "long")
                                if entry_params is not None:
                                    await self._open_position_ifdoco(
                                        pair, box, entry_params, "long"
                                    )

                    # near_upper → 숏 IFD-OCO
                    elif box_state == "near_upper" and prev_state != "near_upper":
                        if direction_mode in ("both", "short_only"):
                            box = await self._get_active_box(pair)
                            if box:
                                entry_params = await self._apply_bias(pair, params, "short")
                                if entry_params is not None:
                                    await self._open_position_ifdoco(
                                        pair, box, entry_params, "short"
                                    )

                    self._prev_box_state[pair] = box_state
                    continue  # 기존 MARKET 경로 건너뜀

                # ── near_lower 도달 ──────────────────────
                if box_state == "near_lower" and prev_state != "near_lower":
                    pos = await self._get_open_position(pair)

                    # 숏 포지션 청산 (both 모드)
                    if pos and pos.side == "sell" and direction_mode in ("both",):
                        try:
                            await self._close_position_market(pair, pos, "near_lower_exit")
                            pos = None  # 청산 완료
                        except Exception:
                            logger.error(
                                f"[BoxMgr] {pair}: 숏→롱 전환 중 청산 실패, 롱 진입 스킵"
                            )
                            self._prev_box_state[pair] = box_state
                            continue

                    # 롱 진입 (long_only 또는 both)
                    if direction_mode in ("long_only", "both") and pos is None:
                        box = await self._get_active_box(pair)
                        if box:
                            # 방향 바이어스 체크 (bearish면 사이즈 축소 또는 스킵)
                            entry_params = await self._apply_bias(pair, params, "long")
                            if entry_params is not None:
                                await self._open_position_market(
                                    pair, box, price, entry_params, direction="long"
                                )

                # ── near_upper 도달 ──────────────────────
                elif box_state == "near_upper" and prev_state != "near_upper":
                    pos = await self._get_open_position(pair)

                    # 롱 포지션 청산 (long_only 또는 both)
                    if pos and pos.side == "buy" and direction_mode in ("long_only", "both"):
                        try:
                            await self._close_position_market(pair, pos, "near_upper_exit")
                            pos = None  # 청산 완료
                        except Exception:
                            logger.error(
                                f"[BoxMgr] {pair}: 롱→숏 전환 중 청산 실패, 숏 진입 스킵"
                            )
                            self._prev_box_state[pair] = box_state
                            continue

                    # 숏 진입 (both 또는 short_only)
                    if direction_mode in ("both", "short_only") and pos is None:
                        box = await self._get_active_box(pair)
                        if box:
                            # 방향 바이어스 체크 (bullish면 사이즈 축소 또는 스킵)
                            entry_params = await self._apply_bias(pair, params, "short")
                            if entry_params is not None:
                                await self._open_position_market(
                                    pair, box, price, entry_params, direction="short"
                                )

                # ── 가격 기반 손절: 마지막 방어선 (매 tick 체크, 방향별) ──
                pos = await self._get_open_position(pair)
                if pos and pos.entry_price:
                    sl_pct = float(params.get("stop_loss_pct", 1.5))
                    if sl_pct > 0:
                        ep = float(pos.entry_price)
                        if pos.side == "buy":
                            sl_price = ep * (1 - sl_pct / 100)
                            sl_triggered = price <= sl_price
                        else:  # sell (숏)
                            sl_price = ep * (1 + sl_pct / 100)
                            sl_triggered = price >= sl_price

                        if sl_triggered:
                            logger.warning(
                                f"[BoxMgr] {pair}: 가격 SL 발동 "
                                f"side={pos.side} price={price} sl={sl_price:.4f} "
                                f"(entry={pos.entry_price}, sl_pct={sl_pct}%)"
                            )
                            await self._close_position_market(pair, pos, "price_stop_loss")

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
        cluster_percentile = float(params.get("box_cluster_percentile", 100.0))

        # 쿨다운 체크: 무효화 직후 노이즈성 재형성 방지 (32시간)
        last_inv = self._last_invalidation_time.get(pair)
        if last_inv is not None:
            elapsed_candles = (datetime.now(timezone.utc) - last_inv).total_seconds() / (4 * 3600)
            if elapsed_candles < _BOX_COOLDOWN_CANDLES:
                logger.debug(
                    f"[BoxMgr] {pair}: 쿨다운 중 ({elapsed_candles:.1f}/{_BOX_COOLDOWN_CANDLES}캔들)"
                )
                return None

        # 포지션 가드: 포지션 보유 중 신규 박스 생성 금지
        if await self._has_open_position(pair):
            logger.debug(f"[BoxMgr] {pair}: 포지션 보유 중 — 신규 박스 생성 금지")
            return None

        existing = await self._get_active_box(pair)
        if existing:
            logger.debug(f"[BoxMgr] {pair}: active 박스 이미 존재 (id={existing.id}), 감지 스킵")
            return None

        candles = await self._get_completed_candles(pair, basis_tf, lookback)
        if len(candles) < min_touches * 2:
            logger.debug(f"[BoxMgr] {pair}: 캔들 부족 ({len(candles)}개 < {min_touches * 2})")
            return None

        upper, upper_count = self._find_cluster(
            [self._candle_high(c) for c in candles],
            tolerance_pct, min_touches, mode="high", percentile=cluster_percentile,
        )
        lower, lower_count = self._find_cluster(
            [self._candle_low(c) for c in candles],
            tolerance_pct, min_touches, mode="low", percentile=cluster_percentile,
        )

        if upper is None or lower is None:
            logger.debug(f"[BoxMgr] {pair}: 박스 불형성 (upper={upper}, lower={lower})")
            return None

        if upper <= lower:
            logger.debug(f"[BoxMgr] {pair}: 상단 ≤ 하단, 박스 무효")
            return None

        # 최소 박스 폭: tolerance×2 + fee×2
        # 어댑터가 fee_rate_pct를 제공하면 우선 사용 (GMO FX 트라이얼 자동 전환)
        if hasattr(self._adapter, "fee_rate_pct"):
            fee_rate_pct = float(self._adapter.fee_rate_pct)
        else:
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
        box.strategy_id = self._strategy_id_map.get(pair)
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

        # ── 생성 직후 현재가가 outside이면 즉시 무효화 ──
        try:
            ticker = await self._adapter.get_ticker(pair)
            current_price = ticker.last
            if current_price:
                near_pct = float(params.get("near_bound_pct", 0.3)) / 100
                tol = tolerance_pct / 100
                cp = float(current_price)
                is_outside = cp < lower * (1 - tol) or cp > upper * (1 + tol)
                if is_outside:
                    await self._invalidate_box(box.id, "created_outside_price", pair=pair)
                    logger.info(
                        f"[BoxMgr] {pair}: 박스 생성 직후 현재가 outside → 즉시 무효화 "
                        f"(price={cp:.4f} box=[{lower:.4f}, {upper:.4f}])"
                    )
                    return None
        except Exception as e:
            logger.warning(f"[BoxMgr] {pair}: 박스 생성 후 outside 체크 실패 — {e}")

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
            await self._invalidate_box(box.id, reason, pair=pair)
            logger.info(
                f"[BoxMgr] 박스 무효화: {pair} id={box.id} reason={reason} "
                f"close={close} upper={upper} lower={lower}"
            )

        return reason

    async def _check_converging_triangle(
        self, pair: str, box: Any, params: Dict[str, Any],
    ) -> Optional[str]:
        """수렴 삼각형 감지: 고점 하락 + 저점 상승이면 'converging_triangle'.
        정본: core.strategy.box_signals.check_box_invalidation (D-4 부분)"""
        lookback = min(int(params.get("box_lookback_candles", 60)), 20)
        basis_tf = params.get("basis_timeframe", "4h")
        candles = await self._get_completed_candles(pair, basis_tf, lookback)
        if len(candles) < 8:
            return None

        highs = [self._candle_high(c) for c in candles]
        lows = [self._candle_low(c) for c in candles]

        xs = list(range(len(highs)))
        high_slope = linear_slope(xs, highs)
        low_slope = linear_slope(xs, lows)

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

        진입 범위는 near_bound_pct(기본 0.3%) 기준 양방향 경계 사용.
        정본: core.strategy.box_signals.classify_price_in_box
        """
        box = await self._get_active_box(pair)
        if not box:
            return None

        upper = float(box.upper_bound)
        lower = float(box.lower_bound)
        params = self._params.get(pair, {})
        near_pct = float(params.get("near_bound_pct", 0.3))

        return classify_price_in_box(price, upper, lower, near_pct)

    # ──────────────────────────────────────────
    # 주문 실행
    # ──────────────────────────────────────────

    async def _open_position_market(
        self,
        pair: str,
        box: Any,
        price: float,
        params: Dict[str, Any],
        direction: str = "long",
    ) -> None:
        """market 자동 진입 + DB 포지션 기록. direction: 'long' | 'short'."""
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

            is_margin = getattr(self._adapter, "is_margin_trading", False)

            if is_margin:
                # FX: invest_jpy를 통화 수량으로 변환 (1,000통화 단위 내림)
                leverage = float(params.get("leverage", 1))
                lot_unit = int(params.get("lot_unit", 1000))
                size_raw = invest_jpy * leverage / price
                order_size = math.floor(size_raw / lot_unit) * lot_unit
                min_lot = int(params.get("min_lot_size", 1000))
                if order_size < min_lot:
                    logger.info(
                        f"[BoxMgr] {pair}: FX 수량({order_size}) < 최소 로트({min_lot}), 진입 스킵"
                    )
                    return
                order_type = OrderType.MARKET_BUY if direction == "long" else OrderType.MARKET_SELL
                order = await self._get_executor(pair).place_order(
                    self._adapter, order_type, pair, float(order_size),
                )
                exec_price = order.price or price
                exec_amount = order.amount or float(order_size)

                # positionId 확보: get_positions()로 직후 조회
                exchange_position_id = await self._find_exchange_position_id(pair)

                # 페이퍼 진입 기록 (PaperExecutor 시)
                strategy_id = params.get("strategy_id", 0)
                paper_id = await self._get_executor(pair).record_paper_entry(
                    strategy_id=strategy_id,
                    pair=pair,
                    direction=direction,
                    entry_price=exec_price,
                )
                if paper_id is not None:
                    self._cached_position[pair] = {"paper_trade_id": paper_id, "entry_price": exec_price, "invest_jpy": invest_jpy, "direction": direction}
                    return  # 페이퍼: DB box_position 기록 스킵

                await self._record_open_position(
                    pair=pair,
                    box_id=box.id,
                    entry_order_id=order.order_id,
                    entry_price=exec_price,
                    entry_amount=exec_amount,
                    entry_jpy=invest_jpy,
                    exchange_position_id=exchange_position_id,
                    direction=direction,
                )

                # 거래소 역지정주문 SL 등록 (FX, real trade, positionId 확보 시에만)
                if exchange_position_id is not None:
                    sl_pct = float(params.get("stop_loss_pct", 1.5))
                    sl_price = (
                        exec_price * (1 - sl_pct / 100)
                        if direction == "long"
                        else exec_price * (1 + sl_pct / 100)
                    )
                    await self._register_exchange_stop_loss(
                        pair=pair,
                        direction=direction,
                        position_id=int(exchange_position_id),
                        size=int(exec_amount),
                        sl_price=sl_price,
                    )
            else:
                # 현물: 항상 롱(MARKET_BUY). is_margin=False 시 direction=long 강제.
                order = await self._get_executor(pair).place_order(
                    self._adapter, OrderType.MARKET_BUY, pair, invest_jpy,
                )
                exec_price = order.price or price
                exec_amount = order.amount
                if exec_amount == 0 and exec_price > 0:
                    exec_amount = invest_jpy / exec_price

                # 페이퍼 진입 기록 (PaperExecutor 시)
                strategy_id = params.get("strategy_id", 0)
                paper_id = await self._get_executor(pair).record_paper_entry(
                    strategy_id=strategy_id,
                    pair=pair,
                    direction="long",
                    entry_price=exec_price,
                )
                if paper_id is not None:
                    self._cached_position[pair] = {"paper_trade_id": paper_id, "entry_price": exec_price, "invest_jpy": invest_jpy, "direction": "long"}
                    return  # 페이퍼: DB box_position 기록 스킵

                await self._record_open_position(
                    pair=pair,
                    box_id=box.id,
                    entry_order_id=order.order_id,
                    entry_price=exec_price,
                    entry_amount=exec_amount,
                    entry_jpy=invest_jpy,
                    direction="long",
                )

            logger.info(
                f"[BoxMgr] {pair}: 자동 진입 완료 "
                f"direction={direction} order_id={order.order_id} "
                f"price={exec_price} amount={exec_amount}"
            )
        except Exception as e:
            logger.error(f"[BoxMgr] {pair}: 진입 주문 오류 — {e}", exc_info=True)

    async def _close_position_market(
        self, pair: str, pos: Any, reason: str,
    ) -> None:
        """자동 청산. 현물은 MARKET_SELL, FX는 closeOrder(positionId)."""
        try:
            # 페이퍼 트레이드: DB box_position 없이 paper_trades만 갱신
            cached = self._cached_position.get(pair)
            if isinstance(cached, dict) and "paper_trade_id" in cached:
                paper_id = cached["paper_trade_id"]
                entry_price = cached.get("entry_price", 0.0)
                invest_jpy = cached.get("invest_jpy", 0.0)
                direction = cached.get("direction", "long")
                try:
                    ticker = await self._adapter.get_ticker(pair)
                    exit_price = ticker.last
                except Exception:
                    exit_price = entry_price
                if paper_id:
                    await self._get_executor(pair).record_paper_exit(
                        paper_trade_id=paper_id,
                        exit_price=exit_price,
                        exit_reason=reason,
                        entry_price=entry_price,
                        invest_jpy=invest_jpy,
                        direction=direction,
                    )
                self._cached_position[pair] = None
                logger.info(
                    f"[BoxMgr] {pair}: 페이퍼 청산 완료 "
                    f"reason={reason} exit_price={exit_price}"
                )
                return

            is_margin = getattr(self._adapter, "is_margin_trading", False)

            if is_margin:
                # IFD-OCO 활성 시: OCO 취소 후 청산 (이중 체결 방지)
                await self._cancel_active_ifdoco(pair)
                # 서버가 먼저 청산 → 거래소 SL 취소 (중복 체결 방지)
                await self._cancel_exchange_stop_loss(pair)
                await self._close_position_market_fx(pair, pos, reason)
            else:
                await self._close_position_market_spot(pair, pos, reason)

            # T1 트리거: real 청산 완료 직후 全 전략 Score 스냅샷 수집 (P-1)
            if self._snapshot_collector is not None:
                asyncio.create_task(
                    self._snapshot_collector.collect_all_snapshots(
                        "T1_position_close", pair
                    )
                )
        except Exception as e:
            logger.error(f"[BoxMgr] {pair}: 청산 주문 오류 — {e}", exc_info=True)

    async def _close_position_market_spot(
        self, pair: str, pos: Any, reason: str,
    ) -> None:
        """현물 청산: 코인 잔고 MARKET_SELL."""
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

        order = await self._get_executor(pair).place_order(
            self._adapter, OrderType.MARKET_SELL, pair, sell_amount,
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

        # BUG-009: 청산 후 dust 잔고 감지 로깅 (현물 전용)
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

    async def _close_position_market_fx(
        self, pair: str, pos: Any, reason: str,
    ) -> None:
        """FX 청산: closeOrder(positionId)로 건옥 결제. 롱/숏 양방향 대응."""
        symbol = pair.upper()

        # 1. positionId 확보: DB 저장값 우선, 없으면 API 조회 매칭
        exchange_pid = getattr(pos, "exchange_position_id", None)
        close_size = int(float(pos.entry_amount))

        if exchange_pid:
            position_id = int(exchange_pid)
        else:
            # DB에 positionId 없으면 get_positions()로 매칭 시도
            position_id = await self._match_fx_position_id(
                pair, float(pos.entry_price), close_size, pos.side,
            )
            if position_id is None:
                logger.error(
                    f"[BoxMgr] {pair}: FX 청산 실패 — positionId 매칭 불가 "
                    f"(entry_price={pos.entry_price}, amount={pos.entry_amount}, side={pos.side})"
                )
                return

        # 2. close_side: 롱(buy) → SELL 청산, 숏(sell) → BUY 청산
        close_side = "SELL" if pos.side == "buy" else "BUY"

        order = await self._adapter.close_position(
            symbol=symbol,
            side=close_side,
            position_id=position_id,
            size=close_size,
        )

        # 3. 체결가 확보
        exec_price = order.price or 0.0
        if exec_price == 0:
            try:
                ticker = await self._adapter.get_ticker(pair)
                exec_price = ticker.last
                logger.warning(f"[BoxMgr] {pair}: FX 체결가 미반환, ticker={exec_price}로 대체")
            except Exception as te:
                logger.warning(f"[BoxMgr] {pair}: ticker 조회 실패 — {te}")
        exec_amount = order.amount or float(close_size)

        await self._record_close_position(
            pair=pair,
            exit_order_id=order.order_id,
            exit_price=exec_price,
            exit_amount=exec_amount,
            exit_reason=reason,
        )
        logger.info(
            f"[BoxMgr] {pair}: FX 청산 완료 "
            f"reason={reason} positionId={position_id} "
            f"order_id={order.order_id} price={exec_price}"
        )

    # ──────────────────────────────────────────
    # IFD-OCO 주문 실행 (FX 전용)
    # ──────────────────────────────────────────

    async def _open_position_ifdoco(
        self, pair: str, box: Any, params: Dict[str, Any], direction: str
    ) -> None:
        """GMO FX IFD-OCO 지정가 주문 발주. pending 상태로 거래소에 등록만 한다."""
        try:
            balance = await self._adapter.get_balance()
            jpy_available = balance.get_available("jpy")
            position_size_pct = float(params.get("position_size_pct", 10.0))
            invest_jpy = jpy_available * position_size_pct / 100
            min_jpy = float(params.get("min_order_jpy", 500))
            if invest_jpy < min_jpy:
                logger.info(f"[BoxMgr] {pair}: IFD-OCO 잔고 부족 invest={invest_jpy:.0f} < min={min_jpy:.0f}")
                return

            ticker = await self._adapter.get_ticker(pair)
            ref_price = float(ticker.last) if ticker.last else float(box.lower_bound)
            if ref_price <= 0:
                return
            leverage = float(params.get("leverage", 1))
            lot_unit = int(params.get("lot_unit", 1000))
            order_size = math.floor(invest_jpy * leverage / ref_price / lot_unit) * lot_unit
            min_lot = int(params.get("min_lot_size", 1000))
            if order_size < min_lot:
                logger.info(f"[BoxMgr] {pair}: IFD-OCO lot 부족 size={order_size} < min={min_lot}")
                return

            upper = float(box.upper_bound)
            lower = float(box.lower_bound)
            sl_pct = float(params.get("stop_loss_pct", 1.5))

            if direction == "long":
                entry_price = lower
                tp_price = upper
                sl_price = lower * (1 - sl_pct / 100)
                side = "BUY"
            else:
                entry_price = upper
                tp_price = lower
                sl_price = upper * (1 + sl_pct / 100)
                side = "SELL"

            round_fn = getattr(self._adapter, "_round_price", lambda p, v: v)
            entry_price = round_fn(pair, entry_price)
            tp_price = round_fn(pair, tp_price)
            sl_price = round_fn(pair, sl_price)

            response = await self._adapter.place_ifdoco_order(
                pair=pair,
                side=side,
                size=int(order_size),
                first_execution_type="LIMIT",
                first_price=entry_price,
                take_profit_price=tp_price,
                stop_loss_price=sl_price,
            )
            root_order_id = str(response.get("rootOrderId", ""))
            if not root_order_id:
                logger.error(f"[BoxMgr] {pair}: IFD-OCO rootOrderId 미반환")
                return

            self._ifdoco_orders[pair] = root_order_id
            self._ifdoco_meta[pair] = {
                "root_order_id": root_order_id,
                "direction": direction,
                "entry_price": entry_price,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "order_size": int(order_size),
                "box_id": box.id,
                "invest_jpy": invest_jpy,
            }
            logger.info(
                f"[BoxMgr] {pair}: IFD-OCO 발주 완료 "
                f"direction={direction} root_id={root_order_id} "
                f"entry={entry_price} tp={tp_price} sl={sl_price}"
            )
        except Exception as e:
            logger.error(f"[BoxMgr] {pair}: IFD-OCO 발주 오류 — {e}", exc_info=True)

    async def _cancel_active_ifdoco(self, pair: str) -> None:
        """등록된 IFD-OCO 주문 취소. pending 또는 first_filled 상태에서 강제 청산 시 호출."""
        root_id = self._ifdoco_orders.pop(pair, None)
        self._ifdoco_meta.pop(pair, None)
        if root_id:
            try:
                await self._adapter.cancel_order(root_id, pair)
                logger.info(f"[BoxMgr] {pair}: IFD-OCO 취소 완료 (root_id={root_id})")
            except Exception as e:
                logger.warning(f"[BoxMgr] {pair}: IFD-OCO 취소 실패 — {e}")

    async def _poll_ifdoco_status(self, pair: str) -> None:
        """IFD-OCO 체결 상태 폴링. 60초 주기 모니터에서 호출."""
        root_id = self._ifdoco_orders.get(pair)
        if not root_id:
            return
        try:
            sub_orders = await self._adapter.get_orders_by_root(root_id)
        except Exception as e:
            logger.warning(f"[BoxMgr] {pair}: IFD-OCO 폴링 실패 — {e}")
            return
        if not sub_orders:
            return

        open_order = next(
            (o for o in sub_orders if o.get("settleType") == "OPEN"), None
        )
        tp_order = next(
            (o for o in sub_orders
             if o.get("settleType") == "CLOSE" and o.get("executionType") == "LIMIT"),
            None,
        )
        sl_order = next(
            (o for o in sub_orders
             if o.get("settleType") == "CLOSE" and o.get("executionType") == "STOP"),
            None,
        )

        # TP 체결 감지 (우선순위: TP > SL > 1차 체결)
        if tp_order and tp_order.get("status") == "EXECUTED":
            await self._handle_ifdoco_completion(pair, "completed_tp", tp_order)
        elif sl_order and sl_order.get("status") == "EXECUTED":
            await self._handle_ifdoco_completion(pair, "completed_sl", sl_order)
        elif open_order and open_order.get("status") == "EXECUTED":
            # 1차 체결 — DB pos가 아직 없는 경우에만 기록
            pos = await self._get_open_position(pair)
            if pos is None:
                await self._handle_ifdoco_first_fill(pair, open_order)
        elif any(o.get("status") == "CANCELED" for o in sub_orders):
            logger.info(f"[BoxMgr] {pair}: IFD-OCO CANCELED 감지 → 메모리 정리")
            self._ifdoco_orders.pop(pair, None)
            self._ifdoco_meta.pop(pair, None)

    async def _handle_ifdoco_first_fill(self, pair: str, open_order: Dict) -> None:
        """IFD-OCO 1차(진입) 체결 처리: DB 포지션 생성 + ifdoco_status='first_filled' 기록."""
        meta = self._ifdoco_meta.get(pair) or {}
        entry_price = float(open_order.get("price", meta.get("entry_price", 0)))
        order_size = float(open_order.get("size", meta.get("order_size", 0)))
        direction = meta.get("direction", "long")
        box_id = meta.get("box_id")
        invest_jpy = meta.get("invest_jpy")

        if not entry_price or not order_size:
            logger.warning(f"[BoxMgr] {pair}: IFD-OCO first_fill 메타 부족 — 건너뜀")
            return

        exchange_position_id = await self._find_exchange_position_id(pair)
        await self._record_open_position(
            pair=pair,
            box_id=box_id,
            entry_order_id=self._ifdoco_orders.get(pair, ""),
            entry_price=entry_price,
            entry_amount=order_size,
            entry_jpy=invest_jpy,
            exchange_position_id=exchange_position_id,
            direction=direction,
        )
        await self._update_ifdoco_db(
            pair,
            status="first_filled",
            root_order_id=self._ifdoco_orders.get(pair),
            tp_price=meta.get("tp_price"),
            sl_price=meta.get("sl_price"),
        )
        logger.info(
            f"[BoxMgr] {pair}: IFD-OCO 1차 체결 기록 "
            f"entry={entry_price} size={order_size} direction={direction}"
        )

    async def _handle_ifdoco_completion(
        self, pair: str, completion_type: str, executed_order: Dict
    ) -> None:
        """IFD-OCO TP 또는 SL 체결 처리: DB 포지션 closed + 스냅샷 트리거."""
        meta = self._ifdoco_meta.get(pair) or {}
        direction = meta.get("direction", "long")
        exit_price = float(executed_order.get("price", 0))

        if completion_type == "completed_tp":
            exit_reason = "near_upper_exit" if direction == "long" else "near_lower_exit"
        else:
            exit_reason = "price_stop_loss"

        pos = await self._get_open_position(pair)
        if pos:
            exit_amount = float(pos.entry_amount)
            if not exit_price:
                try:
                    ticker = await self._adapter.get_ticker(pair)
                    exit_price = ticker.last
                except Exception:
                    exit_price = float(pos.entry_price)
            order_id = str(executed_order.get("orderId", "ifdoco_completion"))
            await self._record_close_position(
                pair=pair,
                exit_order_id=order_id,
                exit_price=exit_price,
                exit_amount=exit_amount,
                exit_reason=exit_reason,
            )
            await self._update_ifdoco_db(pair, status=completion_type)

        self._ifdoco_orders.pop(pair, None)
        self._ifdoco_meta.pop(pair, None)
        logger.info(
            f"[BoxMgr] {pair}: IFD-OCO 완료 {completion_type} exit={exit_price}"
        )

        # both 모드 + TP 체결 → 반대 방향 IFD-OCO 자동 재발주
        if completion_type == "completed_tp":
            params = self._params.get(pair, {})
            if (
                params.get("direction_mode") == "both"
                and params.get("use_ifdoco")
                and getattr(self._adapter, "is_margin_trading", False)
            ):
                box = await self._get_active_box(pair)
                if box:
                    next_direction = "short" if direction == "long" else "long"
                    entry_params = await self._apply_bias(pair, params, next_direction)
                    if entry_params is not None:
                        await self._open_position_ifdoco(pair, box, entry_params, next_direction)

        if self._snapshot_collector is not None:
            asyncio.create_task(
                self._snapshot_collector.collect_all_snapshots("T1_position_close", pair)
            )

    async def _update_ifdoco_db(
        self,
        pair: str,
        *,
        status: str,
        root_order_id: Optional[str] = None,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
    ) -> None:
        """open 포지션의 ifdoco 컬럼 갱신. 실패해도 매매 로직에 영향 없음."""
        try:
            async with self._session_factory() as session:
                BoxPos = self._box_position_model
                pair_col = getattr(BoxPos, self._pair_column)
                values: Dict[str, Any] = {"ifdoco_status": status}
                if root_order_id is not None:
                    values["ifdoco_root_order_id"] = root_order_id
                if tp_price is not None:
                    values["tp_price"] = tp_price
                if sl_price is not None:
                    values["sl_price_registered"] = sl_price
                await session.execute(
                    update(BoxPos)
                    .where(pair_col == pair, BoxPos.status == "open")
                    .values(**values)
                )
                await session.commit()
        except Exception as e:
            logger.warning(f"[BoxMgr] {pair}: ifdoco DB 갱신 실패 — {e}")

    # ──────────────────────────────────────────
    # 거래소 역지정주문 SL 관리 (FX 전용)
    # ──────────────────────────────────────────

    async def _update_exchange_sl_db(
        self, pair: str, *, status: str,
        order_id: Optional[str] = None,
        price: Optional[float] = None,
    ) -> None:
        """open 포지션의 exchange_sl 컬럼 갱신. 실패해도 매매 로직에 영향 없음."""
        try:
            async with self._session_factory() as session:
                BoxPos = self._box_position_model
                pair_col = getattr(BoxPos, self._pair_column)
                values: dict = {"exchange_sl_status": status}
                if order_id is not None:
                    values["exchange_sl_order_id"] = order_id
                if price is not None:
                    values["exchange_sl_price"] = price
                stmt = (
                    update(BoxPos)
                    .where(pair_col == pair, BoxPos.status == "open")
                    .values(**values)
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as e:
            logger.warning(f"[BoxMgr] {pair}: exchange_sl DB 갱신 실패 — {e}")

    async def _register_exchange_stop_loss(
        self, pair: str, direction: str, position_id: int, size: int, sl_price: float,
    ) -> None:
        """거래소에 역지정(STOP) SL 주문 등록. FX + real trade 전용.

        이중 안전망: 서버 장애/WS 끊김 시에도 거래소가 직접 SL을 실행한다.
        등록 실패 시 경고만 하고 서버 감시를 계속 유지한다.
        """
        try:
            close_side = "SELL" if direction == "long" else "BUY"
            symbol = pair.upper()
            order = await self._adapter.close_order_stop(
                symbol=symbol,
                side=close_side,
                position_id=position_id,
                size=size,
                trigger_price=sl_price,
            )
            self._exchange_sl_orders[pair] = order.order_id
            logger.info(
                f"[BoxMgr] {pair}: 거래소 SL 등록 완료 "
                f"(order_id={order.order_id}, trigger={sl_price:.4f})"
            )
            await self._update_exchange_sl_db(
                pair, status="registered", order_id=order.order_id, price=sl_price
            )
        except Exception as e:
            logger.warning(f"[BoxMgr] {pair}: 거래소 SL 등록 실패 — {e} (서버 감시로 계속)")
            await self._update_exchange_sl_db(pair, status="failed")

    async def _cancel_exchange_stop_loss(self, pair: str) -> None:
        """거래소에 등록된 SL 주문 취소. 서버 SL 발동 또는 포지션 청산 시 호출."""
        sl_order_id = self._exchange_sl_orders.pop(pair, None)
        if sl_order_id:
            try:
                await self._adapter.cancel_order(sl_order_id, pair)
                logger.info(f"[BoxMgr] {pair}: 거래소 SL 주문 취소 (order_id={sl_order_id})")
                await self._update_exchange_sl_db(pair, status="cancelled")
            except Exception as e:
                logger.warning(f"[BoxMgr] {pair}: 거래소 SL 취소 실패 — {e}")

    async def _sync_exchange_sl_status(self, pair: str) -> None:
        """거래소 SL 체결 감지 → DB 포지션 동기화 (60초 주기 모니터에서 호출).

        거래소가 SL을 실행했는데 서버는 아직 open 상태인 경우를 감지하여
        DB를 exchange_stop_loss 사유로 closed 처리한다.
        """
        if self._exchange_sl_orders.get(pair) is None:
            return

        pos = await self._get_open_position(pair)
        if pos is None:
            # 이미 서버에서 closed (서버 SL이 먼저 발동됨)
            self._exchange_sl_orders.pop(pair, None)
            return

        try:
            exchange_pid = getattr(pos, "exchange_position_id", None)
            if not exchange_pid:
                return
            positions = await self._adapter.get_positions(pair.upper())
            pid_int = int(exchange_pid)
            still_open = any(p.position_id == pid_int for p in positions)
            if not still_open:
                # 거래소 SL 체결됨 → 서버 동기화
                exec_price = 0.0
                try:
                    ticker = await self._adapter.get_ticker(pair)
                    exec_price = ticker.last
                except Exception:
                    exec_price = float(pos.entry_price)
                sl_order_id = self._exchange_sl_orders.pop(pair, None) or "exchange_sl"
                await self._record_close_position(
                    pair=pair,
                    exit_order_id=sl_order_id,
                    exit_price=exec_price,
                    exit_amount=float(pos.entry_amount),
                    exit_reason="exchange_stop_loss",
                )
                logger.info(
                    f"[BoxMgr] {pair}: 거래소 SL 체결 감지 → 서버 동기화 완료 "
                    f"(price≈{exec_price:.4f})"
                )
        except Exception as e:
            logger.warning(f"[BoxMgr] {pair}: 거래소 SL 동기화 실패 — {e}")

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
        exchange_position_id: Optional[str] = None,
        direction: str = "long",
    ) -> Any:
        """진입 주문 직후 포지션 기록. direction: 'long'→side='buy', 'short'→side='sell'."""
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
        pos.side = "buy" if direction == "long" else "sell"
        pos.entry_order_id = str(entry_order_id)
        pos.entry_price = Decimal(str(entry_price))
        pos.entry_amount = Decimal(str(entry_amount))
        pos.entry_jpy = Decimal(str(entry_jpy)) if entry_jpy is not None else None
        if exchange_position_id is not None:
            pos.exchange_position_id = str(exchange_position_id)
        pos.status = "open"
        pos.created_at = datetime.now(timezone.utc)

        async with self._session_factory() as db:
            db.add(pos)
            await db.commit()
            await db.refresh(pos)

        self._cached_position[pair] = pos  # 캐시 갱신
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
        # 방향별 PnL: 롱은 (exit-entry)*amount, 숏은 (entry-exit)*amount
        if pos.side == "buy":
            pnl_jpy = (exit_price - ep) * min(exit_amount, ea)
        else:  # sell (숏)
            pnl_jpy = (ep - exit_price) * min(exit_amount, ea)
        pnl_pct = pnl_jpy / (ep * min(exit_amount, ea)) * 100 if ep > 0 and ea > 0 else 0.0

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

        self._cached_position[pair] = None  # 캐시 해제: 포지션 없음
        logger.info(
            f"[BoxMgr] 청산 기록: {pair} "
            f"reason={exit_reason} pnl={pnl_jpy:+.2f}JPY ({pnl_pct:+.2f}%)"
        )
        return pos

    # ──────────────────────────────────────────
    # 인터마켓 바이어스 적용
    # ──────────────────────────────────────────

    async def _apply_bias(
        self, pair: str, params: dict, direction: str
    ) -> Optional[dict]:
        """
        방향 바이어스를 확인하고 진입 파라미터를 조정한다.

        - intermarket_bias_enabled=False(기본) → params 그대로 반환 (기능 비활성)
        - bias가 진입 방향에 반하면 bias_action에 따라:
            "skip"        → None 반환 (진입 취소)
            "reduce_size" → position_size_pct 축소 후 반환
        - API 실패 또는 neutral → params 그대로 반환

        Returns:
            None  → 진입 취소
            dict  → 사용할 params (사이즈 조정 포함 가능)
        """
        if not self._intermarket_client or not params.get("intermarket_bias_enabled", False):
            return params

        try:
            bias, confidence, reasons = await self._intermarket_client.get_direction_bias(
                pair, params
            )
        except Exception as e:
            logger.debug(f"[BoxMgr] {pair}: 인터마켓 바이어스 조회 실패 ({e}) — 진입 허용")
            return params

        # 바이어스가 진입 방향에 반하는지 판단
        opposed = (direction == "long" and bias == "bearish") or (
            direction == "short" and bias == "bullish"
        )
        if not opposed:
            return params

        action = params.get("bias_action", "reduce_size")
        logger.info(
            f"[BoxMgr] {pair}: 바이어스 반대 ({bias} vs {direction}) "
            f"confidence={confidence:.2f} reasons={reasons[:2]} action={action}"
        )

        if action == "skip":
            return None

        # reduce_size
        factor = float(params.get("bias_size_factor", 0.5))
        adjusted = dict(params)
        original_size = float(params.get("position_size_pct", 50.0))
        adjusted["position_size_pct"] = round(original_size * factor, 1)
        logger.info(
            f"[BoxMgr] {pair}: 사이즈 축소 {original_size}% → {adjusted['position_size_pct']}%"
        )
        return adjusted

    # ──────────────────────────────────────────
    # DB: 박스 조회/관리
    # ──────────────────────────────────────────

    async def _get_active_box(self, pair: str) -> Optional[Any]:
        """현재 active 박스 반환. strategy_id로 격리 (None = active 전략, N = paper 전략)."""
        BoxModel = self._box_model
        pair_col_attr = getattr(BoxModel, self._pair_column)
        strategy_id = self._strategy_id_map.get(pair)
        async with self._session_factory() as db:
            if strategy_id is None:
                sid_filter = BoxModel.strategy_id.is_(None)
            else:
                sid_filter = BoxModel.strategy_id == strategy_id
            result = await db.execute(
                select(BoxModel)
                .where(and_(pair_col_attr == pair, BoxModel.status == "active", sid_filter))
                .order_by(desc(BoxModel.created_at))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def _invalidate_box(self, box_id: int, reason: str, pair: Optional[str] = None) -> None:
        """박스를 invalidated 처리. pair를 넘기면 쿨다운 타이머 시작."""
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
        if pair is not None:
            self._last_invalidation_time[pair] = datetime.now(timezone.utc)
            logger.info(f"[BoxMgr] {pair}: 쿨다운 시작 ({_BOX_COOLDOWN_CANDLES}캔들 = 32시간)")

    # ──────────────────────────────────────────
    # DB: 포지션 조회
    # ──────────────────────────────────────────

    async def _get_open_position(self, pair: str) -> Optional[Any]:
        """현재 open 포지션 반환. 인메모리 캐시 우선 — tick 루프의 DB 부하 최소화."""
        if pair in self._cached_position:
            return self._cached_position[pair]
        # cold path: 최초 또는 캐시 무효화 후 DB 조회
        PosModel = self._box_position_model
        pair_col_attr = getattr(PosModel, self._pair_column)
        async with self._session_factory() as db:
            result = await db.execute(
                select(PosModel)
                .where(and_(pair_col_attr == pair, PosModel.status == "open"))
                .order_by(desc(PosModel.created_at))
                .limit(1)
            )
            pos = result.scalar_one_or_none()
        self._cached_position[pair] = pos
        return pos

    async def _has_open_position(self, pair: str) -> bool:
        """open 포지션 존재 여부."""
        return (await self._get_open_position(pair)) is not None

    # ──────────────────────────────────────────
    # FX: positionId 헬퍼
    # ──────────────────────────────────────────

    async def _find_exchange_position_id(self, pair: str) -> Optional[str]:
        """진입 직후 get_positions()로 최신 positionId를 반환."""
        try:
            positions = await self._adapter.get_positions(pair.upper())
            if positions:
                # 가장 최근 포지션 (진입 직후이므로 마지막이 해당)
                latest = max(positions, key=lambda p: p.open_date or datetime.min)
                if latest.position_id is not None:
                    return str(latest.position_id)
        except Exception as e:
            logger.warning(f"[BoxMgr] {pair}: positionId 조회 실패 — {e}")
        return None

    async def _match_fx_position_id(
        self, pair: str, entry_price: float, size: int, side: str = "buy",
    ) -> Optional[int]:
        """get_positions()에서 진입가/수량/방향으로 매칭하여 positionId 반환."""
        # DB side('buy'/'sell') → GMO API side('BUY'/'SELL') 변환
        api_side = "BUY" if side == "buy" else "SELL"
        try:
            positions = await self._adapter.get_positions(pair.upper())
            for p in positions:
                if p.side == api_side and int(p.size) == size:
                    # 진입가 근사 매칭 (0.1% 이내)
                    if entry_price > 0 and abs(p.price - entry_price) / entry_price < 0.001:
                        return p.position_id
            # 정확한 매칭 실패 시, 해당 방향 포지션 중 단일 건이면 반환
            side_positions = [p for p in positions if p.side == api_side]
            if len(side_positions) == 1 and side_positions[0].position_id is not None:
                return side_positions[0].position_id
        except Exception as e:
            logger.warning(f"[BoxMgr] {pair}: FX 포지션 매칭 실패 — {e}")
        return None

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
        """꼬리 포함 고점 (candle.high) — 백테스트/box-detect API와 통일."""
        return float(candle.high)

    @staticmethod
    def _candle_low(candle: Any) -> float:
        """꼬리 포함 저점 (candle.low) — 백테스트/box-detect API와 통일."""
        return float(candle.low)

    @staticmethod
    def _find_cluster(
        prices: list[float],
        tolerance_pct: float,
        min_touches: int,
        mode: str,
        percentile: float = 100.0,
    ) -> tuple[Optional[float], int]:
        """core.analysis.box_detector.find_cluster_percentile 위임."""
        return find_cluster_percentile(prices, tolerance_pct, min_touches, mode, percentile)


