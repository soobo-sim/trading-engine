"""
GmoCoinTrendManager — GMO Coin 레버리지 추세추종 매니저.

MarginTrendManager(CdfTrendFollowingManager) 상속.
GMO Coin 어댑터와 호환되지 않는 메서드만 오버라이드:

  - _open_position: MARKET_BUY = JPY 전달 (어댑터 내부 `jpy / ticker.ask` → BTC 변환)
  - _close_position_impl: close_position_bulk API 사용 (반대매매 사용 금지)
  - _update_trailing_stop: 인메모리 스탑 갱신 후 changeLosscutPrice 거래소 동기화

나머지 양방향 로직(SL·trailing·position detection·exit_warning·스탑 타이트닝 등)은
CfdTrendFollowingManager에서 그대로 상속.

GMO Coin 어댑터 주문 시맨틱:
    MARKET_BUY:  amount = JPY 금액  → 어댑터 내부에서 jpy / ticker.ask → BTC 변환
    MARKET_SELL: amount = BTC 수량  (어댑터 변환 없음)

주의: 반대 place_order로 청산 시 신규 포지션이 열려버림 → close_position_bulk 필수.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.exchange.errors import ExchangeError
from core.exchange.types import OrderType, Position
from core.strategy.plugins.cfd_trend_following.manager import MarginTrendManager as CfdTrendFollowingManager

logger = logging.getLogger(__name__)


class GmoCoinTrendManager(CfdTrendFollowingManager):
    """GMO Coin レバレッジ 추세추종 매니저. 롱/숏 양방향."""

    _task_prefix = "gmoc_trend"
    _log_prefix = "[TrendMgr]"
    # _supports_short = True — CdfTrendFollowingManager에서 상속

    def _get_strategy_type(self) -> str:
        return "trend_following"

    # ──────────────────────────────────────────
    # 진입
    # ──────────────────────────────────────────

    async def _open_position(
        self,
        product_code: str,
        side: str,
        price: float,
        atr: Any,
        params: Dict,
        *,
        signal_data: dict | None = None,
    ) -> None:
        """GMO Coin 증거금 기반 레버리지 포지션 진입.

        MARKET_BUY: invest_jpy(정수)를 어댑터에 전달 → 내부에서 jpy/ticker.ask로 BTC 변환.
        MARKET_SELL: coin_size를 직접 전달.
        """
        try:
            # ── BUG-031: approve 후 파이프라인 (① 재ticker, ② 시그널 재평가, ③ TTL 30s) ──
            APPROVE_TTL_SEC = 30
            sd = signal_data or {}

            # ③ TTL 체크: approve 후 30초 초과 시 만료
            approved_at_str = sd.get("approved_at")
            if approved_at_str:
                try:
                    approved_at = datetime.fromisoformat(approved_at_str)
                    elapsed = (datetime.now(timezone.utc) - approved_at).total_seconds()
                    if elapsed > APPROVE_TTL_SEC:
                        logger.warning(
                            f"[TrendMgr] {product_code}: 승인 TTL 만료 ({elapsed:.0f}s > {APPROVE_TTL_SEC}s) → 진입 차단"
                        )
                        return
                except Exception:
                    pass

            # ① 최신 ticker 재취득 — 주문 가격 기준 갱신
            try:
                latest_ticker = await self._adapter.get_ticker(product_code)
                latest_price = latest_ticker.ask if side == "buy" else latest_ticker.bid
                if latest_price and latest_price > 0:
                    price = latest_price  # 이후 계산에 최신 가격 사용
            except Exception as e:
                logger.warning(f"[TrendMgr] {product_code}: 최신 ticker 재취득 실패 — {e}. 원래 가격 유지")

            # ② 시그널 재평가 — EMA/RSI/slope 전체 재계산
            if approved_at_str:  # approve 게이트를 통과한 경우만
                basis_tf = str(params.get("basis_timeframe", "4h"))
                try:
                    fresh_signal = await self._compute_signal(product_code, basis_tf, params=params)
                    if fresh_signal is None:
                        logger.warning(f"[TrendMgr] {product_code}: 재평가 시그널 계산 실패 → 진입 차단")
                        return
                    sig = fresh_signal.get("signal", "no_signal")
                    if sig not in ("long_setup", "short_setup", "entry_preview"):
                        logger.info(
                            f"[TrendMgr] {product_code}: 시그널 소멸 (approve 후 재평가={sig}) → 진입 차단"
                        )
                        # Telegram 알림 (fire-and-forget)
                        try:
                            from core.punisher.task.auto_reporter import send_telegram_message
                            asyncio.ensure_future(
                                send_telegram_message(
                                    f"[TrendMgr] {product_code} 시그널 소멸\napprove 후 재평가 결과: {sig}\n진입 차단됨"
                                )
                            )
                        except Exception:
                            pass
                        return
                except Exception as e:
                    logger.warning(f"[TrendMgr] {product_code}: 시그널 재평가 실패 — {e}. fail-safe → 진입 차단")
                    return
            # ──────────────────────────────────────────────────────────

            if not hasattr(self._adapter, "get_collateral"):
                logger.error(f"[TrendMgr] {product_code}: 어댑터에 get_collateral 없음")
                return

            collateral = await self._adapter.get_collateral()
            available_collateral = collateral.collateral - collateral.require_collateral
            if available_collateral <= 0:
                logger.debug(f"[TrendMgr] {product_code}: 여유 증거금 없음, 진입 스킵")
                return

            position_size_pct = float(params.get("position_size_pct", 10.0))
            invest_jpy = available_collateral * position_size_pct / 100
            min_jpy = float(params.get("min_order_jpy", 500))

            if invest_jpy < min_jpy:
                logger.info(
                    f"[TrendMgr] {product_code}: 투입 JPY({invest_jpy:.0f}) < {min_jpy:.0f}, 스킵"
                )
                return

            # 레버리지 체크
            max_leverage = float(params.get("max_leverage", 1.5))
            coin_size = round(invest_jpy / price, 8)
            effective_leverage = (
                (coin_size * price) / collateral.collateral
                if collateral.collateral > 0
                else 0
            )
            if effective_leverage > max_leverage:
                coin_size = round(collateral.collateral * max_leverage / price, 8)
                logger.info(
                    f"[TrendMgr] {product_code}: 레버리지 제한 → size={coin_size:.8f}"
                )

            min_coin = float(params.get("min_coin_size", 0.001))
            if coin_size < min_coin:
                logger.debug(
                    f"[TrendMgr] {product_code}: 수량 부족 ({coin_size} < {min_coin})"
                )
                return

            # 슬리피지 체크: 시그널 재평가(approved_at)를 통과한 경우 스킵
            # 재평가가 "현재 시장에서 진입 유효"를 이미 검증했으므로 중복 차단 불필요
            if not approved_at_str:
                max_slippage_pct = float(params.get("max_slippage_pct", 0.3))
                if side == "buy" and price > 0:
                    # 최신 ticker 기준 bid/ask 스프레드 비율
                    try:
                        chk_ticker = await self._adapter.get_ticker(product_code)
                        if chk_ticker.bid and chk_ticker.ask and chk_ticker.bid > 0:
                            spread_pct = (chk_ticker.ask - chk_ticker.bid) / chk_ticker.bid * 100
                            if spread_pct > max_slippage_pct:
                                logger.warning(
                                    f"[TrendMgr] {product_code}: 스프레드 초과 {spread_pct:.3f}%, 스킵"
                                )
                                return
                    except Exception as e:
                        logger.warning(f"[TrendMgr] {product_code}: 슬리피지 체크 시세 조회 실패 — {e}")

            # GMO Coin 어댑터 주문 시맨틱:
            #   MARKET_BUY: JPY 금액 전달 → 어댑터가 jpy / ticker.ask 로 BTC 변환
            #   MARKET_SELL: BTC 수량 직접 전달 (변환 없음)
            if side == "buy":
                order = await self._adapter.place_order(
                    order_type=OrderType.MARKET_BUY,
                    pair=product_code,
                    amount=round(invest_jpy, 0),
                )
            else:
                order = await self._adapter.place_order(
                    order_type=OrderType.MARKET_SELL,
                    pair=product_code,
                    amount=coin_size,
                )

            exec_price = order.price or price
            exec_amount = order.amount if order.amount > 0 else coin_size

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
                    "pyramid_count": 0,
                    "pyramid_entries": [],
                    "total_size_pct": position_size_pct / 100.0,
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
                f"[TrendMgr] {product_code}: {side} 진입 완료 "
                f"order_id={order.order_id} price=¥{exec_price} size={exec_amount} "
                f"stop_loss=¥{initial_sl}"
            )
        except Exception as e:
            logger.error(f"[TrendMgr] {product_code}: 진입 오류 — {e}", exc_info=True)

    # ──────────────────────────────────────────
    # 청산
    # ──────────────────────────────────────────

    async def _close_position_impl(self, product_code: str, reason: str) -> None:
        """close_position_bulk API로 GMO Coin 레버리지 건옥 청산.

        반대 place_order 사용 금지: GMO Coin에서 반대 place_order는 청산이 아니라
        신규 포지션을 열어버림.
        """
        try:
            pos = self._position.get(product_code)
            if pos is None:
                return

            side = pos.extra.get("side", "buy")
            close_size = pos.entry_amount
            min_size = float(
                self._params.get(product_code, {}).get("min_coin_size", 0.001)
            )

            if close_size < min_size:
                logger.warning(
                    f"[TrendMgr] {product_code}: 포지션 수량 부족 ({close_size} < {min_size})"
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

            if not hasattr(self._adapter, "close_position_bulk"):
                logger.error(
                    f"[TrendMgr] {product_code}: 어댑터에 close_position_bulk 없음"
                )
                return

            order = await self._adapter.close_position_bulk(
                symbol=product_code,
                side=side,
                size=close_size,
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
                f"[TrendMgr] {product_code}: {side} 청산 완료 reason={reason} "
                f"order_id={order.order_id} size={close_size}"
            )
        except ExchangeError as e:
            if "ERR-422" in str(e):
                # 거래소에 포지션 없음 → 이미 청산된 것으로 간주
                logger.warning(
                    f"[TrendMgr] {product_code}: 거래소 포지션 없음(ERR-422) "
                    f"→ 인메모리 클리어 reason={reason}"
                )
                prev_pos = self._position.get(product_code)
                prev_db_id = prev_pos.db_record_id if prev_pos else None
                self._position[product_code] = None
                if prev_db_id:
                    try:
                        exec_price = self._latest_price.get(product_code, 0)
                        await self._record_close(
                            db_record_id=prev_db_id,
                            product_code=product_code,
                            side=side,
                            order_id="",
                            price=exec_price,
                            size=close_size,
                            reason=f"{reason}_exchange_already_closed",
                            entry_price=prev_pos.entry_price if prev_pos else None,
                        )
                    except Exception as rec_err:
                        logger.error(
                            f"[TrendMgr] {product_code}: ERR-422 후 DB 기록 실패 — {rec_err}"
                        )
            else:
                logger.error(f"[TrendMgr] {product_code}: 청산 오류 — {e}", exc_info=True)
        except Exception as e:
            logger.error(f"[TrendMgr] {product_code}: 청산 오류 — {e}", exc_info=True)

    # ──────────────────────────────────────────
    # 피라미딩 (포지션 추가 매수)
    # ──────────────────────────────────────────

    async def _add_to_position(
        self,
        product_code: str,
        side: str,
        price: float,
        atr: Optional[float],
        params: Dict,
        *,
        result: Any = None,
    ) -> None:
        """피라미딩 추가 매수 실행.

        1) 증거금 여유 확인
        2) 투입 JPY 계산 (decision.size_pct 또는 position_size_pct 사용)
        3) 레버리지 체크 후 주문 실행
        4) 가중평균가 · 합산 수량 갱신
        5) SL 재계산 (not-worsen: 롱은 더 아래로만, 숏은 더 위로만)
        6) DB 업데이트
        """
        try:
            pos = self._position.get(product_code)
            if pos is None:
                logger.warning(f"[TrendMgr] {product_code}: add_to_position 호출 시 포지션 없음 — 스킵")
                return

            # ── 투입 비율 결정 ──
            # Decision.size_pct는 소수(0.0~1.0), params.position_size_pct는 퍼센트(10~100) 단위.
            # 내부 계산 단위를 퍼센트(0~100)로 통일.
            add_size_pct: float
            if result is not None and hasattr(result, "decision") and result.decision is not None:
                raw_size = getattr(result.decision, "size_pct", None)
                if raw_size is not None:
                    add_size_pct = float(raw_size) * 100  # 소수 → 퍼센트 변환 (0.15 → 15.0)
                else:
                    add_size_pct = float(params.get("position_size_pct", 10.0))
            else:
                add_size_pct = float(params.get("position_size_pct", 10.0))

            # ── 증거금 조회 ──
            if not hasattr(self._adapter, "get_collateral"):
                logger.error(f"[TrendMgr] {product_code}: 어댑터에 get_collateral 없음")
                return

            collateral = await self._adapter.get_collateral()
            available_collateral = collateral.collateral - collateral.require_collateral
            if available_collateral <= 0:
                logger.warning(f"[TrendMgr] {product_code}: 피라미딩 — 여유 증거금 없음, 스킵")
                return

            invest_jpy = available_collateral * add_size_pct / 100
            min_jpy = float(params.get("min_order_jpy", 500))
            if invest_jpy < min_jpy:
                logger.info(
                    f"[TrendMgr] {product_code}: 피라미딩 투입 JPY({invest_jpy:.0f}) < {min_jpy:.0f}, 스킵"
                )
                return

            # ── 레버리지 체크 ──
            max_leverage = float(params.get("max_leverage", 1.5))
            coin_size = round(invest_jpy / price, 8)
            effective_leverage = (
                (coin_size * price) / collateral.collateral
                if collateral.collateral > 0
                else 0
            )
            if effective_leverage > max_leverage:
                coin_size = round(collateral.collateral * max_leverage / price, 8)
                logger.info(f"[TrendMgr] {product_code}: 피라미딩 레버리지 제한 → size={coin_size:.8f}")

            min_coin = float(params.get("min_coin_size", 0.001))
            if coin_size < min_coin:
                logger.info(f"[TrendMgr] {product_code}: 피라미딩 수량 부족 ({coin_size} < {min_coin}), 스킵")
                return

            # ── 주문 실행 ──
            if side == "buy":
                order = await self._adapter.place_order(
                    order_type=OrderType.MARKET_BUY,
                    pair=product_code,
                    amount=round(invest_jpy, 0),
                )
            else:
                order = await self._adapter.place_order(
                    order_type=OrderType.MARKET_SELL,
                    pair=product_code,
                    amount=coin_size,
                )

            exec_price = order.price or price
            exec_amount = order.amount if order.amount > 0 else coin_size

            # ── 가중평균가 계산 ──
            prev_entry = pos.entry_price
            prev_amount = pos.entry_amount
            new_amount = round(prev_amount + exec_amount, 8)
            if new_amount > 0:
                new_avg_price = round(
                    (prev_entry * prev_amount + exec_price * exec_amount) / new_amount, 2
                )
            else:
                new_avg_price = exec_price

            # ── SL 재계산 (not-worsen) ──
            atr_mult = float(params.get("atr_multiplier_stop", 2.0))
            prev_sl = pos.stop_loss_price
            if atr:
                if side == "buy":
                    new_sl_candidate = round(new_avg_price - atr * atr_mult, 6)
                    # 롱: 기존 SL보다 낮으면 not-worsen으로 유지
                    new_sl = max(prev_sl, new_sl_candidate) if prev_sl else new_sl_candidate
                else:
                    new_sl_candidate = round(new_avg_price + atr * atr_mult, 6)
                    # 숏: 기존 SL보다 높으면 not-worsen으로 유지
                    new_sl = min(prev_sl, new_sl_candidate) if prev_sl else new_sl_candidate
            else:
                new_sl = prev_sl

            # ── pyramid_entries 기록 ──
            pyramid_entries: list = pos.extra.get("pyramid_entries", [])
            pyramid_count = pos.extra.get("pyramid_count", 0)
            pyramid_entries.append({
                "n": pyramid_count + 1,
                "price": exec_price,
                "amount": exec_amount,
                "order_id": order.order_id,
            })

            # ── 인메모리 Position 업데이트 ──
            pos.entry_price = new_avg_price
            pos.entry_amount = new_amount
            pos.stop_loss_price = new_sl
            pos.extra["pyramid_count"] = pyramid_count + 1
            pos.extra["pyramid_entries"] = pyramid_entries
            pos.extra["total_size_pct"] = pos.extra.get("total_size_pct", 0.0) + add_size_pct / 100.0

            logger.info(
                f"[TrendMgr] {product_code}: 피라미딩 #{pyramid_count + 1}/3 완료 "
                f"order_id={order.order_id} exec_price=¥{exec_price} exec_amount={exec_amount} "
                f"new_avg=¥{new_avg_price} total_amount={new_amount} new_sl=¥{new_sl}"
            )

            # ── DB 업데이트 ──
            await self._update_position_in_db(
                product_code=product_code,
                db_record_id=pos.db_record_id,
                entry_price=new_avg_price,
                size=new_amount,
                stop_loss_price=new_sl,
                pyramid_count=pyramid_count + 1,
            )

        except Exception as e:
            logger.error(f"[TrendMgr] {product_code}: 피라미딩 오류 — {e}", exc_info=True)

    async def _update_position_in_db(
        self,
        product_code: str,
        db_record_id: Optional[int],
        entry_price: float,
        size: float,
        stop_loss_price: Optional[float],
        pyramid_count: int,
    ) -> None:
        """gmoc_trend_positions 레코드를 피라미딩 후 상태로 업데이트."""
        if db_record_id is None:
            logger.warning(f"[TrendMgr] {product_code}: DB 업데이트 스킵 — db_record_id 없음")
            return
        try:
            from sqlalchemy import update as sa_update

            Model = self._position_model
            async with self._session_factory() as db:
                await db.execute(
                    sa_update(Model)
                    .where(Model.id == db_record_id)
                    .values(
                        entry_price=entry_price,
                        entry_size=size,
                        stop_loss_price=stop_loss_price,
                        pyramid_count=pyramid_count,
                    )
                )
                await db.commit()
            logger.debug(
                f"[TrendMgr] {product_code}: DB 피라미딩 업데이트 id={db_record_id} "
                f"avg=¥{entry_price} size={size} sl=¥{stop_loss_price} pyramid={pyramid_count}"
            )
        except Exception as e:
            logger.error(f"[TrendMgr] {product_code}: DB 피라미딩 업데이트 실패 — {e}", exc_info=True)

    # ──────────────────────────────────────────
    # 진입 전 체크 오버라이드
    # ──────────────────────────────────────────

    async def _pre_entry_checks(self, pair: str, side: str, params: dict) -> bool:
        """GMO Coin 전용 진입 전 검사.

        GMO Coin 레버레지는 keep_rate / FX 주말 휴장 개념 없음.
        부모(CfdTrendFollowingManager)의 keep_rate 차단 로직을 완전히 교체한다.
        """
        # GMO Coin은 keep_rate / FX 시장 휴장 체크 불필요 → 통과
        return True

    # ──────────────────────────────────────────
    # 트레일링 스탑 + 거래소 ロスカットレート 동기화
    # ──────────────────────────────────────────

    async def _update_trailing_stop(
        self, pair, pos, current_price, atr, ema_slope_pct, rsi, params
    ) -> None:
        """인메모리 트레일링 스탑 갱신 후 거래소 ロスカットレート에 동기화.

        부모(CdfTrendFollowingManager)가 pos.stop_loss_price를 갱신하면,
        해당 값을 GMO Coin 건옥의 losscutPrice로 즉시 동기화한다.
        봇 다운 / WS 끊김 시에도 거래소 자체가 강제청산을 실행하는 안전망.
        """
        sl_before = pos.stop_loss_price
        await super()._update_trailing_stop(pair, pos, current_price, atr, ema_slope_pct, rsi, params)
        sl_after = pos.stop_loss_price

        # 스탑이 실제로 갱신된 경우에만 거래소 동기화
        if sl_after is not None and sl_after != sl_before:
            await self._sync_losscut_price(pair, sl_after)

    async def _sync_losscut_price(self, pair: str, new_sl: float) -> None:
        """현재 보유 중인 모든 건옥의 ロスカットレート를 new_sl로 동기화.

        피라미딩으로 복수 건옥이 있을 수 있으므로 get_positions()로 전체 조회.
        실패해도 인메모리 스탑 로직에 영향 없음 (WARNING 로그만).
        """
        if not hasattr(self._adapter, "get_positions") or not hasattr(self._adapter, "change_losscut_price"):
            return
        try:
            fx_positions = await self._adapter.get_positions(pair)
            if not fx_positions:
                return
            for fx_pos in fx_positions:
                if fx_pos.position_id is None:
                    continue
                ok = await self._adapter.change_losscut_price(fx_pos.position_id, new_sl)
                if not ok:
                    logger.warning(
                        f"[TrendMgr] {pair}: ロスカットレート 동기화 실패 "
                        f"(position_id={fx_pos.position_id}, sl=¥{new_sl:.0f}) — "
                        "in-memory stop이 보호 중"
                    )
                    # BUG-045: ERR-578 시 현재가 vs in-memory SL 진단 + 긴급 청산 트리거
                    try:
                        ticker = await self._adapter.get_ticker(pair)
                        pos = self._position.get(pair)
                        side = pos.extra.get("side", "?") if pos else "?"
                        in_mem_sl = pos.stop_loss_price if pos else None
                        if in_mem_sl is not None:
                            if side == "sell":
                                sl_breached = ticker.last > new_sl
                            elif side == "buy":
                                sl_breached = ticker.last < new_sl
                            else:
                                sl_breached = False
                        else:
                            sl_breached = False
                        status_str = "⚠️ SL 초과 — 긴급 청산 발동" if sl_breached else "정상 범위"
                        logger.error(
                            f"[TrendMgr] {pair}: ERR-578 진단 — "
                            f"현재가=¥{ticker.last:,.0f} / in-memory SL=¥{new_sl:,.0f} / "
                            f"side={side} / 상태={status_str}"
                        )
                        if sl_breached:
                            logger.critical(
                                f"[TrendMgr] {pair}: ERR-578 + SL 초과 확인 — 긴급 청산 태스크 생성"
                            )
                            asyncio.create_task(self._close_position(pair, "stop_loss_err578"))
                    except Exception:
                        pass
                    try:
                        import os
                        from core.shared.logging.telegram_handlers import _send_telegram
                        bot_token = os.getenv("AUTO_REPORT_BOT_TOKEN", "")
                        chat_id = os.getenv("AUTO_REPORT_CHAT_ID", "")
                        asyncio.ensure_future(
                            _send_telegram(
                                bot_token,
                                chat_id,
                                f"⚠️ [TrendMgr] {pair} 로스컷 동기화 실패\n"
                                f"position_id={fx_pos.position_id}\n"
                                f"SL=¥{new_sl:,.0f} — 인메모리 SL 보호 중",
                            )
                        )
                    except Exception:
                        pass
                else:
                    logger.debug(
                        f"[TrendMgr] {pair}: ロスカットレート 동기화 완료 "
                        f"(position_id={fx_pos.position_id}, sl=¥{new_sl})"
                    )
        except Exception as e:
            logger.warning(f"[TrendMgr] {pair}: ロスカットレート 동기화 오류 — {e}")
