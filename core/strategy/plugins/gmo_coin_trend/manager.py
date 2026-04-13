"""
GmoCoinTrendManager — GMO Coin 레버리지 추세추종 매니저.

CdfTrendFollowingManager 상속.
GMO Coin 어댑터와 호환되지 않는 2개 메서드만 오버라이드:

  - _open_position: MARKET_BUY = JPY 전달 (어댑터 내부 `jpy / ticker.ask` → BTC 변환)
  - _close_position_impl: close_position_bulk API 사용 (반대매매 사용 금지)

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

from core.exchange.types import OrderType, Position
from core.strategy.plugins.cfd_trend_following.manager import CfdTrendFollowingManager

logger = logging.getLogger(__name__)


class GmoCoinTrendManager(CfdTrendFollowingManager):
    """GMO Coin レバレッジ 추세추종 매니저. 롱/숏 양방향."""

    _task_prefix = "gmoc_trend"
    _log_prefix = "[GmocMgr]"
    # _supports_short = True — CdfTrendFollowingManager에서 상속

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
                            f"[GmocMgr] {product_code}: 승인 TTL 만료 ({elapsed:.0f}s > {APPROVE_TTL_SEC}s) → 진입 차단"
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
                logger.warning(f"[GmocMgr] {product_code}: 최신 ticker 재취득 실패 — {e}. 원래 가격 유지")

            # ② 시그널 재평가 — EMA/RSI/slope 전체 재계산
            if approved_at_str:  # approve 게이트를 통과한 경우만
                basis_tf = str(params.get("basis_timeframe", "4h"))
                try:
                    fresh_signal = await self._compute_signal(product_code, basis_tf, params=params)
                    if fresh_signal is None:
                        logger.warning(f"[GmocMgr] {product_code}: 재평가 시그널 계산 실패 → 진입 차단")
                        return
                    sig = fresh_signal.get("signal", "no_signal")
                    if sig not in ("entry_ok", "entry_sell", "entry_preview"):
                        logger.info(
                            f"[GmocMgr] {product_code}: 시그널 소멸 (approve 후 재평가={sig}) → 진입 차단"
                        )
                        # Telegram 알림 (fire-and-forget)
                        try:
                            from core.task.auto_reporter import send_telegram_message
                            asyncio.ensure_future(
                                send_telegram_message(
                                    f"[GmocMgr] {product_code} 시그널 소멸\napprove 후 재평가 결과: {sig}\n진입 차단됨"
                                )
                            )
                        except Exception:
                            pass
                        return
                except Exception as e:
                    logger.warning(f"[GmocMgr] {product_code}: 시그널 재평가 실패 — {e}. fail-safe → 진입 차단")
                    return
            # ──────────────────────────────────────────────────────────

            if not hasattr(self._adapter, "get_collateral"):
                logger.error(f"[GmocMgr] {product_code}: 어댑터에 get_collateral 없음")
                return

            collateral = await self._adapter.get_collateral()
            available_collateral = collateral.collateral - collateral.require_collateral
            if available_collateral <= 0:
                logger.debug(f"[GmocMgr] {product_code}: 여유 증거금 없음, 진입 스킵")
                return

            position_size_pct = float(params.get("position_size_pct", 10.0))
            invest_jpy = available_collateral * position_size_pct / 100
            min_jpy = float(params.get("min_order_jpy", 500))

            if invest_jpy < min_jpy:
                logger.info(
                    f"[GmocMgr] {product_code}: 투입 JPY({invest_jpy:.0f}) < {min_jpy:.0f}, 스킵"
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
                    f"[GmocMgr] {product_code}: 레버리지 제한 → size={coin_size:.8f}"
                )

            min_coin = float(params.get("min_coin_size", 0.001))
            if coin_size < min_coin:
                logger.debug(
                    f"[GmocMgr] {product_code}: 수량 부족 ({coin_size} < {min_coin})"
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
                                    f"[GmocMgr] {product_code}: 스프레드 초과 {spread_pct:.3f}%, 스킵"
                                )
                                return
                    except Exception as e:
                        logger.warning(f"[GmocMgr] {product_code}: 슬리피지 체크 시세 조회 실패 — {e}")

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
                extra={"side": side, "opened_at": datetime.now(timezone.utc)},
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
                f"[GmocMgr] {product_code}: {side} 진입 완료 "
                f"order_id={order.order_id} price=¥{exec_price} size={exec_amount} "
                f"stop_loss=¥{initial_sl}"
            )
        except Exception as e:
            logger.error(f"[GmocMgr] {product_code}: 진입 오류 — {e}", exc_info=True)

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
                    f"[GmocMgr] {product_code}: 포지션 수량 부족 ({close_size} < {min_size})"
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
                    f"[GmocMgr] {product_code}: 어댑터에 close_position_bulk 없음"
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
                f"[GmocMgr] {product_code}: {side} 청산 완료 reason={reason} "
                f"order_id={order.order_id} size={close_size}"
            )
        except Exception as e:
            logger.error(f"[GmocMgr] {product_code}: 청산 오류 — {e}", exc_info=True)

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
