"""
JudgeMixin — 판단 도메인 전담 Mixin.

JUDGE 도메인 소유. 져지 에이전트만 수정한다.
퍼니셔가 이 파일을 수정하면 도메인 경계 침범.

포함 메서드:
  - _compute_signal()          DB 캔들 조회 → compute_trend_signal() 호출
  - _build_signal_snapshot()   signal_data + 현재 상태 → SignalSnapshot DTO
  - _on_signal_computed()      시그널 후처리 hook (기본: pass-through)
  - _check_exit_warning()      실시간 가격으로 exit_warning 보정
  - _describe_signal()         시그널 → 운영자 서술
  - _try_preview_entry()       미완성 캔들 프리뷰 시그널 계산 → 오케스트레이터 위임

NOTE: self._* 필드는 BaseTrendManager.__init__()에서 초기화된다.
      직접 import하지 않고 self 경유로만 접근한다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

from sqlalchemy import and_, select

from core.data.dto import PositionDTO, SignalSnapshot
from core.strategy.signals import compute_trend_signal

if TYPE_CHECKING:
    from core.exchange.types import Position

logger = logging.getLogger("core.strategy.base_trend")


class JudgeMixin:
    """시그널 계산 + DTO 빌드. JUDGE 도메인 소유."""

    # ──────────────────────────────────────────
    # 시그널 계산
    # ──────────────────────────────────────────

    async def _compute_signal(
        self,
        pair: str,
        timeframe: str,
        entry_price: Optional[float] = None,
        params: Optional[dict] = None,
        side: Optional[str] = None,
        include_incomplete: bool = False,
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
                        pair_col == pair,
                        CandleModel.timeframe == timeframe,
                        CandleModel.is_complete == True,  # noqa: E712
                    )
                )
                .order_by(CandleModel.open_time.desc())
                .limit(limit)
            )
            candles = list(reversed(result.scalars().all()))

        # include_incomplete=True: 미완성 캔들 1개를 추가로 조회해 맨 끝에 붙인다
        incomplete_candle = None
        if include_incomplete:
            async with self._session_factory() as db:
                result = await db.execute(
                    select(CandleModel)
                    .where(
                        and_(
                            pair_col == pair,
                            CandleModel.timeframe == timeframe,
                            CandleModel.is_complete == False,  # noqa: E712
                        )
                    )
                    .order_by(CandleModel.open_time.desc())
                    .limit(1)
                )
                incomplete_candle = result.scalars().first()
            if incomplete_candle is not None:
                candles = candles + [incomplete_candle]

        if len(candles) < ema_period + 1:
            logger.debug(f"{self._log_prefix} {pair}: 캔들 부족 ({len(candles)}개)")
            return None

        kwargs: dict[str, Any] = {"params": params or {}, "entry_price": entry_price}
        if side is not None:
            kwargs["side"] = side
        result = compute_trend_signal(candles, **kwargs)
        if result is not None:
            result["latest_candle_open_time"] = str(candles[-1].open_time)
            result["candles"] = candles
            if include_incomplete and incomplete_candle is not None:
                result["has_incomplete"] = True
                result["incomplete_candle"] = incomplete_candle
        return result

    # ──────────────────────────────────────────
    # 시그널 후처리 hooks
    # ──────────────────────────────────────────

    def _on_signal_computed(
        self, pair: str, signal: str, signal_data: dict, pos: Optional["Position"]
    ) -> str:
        """시그널 계산 후 후처리 hook. 기본: pass-through."""
        return signal

    def _check_exit_warning(
        self, pair: str, signal: str, realtime_price: float, ema: float, pos: "Position"
    ) -> str:
        """실시간 가격으로 exit_warning 보정. 서브클래스에서 양방향 지원 가능."""
        if realtime_price < ema and signal != "exit_warning":
            logger.info(
                f"{self._log_prefix} {pair}: 실시간 가격 ¥{realtime_price} < EMA20 ¥{ema:.4f} "
                f"→ 추세 이탈 감지 (즉각 보정)"
            )
            return "exit_warning"
        return signal

    def _describe_signal(self, signal: str, pos: Optional["Position"]) -> str:
        """시그널을 운영자가 이해할 수 있는 서술로 변환."""
        has_pos = pos is not None
        if signal == "exit_warning":
            return "추세 이탈, 청산 경고" if has_pos else "추세 약세, 진입 보류"
        if signal in ("entry_ok", "entry_buy"):
            return "추세 유지 중" if has_pos else "롱 진입 조건 충족"
        _descriptions = {
            "entry_sell":    "숏 진입 조건 충족",
            "entry_short":   "숏 진입 조건 충족",
            "entry_preview": "프리뷰 진입 검토",
            "wait_dip":      "RSI 과매수, 눌림 대기",
            "wait_regime":   "박스권, 추세 전환 대기",
            "no_signal":     "시그널 없음",
            "hold":          "대기",
        }
        return _descriptions.get(signal, signal)

    # ──────────────────────────────────────────
    # Execution Layer 연동 — DTO 빌드
    # ──────────────────────────────────────────

    async def _build_signal_snapshot(
        self,
        pair: str,
        signal_data: dict,
        params: dict,
        pos: Optional["Position"],
        is_preview: bool = False,
    ) -> SignalSnapshot:
        """signal_data dict + 현재 상태 → SignalSnapshot DTO."""
        pos_dto: Optional[PositionDTO] = None
        if pos is not None:
            pos_dto = PositionDTO(
                pair=pos.pair,
                entry_price=pos.entry_price,
                entry_amount=pos.entry_amount,
                stop_loss_price=pos.stop_loss_price,
                stop_tightened=pos.stop_tightened,
                extra=dict(pos.extra),
            )

        candles_raw = signal_data.get("candles") or []
        rsi_series_raw = signal_data.get("rsi_series") or []

        # ── v1.5: DataHub에서 매크로/이벤트/교훈 조회 ──
        macro = None
        upcoming_events = None
        relevant_lessons = None
        news = None
        sentiment = None
        if self._data_hub is not None:
            try:
                macro = await self._data_hub.get_macro_snapshot()
            except Exception as e:
                logger.debug(f"{self._log_prefix} {pair}: DataHub macro 조회 실패: {e}")
            try:
                upcoming_events = await self._data_hub.get_upcoming_events()
            except Exception as e:
                logger.debug(f"{self._log_prefix} {pair}: DataHub events 조회 실패: {e}")
            try:
                relevant_lessons = await self._data_hub.get_lessons(
                    pair, signal_data["signal"]
                )
            except Exception as e:
                logger.debug(f"{self._log_prefix} {pair}: DataHub lessons 조회 실패: {e}")
            try:
                news = await self._data_hub.get_news_summary(pair)
            except Exception as e:
                logger.debug(f"{self._log_prefix} {pair}: DataHub news 조회 실패: {e}")
            try:
                sentiment = await self._data_hub.get_sentiment()
            except Exception as e:
                logger.debug(f"{self._log_prefix} {pair}: DataHub sentiment 조회 실패: {e}")

        return SignalSnapshot(
            pair=pair,
            exchange=self._adapter.exchange_name,
            timestamp=datetime.now(timezone.utc),
            signal=signal_data["signal"],
            current_price=signal_data["current_price"],
            exit_signal=signal_data.get("exit_signal", {}),
            ema=signal_data.get("ema"),
            ema_slope_pct=signal_data.get("ema_slope_pct"),
            rsi=signal_data.get("rsi"),
            atr=signal_data.get("atr"),
            stop_loss_price=signal_data.get("stop_loss_price"),
            position=pos_dto,
            candles=tuple(candles_raw) if candles_raw else None,
            rsi_series=tuple(rsi_series_raw) if rsi_series_raw else None,
            params=params,
            macro=macro,
            upcoming_events=upcoming_events,
            relevant_lessons=relevant_lessons,
            news=news,
            sentiment=sentiment,
            is_preview=is_preview,
            strategy_type=self._get_strategy_type(),
        )

    # ──────────────────────────────────────────
    # 프리뷰 진입 (JUDGE 발의)
    # ──────────────────────────────────────────

    async def _try_preview_entry(self, pair: str, basis_tf: str, params: dict) -> None:
        """미완성 캔들 포함 프리뷰 시그널 계산 → entry_preview 시 오케스트레이터 위임.

        조건:
          - preview_entry_enabled=True (opt-in)
          - 미완성 캔들 진행률 ≥ 50% (noise 제거)
          - tick_count ≥ preview_min_tick_count (유동성 확인)
          - 직전 완성 캔들 시그널이 entry_ok/entry_sell 아님 (이미 진입됐거나 청산 중 아님)
        """
        if self._orchestrator is None:
            return

        try:
            preview_data = await self._compute_signal(
                pair, basis_tf, params=params, include_incomplete=True,
            )
        except Exception as e:
            logger.debug(f"{self._log_prefix} {pair}: 프리뷰 시그널 계산 실패 — {e}")
            return

        if not preview_data or not preview_data.get("has_incomplete"):
            return

        if preview_data.get("signal") != "entry_ok":
            return

        # 미완성 캔들 필터
        incomplete = preview_data.get("incomplete_candle")
        if incomplete is not None:
            min_tick = int(params.get("preview_min_tick_count", 3))
            tick_count = getattr(incomplete, "tick_count", None)
            if tick_count is not None and tick_count < min_tick:
                logger.debug(
                    f"{self._log_prefix} {pair}: 프리뷰 스킵 — tick_count={tick_count} < {min_tick}"
                )
                return

            # 진행률 50% 이상 체크
            open_time = getattr(incomplete, "open_time", None)
            close_time = getattr(incomplete, "close_time", None)
            if open_time is not None and close_time is not None:
                now = datetime.now(timezone.utc)
                total = (close_time - open_time).total_seconds()
                elapsed = (now - open_time).total_seconds()
                if total > 0 and elapsed / total < 0.5:
                    logger.debug(
                        f"{self._log_prefix} {pair}: 프리뷰 스킵 — "
                        f"캔들 진행률 {elapsed/total*100:.0f}% < 50%"
                    )
                    return

        # 프리뷰 시그널로 snapshot 구성: signal을 "entry_preview"로 변경
        preview_signal_data = {**preview_data, "signal": "entry_preview"}
        snapshot = await self._build_signal_snapshot(
            pair, preview_signal_data, params, None, is_preview=True
        )
        result = await self._orchestrator.process(snapshot)
        await self._handle_execution_result(pair, result, snapshot, preview_signal_data, params)
