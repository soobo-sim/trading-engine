"""
TrendFollowingManager — 현물 추세추종 전략 매니저.

BaseTrendManager를 상속하여 현물 거래소(CK/BF) 고유 로직만 구현한다.

현물 고유 로직:
    - 잔고(get_balance) 기반 포지션 감지/동기화
    - JPY 투입 → market_buy 진입
    - 매도 수수료 차감 + dust 잔고 처리
    - 롱 전용 (숏 불가)
    - _last_rsi 캐시 (SF-10 헬스체크용)
    - _record_partial_close 부분 청산 DB 기록

통합된 버그 수정:
    - BUG-003: dust 잔고 → min_coin_size 미만 시 포지션 없음 + DB 종료
    - BUG-004: 매도 수수료 차감 → available / (1 + fee_rate)
    - BUG-005: 스탑로스 무한 재시도 → 5회 실패마다 60초 쿨다운 (base)
    - BUG-006: 잔고-포지션 괴리 → 5분마다 정합성 검사 (base)
    - BUG-008: market_sell 체결가 미반환 → ticker fallback
    - BUG-009: 청산 후 dust 잔고 감지 로깅
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Dict, Optional, Type

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.exchange.base import ExchangeAdapter
from core.exchange.types import OrderType, Position
from core.strategy.base_trend import BaseTrendManager
from core.task.supervisor import TaskSupervisor

logger = logging.getLogger(__name__)


class TrendFollowingManager(BaseTrendManager):
    """현물 추세추종 매니저 (CK/BF). 롱 전용."""

    _task_prefix = "trend"
    _log_prefix = "[TrendMgr]"

    def __init__(
        self,
        adapter: ExchangeAdapter,
        supervisor: TaskSupervisor,
        session_factory: async_sessionmaker[AsyncSession],
        candle_model: Type,
        trend_position_model: Type,
        pair_column: str = "pair",
    ) -> None:
        super().__init__(
            adapter=adapter,
            supervisor=supervisor,
            session_factory=session_factory,
            candle_model=candle_model,
            position_model=trend_position_model,
            pair_column=pair_column,
            position_pair_column="pair",  # TrendPosition always has "pair" column
        )
        # SF-10: 최근 RSI 캐시 (헬스체크용)
        self._last_rsi: Dict[str, Optional[float]] = {}

    # ──────────────────────────────────────────
    # 포지션 감지 / 동기화
    # ──────────────────────────────────────────

    async def _detect_existing_position(self, pair: str) -> Optional[Position]:
        """잔고 확인으로 기존 포지션 감지. dust(< min_coin_size) 무시."""
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
                    pair=pair, entry_price=None,
                    entry_amount=amount, stop_loss_price=None,
                )
            if amount > 0:
                logger.info(
                    f"[TrendMgr] {pair}: dust 잔고 무시 "
                    f"({currency} {amount:.6f}개 < min_coin_size {min_size})"
                )
        except Exception as e:
            logger.warning(f"[TrendMgr] {pair}: 포지션 복원 실패 — {e}")
        return None

    async def _sync_position_state(self, pair: str) -> None:
        """실잔고 vs 인메모리 비교 → 1% 이상 괴리 시 인메모리 갱신."""
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
                    pair=pos.pair, entry_price=pos.entry_price,
                    entry_amount=real_available, stop_loss_price=pos.stop_loss_price,
                    db_record_id=pos.db_record_id, stop_tightened=pos.stop_tightened,
                    extra=pos.extra,
                )
        except Exception as e:
            logger.debug(f"[TrendMgr] {pair}: 잔고 동기화 실패 — {e}")

    # ──────────────────────────────────────────
    # Hooks
    # ──────────────────────────────────────────

    def _on_signal_computed(self, pair, signal, signal_data, pos):
        """SF-10: RSI 캐시."""
        self._last_rsi[pair] = signal_data.get("rsi")
        return signal

    # ──────────────────────────────────────────
    # 진입
    # ──────────────────────────────────────────

    async def _open_position(
        self, pair: str, price: float, atr: Optional[float], params: Dict, *, signal_data: dict | None = None
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

            order = await self._adapter.place_order(
                order_type=OrderType.MARKET_BUY, pair=pair,
                amount=round(invest_jpy, 0),
            )

            exec_price = order.price or price
            exec_amount = order.amount
            if exec_amount == 0 and exec_price > 0:
                exec_amount = round(invest_jpy / exec_price, 8)

            atr_mult = float(params.get("atr_multiplier_stop", 2.0))
            initial_sl = round(exec_price - atr * atr_mult, 6) if atr else None

            pos = Position(
                pair=pair, entry_price=exec_price,
                entry_amount=exec_amount, stop_loss_price=initial_sl,
            )
            self._position[pair] = pos

            pos.db_record_id = await self._record_open(
                pair=pair, order_id=order.order_id,
                price=exec_price, amount=exec_amount,
                invest_jpy=invest_jpy, stop_loss_price=initial_sl,
                strategy_id=params.get("strategy_id"),
                signal_data=signal_data or {},
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
        """market_sell 자동 청산. BUG-003 dust, BUG-004 수수료 차감."""
        try:
            balance = await self._adapter.get_balance()
            currency = pair.split("_")[0]
            coin_available = balance.get_available(currency)
            min_size = float(self._params.get(pair, {}).get("min_coin_size", 0.001))

            # BUG-004: 매도 수수료 차감
            fee_rate = float(self._params.get(pair, {}).get("trading_fee_rate", 0.002))
            sell_amount = math.floor(coin_available / (1 + fee_rate) * 1e8) / 1e8

            if sell_amount < min_size:
                logger.warning(
                    f"[TrendMgr] {pair}: 잔고 부족 ({coin_available} < {min_size}) — 포지션 클리어"
                )
                prev_pos = self._position.get(pair)
                prev_db_id = prev_pos.db_record_id if prev_pos else None
                self._position[pair] = None
                if prev_db_id:
                    await self._record_close(
                        db_record_id=prev_db_id, pair=pair, order_id="",
                        price=0.0, amount=coin_available,
                        reason="dust_position_cleared",
                        entry_price=prev_pos.entry_price if prev_pos else None,
                    )
                return

            order = await self._adapter.place_order(
                order_type=OrderType.MARKET_SELL, pair=pair, amount=sell_amount,
            )

            prev_pos = self._position.get(pair)
            prev_db_id = prev_pos.db_record_id if prev_pos else None
            self._position[pair] = None

            exec_price = order.price or 0
            # BUG-008: 체결가 미반환 → ticker fallback
            if exec_price == 0:
                try:
                    ticker = await self._adapter.get_ticker(pair)
                    exec_price = ticker.last
                    logger.warning(f"[TrendMgr] {pair}: 체결가 미반환, ticker last={exec_price}로 대체")
                except Exception as te:
                    logger.warning(f"[TrendMgr] {pair}: ticker 조회도 실패 — {te}")

            await self._record_close(
                db_record_id=prev_db_id, pair=pair,
                order_id=order.order_id, price=exec_price,
                amount=sell_amount, reason=reason,
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
                        f"[TrendMgr] {pair}: 청산 후 dust 잔고 ({currency} {dust:.8f})"
                    )
            except Exception as de:
                logger.debug(f"[TrendMgr] {pair}: dust 확인 실패 — {de}")
        except Exception as e:
            logger.error(f"[TrendMgr] {pair}: 청산 주문 오류 — {e}", exc_info=True)

    async def _apply_stop_tightening(
        self, pair: str, current_price: float, atr: float, params: dict
    ) -> None:
        """롱 전용 스탑 타이트닝."""
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

    async def _record_open(self, **kwargs) -> Optional[int]:
        """진입 시 trend_positions 레코드 생성."""
        pair = kwargs["pair"]
        sd = kwargs.get("signal_data") or {}
        try:
            Model = self._position_model
            async with self._session_factory() as db:
                rec = Model(
                    pair=pair,
                    strategy_id=kwargs.get("strategy_id"),
                    entry_order_id=kwargs["order_id"],
                    entry_price=kwargs["price"],
                    entry_amount=kwargs["amount"],
                    entry_jpy=round(kwargs["invest_jpy"], 2),
                    stop_loss_price=kwargs.get("stop_loss_price"),
                    status="open",
                    # 진입 시그널 스냅샷
                    entry_rsi=round(sd["rsi"], 4) if sd.get("rsi") is not None else None,
                    entry_ema_slope=round(sd["ema_slope_pct"], 6) if sd.get("ema_slope_pct") is not None else None,
                    entry_atr=sd.get("atr"),
                    entry_regime=sd.get("regime"),
                    entry_bb_width=round(sd["bb_width_pct"], 4) if sd.get("bb_width_pct") is not None else None,
                )
                db.add(rec)
                await db.commit()
                await db.refresh(rec)
                logger.debug(f"[TrendMgr] {pair}: DB 포지션 기록 id={rec.id}")
                return rec.id
        except Exception as e:
            logger.error(f"[TrendMgr] {pair}: DB 진입 기록 실패 — {e}", exc_info=True)
            return None

    async def _record_close(self, **kwargs) -> None:
        """청산 시 trend_positions 레코드 업데이트."""
        pair = kwargs.get("pair", "")
        db_record_id = kwargs.get("db_record_id")
        try:
            if db_record_id is None:
                return

            price = kwargs["price"]
            amount = kwargs["amount"]
            entry_price = kwargs.get("entry_price")

            exit_jpy = round(price * amount, 2) if price and amount else None
            pnl_jpy: Optional[float] = None
            pnl_pct: Optional[float] = None
            if entry_price and entry_price > 0 and price > 0:
                pnl_jpy = round((price - entry_price) * amount, 2)
                pnl_pct = round((price - entry_price) / entry_price * 100, 4)

            Model = self._position_model
            async with self._session_factory() as db:
                result = await db.execute(
                    select(Model).where(Model.id == db_record_id)
                )
                rec = result.scalars().first()
                if rec is None:
                    return
                rec.exit_order_id = kwargs["order_id"]
                rec.exit_price = price
                rec.exit_amount = amount
                rec.exit_jpy = exit_jpy
                rec.exit_reason = kwargs["reason"]
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

    async def _record_partial_close(
        self, pair: str, order_id: str, price: float, amount: float, reason: str,
    ) -> None:
        """부분 청산 시 trend_positions 누적 컬럼 갱신."""
        try:
            pos = self._position.get(pair)
            if pos is None or pos.db_record_id is None:
                return
            partial_jpy = round(price * amount, 2) if price and amount else 0.0
            Model = self._position_model
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
