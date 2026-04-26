"""
CandleLoopMixin — 캔들 모니터 루프 + 스탑로스 모니터. 접합점.

이 파일은 JudgeMixin과 ExecutionMixin의 메서드를 호출만 한다.
자체 로직은 최소화하고, 두 도메인 사이의 호출 순서만 조율한다.

수정 필요 시: 아키가 져지/퍼니셔 양쪽에 Contract 분리하여 handoff.
단독 수정 금지.

포함 메서드:
  - _candle_monitor()         60초 루프. JUDGE 사이클 → PUNISHER 사이클
  - _stop_loss_monitor()      WS 실시간 스탑로스 감시 (전적으로 PUNISHER 소유)
  - _on_candle_extra_checks() 캔들 사이클 진입 전 서브클래스 훅 (기본 True)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Dict

from core.strategy.signals import detect_bearish_divergences
from core.shared.signals import compute_exit_signal

logger = logging.getLogger("core.strategy.base_trend")

_CANDLE_POLL_INTERVAL = 60   # 초
_SYNC_INTERVAL_CYCLES = 30   # 잔고/포지션 정합성 검사 주기 (사이클 단위, 30사이클=30분)


class CandleLoopMixin:
    """캔들 모니터 루프 + 스탑로스 모니터. 접합점.

    JudgeMixin + ExecutionMixin의 메서드를 self.*로 호출한다.
    직접 도메인 로직을 포함하지 않는다.
    """

    # ──────────────────────────────────────────
    # Task 2: 스탑로스 모니터 (PUNISHER 소유)
    # ──────────────────────────────────────────

    async def _stop_loss_monitor(self, pair: str) -> None:
        """WS 실시간 체결가 → 스탑로스 이탈 시 청산."""
        price_queue: asyncio.Queue[float] = asyncio.Queue()

        async def _on_trade(price: float, amount: float) -> None:
            await price_queue.put(price)

        # subscribe_trades는 내부 while-True 루프를 가진 블로킹 코루틴이므로
        # 백그라운드 태스크로 실행해야 큐 소비 루프가 즉시 시작된다.
        ws_task = asyncio.create_task(
            self._adapter.subscribe_trades(pair, _on_trade)
        )

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

                self._latest_price[pair] = price

                if not self._is_stop_triggered(pos, price, stop_loss_price):
                    continue

                cooldown_until = self._close_fail_until.get(pair, 0)
                if time.time() < cooldown_until:
                    continue

                logger.info(
                    f"{self._log_prefix} {pair}: 하드 스탑로스 발동 "
                    f"현재가 ¥{price} / 스탑 ¥{stop_loss_price}"
                )
                await self._close_position(pair, "stop_loss")

                if self._position.get(pair) is None:
                    self._close_fail_count[pair] = 0
                    self._close_fail_until[pair] = 0
                else:
                    fail_count = self._close_fail_count.get(pair, 0) + 1
                    self._close_fail_count[pair] = fail_count
                    if fail_count % 5 == 0:
                        self._close_fail_until[pair] = time.time() + 60
                        logger.warning(
                            f"{self._log_prefix} {pair}: 청산 {fail_count}회 실패 — 60초 쿨다운"
                        )
        except asyncio.CancelledError:
            ws_task.cancel()
            raise

    # ──────────────────────────────────────────
    # Task 1: 캔들 모니터 (접합점 — JUDGE→PUNISHER 사이클)
    # ──────────────────────────────────────────

    async def _candle_monitor(self, pair: str) -> None:
        """60초마다 시그널 재계산 → 진입/청산/트레일링."""
        while True:
            await asyncio.sleep(_CANDLE_POLL_INTERVAL)

            params = self._params.get(pair, {})
            basis_tf = params.get("basis_timeframe", "4h")
            pos = self._position.get(pair)
            entry_price = pos.entry_price if pos else None

            # 30사이클(30분)마다 정합성 검사
            if pos is not None:
                cnt = self._sync_counter.get(pair, 0) + 1
                self._sync_counter[pair] = cnt
                # Paper pair는 실잔고 조회 스킵 (entry_amount=0 → ZeroDivisionError 방지)
                if cnt % _SYNC_INTERVAL_CYCLES == 0 and pair not in self._paper_executors:
                    await self._sync_position_state(pair)

            # ══════════════════════════════════════
            # JUDGE 사이클: 시그널 계산 (extra_checks 전 — BUG-041)
            # ══════════════════════════════════════
            try:
                signal_data = await self._compute_signal(
                    pair, basis_tf,
                    entry_price=entry_price,
                    params=params,
                    side=pos.extra.get("side") if pos else None,
                )
            except Exception as e:
                logger.warning(f"{self._log_prefix} {pair}: 시그널 계산 실패 — {e}")
                continue

            if signal_data is None:
                continue

            latest_candle_key = signal_data.get("latest_candle_open_time")

            # ── RegimeGate 갱신 (extra_checks 전, 포지션 상태 무관 — BUG-041) ──
            if latest_candle_key != self._ema_slope_last_key.get(pair):
                if self._regime_gate is not None:
                    _prev_key = self._regime_gate.last_candle_key
                    self._regime_gate.update_regime(
                        regime=signal_data.get("regime", "unclear"),
                        bb_width_pct=signal_data.get("bb_width_pct", 0.0),
                        range_pct=signal_data.get("range_pct", 0.0),
                        candle_key=latest_candle_key,
                    )
                    if self._regime_gate.last_candle_key != _prev_key:
                        from core.execution.regime_gate_persistence import save_regime_gate_state
                        await save_regime_gate_state(self._session_factory, self._regime_gate)

            # 서브클래스 추가 체크 (keep_rate, 보유시간 등)
            should_continue = await self._on_candle_extra_checks(pair, params)
            if not should_continue:
                continue
            # 추가 체크로 포지션이 청산됐을 수 있음
            pos = self._position.get(pair)
            entry_price = pos.entry_price if pos else None

            # exit_signal realtime price 보정 (profit_target 정확도)
            if pos is not None:
                realtime = self._latest_price.get(pair)
                if realtime is not None and realtime != signal_data.get("current_price"):
                    signal_data["exit_signal"] = compute_exit_signal(
                        ema_slope_pct=signal_data.get("ema_slope_pct"),
                        rsi=signal_data.get("rsi"),
                        atr=signal_data.get("atr"),
                        current_price=realtime,
                        entry_price=entry_price,
                        params=params,
                        side=pos.extra.get("side", "buy"),
                    )
                    signal_data["current_price"] = realtime

            signal = signal_data["signal"]
            current_price = signal_data["current_price"]
            atr = signal_data.get("atr")
            ema = signal_data.get("ema")
            ema_slope_pct = signal_data.get("ema_slope_pct")
            rsi = signal_data.get("rsi")
            self._last_rsi[pair] = rsi
            exit_signal = signal_data.get("exit_signal", {})
            exit_action = exit_signal.get("action", "hold")
            latest_candle_key = signal_data.get("latest_candle_open_time")

            # ── JUDGE 사이클: 시그널 후처리 ──
            signal = self._on_signal_computed(pair, signal, signal_data, pos)

            # ── 포지션 보유 중 entry signal 무시 ──
            if pos is not None and signal in ("entry_ok", "entry_sell", "wait_dip", "wait_regime"):
                signal = "hold"

            # 실시간 가격으로 exit_warning 보정
            realtime_price = self._latest_price.get(pair)
            if realtime_price is not None and ema is not None:
                signal = self._check_exit_warning(pair, signal, realtime_price, ema, pos, atr=atr)

            # 시그널 로그: hold=DEBUG, 시그널 변경=INFO, 동일 반복=DEBUG
            signal_changed = signal != self._last_signal.get(pair, "")
            if signal_changed:
                self._last_signal[pair] = signal
            _sig_level = not signal_changed
            _sig_log = logger.debug if _sig_level else logger.info
            if pos:
                _side = {"buy": "롱", "sell": "숏"}.get(pos.extra.get("side", "buy"), pos.extra.get("side", "buy"))
                _pos_label = f" {_side} 보유중"
            else:
                _pos_label = ""
            _slope_str = f"{ema_slope_pct:.4f}" if ema_slope_pct is not None else "N/A"
            _rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
            _ema_str = f"{ema:.0f}" if ema is not None else "N/A"
            _sig_log(
                f"{self._log_prefix} {pair}: {self._describe_signal(signal, pos)} "
                f"signal={signal} ema_slope_pct={_slope_str} rsi={_rsi_str} ema={_ema_str} "
                f"price={current_price:.0f}{_pos_label}"
            )

            # ══════════════════════════════════════
            # PUNISHER 사이클: Pending Limit Order
            # ══════════════════════════════════════
            pending = self._pending_limit_orders.get(pair)
            if pending is not None:
                pl_continue = await self._check_pending_limit_order(pair, pending, signal, params)
                if pl_continue:
                    continue

            # ── EMA 기울기 이력 + 다이버전스 (RegimeGate는 extra_checks 전으로 이동) ──
            if latest_candle_key != self._ema_slope_last_key.get(pair):
                # 새 4H 캔들 완성: candle change time 기록 (개선 A cooling period)
                if not hasattr(self, "_last_candle_change_time"):
                    self._last_candle_change_time = {}
                from datetime import datetime, timezone as _tz
                self._last_candle_change_time[pair] = datetime.now(_tz.utc)

                # 새 4H 캔들 완성: 프리뷰 진입 검증
                cur_pos = self._position.get(pair)
                if cur_pos is not None and cur_pos.extra.get("preview_entry"):
                    self._ema_slope_last_key[pair] = latest_candle_key
                    if signal == "entry_ok":
                        cur_pos.extra.pop("preview_entry")
                        logger.info(
                            f"{self._log_prefix} {pair}: 프리뷰 진입 확인 — signal=entry_ok → 정상 포지션 전환"
                        )
                    else:
                        logger.warning(
                            f"{self._log_prefix} {pair}: 프리뷰 오판 보호 — signal={signal} → 즉시 청산"
                        )
                        await self._close_position(pair, "preview_invalidated")
                        continue

                slope_history = self._ema_slope_history.setdefault(pair, [])
                slope_history.append(ema_slope_pct)
                if len(slope_history) > 3:
                    slope_history.pop(0)
                self._ema_slope_last_key[pair] = latest_candle_key

                # ── PUNISHER: EMA 기울기 연속 하락 → 스탑 타이트닝 ──
                if (
                    len(slope_history) == 3
                    and all(s is not None for s in slope_history)
                    and slope_history[0] > slope_history[1] > slope_history[2]
                    and pos is not None
                    and not pos.stop_tightened
                    and atr is not None
                ):
                    # 개선 B: 진입 후 grace period 체크
                    grace_sec = float(params.get("entry_grace_period_sec", 900))
                    opened_at = pos.extra.get("opened_at")
                    if opened_at is not None:
                        from datetime import datetime, timezone as _tz2
                        elapsed_since_entry = (datetime.now(_tz2.utc) - opened_at).total_seconds()
                        if elapsed_since_entry < grace_sec:
                            logger.debug(
                                f"{self._log_prefix} {pair}: 진입 후 grace period 중 "
                                f"({elapsed_since_entry:.0f}s/{grace_sec:.0f}s) — 기울기 하락 tighten_stop 억제"
                            )
                        else:
                            logger.info(
                                f"{self._log_prefix} {pair}: EMA 기울기 3캔들 연속 하락 → 스탑 타이트닝"
                            )
                            await self._apply_stop_tightening(pair, current_price, atr, params)
                    else:
                        logger.info(
                            f"{self._log_prefix} {pair}: EMA 기울기 3캔들 연속 하락 → 스탑 타이트닝"
                        )
                        await self._apply_stop_tightening(pair, current_price, atr, params)

                # ── JUDGE: 다이버전스 감지 → 스탑 타이트닝 ──
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
                            # 개선 B: 진입 후 grace period 체크
                            grace_sec = float(params.get("entry_grace_period_sec", 900))
                            opened_at = pos.extra.get("opened_at")
                            if opened_at is not None:
                                from datetime import datetime, timezone as _tz3
                                elapsed_since_entry = (datetime.now(_tz3.utc) - opened_at).total_seconds()
                                if elapsed_since_entry < grace_sec:
                                    logger.debug(
                                        f"{self._log_prefix} {pair}: 진입 후 grace period 중 "
                                        f"({elapsed_since_entry:.0f}s/{grace_sec:.0f}s) — 다이버전스 tighten_stop 억제"
                                    )
                                else:
                                    logger.info(
                                        f"{self._log_prefix} {pair}: 다이버전스 감지 → 스탑 타이트닝"
                                    )
                                    await self._apply_stop_tightening(pair, current_price, atr, params)
                            else:
                                logger.info(
                                    f"{self._log_prefix} {pair}: 다이버전스 감지 → 스탑 타이트닝"
                                )
                                await self._apply_stop_tightening(pair, current_price, atr, params)

            # ══════════════════════════════════════
            # JUDGE 사이클: 오케스트레이터 판단
            # ══════════════════════════════════════
            if self._orchestrator is None:
                logger.error(
                    f"{self._log_prefix} {pair}: _orchestrator 미설정 "
                    "— set_orchestrator() 필요. 이번 사이클 스킵."
                )
                continue
            snapshot = await self._build_signal_snapshot(pair, signal_data, params, pos)
            result = await self._orchestrator.process(snapshot)

            # ══════════════════════════════════════
            # PUNISHER 사이클: 실행
            # ══════════════════════════════════════
            should_continue = await self._handle_execution_result(
                pair, result, snapshot, signal_data, params
            )
            if should_continue:
                continue

            # ── JUDGE 발의 → PUNISHER 실행: 프리뷰 진입 시도 ──
            if (
                self._position.get(pair) is None
                and pair not in self._pending_limit_orders
                and signal not in ("entry_ok", "entry_sell")
                and params.get("preview_entry_enabled", False)
            ):
                await self._try_preview_entry(pair, basis_tf, params)

    # ──────────────────────────────────────────
    # 서브클래스 훅 (기본 구현 제공)
    # ──────────────────────────────────────────

    async def _on_candle_extra_checks(self, pair: str, params: Dict) -> bool:
        """캔들 시그널 계산 전 추가 체크. False 반환 시 이번 사이클 스킵.

        기본: 아무것도 하지 않음 (True). CFD에서 keep_rate/보유시간 체크.
        """
        return True
