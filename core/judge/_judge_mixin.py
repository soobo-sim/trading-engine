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
from core.shared.signals import compute_candle_limit, compute_trend_signal

if TYPE_CHECKING:
    from core.exchange.types import Position

logger = logging.getLogger("core.judge.signal")


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
    ) -> Optional[dict]:
        ema_period = int((params or {}).get("ema_period", 20))
        limit = compute_candle_limit(params)

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
        """실시간 가격으로 long_caution/short_caution 보정. side 기반 양방향 지원."""
        if pos is None:
            return signal
        side = pos.extra.get("side", "buy") if hasattr(pos, "extra") else "buy"
        if side in ("sell", "short"):
            # 숏 포지션: price > EMA 이탈
            if realtime_price > ema and signal != "short_caution":
                logger.info(
                    f"{self._log_prefix} {pair}: 실시간 가격 ¥{realtime_price} > EMA20 ¥{ema:.4f} "
                    f"→ 숏 추세 이탈 감지 (즉각 보정)"
                )
                return "short_caution"
        else:
            # 롱 포지션: price < EMA 이탈
            if realtime_price < ema and signal != "long_caution":
                logger.info(
                    f"{self._log_prefix} {pair}: 실시간 가격 ¥{realtime_price} < EMA20 ¥{ema:.4f} "
                    f"→ 롱 추세 이탈 감지 (즉각 보정)"
                )
                return "long_caution"
        return signal

    def _describe_signal(self, signal: str, pos: "Optional['Position']") -> str:
        """시그널을 운영자가 이해할 수 있는 서술로 변환."""
        has_pos = pos is not None
        if signal == "long_caution":
            return "롱 추세 이탈, 청산 경고" if has_pos else "롱 약세, 진입 보류"
        if signal == "short_caution":
            return "숏 추세 이탈, 청산 경고" if has_pos else "숏 불리, 진입 보류"
        if signal in ("long_setup", "entry_buy"):
            return "추세 유지 중" if has_pos else "롱 진입 조건 충족"
        _descriptions = {
            "short_setup":      "숏 진입 조건 충족",
            "entry_short":      "숏 진입 조건 충족",
            "long_overheated":  "롱 RSI 과열, 눌림 대기",
            "short_oversold":   "숏 RSI 과매도, 반등 위험 대기",
            "wait_regime":      "박스권, 추세 전환 대기",
            "no_signal":        "시그널 없음",
            "hold":             "대기",
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
            strategy_type=self._get_strategy_type(),
        )
