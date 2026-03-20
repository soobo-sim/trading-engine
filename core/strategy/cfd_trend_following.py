"""
CfdTrendFollowingManager — BitFlyer CFD (FX_BTC_JPY) 전용 추세추종 매니저.

현물 TrendFollowingManager와 동일한 signals.py를 재사용하되,
CFD 고유 로직을 추가한다:
- 롱/숏 양방향 진입
- 증거금(getcollateral) 기반 주문
- keep_rate 모니터링 → 자동 청산
- 포지션 보유 시간 제한 → 스왑 비용 관리
- getpositions으로 실 포지션 정합성 검사

모든 리스크 수치는 strategy.parameters에서 읽는다 (하드코딩 금지).

아키텍처:
    main.py (EXCHANGE=bitflyer 전용)
      → BitFlyerAdapter
      → CfdTrendFollowingManager (이 클래스)
        → TaskSupervisor
        → signals.py
        → ORM models (bf_cfd_positions)

태스크 구성 (product_code당 2개):
    1. CandleMonitor  — 60초 폴링, 시그널 계산 → 진입/청산/트레일링
    2. StopLossMonitor — WS 틱 기반 하드 스탑 + keep_rate 감시
"""
from __future__ import annotations

import asyncio
import logging
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


class CfdTrendFollowingManager:
    """
    BitFlyer CFD (FX_BTC_JPY) 추세추종 매니저.

    현물 TrendFollowingManager와 동일 구조 (start/stop/stop_all + 태스크 2개).
    CFD 고유 로직: 양방향, 증거금 기반, keep_rate 감시, 보유 시간 제한.
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        supervisor: TaskSupervisor,
        session_factory: async_sessionmaker[AsyncSession],
        candle_model: Type,
        cfd_position_model: Type,
        pair_column: str = "product_code",
    ) -> None:
        self._adapter = adapter
        self._supervisor = supervisor
        self._session_factory = session_factory
        self._candle_model = candle_model
        self._cfd_position_model = cfd_position_model
        self._pair_column = pair_column

        # product_code별 상태
        self._params: Dict[str, Dict] = {}
        self._position: Dict[str, Optional[Position]] = {}
        self._latest_price: Dict[str, float] = {}
        self._last_seen_open_time: Dict[str, Optional[str]] = {}
        self._ema_slope_history: Dict[str, List[float]] = {}
        self._ema_slope_last_key: Dict[str, Optional[str]] = {}

        # 스탑로스 백오프
        self._close_fail_count: Dict[str, int] = {}
        self._close_fail_until: Dict[str, float] = {}
        # 잔고 정합성 검사 카운터
        self._position_sync_counter: Dict[str, int] = {}

    # ──────────────────────────────────────────
    # 시작 / 종료
    # ──────────────────────────────────────────

    async def start(self, product_code: str, params: Dict) -> None:
        """product_code에 대한 CFD 추세추종 태스크 2개 등록."""
        self._params[product_code] = params
        self._last_seen_open_time[product_code] = None
        self._latest_price.pop(product_code, None)
        self._ema_slope_history[product_code] = []
        self._ema_slope_last_key[product_code] = None
        self._close_fail_count[product_code] = 0
        self._close_fail_until[product_code] = 0

        # 기존 포지션 복원
        pos = await self._detect_existing_position(product_code)
        self._position[product_code] = pos
        if pos:
            pos.db_record_id = await self._recover_db_position_id(product_code)

        await self._supervisor.register(
            f"cfd_candle:{product_code}",
            lambda pc=product_code: self._candle_monitor(pc),
            max_restarts=5,
        )
        await self._supervisor.register(
            f"cfd_stoploss:{product_code}",
            lambda pc=product_code: self._stop_loss_monitor(pc),
            max_restarts=5,
        )

        logger.info(
            f"[CfdMgr] {product_code}: CFD 추세추종 시작 "
            f"(position={'있음' if pos else '없음'}, exchange={self._adapter.exchange_name})"
        )

    async def stop(self, product_code: str) -> None:
        """product_code에 대한 CFD 태스크 종료."""
        await self._supervisor.stop(f"cfd_candle:{product_code}")
        await self._supervisor.stop(f"cfd_stoploss:{product_code}")
        self._params.pop(product_code, None)
        self._position.pop(product_code, None)
        self._last_seen_open_time.pop(product_code, None)
        self._latest_price.pop(product_code, None)
        self._ema_slope_history.pop(product_code, None)
        self._ema_slope_last_key.pop(product_code, None)
        self._close_fail_count.pop(product_code, None)
        self._close_fail_until.pop(product_code, None)
        logger.info(f"[CfdMgr] {product_code}: CFD 추세추종 태스크 종료")

    async def stop_all(self) -> None:
        for pc in list(self._params.keys()):
            await self.stop(pc)
        logger.info("[CfdMgr] 전체 CFD 추세추종 인프라 종료")

    def is_running(self, product_code: str) -> bool:
        return (
            self._supervisor.is_running(f"cfd_candle:{product_code}")
            or self._supervisor.is_running(f"cfd_stoploss:{product_code}")
        )

    def running_pairs(self) -> list[str]:
        return [pc for pc in self._params if self.is_running(pc)]

    def get_position(self, product_code: str) -> Optional[Position]:
        return self._position.get(product_code)

    def get_task_health(self) -> dict:
        result: Dict[str, dict] = {}
        for pc in self._params:
            result[pc] = {
                "candle_monitor": self._supervisor.get_health().get(f"cfd_candle:{pc}", {}),
                "stop_loss_monitor": self._supervisor.get_health().get(f"cfd_stoploss:{pc}", {}),
            }
        return result

    # ──────────────────────────────────────────
    # 재시작 시 포지션 복원 (getpositions 사용)
    # ──────────────────────────────────────────

    async def _detect_existing_position(self, product_code: str) -> Optional[Position]:
        """getpositions으로 기존 FX 포지션 감지."""
        try:
            if not hasattr(self._adapter, "get_positions"):
                return None
            fx_positions = await self._adapter.get_positions(product_code)
            if not fx_positions:
                return None
            # 모든 포지션의 사이드·수량 집계
            total_size = sum(p.size for p in fx_positions)
            if total_size <= 0:
                return None
            first = fx_positions[0]
            avg_price = sum(p.price * p.size for p in fx_positions) / total_size
            logger.info(
                f"[CfdMgr] {product_code}: 기존 FX 포지션 감지 "
                f"(side={first.side}, size={total_size:.6f} BTC, avg_price=¥{avg_price:.0f})"
            )
            return Position(
                pair=product_code,
                entry_price=avg_price,
                entry_amount=total_size,
                stop_loss_price=None,
                extra={"side": first.side.lower()},
            )
        except Exception as e:
            logger.warning(f"[CfdMgr] {product_code}: 포지션 복원 실패 — {e}")
        return None

    async def _recover_db_position_id(self, product_code: str) -> Optional[int]:
        """열린 DB 포지션 레코드 ID 복원."""
        try:
            Model = self._cfd_position_model
            pair_col = getattr(Model, self._pair_column)
            async with self._session_factory() as db:
                result = await db.execute(
                    select(Model)
                    .where(
                        pair_col == product_code,
                        Model.status == "open",
                    )
                    .order_by(Model.created_at.desc())
                    .limit(1)
                )
                rec = result.scalars().first()
                if rec:
                    logger.info(f"[CfdMgr] {product_code}: DB CFD 포지션 복원 id={rec.id}")
                    return rec.id
        except Exception as e:
            logger.warning(f"[CfdMgr] {product_code}: DB 포지션 ID 복원 실패 — {e}")
        return None

    # ──────────────────────────────────────────
    # 포지션-실잔고 정합성 검사 (getpositions vs 인메모리)
    # ──────────────────────────────────────────

    async def _sync_position_state(self, product_code: str) -> None:
        """getpositions와 인메모리 포지션 비교. 괴리 시 갱신."""
        pos = self._position.get(product_code)
        if pos is None:
            return
        try:
            if not hasattr(self._adapter, "get_positions"):
                return
            fx_positions = await self._adapter.get_positions(product_code)
            real_size = sum(p.size for p in fx_positions) if fx_positions else 0.0
            mem_size = pos.entry_amount
            if mem_size <= 0:
                return
            drift_pct = abs(real_size - mem_size) / mem_size * 100
            if drift_pct > 1.0:
                logger.warning(
                    f"[CfdMgr] {product_code}: 포지션 괴리 감지 "
                    f"(인메모리 {mem_size:.8f} vs 실포지션 {real_size:.8f}, "
                    f"차이 {drift_pct:.1f}%) → 인메모리 갱신"
                )
                if real_size <= 0:
                    # 포지션이 외부에서 청산됨
                    self._position[product_code] = None
                else:
                    self._position[product_code] = Position(
                        pair=pos.pair,
                        entry_price=pos.entry_price,
                        entry_amount=real_size,
                        stop_loss_price=pos.stop_loss_price,
                        db_record_id=pos.db_record_id,
                        stop_tightened=pos.stop_tightened,
                        extra=pos.extra,
                    )
        except Exception as e:
            logger.debug(f"[CfdMgr] {product_code}: 포지션 동기화 실패 — {e}")

    # ──────────────────────────────────────────
    # keep_rate 체크
    # ──────────────────────────────────────────

    async def _check_keep_rate(self, product_code: str) -> Optional[float]:
        """getcollateral로 keep_rate 확인. 위험 시 자동 청산.

        Returns: 현재 keep_rate (조회 실패 시 None)
        """
        try:
            if not hasattr(self._adapter, "get_collateral"):
                return None
            collateral = await self._adapter.get_collateral()
            keep_rate = collateral.keep_rate
            params = self._params.get(product_code, {})
            critical = float(params.get("keep_rate_critical", 1.3))

            if keep_rate < critical and self._position.get(product_code) is not None:
                logger.warning(
                    f"[CfdMgr] {product_code}: keep_rate={keep_rate:.2f} < "
                    f"critical={critical} → 긴급 전량 청산"
                )
                await self._close_position(product_code, "risk_cut")

            return keep_rate
        except Exception as e:
            logger.warning(f"[CfdMgr] {product_code}: keep_rate 조회 실패 — {e}")
            return None

    # ──────────────────────────────────────────
    # Task 1: 캔들 모니터 (진입 / EMA 이탈 청산)
    # ──────────────────────────────────────────

    async def _candle_monitor(self, product_code: str) -> None:
        """
        60초마다 캔들 시그널 재계산 → 진입/청산 결정.
        현물 TrendFollowingManager의 _candle_monitor와 동일 구조.

        추가: keep_rate 경고 체크, 보유 시간 제한 체크.
        """
        while True:
            await asyncio.sleep(_CANDLE_POLL_INTERVAL)

            params = self._params.get(product_code, {})
            basis_tf = params.get("basis_timeframe", "4h")
            pos = self._position.get(product_code)
            entry_price = pos.entry_price if pos else None

            # 5사이클(5분)마다 포지션 정합성 검사
            if pos is not None:
                cnt = self._position_sync_counter.get(product_code, 0) + 1
                self._position_sync_counter[product_code] = cnt
                if cnt % 5 == 0:
                    await self._sync_position_state(product_code)

            # keep_rate 체크
            keep_rate = await self._check_keep_rate(product_code)
            # risk_cut으로 포지션이 청산됐을 수 있음
            pos = self._position.get(product_code)

            # 보유 시간 제한 체크
            if pos is not None:
                max_hours = float(params.get("max_holding_hours", 0))
                if max_hours > 0 and pos.extra.get("opened_at"):
                    opened_at = pos.extra["opened_at"]
                    elapsed_hours = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600
                    if elapsed_hours >= max_hours:
                        logger.info(
                            f"[CfdMgr] {product_code}: 보유 시간 초과 "
                            f"{elapsed_hours:.1f}h ≥ {max_hours}h → 자동 청산"
                        )
                        await self._close_position(product_code, "time_limit")
                        continue

            try:
                pos_side = pos.extra.get("side") if pos else None
                signal_data = await self._compute_signal(
                    product_code, basis_tf,
                    entry_price=entry_price,
                    params=params,
                    side=pos_side,
                )
            except Exception as e:
                logger.warning(f"[CfdMgr] {product_code}: 시그널 계산 실패 — {e}")
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

            # 실시간 가격으로 exit_warning 보정 (포지션 보유 시만)
            realtime_price = self._latest_price.get(product_code)
            if pos is not None and realtime_price is not None and ema is not None:
                pos_side = pos.extra.get("side", "buy")
                if pos_side == "buy" and realtime_price < ema:
                    signal = "exit_warning"
                elif pos_side == "sell" and realtime_price > ema:
                    signal = "exit_warning"

            logger.debug(
                f"[CfdMgr] {product_code}: signal={signal} exit={exit_action} "
                f"price={current_price} pos={'있음' if pos else '없음'}"
            )

            # ── EMA 기울기 이력 갱신 ──
            if latest_candle_key != self._ema_slope_last_key.get(product_code):
                slope_history = self._ema_slope_history.setdefault(product_code, [])
                slope_history.append(ema_slope_pct)
                if len(slope_history) > 3:
                    slope_history.pop(0)
                self._ema_slope_last_key[product_code] = latest_candle_key

                if (
                    len(slope_history) == 3
                    and all(s is not None for s in slope_history)
                    and slope_history[0] > slope_history[1] > slope_history[2]
                    and pos is not None
                    and not pos.stop_tightened
                    and atr is not None
                ):
                    logger.info(
                        f"[CfdMgr] {product_code}: EMA 기울기 3캔들 연속 하락 → 스탑 타이트닝"
                    )
                    await self._apply_stop_tightening(product_code, current_price, atr, params)

                # 다이버전스 감지
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
                                f"[CfdMgr] {product_code}: 다이버전스 감지 → 스탑 타이트닝"
                            )
                            await self._apply_stop_tightening(product_code, current_price, atr, params)

            # ── 포지션 있을 때: 청산 우선순위 ──
            if pos is not None:
                if signal == "exit_warning":
                    logger.info(f"[CfdMgr] {product_code}: exit_warning → 전량 청산")
                    await self._close_position(product_code, "exit_warning")
                    continue

                if exit_action == "full_exit":
                    triggers = exit_signal.get("triggers", {})
                    reason_code = (
                        "full_exit_ema_slope" if triggers.get("ema_slope_negative")
                        else "full_exit_rsi_breakdown" if triggers.get("rsi_breakdown")
                        else "full_exit"
                    )
                    logger.info(f"[CfdMgr] {product_code}: {reason_code} → 전량 청산")
                    await self._close_position(product_code, reason_code)
                    continue

                if exit_action == "tighten_stop" and not pos.stop_tightened:
                    if atr:
                        await self._apply_stop_tightening(product_code, current_price, atr, params)

                # 적응형 트레일링 스탑
                if atr is not None:
                    side = pos.extra.get("side", "buy")
                    if pos.stop_tightened:
                        mult = float(params.get("tighten_stop_atr", 1.0))
                    else:
                        mult = compute_adaptive_trailing_mult(ema_slope_pct, rsi, params)

                    if side == "buy":
                        new_sl = round(current_price - atr * mult, 6)
                        current_sl = pos.stop_loss_price
                        if current_sl is None or new_sl > current_sl:
                            pos.stop_loss_price = new_sl
                            await self._update_trailing_stop_in_db(product_code, new_sl)
                            logger.info(
                                f"[CfdMgr] {product_code}: 롱 트레일링 스탑 "
                                f"¥{current_sl} → ¥{new_sl} (x{mult:.1f})"
                            )
                    else:
                        new_sl = round(current_price + atr * mult, 6)
                        current_sl = pos.stop_loss_price
                        if current_sl is None or new_sl < current_sl:
                            pos.stop_loss_price = new_sl
                            await self._update_trailing_stop_in_db(product_code, new_sl)
                            logger.info(
                                f"[CfdMgr] {product_code}: 숏 트레일링 스탑 "
                                f"¥{current_sl} → ¥{new_sl} (x{mult:.1f})"
                            )

            # ── 포지션 없을 때: 진입 ──
            elif signal in ("entry_ok", "entry_sell"):
                # keep_rate 경고 체크 — 신규 주문 차단
                warn_threshold = float(params.get("keep_rate_warn", 1.5))
                if keep_rate is not None and keep_rate < warn_threshold:
                    logger.info(
                        f"[CfdMgr] {product_code}: keep_rate={keep_rate:.2f} < "
                        f"warn={warn_threshold} → 신규 주문 차단"
                    )
                    continue

                entry_side = "sell" if signal == "entry_sell" else "buy"
                logger.info(f"[CfdMgr] {product_code}: {signal} → {entry_side} 진입 시도")
                await self._open_position(product_code, entry_side, current_price, atr, params)

    # ──────────────────────────────────────────
    # Task 2: 스탑로스 모니터 (WS 틱 기반)
    # ──────────────────────────────────────────

    async def _stop_loss_monitor(self, product_code: str) -> None:
        """WS 실시간 체결가 → 스탑로스 이탈 시 청산."""
        price_queue: asyncio.Queue[float] = asyncio.Queue()

        async def _on_trade(price: float, amount: float) -> None:
            await price_queue.put(price)

        await self._adapter.subscribe_trades(product_code, _on_trade)

        try:
            while True:
                try:
                    price = await asyncio.wait_for(price_queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    continue

                pos = self._position.get(product_code)
                if pos is None:
                    continue

                stop_loss_price = pos.stop_loss_price
                if stop_loss_price is None:
                    continue

                self._latest_price[product_code] = price
                side = pos.extra.get("side", "buy")

                triggered = False
                if side == "buy" and price <= stop_loss_price:
                    triggered = True
                elif side == "sell" and price >= stop_loss_price:
                    triggered = True

                if triggered:
                    cooldown_until = self._close_fail_until.get(product_code, 0)
                    if time.time() < cooldown_until:
                        continue

                    logger.info(
                        f"[CfdMgr] {product_code}: 하드 스탑로스 발동 "
                        f"(side={side}, price=¥{price}, stop=¥{stop_loss_price})"
                    )
                    await self._close_position(product_code, "stop_loss")

                    if self._position.get(product_code) is None:
                        self._close_fail_count[product_code] = 0
                        self._close_fail_until[product_code] = 0
                    else:
                        fail_count = self._close_fail_count.get(product_code, 0) + 1
                        self._close_fail_count[product_code] = fail_count
                        if fail_count % 5 == 0:
                            self._close_fail_until[product_code] = time.time() + 60
                            logger.warning(
                                f"[CfdMgr] {product_code}: 청산 {fail_count}회 실패 — 60초 쿨다운"
                            )
        except asyncio.CancelledError:
            raise

    # ──────────────────────────────────────────
    # 시그널 계산
    # ──────────────────────────────────────────

    async def _compute_signal(
        self, product_code: str, timeframe: str,
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
                        pair_col == product_code,
                        CandleModel.timeframe == timeframe,
                        CandleModel.is_complete == True,  # noqa: E712
                    )
                )
                .order_by(CandleModel.open_time.desc())
                .limit(limit)
            )
            candles = list(reversed(result.scalars().all()))

        if len(candles) < ema_period + 1:
            logger.debug(f"[CfdMgr] {product_code}: 캔들 부족 ({len(candles)}개)")
            return None

        result = compute_trend_signal(
            candles, params=params or {}, entry_price=entry_price, side=side,
        )
        if result is not None:
            result["latest_candle_open_time"] = str(candles[-1].open_time)
            result["candles"] = candles
        return result

    # ──────────────────────────────────────────
    # 진입
    # ──────────────────────────────────────────

    async def _open_position(
        self, product_code: str, side: str, price: float, atr: Optional[float], params: Dict
    ) -> None:
        """증거금 기반 CFD 포지션 진입."""
        try:
            # 여유 증거금 확인
            if not hasattr(self._adapter, "get_collateral"):
                logger.error(f"[CfdMgr] {product_code}: 어댑터에 get_collateral 없음")
                return
            collateral = await self._adapter.get_collateral()
            available_collateral = collateral.collateral - collateral.require_collateral
            if available_collateral <= 0:
                logger.info(f"[CfdMgr] {product_code}: 여유 증거금 없음, 진입 스킵")
                return

            position_size_pct = float(params.get("position_size_pct", 10.0))
            invest_jpy = available_collateral * position_size_pct / 100
            min_jpy = float(params.get("min_order_jpy", 500))

            if invest_jpy < min_jpy:
                logger.info(
                    f"[CfdMgr] {product_code}: 투입 JPY({invest_jpy:.0f}) < {min_jpy:.0f}, 스킵"
                )
                return

            # 레버리지 체크
            max_leverage = float(params.get("max_leverage", 1.5))
            coin_size = round(invest_jpy / price, 8)
            effective_leverage = (coin_size * price) / collateral.collateral if collateral.collateral > 0 else 0
            if effective_leverage > max_leverage:
                coin_size = round(collateral.collateral * max_leverage / price, 8)
                logger.info(
                    f"[CfdMgr] {product_code}: 레버리지 제한 → size={coin_size:.8f}"
                )

            min_coin = float(params.get("min_coin_size", 0.001))
            if coin_size < min_coin:
                logger.info(f"[CfdMgr] {product_code}: 수량 부족 ({coin_size} < {min_coin})")
                return

            # 슬리피지 체크
            max_slippage_pct = float(params.get("max_slippage_pct", 0.3))
            try:
                ticker = await self._adapter.get_ticker(product_code)
                if side == "buy" and ticker.ask > 0:
                    slippage = (ticker.ask - price) / price * 100
                    if slippage > max_slippage_pct:
                        logger.warning(
                            f"[CfdMgr] {product_code}: 슬리피지 초과 {slippage:.3f}%, 스킵"
                        )
                        return
            except Exception as e:
                logger.warning(f"[CfdMgr] {product_code}: 시세 조회 실패 — {e}")

            # 주문 실행
            order_type = OrderType.MARKET_BUY if side == "buy" else OrderType.MARKET_SELL
            # CFD: 어댑터가 FX_ 상품은 JPY→코인 변환을 스킵하므로 항상 코인 수량 전달
            order = await self._adapter.place_order(
                order_type=order_type,
                pair=product_code,
                amount=coin_size,
            )

            exec_price = order.price or price
            exec_amount = order.amount if order.amount > 0 else coin_size

            # 초기 스탑로스
            atr_mult = float(params.get("atr_multiplier_stop", 2.0))
            if side == "buy":
                initial_sl = round(exec_price - atr * atr_mult, 6) if atr else None
            else:
                initial_sl = round(exec_price + atr * atr_mult, 6) if atr else None

            pos = Position(
                pair=product_code,
                entry_price=exec_price,
                entry_amount=exec_amount,
                stop_loss_price=initial_sl,
                extra={
                    "side": side,
                    "opened_at": datetime.now(timezone.utc),
                },
            )
            self._position[product_code] = pos

            pos.db_record_id = await self._record_open(
                product_code=product_code,
                side=side,
                order_id=order.order_id,
                price=exec_price,
                size=exec_amount,
                collateral_jpy=invest_jpy,
                stop_loss_price=initial_sl,
                strategy_id=params.get("strategy_id"),
            )

            logger.info(
                f"[CfdMgr] {product_code}: {side} 진입 완료 "
                f"order_id={order.order_id} price=¥{exec_price} size={exec_amount} "
                f"stop_loss=¥{initial_sl}"
            )
        except Exception as e:
            logger.error(f"[CfdMgr] {product_code}: 진입 오류 — {e}", exc_info=True)

    # ──────────────────────────────────────────
    # 청산
    # ──────────────────────────────────────────

    async def _close_position(self, product_code: str, reason: str) -> None:
        """반대 매매로 CFD 포지션 청산."""
        try:
            pos = self._position.get(product_code)
            if pos is None:
                return

            side = pos.extra.get("side", "buy")
            close_size = pos.entry_amount
            min_size = float(self._params.get(product_code, {}).get("min_coin_size", 0.001))

            if close_size < min_size:
                logger.warning(
                    f"[CfdMgr] {product_code}: 포지션 수량 부족 ({close_size} < {min_size})"
                )
                prev_db_id = pos.db_record_id
                self._position[product_code] = None
                if prev_db_id:
                    await self._record_close(
                        db_record_id=prev_db_id,
                        product_code=product_code,
                        side=side,
                        order_id="",
                        price=0.0,
                        size=close_size,
                        reason="dust_position_cleared",
                        entry_price=pos.entry_price,
                    )
                return

            # 반대 매매
            if side == "buy":
                order_type = OrderType.MARKET_SELL
            else:
                order_type = OrderType.MARKET_BUY

            order = await self._adapter.place_order(
                order_type=order_type,
                pair=product_code,
                amount=close_size,
            )

            prev_pos = self._position.get(product_code)
            prev_db_id = prev_pos.db_record_id if prev_pos else None
            self._position[product_code] = None

            exec_price = order.price or 0
            if exec_price == 0:
                try:
                    ticker = await self._adapter.get_ticker(product_code)
                    exec_price = ticker.last
                except Exception:
                    pass

            await self._record_close(
                db_record_id=prev_db_id,
                product_code=product_code,
                side=side,
                order_id=order.order_id,
                price=exec_price,
                size=close_size,
                reason=reason,
                entry_price=prev_pos.entry_price if prev_pos else None,
            )

            logger.info(
                f"[CfdMgr] {product_code}: {side} 청산 완료 reason={reason} "
                f"order_id={order.order_id} size={close_size}"
            )
        except Exception as e:
            logger.error(f"[CfdMgr] {product_code}: 청산 오류 — {e}", exc_info=True)

    async def _apply_stop_tightening(
        self, product_code: str, current_price: float, atr: float, params: dict
    ) -> None:
        pos = self._position.get(product_code)
        if pos is None:
            return
        side = pos.extra.get("side", "buy")
        tighten_mult = float(params.get("tighten_stop_atr", 1.0))

        if side == "buy":
            new_sl = round(current_price - atr * tighten_mult, 6)
            current_sl = pos.stop_loss_price
            if current_sl is None or new_sl > current_sl:
                pos.stop_loss_price = new_sl
                await self._update_trailing_stop_in_db(product_code, new_sl)
        else:
            new_sl = round(current_price + atr * tighten_mult, 6)
            current_sl = pos.stop_loss_price
            if current_sl is None or new_sl < current_sl:
                pos.stop_loss_price = new_sl
                await self._update_trailing_stop_in_db(product_code, new_sl)

        pos.stop_tightened = True
        logger.info(
            f"[CfdMgr] {product_code}: 스탑 타이트닝 ¥{current_sl} → ¥{new_sl} (x{tighten_mult})"
        )

    # ──────────────────────────────────────────
    # DB 기록 헬퍼
    # ──────────────────────────────────────────

    async def _record_open(
        self,
        product_code: str,
        side: str,
        order_id: str,
        price: float,
        size: float,
        collateral_jpy: float,
        stop_loss_price: Optional[float],
        strategy_id: Optional[int],
    ) -> Optional[int]:
        try:
            Model = self._cfd_position_model
            async with self._session_factory() as db:
                kwargs = {
                    self._pair_column: product_code,
                    "strategy_id": strategy_id,
                    "side": side,
                    "entry_order_id": order_id,
                    "entry_price": price,
                    "entry_size": size,
                    "entry_collateral_jpy": round(collateral_jpy, 2),
                    "stop_loss_price": stop_loss_price,
                    "status": "open",
                }
                rec = Model(**kwargs)
                db.add(rec)
                await db.commit()
                await db.refresh(rec)
                logger.debug(f"[CfdMgr] {product_code}: DB CFD 포지션 기록 id={rec.id}")
                return rec.id
        except Exception as e:
            logger.error(f"[CfdMgr] {product_code}: DB 진입 기록 실패 — {e}", exc_info=True)
            return None

    async def _record_close(
        self,
        db_record_id: Optional[int],
        product_code: str,
        side: str,
        order_id: str,
        price: float,
        size: float,
        reason: str,
        entry_price: Optional[float],
    ) -> None:
        try:
            if db_record_id is None:
                return

            pnl_jpy: Optional[float] = None
            pnl_pct: Optional[float] = None
            if entry_price and entry_price > 0 and price > 0:
                if side == "buy":
                    pnl_jpy = round((price - entry_price) * size, 2)
                else:
                    pnl_jpy = round((entry_price - price) * size, 2)
                pnl_pct = round(pnl_jpy / (entry_price * size) * 100, 4)

            Model = self._cfd_position_model
            async with self._session_factory() as db:
                result = await db.execute(
                    select(Model).where(Model.id == db_record_id)
                )
                rec = result.scalars().first()
                if rec is None:
                    return
                rec.exit_order_id = order_id
                rec.exit_price = price
                rec.exit_size = size
                rec.exit_reason = reason
                rec.realized_pnl_jpy = pnl_jpy
                rec.realized_pnl_pct = pnl_pct
                rec.status = "closed"
                rec.closed_at = datetime.now(timezone.utc)
                await db.commit()
                logger.debug(
                    f"[CfdMgr] {product_code}: DB 청산 기록 id={db_record_id} "
                    f"pnl=¥{pnl_jpy} ({pnl_pct}%)"
                )
        except Exception as e:
            logger.error(f"[CfdMgr] {product_code}: DB 청산 기록 실패 — {e}", exc_info=True)

    async def _update_trailing_stop_in_db(self, product_code: str, stop_loss_price: float) -> None:
        try:
            pos = self._position.get(product_code)
            if pos is None or pos.db_record_id is None:
                return
            Model = self._cfd_position_model
            async with self._session_factory() as db:
                result = await db.execute(
                    select(Model).where(Model.id == pos.db_record_id)
                )
                rec = result.scalars().first()
                if rec:
                    rec.stop_loss_price = stop_loss_price
                    await db.commit()
        except Exception as e:
            logger.warning(f"[CfdMgr] {product_code}: DB 트레일링 스탑 갱신 실패 — {e}")
