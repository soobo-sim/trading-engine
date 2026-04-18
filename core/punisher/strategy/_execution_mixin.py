"""
ExecutionMixin — 실행 도메인 전담 Mixin.

PUNISHER 도메인 소유. 퍼니셔 에이전트만 수정한다.
져지가 이 파일을 수정하면 도메인 경계 침범.

포함 메서드:
  - _handle_execution_result()      ExecutionResult → action별 실행 디스패치
  - _on_entry_signal()              진입 시그널 → entry_mode 디스패치(market/limit)
  - _update_trailing_stop()         적응형 트레일링 스탑 ratchet
  - _update_trailing_stop_in_db()   트레일링 스탑 DB 갱신
  - _close_position()               청산 wrapper (paper/real + 학습 루프)
  - _update_judgment_outcome()      ai_judgments outcome UPDATE (학습 루프)
  - _check_pending_limit_order()    Pending limit order 상태 확인
  - _try_paper_entry()              Paper 모드 진입 처리
  - _open_position_limit()          Limit order 진입 (서브클래스 override)
  - _finalize_limit_entry()         Limit 체결 후 포지션 등록 (서브클래스 override)
  - _on_adjust_risk_hook()          adjust_risk 후 서브클래스별 추가 처리

NOTE: self._* 필드는 BaseTrendManager.__init__()에서 초기화된다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

from core.data.dto import SignalSnapshot
from core.strategy.signals import (
    compute_adaptive_trailing_mult,
    compute_profit_based_mult,
    detect_bearish_divergences,
)

if TYPE_CHECKING:
    from core.exchange.types import Position

logger = logging.getLogger("core.strategy.base_trend")


class ExecutionMixin:
    """실행 디스패치 + 주문 + 트레일링 + 청산. PUNISHER 도메인 소유."""

    # ──────────────────────────────────────────
    # Paper Trading
    # ──────────────────────────────────────────

    async def _try_paper_entry(
        self,
        pair: str,
        direction: str,
        current_price: float,
        atr: Optional[float],
        params: Dict,
    ) -> bool:
        """Paper 모드 진입 처리. paper pair가 아니면 False 반환(실주문 진행).

        True 반환 시 _open_position 호출 스킵.
        인메모리 Position을 생성해 stop_loss_monitor가 동작하도록 유지한다.
        """
        paper_exec = self._paper_executors.get(pair)
        if paper_exec is None:
            return False

        strategy_id = params.get("strategy_id", 0)
        paper_id = await paper_exec.record_paper_entry(
            strategy_id=strategy_id,
            pair=pair,
            direction=direction,
            entry_price=current_price,
        )
        if paper_id is None:
            return False  # 기록 실패 시 실주문 진행 (안전 방향)

        # 인메모리 포지션 생성 (스탑로스 모니터 유지 + 트레일링 스탑 동작)
        from core.exchange.types import Position
        atr_mult = float(params.get("atr_multiplier_stop", 2.0))
        if direction in ("sell", "short"):
            initial_sl = round(current_price + atr * atr_mult, 6) if atr else None
        else:
            initial_sl = round(current_price - atr * atr_mult, 6) if atr else None
        self._position[pair] = Position(
            pair=pair,
            entry_price=current_price,
            entry_amount=0.0,  # paper: 실수량 없음
            stop_loss_price=initial_sl,
        )
        self._paper_positions[pair] = {
            "paper_trade_id": paper_id,
            "entry_price": current_price,
            "direction": direction,
        }
        logger.info(
            f"{self._log_prefix} {pair}: Paper 진입 기록 id={paper_id} "
            f"direction={direction} price={current_price}"
        )
        return True

    # ──────────────────────────────────────────
    # 트레일링 스탑
    # ──────────────────────────────────────────

    async def _update_trailing_stop_in_db(self, pair: str, stop_loss_price: float) -> None:
        try:
            pos = self._position.get(pair)
            if pos is None or pos.db_record_id is None:
                return
            Model = self._position_model
            from sqlalchemy import select
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

    async def _update_trailing_stop(
        self, pair: str, pos: "Position", current_price: float,
        atr: float, ema_slope_pct: Optional[float], rsi: Optional[float], params: Dict
    ) -> None:
        """적응형 트레일링 스탑 ratchet. 서브클래스에서 양방향 지원 가능."""
        side = pos.extra.get("side", "buy")
        profit_mult = compute_profit_based_mult(
            pos.entry_price or 0.0, current_price, atr, params, side=side
        )
        if pos.stop_tightened:
            # tighten_stop_atr는 배수 상한(ceiling). 이익이 더 크면 profit_mult가 더 좁으므로 그 쪽 사용.
            tighten_ceiling = float(params.get("tighten_stop_atr", 1.0))
            mult = min(tighten_ceiling, profit_mult)
        else:
            adaptive_mult = compute_adaptive_trailing_mult(ema_slope_pct, rsi, params)
            mult = min(adaptive_mult, profit_mult)
        new_sl = round(current_price - atr * mult, 6)

        # 손익분기 바닥: 이익 >= ATR×breakeven_trigger_atr 이면 floor=진입가
        if pos.entry_price and pos.entry_price > 0:
            breakeven_trigger = float(params.get("breakeven_trigger_atr", 1.0))
            if (current_price - pos.entry_price) >= atr * breakeven_trigger:
                new_sl = max(new_sl, pos.entry_price)

        current_sl = pos.stop_loss_price
        if current_sl is None or new_sl > current_sl:
            pos.stop_loss_price = new_sl
            await self._update_trailing_stop_in_db(pair, new_sl)
            logger.info(
                f"{self._log_prefix} {pair}: 트레일링 스탑 갱신 "
                f"¥{current_sl} → ¥{new_sl} "
                f"(x{mult:.1f} {'tight' if pos.stop_tightened else 'adaptive+profit'})"
            )

    # ──────────────────────────────────────────
    # ExecutionResult 디스패치
    # ──────────────────────────────────────────

    async def _handle_execution_result(
        self,
        pair: str,
        result: Any,             # ExecutionResult — Any로 선언해 순환 import 방지
        snapshot: SignalSnapshot,
        signal_data: dict,
        params: dict,
    ) -> bool:
        """ExecutionResult → 실제 실행.

        Returns:
            True  — 호출자(_candle_monitor)가 continue해야 하는 경우 (청산 후)
            False — 다음 로직 불필요 (hold / entry 등)
        """
        action = result.action
        pos = self._position.get(pair)
        current_price = snapshot.current_price
        atr = snapshot.atr
        ema_slope_pct = snapshot.ema_slope_pct
        rsi = snapshot.rsi

        if action == "exit":
            trigger = result.decision.trigger if result.decision else "orchestrator_exit"
            logger.info(
                f"{self._log_prefix} {pair}: {trigger} @ ¥{current_price} → 전량 청산"
            )
            await self._close_position(pair, trigger)
            return True  # continue

        if action == "tighten_stop":
            if pos is not None and not pos.stop_tightened and atr:
                await self._apply_stop_tightening(pair, current_price, atr, params)
            return False

        # ── RegimeGate 진입 차단 체크 (entry_long / entry_short) ──
        if action in ("entry_long", "entry_short") and self._regime_gate is not None:
            manager_type = self._get_strategy_type()
            if not self._regime_gate.should_allow_entry(manager_type):
                logger.debug(
                    f"{self._log_prefix} {pair}: RegimeGate 진입 차단 "
                    f"(active={self._regime_gate.active_strategy}, this={manager_type})"
                )
                return False

        if action == "entry_long":
            is_preview = getattr(snapshot, "is_preview", False)
            entry_signal = "entry_preview" if is_preview else "entry_ok"
            # approved_at을 signal_data에 병합 (BUG-031 TTL 체크용)
            if result.decision and result.decision.meta.get("approved_at"):
                signal_data = {**signal_data, "approved_at": result.decision.meta["approved_at"]}
            await self._on_entry_signal(
                pair, entry_signal, current_price, atr, params, signal_data
            )
            # 진입 성공 시 학습 루프 연결: judgment_id / entry_time / confidence 기록
            new_pos = self._position.get(pair)
            if new_pos is not None and result.judgment_id is not None:
                new_pos.extra["judgment_id"] = result.judgment_id
                new_pos.extra["entry_time"] = datetime.now(timezone.utc).isoformat()
                if result.decision is not None:
                    new_pos.extra["confidence"] = result.decision.confidence
                    new_pos.extra["side"] = new_pos.extra.get("side", "long")
                logger.info(
                    f"{self._log_prefix} {pair}: 판단→실행 연결 — "
                    f"judgment_id={result.judgment_id}, "
                    f"확신도={new_pos.extra.get('confidence', 0.0):.0%}, "
                    f"side={new_pos.extra.get('side', 'long')}"
                )
            if is_preview and new_pos is not None:
                new_pos.extra["preview_entry"] = True
            return False

        if action == "entry_short":
            if not self._supports_short:
                logger.warning(
                    f"{self._log_prefix} {pair}: entry_short 차단 — "
                    f"롱 전용 매니저. 숏이 필요하면 cfd_trend_following 사용"
                )
                return False
            is_preview = getattr(snapshot, "is_preview", False)
            entry_signal = "entry_preview" if is_preview else "entry_sell"
            # approved_at을 signal_data에 병합 (BUG-031 TTL 체크용)
            if result.decision and result.decision.meta.get("approved_at"):
                signal_data = {**signal_data, "approved_at": result.decision.meta["approved_at"]}
            await self._on_entry_signal(
                pair, entry_signal, current_price, atr, params, signal_data
            )
            # 진입 성공 시 학습 루프 연결: judgment_id / entry_time / confidence 기록
            new_pos = self._position.get(pair)
            if new_pos is not None and result.judgment_id is not None:
                new_pos.extra["judgment_id"] = result.judgment_id
                new_pos.extra["entry_time"] = datetime.now(timezone.utc).isoformat()
                if result.decision is not None:
                    new_pos.extra["confidence"] = result.decision.confidence
                    new_pos.extra["side"] = new_pos.extra.get("side", "short")
                logger.info(
                    f"{self._log_prefix} {pair}: 판단→실행 연결 — "
                    f"judgment_id={result.judgment_id}, "
                    f"확신도={new_pos.extra.get('confidence', 0.0):.0%}, "
                    f"side={new_pos.extra.get('side', 'short')}"
                )
            if is_preview and new_pos is not None:
                new_pos.extra["preview_entry"] = True
            return False

        if action == "blocked":
            logger.info(
                f"{self._log_prefix} {pair}: 진입 차단 — {result.reason}"
            )
            return False

        if action == "add_position":
            # 피라미딩 추가 매수 — 포지션 보유 중 추가 진입
            pos = self._position.get(pair)
            if pos is None:
                logger.warning(
                    f"{self._log_prefix} {pair}: add_position이나 포지션 없음 — 스킵"
                )
                return False

            pyramid_count = pos.extra.get("pyramid_count", 0)
            _MAX_PYRAMID = 3
            if pyramid_count >= _MAX_PYRAMID:
                logger.info(
                    f"{self._log_prefix} {pair}: 피라미딩 상한 {_MAX_PYRAMID} 도달 — "
                    f"add_position 스킵 (이중 안전)"
                )
                return False

            side = pos.extra.get("side", "buy")
            logger.info(
                f"{self._log_prefix} {pair}: add_position 실행 시작 — "
                f"피라미딩 #{pyramid_count+1}/{_MAX_PYRAMID} "
                f"side={side} 현재가=¥{current_price:,.0f} "
                f"기존 진입가=¥{pos.entry_price:,.0f} "
                f"기존 수량={pos.entry_amount}"
            )
            await self._add_to_position(pair, side, current_price, atr, params, result=result)

            # 실행 후 결과 로그
            updated_pos = self._position.get(pair)
            if updated_pos is not None and updated_pos.extra.get("pyramid_count", 0) > pyramid_count:
                logger.info(
                    f"{self._log_prefix} {pair}: add_position 완료 — "
                    f"피라미딩 #{updated_pos.extra['pyramid_count']}/{_MAX_PYRAMID} "
                    f"평균단가=¥{updated_pos.entry_price:,.0f} "
                    f"합산수량={updated_pos.entry_amount} "
                    f"SL=¥{updated_pos.stop_loss_price}"
                )
            else:
                logger.info(
                    f"{self._log_prefix} {pair}: add_position 미완료 — "
                    f"Position 미변경 (증거금 부족 또는 주문 실패)"
                )

            # 학습 루프 연결
            if updated_pos is not None and result.judgment_id is not None:
                updated_pos.extra.setdefault("pyramid_judgment_ids", []).append(result.judgment_id)

            return False

        if action == "adjust_risk":
            # 레이첼 advisory adjust_risk — 포지션 리스크 파라미터 동적 재조정
            adjustments: dict = (
                result.decision.meta.get("adjustments", {}) if result.decision else {}
            )
            if adjustments:
                # force_exit 처리: true이면 즉시 전량 청산 (설계서 §7-3, §7-4)
                if adjustments.get("force_exit", False):
                    logger.warning(
                        f"{self._log_prefix} {pair}: adjust_risk force_exit=true → 즉시 청산"
                    )
                    await self._close_position(pair, "advisory_force_exit")
                    return False

                _adjustable_keys = {
                    "stop_loss_pct",
                    "trailing_stop_atr_initial",
                    "trailing_stop_atr_mature",
                    "tighten_stop_atr",
                    "ema_slope_weak_threshold",
                }
                applied = {}
                for k, v in adjustments.items():
                    if k in _adjustable_keys:
                        params[k] = v
                        applied[k] = v
                if applied:
                    logger.info(
                        f"{self._log_prefix} {pair}: adjust_risk 적용 — {applied}"
                    )
                    # 새로운 SL이 있으면 즉시 갱신
                    if pos is not None and atr is not None and "stop_loss_pct" in applied:
                        sl_pct = float(applied["stop_loss_pct"])
                        new_sl = round(current_price * (1.0 - sl_pct / 100.0), 6)
                        if pos.stop_loss_price is None or new_sl > pos.stop_loss_price:
                            pos.stop_loss_price = new_sl
                            await self._update_trailing_stop_in_db(pair, new_sl)
                            logger.info(
                                f"{self._log_prefix} {pair}: adjust_risk SL 갱신 → ¥{new_sl}"
                            )
                # 서브클래스 훅 (GMO FX IFD-OCO 주문 변경 등)
                await self._on_adjust_risk_hook(pair, adjustments, params)
            return False

        # hold: 트레일링 스탑 (포지션 있을 때만)
        if pos is not None and atr is not None:
            await self._update_trailing_stop(
                pair, pos, current_price, atr, ema_slope_pct, rsi, params
            )
        return False

    # ──────────────────────────────────────────
    # 진입 실행
    # ──────────────────────────────────────────

    async def _on_entry_signal(
        self, pair: str, signal: str, current_price: float,
        atr: Optional[float], params: Dict, signal_data: dict
    ) -> None:
        """진입 시그널 처리.

        signal:
          "entry_ok"      — 4H 완성 시그널, 로직: entry_mode에 따라 market 또는 limit
          "entry_preview" — 미완성 캔들 프리뷰 시그널, 동일하게 entry_mode 디스패치
          "entry_sell"    — 숏 진입 (CFD 전용)
        """
        is_long_entry = signal in ("entry_ok", "entry_preview")
        is_short_entry = signal == "entry_sell"

        if not (is_long_entry or is_short_entry):
            return

        direction = "long" if is_long_entry else "short"
        side = "sell" if direction == "short" else "buy"

        # ── 서브클래스 훅: keep_rate, FX 주말/시장 체크 등 ──────────────────
        if not await self._pre_entry_checks(pair, side, params):
            return

        logger.info(
            f"{self._log_prefix} {pair}: {signal} @ ¥{current_price} → 진입 시도 (dir={direction})"
        )

        # Paper pair는 실주문 스킵
        if await self._try_paper_entry(pair, direction, current_price, atr, params):
            return

        # ── entry_mode 디스패치: market / limit / limit_then_market ──
        entry_mode = str(params.get("entry_mode", "market"))
        is_preview_signal = (signal == "entry_preview")

        if entry_mode in ("limit", "limit_then_market"):
            pending = await self._open_position_limit(
                pair, current_price, atr, params,
                signal_data=signal_data,
                is_preview=is_preview_signal,
            )
            if pending is not None:
                self._pending_limit_orders[pair] = pending
                logger.info(
                    f"{self._log_prefix} {pair}: limit order 등록 — "
                    f"order_id={pending.order_id} price=¥{pending.limit_price:.0f}"
                )
                return
            # limit 실패
            if entry_mode == "limit":
                logger.info(f"{self._log_prefix} {pair}: limit 진입 실패 — market fallback 없음")
                return
            logger.info(f"{self._log_prefix} {pair}: limit 실패 — market fallback")

        # market order (default 또는 limit_then_market fallback)
        await self._open_position(pair, side, current_price, atr, params, signal_data=signal_data)

    # ──────────────────────────────────────────
    # Pending Limit Order 처리
    # ──────────────────────────────────────────

    async def _check_pending_limit_order(
        self, pair: str, pending: Any, current_signal: str, params: dict
    ) -> bool:
        """Pending limit order 상태 확인.

        Returns:
            True  → _candle_monitor가 continue해야 함 (대기 중 또는 포지션 등록 완료)
            False → pending 제거됨, 정규 흐름 재시도 가능
        """
        from core.exchange.types import OrderStatus

        elapsed = time.time() - pending.placed_at
        limit_timeout_sec = float(params.get("limit_timeout_sec", 300))

        try:
            order = await self._adapter.get_order(pending.order_id, pending.pair)
        except Exception as e:
            logger.warning(f"{self._log_prefix} {pair}: limit order 조회 실패 — {e}")
            return True  # 다음 사이클에서 재시도

        if order is None or order.status == OrderStatus.CANCELLED:
            logger.info(f"{self._log_prefix} {pair}: limit order 취소됨 → pending 제거")
            del self._pending_limit_orders[pair]
            return False

        if order.status == OrderStatus.COMPLETED:
            logger.info(
                f"{self._log_prefix} {pair}: limit order 체결 완료 "
                f"order_id={pending.order_id} price=¥{order.price}"
            )
            await self._finalize_limit_entry(pair, order, pending)
            del self._pending_limit_orders[pair]
            return True  # 포지션 등록 완료 → continue

        # OPEN 상태: 시그널 변경 감지 → 즉시 취소 (추세 이탈 보호)
        if current_signal not in ("entry_ok", "entry_preview"):
            logger.info(
                f"{self._log_prefix} {pair}: 시그널 변경 ({current_signal}) → limit order 취소"
            )
            try:
                await self._adapter.cancel_order(pending.order_id, pending.pair)
            except Exception as e:
                logger.warning(f"{self._log_prefix} {pair}: limit order 취소 실패 — {e}")
            del self._pending_limit_orders[pair]
            return False

        # 타임아웃 체크
        if elapsed > limit_timeout_sec:
            logger.info(
                f"{self._log_prefix} {pair}: limit order 타임아웃 ({elapsed:.0f}초) — 취소"
            )
            try:
                await self._adapter.cancel_order(pending.order_id, pending.pair)
            except Exception as e:
                logger.warning(f"{self._log_prefix} {pair}: limit order 타임아웃 취소 실패 — {e}")
            del self._pending_limit_orders[pair]
            # 타임아웃 후 시그널이 여전히 entry_ok면 다음 사이클에서 market fallback 시도
            return False

        # 대기 중
        logger.debug(
            f"{self._log_prefix} {pair}: limit order 대기 중 "
            f"order_id={pending.order_id} elapsed={elapsed:.0f}s"
        )
        return True

    async def _open_position_limit(
        self, pair: str, price: float, atr: Optional[float], params: Dict,
        *, signal_data: dict | None = None, is_preview: bool = False,
    ) -> Optional[Any]:
        """Limit order 진입 시도. None 반환 → market fallback.

        기본: 미지원 (None). 서브클래스(TrendFollowingManager 등)에서 override.
        """
        return None

    async def _finalize_limit_entry(self, pair: str, order: Any, pending: Any) -> None:
        """Limit order 체결 후 포지션 등록. 서브클래스에서 override.

        기본: 로그만 출력. 서브클래스(TrendFollowingManager)에서 실제 포지션 등록.
        """
        logger.info(f"{self._log_prefix} {pair}: limit 체결 — 서브클래스 미구현, 기본 처리 스킵")

    # ──────────────────────────────────────────
    # 청산 + 학습 루프
    # ──────────────────────────────────────────

    async def _close_position(self, pair: str, reason: str) -> None:
        """청산 wrapper. paper pair면 paper exit 처리 후 return. 그 외 _close_position_impl 위임."""
        paper_info = self._paper_positions.pop(pair, None)
        paper_exec = self._paper_executors.get(pair)
        if paper_exec is not None and paper_info is not None:
            exit_price = self._latest_price.get(pair, paper_info["entry_price"])
            try:
                await paper_exec.record_paper_exit(
                    paper_trade_id=paper_info["paper_trade_id"],
                    exit_price=exit_price,
                    exit_reason=reason,
                    entry_price=paper_info["entry_price"],
                    invest_jpy=100_000.0,  # 가상 투입금: PnL% 계산용
                    direction=paper_info["direction"],
                )
            except Exception as e:
                logger.error(f"{self._log_prefix} {pair}: Paper exit 기록 실패 — {e}")
            self._position[pair] = None
            logger.info(
                f"{self._log_prefix} {pair}: Paper 청산 기록 reason={reason} exit_price={exit_price}"
            )
            return

        # 학습 루프: impl 호출 전에 Position 정보 보존 (impl 내에서 position=None 처리됨)
        pos_before = self._position.get(pair)
        judgment_id: int | None = pos_before.extra.get("judgment_id") if pos_before else None
        entry_price_before = pos_before.entry_price if pos_before else None
        entry_amount_before = pos_before.entry_amount if pos_before else 0.0
        entry_time_str = pos_before.extra.get("entry_time") if pos_before else None
        confidence_before: float = pos_before.extra.get("confidence", 0.5) if pos_before else 0.5
        side_before = pos_before.extra.get("side", "long") if pos_before else "long"

        await self._close_position_impl(pair, reason)

        # 학습 루프: outcome backfill
        if judgment_id is not None and entry_price_before is not None:
            exit_price = self._latest_price.get(pair, entry_price_before)
            if side_before == "short":
                pnl = (entry_price_before - exit_price) * entry_amount_before
                pnl_pct = (entry_price_before - exit_price) / entry_price_before * 100 if entry_price_before > 0 else 0.0
            else:
                pnl = (exit_price - entry_price_before) * entry_amount_before
                pnl_pct = (exit_price - entry_price_before) / entry_price_before * 100 if entry_price_before > 0 else 0.0
            try:
                from datetime import datetime as _dt
                entry_time = _dt.fromisoformat(entry_time_str) if entry_time_str else datetime.now(timezone.utc)
            except (ValueError, TypeError):
                entry_time = datetime.now(timezone.utc)
            logger.info(
                f"{self._log_prefix} {pair}: 청산→학습 연결 — "
                f"judgment_id={judgment_id}, "
                f"pnl={'+'if pnl >= 0 else ''}{pnl:.0f}円 ({pnl_pct:+.2f}%), "
                f"side={side_before}"
            )
            asyncio.create_task(
                self._update_judgment_outcome(
                    pair, judgment_id, pnl, pnl_pct, entry_time, confidence_before
                )
            )

        # T1 트리거: real 청산 완료 직후 全 전략 Score 스냅샷 수집 (P-1)
        if self._snapshot_collector is not None:
            asyncio.create_task(
                self._snapshot_collector.collect_all_snapshots("T1_position_close", pair)
            )

    async def _update_judgment_outcome(
        self,
        pair: str,
        judgment_id: int,
        realized_pnl: float,
        realized_pnl_pct: float,
        entry_time: datetime,
        confidence: float,
    ) -> None:
        """ai_judgments 결과 컬럼 UPDATE (학습 루프 Stage 1).

        거래 청산 직후 asyncio.create_task()로 비동기 실행.
        실패해도 WARNING만 — 거래 흐름 블록하지 않는다.
        """
        if self._session_factory is None:
            return
        # judgment_model은 orchestrator에만 주입됨 → SessionFactory만으로 update
        try:
            from sqlalchemy import update as sa_update
            from adapters.database.models import AiJudgment  # 공유 테이블
            outcome = "win" if realized_pnl >= 0 else "loss"
            now = datetime.now(timezone.utc)
            hold_hours = (now - entry_time).total_seconds() / 3600
            # 확신도 오차: |confidence - (1.0 if win else 0.0)|
            confidence_error = abs(confidence - (1.0 if outcome == "win" else 0.0))
            async with self._session_factory() as session:
                await session.execute(
                    sa_update(AiJudgment)
                    .where(AiJudgment.id == judgment_id)
                    .values(
                        outcome=outcome,
                        realized_pnl=round(realized_pnl, 2),
                        hold_duration_hours=round(hold_hours, 4),
                        confidence_error=round(confidence_error, 4),
                        updated_at=now,
                    )
                )
                await session.commit()
            logger.info(
                f"{self._log_prefix} {pair}: ai_judgments[{judgment_id}] outcome={outcome} "
                f"pnl={realized_pnl:.2f} ({realized_pnl_pct:.2f}%) hold={hold_hours:.1f}h"
            )
        except Exception as e:
            logger.warning(f"{self._log_prefix} {pair}: ai_judgments outcome 업데이트 실패 — {e}")

        # 사후 분석 (ENABLE_POST_ANALYSIS=true + PostAnalyzer 주입 시)
        if self._post_analyzer is not None:
            try:
                await self._post_analyzer.analyze(
                    judgment_id=judgment_id,
                    outcome=outcome,
                    realized_pnl=realized_pnl,
                    hold_duration_hours=hold_hours,
                )
            except Exception as e:
                logger.warning(f"{self._log_prefix} {pair}: 사후 분석 실패 (무시) — {e}")

    # ──────────────────────────────────────────
    # 서브클래스 hook (기본 구현 제공)
    # ──────────────────────────────────────────

    async def _on_adjust_risk_hook(
        self, pair: str, adjustments: dict, params: dict
    ) -> None:
        """adjust_risk 실행 후 서브클래스별 추가 처리 hook.

        기본 구현은 no-op. 서브클래스에서 override:
          - CfdTrendFollowingManager: GMO FX IFD-OCO 주문 변경
        """
