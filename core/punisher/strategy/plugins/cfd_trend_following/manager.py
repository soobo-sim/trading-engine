"""
MarginTrendManager — 증거금(margin) 기반 양방향 추세추종 기반 매니저.

BaseTrendManager를 상속하여 증거금 고유 로직만 구현한다.

고유 로직:
    - 롱/숏 양방향 진입
    - 증거금(get_collateral) 기반 주문 + 레버리지 제한
    - keep_rate 모니터링 → 자동 청산
    - 포지션 보유 시간 제한
    - get_positions으로 실 포지션 정합성 검사
    - 양방향 스탑로스/트레일링

모든 리스크 수치는 strategy.parameters에서 읽는다 (하드코딩 금지).

이 클래스는 GmoCoinTrendManager의 기반 클래스로 사용된다.
"""
# 후방 호환 alias — 기존 import(CfdTrendFollowingManager)가 동작하도록 유지
# from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Type

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.exchange.base import ExchangeAdapter
from core.exchange.types import OrderType, Position
from core.strategy.base_trend import BaseTrendManager
from core.punisher.task.supervisor import TaskSupervisor
from core.punisher.monitoring.maintenance import is_maintenance_window

logger = logging.getLogger("core.punisher.strategy.plugins.cfd_trend_following.manager")


