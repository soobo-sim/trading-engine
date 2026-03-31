"""
CfdTrendFollowingManager — BitFlyer CFD (FX_BTC_JPY) 전용 추세추종 매니저.

BaseTrendManager를 상속하여 CFD 고유 로직만 구현한다.

CFD 고유 로직:
    - 롱/숏 양방향 진입
    - 증거금(getcollateral) 기반 주문 + 레버리지 제한
    - keep_rate 모니터링 → 자동 청산
    - 포지션 보유 시간 제한 → 스왑 비용 관리
    - getpositions으로 실 포지션 정합성 검사
    - 양방향 스탑로스/트레일링

모든 리스크 수치는 strategy.parameters에서 읽는다 (하드코딩 금지).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Type

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.exchange.base import ExchangeAdapter
from core.exchange.types import OrderType, Position
from core.exchange.session import is_fx_market_open, should_close_for_weekend, minutes_until_market_close
from core.strategy.base_trend import BaseTrendManager
from core.task.supervisor import TaskSupervisor

logger = logging.getLogger(__name__)


class CfdTrendFollowingManager(BaseTrendManager):
    """BitFlyer CFD (FX_BTC_JPY) 추세추종 매니저. 양방향."""

    _task_prefix = "cfd"
    _log_prefix = "[CfdMgr]"

    def __init__(
        self,
        adapter: ExchangeAdapter,
        supervisor: TaskSupervisor,
        session_factory: async_sessionmaker[AsyncSession],
        candle_model: Type,
        cfd_position_model: Type,
        pair_column: str = "product_code",
    ) -> None:
        super().__init__(
            adapter=adapter,
            supervisor=supervisor,
            session_factory=session_factory,
            candle_model=candle_model,
            position_model=cfd_position_model,
            pair_column=pair_column,
        )
        # keep_rate 캐시 (캔들 사이클 내에서 진입 시 참조)
        self._last_keep_rate: Dict[str, Optional[float]] = {}

    # ──────────────────────────────────────────
    # 포지션 감지 / 동기화
    # ──────────────────────────────────────────

    async def _detect_existing_position(self, product_code: str) -> Optional[Position]:
        """getpositions으로 기존 FX 포지션 감지."""
        try:
            if not hasattr(self._adapter, "get_positions"):
                return None
            fx_positions = await self._adapter.get_positions(product_code)
            if not fx_positions:
                return None
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
                pair=product_code, entry_price=avg_price,
                entry_amount=total_size, stop_loss_price=None,
                extra={"side": first.side.lower()},
            )
        except Exception as e:
            logger.warning(f"[CfdMgr] {product_code}: 포지션 복원 실패 — {e}")
        return None

    async def _sync_position_state(self, product_code: str) -> None:
        """getpositions vs 인메모리 비교 → 괴리 시 갱신."""
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
                    self._position[product_code] = None
                else:
                    self._position[product_code] = Position(
                        pair=pos.pair, entry_price=pos.entry_price,
                        entry_amount=real_size, stop_loss_price=pos.stop_loss_price,
                        db_record_id=pos.db_record_id, stop_tightened=pos.stop_tightened,
                        extra=pos.extra,
                    )
        except Exception as e:
            logger.debug(f"[CfdMgr] {product_code}: 포지션 동기화 실패 — {e}")

    # ──────────────────────────────────────────
    # keep_rate 체크
    # ──────────────────────────────────────────

    async def _check_keep_rate(self, product_code: str) -> Optional[float]:
        """getcollateral로 keep_rate 확인. 위험 시 자동 청산."""
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
    # Hooks (base override)
    # ──────────────────────────────────────────

    async def _on_candle_extra_checks(self, pair: str, params: Dict) -> bool:
        """keep_rate + 보유 시간 + 주말 청산 + 스왑 추적 체크."""
        keep_rate = await self._check_keep_rate(pair)
        self._last_keep_rate[pair] = keep_rate

        pos = self._position.get(pair)

        # ── 주말 자동 청산 (FX 전용) ───────────────────────
        is_fx = self._adapter.exchange_name == "gmofx"
        if is_fx and pos is not None and should_close_for_weekend():
            mins = minutes_until_market_close()
            logger.warning(
                f"[CfdMgr] {pair}: 주말 마감 임박 (잔여 {mins}분) → 자동 청산"
            )
            await self._close_position(pair, "weekend_close")
            return False

        # ── FX 시장 휴장 시 진입 차단 ─────────────────────
        if is_fx and not is_fx_market_open():
            logger.debug(f"[CfdMgr] {pair}: FX 시장 휴장 — 사이클 스킵")
            return False

        # ── 스왑 포인트 로깅 (FX 전용) ────────────────────
        if is_fx and pos is not None and hasattr(self._adapter, "get_positions"):
            try:
                fx_positions = await self._adapter.get_positions(pair)
                total_swap = sum(p.swap_point_accumulate for p in fx_positions) if fx_positions else 0.0
                if total_swap != 0:
                    logger.info(f"[CfdMgr] {pair}: 누적 스왑: ¥{total_swap:.1f}")
            except Exception:
                pass  # 스왑 로깅 실패는 무시

        # ── 보유 시간 제한 ────────────────────────────────
        if pos is not None:
            max_hours = float(params.get("max_holding_hours", 0))
            if max_hours > 0 and pos.extra.get("opened_at"):
                opened_at = pos.extra["opened_at"]
                elapsed_hours = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600
                if elapsed_hours >= max_hours:
                    logger.info(
                        f"[CfdMgr] {pair}: 보유 시간 초과 "
                        f"{elapsed_hours:.1f}h ≥ {max_hours}h → 자동 청산"
                    )
                    await self._close_position(pair, "time_limit")
                    return False  # 이번 사이클 스킵 (continue)
        return True

    def _check_exit_warning(self, pair, signal, realtime_price, ema, pos):
        """양방향 exit_warning. 포지션 없으면 스킵."""
        if pos is None:
            return signal
        side = pos.extra.get("side", "buy")
        if side == "buy" and realtime_price < ema:
            if signal != "exit_warning":
                logger.info(
                    f"[CfdMgr] {pair}: 롱 실시간가 ¥{realtime_price} < EMA ¥{ema:.4f} → exit_warning"
                )
            return "exit_warning"
        elif side == "sell" and realtime_price > ema:
            if signal != "exit_warning":
                logger.info(
                    f"[CfdMgr] {pair}: 숏 실시간가 ¥{realtime_price} > EMA ¥{ema:.4f} → exit_warning"
                )
            return "exit_warning"
        return signal

    def _is_stop_triggered(self, pos, price, stop_loss_price):
        """양방향 스탑로스."""
        side = pos.extra.get("side", "buy")
        if side == "buy":
            return price <= stop_loss_price
        return price >= stop_loss_price

    async def _update_trailing_stop(self, pair, pos, current_price, atr, ema_slope_pct, rsi, params):
        """양방향 적응형 트레일링 스탑."""
        from core.strategy.signals import compute_adaptive_trailing_mult

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
                await self._update_trailing_stop_in_db(pair, new_sl)
                logger.info(
                    f"[CfdMgr] {pair}: 롱 트레일링 스탑 ¥{current_sl} → ¥{new_sl} (x{mult:.1f})"
                )
        else:
            new_sl = round(current_price + atr * mult, 6)
            current_sl = pos.stop_loss_price
            if current_sl is None or new_sl < current_sl:
                pos.stop_loss_price = new_sl
                await self._update_trailing_stop_in_db(pair, new_sl)
                logger.info(
                    f"[CfdMgr] {pair}: 숏 트레일링 스탑 ¥{current_sl} → ¥{new_sl} (x{mult:.1f})"
                )

    async def _on_entry_signal(self, pair, signal, current_price, atr, params, signal_data):
        """entry_ok / entry_sell → 진입. keep_rate 경고 / 주말 시 차단."""
        if signal not in ("entry_ok", "entry_sell"):
            return

        # FX: 주말 임박 시 신규 진입 차단
        is_fx = self._adapter.exchange_name == "gmofx"
        if is_fx and (should_close_for_weekend() or not is_fx_market_open()):
            logger.info(f"[CfdMgr] {pair}: FX 시장 휴장/주말 임박 → 진입 차단")
            return

        keep_rate = self._last_keep_rate.get(pair)
        warn_threshold = float(params.get("keep_rate_warn", 1.5))
        if keep_rate is not None and keep_rate < warn_threshold:
            logger.info(
                f"[CfdMgr] {pair}: keep_rate={keep_rate:.2f} < warn={warn_threshold} → 차단"
            )
            return
        entry_side = "sell" if signal == "entry_sell" else "buy"
        logger.info(f"[CfdMgr] {pair}: {signal} → {entry_side} 진입 시도")
        await self._open_position(pair, entry_side, current_price, atr, params)

    # ──────────────────────────────────────────
    # 진입
    # ──────────────────────────────────────────

    async def _open_position(
        self, product_code: str, side: str, price: float, atr, params: Dict, *, signal_data: dict | None = None
    ) -> None:
        """증거금 기반 CFD 포지션 진입."""
        try:
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
                logger.info(f"[CfdMgr] {product_code}: 레버리지 제한 → size={coin_size:.8f}")

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

            order_type = OrderType.MARKET_BUY if side == "buy" else OrderType.MARKET_SELL
            order = await self._adapter.place_order(
                order_type=order_type, pair=product_code, amount=coin_size,
            )

            exec_price = order.price or price
            exec_amount = order.amount if order.amount > 0 else coin_size

            atr_mult = float(params.get("atr_multiplier_stop", 2.0))
            if side == "buy":
                initial_sl = round(exec_price - atr * atr_mult, 6) if atr else None
            else:
                initial_sl = round(exec_price + atr * atr_mult, 6) if atr else None

            pos = Position(
                pair=product_code, entry_price=exec_price,
                entry_amount=exec_amount, stop_loss_price=initial_sl,
                extra={"side": side, "opened_at": datetime.now(timezone.utc)},
            )
            self._position[product_code] = pos

            pos.db_record_id = await self._record_open(
                product_code=product_code, side=side,
                order_id=order.order_id, price=exec_price,
                size=exec_amount, collateral_jpy=invest_jpy,
                stop_loss_price=initial_sl, strategy_id=params.get("strategy_id"),
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
                        db_record_id=prev_db_id, product_code=product_code,
                        side=side, order_id="", price=0.0, size=close_size,
                        reason="dust_position_cleared", entry_price=pos.entry_price,
                    )
                return

            order_type = OrderType.MARKET_SELL if side == "buy" else OrderType.MARKET_BUY
            order = await self._adapter.place_order(
                order_type=order_type, pair=product_code, amount=close_size,
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
                db_record_id=prev_db_id, product_code=product_code,
                side=side, order_id=order.order_id, price=exec_price,
                size=close_size, reason=reason,
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
        """양방향 스탑 타이트닝."""
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

    async def _record_open(self, **kwargs) -> Optional[int]:
        product_code = kwargs["product_code"]
        try:
            Model = self._position_model
            async with self._session_factory() as db:
                db_kwargs = {
                    self._pair_column: product_code,
                    "strategy_id": kwargs.get("strategy_id"),
                    "side": kwargs["side"],
                    "entry_order_id": kwargs["order_id"],
                    "entry_price": kwargs["price"],
                    "entry_size": kwargs["size"],
                    "entry_collateral_jpy": round(kwargs["collateral_jpy"], 2),
                    "stop_loss_price": kwargs.get("stop_loss_price"),
                    "status": "open",
                }
                rec = Model(**db_kwargs)
                db.add(rec)
                await db.commit()
                await db.refresh(rec)
                logger.debug(f"[CfdMgr] {product_code}: DB CFD 포지션 기록 id={rec.id}")
                return rec.id
        except Exception as e:
            logger.error(f"[CfdMgr] {product_code}: DB 진입 기록 실패 — {e}", exc_info=True)
            return None

    async def _record_close(self, **kwargs) -> None:
        product_code = kwargs.get("product_code", "")
        db_record_id = kwargs.get("db_record_id")
        try:
            if db_record_id is None:
                return

            price = kwargs["price"]
            size = kwargs["size"]
            side = kwargs["side"]
            entry_price = kwargs.get("entry_price")

            pnl_jpy: Optional[float] = None
            pnl_pct: Optional[float] = None
            if entry_price and entry_price > 0 and price > 0:
                if side == "buy":
                    pnl_jpy = round((price - entry_price) * size, 2)
                else:
                    pnl_jpy = round((entry_price - price) * size, 2)
                pnl_pct = round(pnl_jpy / (entry_price * size) * 100, 4)

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
                rec.exit_size = size
                rec.exit_reason = kwargs["reason"]
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