class MarginTrendManager(BaseTrendManager):
    """증거금 기반 추세추종 기반 매니저. 양방향."""

    _task_prefix = "margin"
    _log_prefix = "[MarginMgr]"
    _supports_short = True  # 양방향 진입 지원

    def __init__(
        self,
        adapter: ExchangeAdapter,
        supervisor: TaskSupervisor,
        session_factory: async_sessionmaker[AsyncSession],
        candle_model: Type,
        cfd_position_model: Type,
        pair_column: str = "product_code",
        snapshot_collector: Optional[Any] = None,
    ) -> None:
        super().__init__(
            adapter=adapter,
            supervisor=supervisor,
            session_factory=session_factory,
            candle_model=candle_model,
            position_model=cfd_position_model,
            pair_column=pair_column,
            snapshot_collector=snapshot_collector,
        )
        # keep_rate 캐시 (캔들 사이클 내에서 진입 시 참조)
        self._last_keep_rate: Dict[str, Optional[float]] = {}

    # ──────────────────────────────────────────
    # 포지션 감지 / 동기화
    # ──────────────────────────────────────────

    async def _detect_existing_position(self, product_code: str) -> Optional[Position]:
        """get_positions으로 기존 포지션 감지."""
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
                f"[MarginMgr] {product_code}: 기존 포지션 감지 "
                f"(side={first.side}, size={total_size:.6f} BTC, avg_price=¥{avg_price:.0f})"
            )
            return Position(
                pair=product_code, entry_price=avg_price,
                entry_amount=total_size, stop_loss_price=None,
                extra={"side": first.side.lower()},
            )
        except Exception as e:
            exchange = getattr(self._adapter, "exchange_name", "gmo_coin")
            if is_maintenance_window(exchange):
                logger.debug(f"[MarginMgr] {product_code}: 포지션 복원 실패 (메인터넌스) — {e}")
            else:
                logger.warning(f"[MarginMgr] {product_code}: 포지션 복원 실패 — {e}")
        return None

    async def _try_restore_position(self, product_code: str) -> None:
        """pos is None인데 DB에 open 레코드가 있으면 _detect_existing_position 재시도.

        메인터넌스(ERR-5201) 중 재시작 등으로 포지션 감지 실패 시
        30분 주기 동기화 사이클마다 자동 복원을 시도한다. (Option B)
        """
        try:
            Model = self._position_model
            pair_col = getattr(Model, self._position_pair_column)
            async with self._session_factory() as db:
                result = await db.execute(
                    select(Model)
                    .where(pair_col == product_code, Model.status == "open")
                    .limit(1)
                )
                rec = result.scalars().first()
            if rec is None:
                return  # DB에도 open 없음 → 재시도 불필요
        except Exception as e:
            logger.debug(f"[MarginMgr] {product_code}: DB open 레코드 조회 실패 — {e}")
            return

        logger.warning(
            f"[MarginMgr] {product_code}: 인메모리 포지션 없음 + DB open 레코드 존재 → 복원 재시도"
        )
        try:
            pos = await self._detect_existing_position(product_code)
            if pos is None:
                logger.warning(
                    f"[MarginMgr] {product_code}: 포지션 복원 재시도 실패 (거래소 API 불가) — 다음 주기에 재시도"
                )
                return
            self._position[product_code] = pos
            pos.db_record_id = await self._recover_db_position_id(product_code)
            sl_str = f"¥{pos.stop_loss_price:,.0f}" if pos.stop_loss_price else "없음"
            logger.warning(
                f"[MarginMgr] {product_code}: 포지션 자동 복원 완료 — "
                f"entry=¥{pos.entry_price:,.0f} size={pos.entry_amount:.6f} SL={sl_str}"
            )
            try:
                import os
                from core.shared.logging.telegram_handlers import _send_telegram
                bot_token = os.getenv("AUTO_REPORT_BOT_TOKEN", "")
                chat_id = os.getenv("AUTO_REPORT_CHAT_ID", "")
                msg = (
                    f"✅ [MarginMgr] {product_code} 포지션 자동 복원\n"
                    f"entry=¥{pos.entry_price:,.0f} size={pos.entry_amount:.6f}\n"
                    f"SL={sl_str}"
                )
                asyncio.ensure_future(_send_telegram(bot_token, chat_id, msg))
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[MarginMgr] {product_code}: 포지션 복원 재시도 오류 — {e}")

    async def _sync_position_state(self, product_code: str) -> None:
        """getpositions vs 인메모리 비교 → 괴리 시 갱신.

        pos is None 상태에서 DB open 레코드가 있으면 _try_restore_position으로
        재감지를 시도한다. (BUG-039 Option B)
        """
        pos = self._position.get(product_code)
        if pos is None:
            await self._try_restore_position(product_code)
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
                    f"[MarginMgr] {product_code}: 포지션 괴리 감지 "
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
            logger.debug(f"[MarginMgr] {product_code}: 포지션 동기화 실패 — {e}")

    # ──────────────────────────────────────────
    # keep_rate 체크
    # ──────────────────────────────────────────

    async def _check_keep_rate(self, product_code: str) -> Optional[float]:
        """get_collateral로 keep_rate 확인. 위험 시 자동 청산."""
        try:
            if not hasattr(self._adapter, "get_collateral"):
                return None
            collateral = await self._adapter.get_collateral()
            keep_rate = collateral.keep_rate
            params = self._params.get(product_code, {})
            critical = float(params.get("keep_rate_critical", 1.3))

            if keep_rate < critical and self._position.get(product_code) is not None:
                logger.warning(
                    f"[MarginMgr] {product_code}: keep_rate={keep_rate:.2f} < "
                    f"critical={critical} → 긴급 전량 청산"
                )
                await self._close_position(product_code, "risk_cut")

            return keep_rate
        except Exception as e:
            exchange = getattr(self._adapter, "exchange_name", "gmo_coin")
            if is_maintenance_window(exchange):
                logger.debug(f"[MarginMgr] {product_code}: keep_rate 조회 실패 (메인터넌스) — {e}")
            else:
                logger.warning(f"[MarginMgr] {product_code}: keep_rate 조회 실패 — {e}")
            return None

    # ──────────────────────────────────────────
    # Hooks (base override)
    # ──────────────────────────────────────────

    async def _on_candle_extra_checks(self, pair: str, params: Dict) -> bool:
        """keep_rate + 보유 시간 제한 체크."""
        keep_rate = await self._check_keep_rate(pair)
        self._last_keep_rate[pair] = keep_rate

        pos = self._position.get(pair)

        # ── 보유 시간 제한 ────────────────────────────────
        if pos is not None:
            max_hours = float(params.get("max_holding_hours", 0))
            if max_hours > 0 and pos.extra.get("opened_at"):
                opened_at = pos.extra["opened_at"]
                elapsed_hours = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600
                if elapsed_hours >= max_hours:
                    logger.info(
                        f"[MarginMgr] {pair}: 보유 시간 초과 "
                        f"{elapsed_hours:.1f}h ≥ {max_hours}h → 자동 청산"
                    )
                    await self._close_position(pair, "time_limit")
                    return False
        return True

    def _check_exit_warning(self, pair, signal, realtime_price, ema, pos, atr=None):
        """양방향 exit_warning + ATR 쿠션 + 4H 캔들 교체 cooling period."""
        if pos is None:
            return signal

        # ── 4H 캔들 교체 cooling period ──
        cooling_sec = float(getattr(self, "_params", {}).get(pair, {}).get("candle_change_cooling_sec", 300))
        last_change = getattr(self, "_last_candle_change_time", {}).get(pair)
        if last_change is not None:
            from datetime import datetime, timezone
            elapsed = (datetime.now(timezone.utc) - last_change).total_seconds()
            if elapsed < cooling_sec:
                if signal == "long_caution":
                    logger.debug(
                        f"[MarginMgr] {pair}: 4H 캔들 교체 cooling 중 ({elapsed:.0f}s/{cooling_sec:.0f}s) "
                        "— exit_warning 억제"
                    )
                    return "no_signal"
                return signal

        # ── ATR 쿠션 ──
        side = pos.extra.get("side", "buy")
        cushion = float(getattr(self, "_params", {}).get(pair, {}).get("exit_ema_atr_cushion", 0.1))
        atr_cushion = (atr * cushion) if (atr is not None and cushion > 0) else 0.0

        if side == "buy":
            if realtime_price < ema - atr_cushion:
                if signal != "long_caution":
                    logger.info(
                        f"[MarginMgr] {pair}: 롱 실시간가 ¥{realtime_price} < EMA ¥{ema:.4f} - cushion → 추세 이탈 감지"
                    )
                return "long_caution"
            else:
                return "no_signal" if signal == "long_caution" else signal
        elif side == "sell":
            if realtime_price > ema + atr_cushion:
                if signal != "short_caution":
                    logger.info(
                        f"[MarginMgr] {pair}: 숏 실시간가 ¥{realtime_price} > EMA ¥{ema:.4f} + cushion → 추세 이탈 감지"
                    )
                return "short_caution"
            else:
                return "no_signal" if signal == "short_caution" else signal

        return signal

    def _is_stop_triggered(self, pos, price, stop_loss_price):
        """양방향 스탑로스."""
        side = pos.extra.get("side", "buy")
        if side == "buy":
            return price <= stop_loss_price
        return price >= stop_loss_price

    async def _update_trailing_stop(self, pair, pos, current_price, atr, ema_slope_pct, rsi, params):
        """양방향 적응형 트레일링 스탑 — 이익 비례 mult + 손익분기 바닥."""
        from core.strategy.signals import compute_adaptive_trailing_mult, compute_profit_based_mult

        side = pos.extra.get("side", "buy")
        profit_mult = compute_profit_based_mult(
            pos.entry_price or 0.0, current_price, atr, params, side=side
        )
        if pos.stop_tightened:
            # tighten_stop_atr는 배수 상한. 이익이 더 크면 profit_mult가 더 좁으므로 그 쪽 사용.
            tighten_ceiling = float(params.get("tighten_stop_atr", 1.0))
            mult = min(tighten_ceiling, profit_mult)
        else:
            adaptive_mult = compute_adaptive_trailing_mult(ema_slope_pct, rsi, params)
            mult = min(adaptive_mult, profit_mult)

        breakeven_trigger = float(params.get("breakeven_trigger_atr", 1.0))

        if side == "buy":
            new_sl = round(current_price - atr * mult, 6)
            # 손익분기 바닥 (롱: 이익 >= ATR×trigger → floor=진입가)
            if pos.entry_price and pos.entry_price > 0:
                if (current_price - pos.entry_price) >= atr * breakeven_trigger:
                    new_sl = max(new_sl, pos.entry_price)
            current_sl = pos.stop_loss_price
            if current_sl is None or new_sl > current_sl:
                pos.stop_loss_price = new_sl
                await self._update_trailing_stop_in_db(pair, new_sl)
                logger.info(
                    f"[CfdMgr] {pair}: 롱 트레일링 스탑 ¥{current_sl} → ¥{new_sl} (x{mult:.1f})"
                )
        else:
            new_sl = round(current_price + atr * mult, 6)
            # 손익분기 바닥 (숏: 이익 >= ATR×trigger → ceiling=진입가)
            if pos.entry_price and pos.entry_price > 0:
                if (pos.entry_price - current_price) >= atr * breakeven_trigger:
                    new_sl = min(new_sl, pos.entry_price)
            current_sl = pos.stop_loss_price
            if current_sl is None or new_sl < current_sl:
                pos.stop_loss_price = new_sl
                await self._update_trailing_stop_in_db(pair, new_sl)
                logger.info(
                    f"[CfdMgr] {pair}: 숏 트레일링 스탑 ¥{current_sl} → ¥{new_sl} (x{mult:.1f})"
                )

    async def _pre_entry_checks(self, pair: str, side: str, params: dict) -> bool:
        """진입 전 검사: keep_rate 증거금 비율 체크."""
        keep_rate = self._last_keep_rate.get(pair)
        warn_threshold = float(params.get("keep_rate_warn", 1.5))
        if keep_rate is not None and keep_rate < warn_threshold:
            logger.info(
                f"[MarginMgr] {pair}: keep_rate={keep_rate:.2f} < warn={warn_threshold} → 차단"
            )
            return False
        return True

    # ──────────────────────────────────────────
    # 진입
    # ──────────────────────────────────────────

    async def _open_position(
        self, product_code: str, side: str, price: float, atr, params: Dict, *, signal_data: dict | None = None
    ) -> None:
        """증거금 기반 포지션 진입."""
        try:
            if not hasattr(self._adapter, "get_collateral"):
                logger.error(f"[MarginMgr] {product_code}: 어댑터에 get_collateral 없음")
                return
            collateral = await self._adapter.get_collateral()
            available_collateral = collateral.collateral - collateral.require_collateral
            if available_collateral <= 0:
                logger.debug(f"[MarginMgr] {product_code}: 여유 증거금 없음, 진입 스킵")
                return

            position_size_pct = float(params.get("position_size_pct", 10.0))
            invest_jpy = available_collateral * position_size_pct / 100
            min_jpy = float(params.get("min_order_jpy", 500))

            if invest_jpy < min_jpy:
                logger.info(
                    f"[MarginMgr] {product_code}: 투입 JPY({invest_jpy:.0f}) < {min_jpy:.0f}, 스킵"
                )
                return

            # 레버리지 체크
            max_leverage = float(params.get("max_leverage", 1.5))
            coin_size = round(invest_jpy / price, 8)
            effective_leverage = (coin_size * price) / collateral.collateral if collateral.collateral > 0 else 0
            if effective_leverage > max_leverage:
                coin_size = round(collateral.collateral * max_leverage / price, 8)
                logger.info(f"[MarginMgr] {product_code}: 레버리지 제한 → size={coin_size:.8f}")

            min_coin = float(params.get("min_coin_size", 0.001))
            if coin_size < min_coin:
                logger.debug(f"[MarginMgr] {product_code}: 수량 부족 ({coin_size} < {min_coin})")
                return

            # 슬리피지 체크
            max_slippage_pct = float(params.get("max_slippage_pct", 0.3))
            try:
                ticker = await self._adapter.get_ticker(product_code)
                if side == "buy" and ticker.ask > 0:
                    slippage = (ticker.ask - price) / price * 100
                    if slippage > max_slippage_pct:
                        logger.warning(
                            f"[MarginMgr] {product_code}: 슬리피지 초과 {slippage:.3f}%, 스킵"
                        )
                        return
            except Exception as e:
                logger.warning(f"[MarginMgr] {product_code}: 시세 조회 실패 — {e}")

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
                f"[MarginMgr] {product_code}: {side} 진입 완료 "
                f"order_id={order.order_id} price=¥{exec_price} size={exec_amount} "
                f"stop_loss=¥{initial_sl}"
            )
        except Exception as e:
            logger.error(f"[MarginMgr] {product_code}: 진입 오류 — {e}", exc_info=True)

    # ──────────────────────────────────────────
    # 청산
    # ──────────────────────────────────────────

    async def _close_position_impl(self, product_code: str, reason: str) -> None:
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
                    f"[MarginMgr] {product_code}: 포지션 수량 부족 ({close_size} < {min_size})"
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
                f"[MarginMgr] {product_code}: {side} 청산 완료 reason={reason} "
                f"order_id={order.order_id} size={close_size}"
            )
        except Exception as e:
            logger.error(f"[MarginMgr] {product_code}: 청산 오류 — {e}", exc_info=True)

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
            f"[MarginMgr] {product_code}: 스탑 타이트닝 ¥{current_sl} → ¥{new_sl} (x{tighten_mult})"
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
                logger.debug(f"[MarginMgr] {product_code}: DB 포지션 기록 id={rec.id}")
                return rec.id
        except Exception as e:
            logger.error(f"[MarginMgr] {product_code}: DB 진입 기록 실패 — {e}", exc_info=True)
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
                    f"[MarginMgr] {product_code}: DB 청산 기록 id={db_record_id} "
                    f"pnl=¥{pnl_jpy} ({pnl_pct}%)"
                )
        except Exception as e:
            logger.error(f"[MarginMgr] {product_code}: DB 청산 기록 실패 — {e}", exc_info=True)

    # ──────────────────────────────────────────
    # adjust_risk hook (서브클래스 오버라이드 가능)
    # ──────────────────────────────────────────

    async def _on_adjust_risk_hook(
        self, pair: str, adjustments: dict, params: dict
    ) -> None:
        """adjust_risk 실행 후 훅. 서브클래스에서 오버라이드 가능."""
        # 기본 구현: no-op (서브클래스에서 IFD-OCO 변경 등 구현 가능)
        pass

    # ──────────────────────────────────────────
    # Limit Order 진입 (적극적 진입 최적화)
    # ──────────────────────────────────────────

    async def _open_position_limit(
        self,
        pair: str,
        price: float,
        atr: Optional[float],
        params: Dict,
        *,
        signal_data: dict | None = None,

    ) -> Optional[Any]:
        """지정가(limit) 진입 주문 발주. 성공 시 PendingLimitOrder 반환, 실패 시 None."""
        import time as _time
        from core.exchange.types import PendingLimitOrder
        try:
            collateral = await self._adapter.get_collateral()
            jpy_available = collateral.collateral
            position_size_pct = float(params.get("position_size_pct", 10.0))
            invest_jpy = jpy_available * position_size_pct / 100
            min_jpy = float(params.get("min_order_jpy", 500))

            if invest_jpy < min_jpy:
                logger.info(
                    f"{self._log_prefix} {pair}: 투입 JPY({invest_jpy:.0f}) < {min_jpy:.0f}, limit 진입 스킵"
                )
                return None

            offset_ratio = float(params.get("limit_offset_atr_ratio", 0.15))
            if atr:
                limit_price = round(price - atr * offset_ratio, 0)
            else:
                limit_price = round(price * 0.999, 0)

            if limit_price <= 0:
                logger.warning(f"{self._log_prefix} {pair}: limit_price={limit_price} 유효하지 않음")
                return None

            coin_amount = round(invest_jpy / limit_price, 8)

            order = await self._adapter.place_order(
                order_type=OrderType.BUY,
                pair=pair,
                amount=coin_amount,
                price=limit_price,
            )

            logger.info(
                f"{self._log_prefix} {pair}: limit order 발주 "
                f"order_id={order.order_id} price=¥{limit_price:.0f} amount={coin_amount}"
            )

            return PendingLimitOrder(
                order_id=order.order_id,
                pair=pair,
                limit_price=limit_price,
                amount=coin_amount,
                invest_jpy=invest_jpy,
                placed_at=_time.time(),
                signal_at_placement="long_setup",
                params=dict(params),
                atr=atr,
                signal_data=signal_data or {},
            )
        except Exception as e:
            logger.error(f"{self._log_prefix} {pair}: limit 진입 주문 오류 — {e}", exc_info=True)
            return None

    async def _finalize_limit_entry(self, pair: str, order: Any, pending: Any) -> None:
        """Limit order 체결 후 포지션 등록."""
        from core.exchange.types import Position
        try:
            exec_price = order.price or pending.limit_price
            exec_amount = order.amount
            if exec_amount == 0 and exec_price > 0:
                exec_amount = round(pending.invest_jpy / exec_price, 8)

            atr = pending.atr
            params = pending.params
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
                invest_jpy=pending.invest_jpy,
                stop_loss_price=initial_sl,
                strategy_id=params.get("strategy_id"),
                signal_data=pending.signal_data,
            )

            logger.info(
                f"{self._log_prefix} {pair}: limit 진입 확정 "
                f"order_id={order.order_id} price=¥{exec_price} amount={exec_amount} "
                f"stop_loss=¥{initial_sl}"
            )
        except Exception as e:
            logger.error(f"{self._log_prefix} {pair}: limit 진입 확정 오류 — {e}", exc_info=True)


# 후방 호환 alias — 기존 import(CfdTrendFollowingManager)가 동작하도록 유지
CfdTrendFollowingManager = MarginTrendManager
