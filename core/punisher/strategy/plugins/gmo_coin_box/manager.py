"""
GmoCoinBoxManager — GMO Coin 레버리지 박스역추세 매니저.

GmoCoinTrendManager 상속. GMO Coin 어댑터/주문 시맨틱은 그대로 재사용.
시그널 계산만 box_signals + box_detector 기반으로 오버라이드.

상속 체인:
    BaseTrendManager → MarginTrendManager → GmoCoinTrendManager → GmoCoinBoxManager

핵심 차이:
    - _compute_signal: detect_box() → classify_price_in_box() 기반 시그널
    - _get_strategy_type: "box_mean_reversion" (RegimeGate 연동)
    - _task_prefix, _log_prefix: 박스 전용

시그널 매핑:
    near_lower  → "long_setup"     — 박스 하단 근처 → 롱 진입
    near_upper  → "short_setup"   — 박스 상단 근처 → 숏 진입
    outside     → "exit_warning" — 박스 이탈 → 청산유도
    middle      → "no_signal"    — 박스 중간 → 대기
    box_none    → "no_signal"    — 박스 미감지 → 대기

파라미터 (params dict):
    near_bound_pct   (float, default 0.5): 경계 밴드 %
    tolerance_pct    (float, default 0.5): 박스 클러스터 허용 오차 %
    min_touches      (int,   default 3):   최소 터치 횟수
    box_lookback     (int,   default 60):  박스 감지 캔들 수
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from core.judge.analysis.box_detector import detect_box
from core.strategy.box_signals import classify_price_in_box
from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager
from core.exchange.types import Position

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[BoxMgr]"

# ── 기본 파라미터 상수 ──────────────────────────────────────────
_DEFAULT_NEAR_BOUND_PCT = 0.5
_DEFAULT_TOLERANCE_PCT = 0.5
_DEFAULT_MIN_TOUCHES = 3
_DEFAULT_BOX_LOOKBACK = 60


class GmoCoinBoxManager(GmoCoinTrendManager):
    """GMO Coin 레버리지 박스역추세 매니저. 롱/숏 양방향."""

    _task_prefix = "gmoc_box"
    _log_prefix = "[BoxMgr]"
    # _supports_short = True — GmoCoinTrendManager에서 상속

    def _get_strategy_type(self) -> str:
        return "box_mean_reversion"

    async def _detect_existing_position(self, pair: str) -> Optional[Position]:
        """box 전략의 기존 포지션 감지 — DB 게이트 추가.

        어댑터 get_positions()는 거래소 레벨에서 전략 구분 불가
        (trend가 연 BTC_JPY 포지션도 동일하게 반환).
        DB(gmoc_box_positions)에 미청산 레코드가 있을 때만 어댑터 포지션을 인식.
        """
        try:
            async with self._session_factory() as db:
                Model = self._position_model
                pair_col = getattr(Model, self._position_pair_column)
                stmt = (
                    select(Model.id)
                    .where(pair_col == pair)
                    .where(Model.realized_pnl_jpy.is_(None))
                    .limit(1)
                )
                result = await db.execute(stmt)
                has_db_position = result.scalar_one_or_none() is not None
        except Exception as e:
            logger.warning(
                f"{_LOG_PREFIX} {pair}: DB 미청산 포지션 조회 실패 → None 반환: {e}"
            )
            return None

        if not has_db_position:
            logger.debug(
                f"{_LOG_PREFIX} {pair}: DB에 box 미청산 포지션 없음 → 어댑터 포지션 무시"
            )
            return None

        # DB에 box 포지션이 있을 때만 어댑터 조회 위임
        return await super()._detect_existing_position(pair)

    # ──────────────────────────────────────────
    # 시그널 계산 (박스역추세)
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
        """
        박스 감지 + 가격 위치 분류 기반 시그널.

        1) 부모 _compute_signal() 호출 → ATR/EMA/RSI + 캔들 목록 취득
        2) detect_box() 실행 — 최근 box_lookback개 캔들의 고/저가로 박스 감지
        3) classify_price_in_box() 로 현재가 위치 분류
        4) 시그널 매핑 후 반환 (ema_slope_pct = None → 기울기 이력 불필요)
        """
        p = params or {}

        # ① 부모 계산 — ATR/EMA/RSI + 캔들 목록
        base = await super()._compute_signal(
            pair, timeframe,
            entry_price=entry_price,
            params=p,
            side=side,
            include_incomplete=include_incomplete,
        )
        if base is None:
            return None

        current_price: float = base["current_price"]
        atr: Optional[float] = base.get("atr")
        candles = base.get("candles") or []

        # ② 박스 감지
        lookback = int(p.get("box_lookback", _DEFAULT_BOX_LOOKBACK))
        tolerance_pct = float(p.get("tolerance_pct", _DEFAULT_TOLERANCE_PCT))
        min_touches = int(p.get("min_touches", _DEFAULT_MIN_TOUCHES))

        box_candles = candles[-lookback:] if len(candles) > lookback else candles
        highs = [float(c.high) for c in box_candles]
        lows = [float(c.low) for c in box_candles]

        box_result = detect_box(
            highs=highs,
            lows=lows,
            tolerance_pct=tolerance_pct,
            min_touches=min_touches,
        )

        # ③ 시그널 결정
        if not box_result.box_detected:
            signal = "no_signal"
            box_upper: Optional[float] = None
            box_lower: Optional[float] = None
            logger.debug(
                f"{_LOG_PREFIX} {pair}: 박스 미감지 "
                f"(reason={box_result.reason}) → no_signal"
            )
        else:
            box_upper = box_result.upper_bound
            box_lower = box_result.lower_bound
            near_bound_pct = float(p.get("near_bound_pct", _DEFAULT_NEAR_BOUND_PCT))

            location = classify_price_in_box(
                price=current_price,
                upper=box_upper,
                lower=box_lower,
                near_bound_pct=near_bound_pct,
            )
            signal = _LOCATION_TO_SIGNAL[location]

            logger.debug(
                f"{_LOG_PREFIX} {pair}: "
                f"박스 ¥{box_lower:,.0f}~¥{box_upper:,.0f} "
                f"(폭 {box_result.width_pct:.2f}%) "
                f"현재가 ¥{current_price:,.0f} → {location} → {signal}"
            )

        # ④ 박스 폭 → range_pct (RegimeGate 로그용)
        range_pct = box_result.width_pct if box_result.box_detected else 0.0

        return {
            **base,
            # 박스 전략 고유 시그널로 덮어쓰기
            "signal": signal,
            # 박스 메타 (리포트용)
            "box_upper": box_upper,
            "box_lower": box_lower,
            "box_detected": box_result.box_detected,
            # RegimeGate에 박스 폭 전달 (range_pct 덮어쓰기)
            "range_pct": range_pct,
            # 박스 매니저는 EMA 기울기가 의미 없으므로 None 고정
            # → 부모 slope 이력 누적 없음 (None 넣으면 리스트에 쌓이지만 조건 판단 무시됨)
            "ema_slope_pct": None,
        }


# ── 시그널 매핑 테이블 ─────────────────────────────────────────
_LOCATION_TO_SIGNAL: dict[str, str] = {
    "near_lower": "long_setup",     # 박스 하단 근처 → 롱 진입
    "near_upper": "short_setup",   # 박스 상단 근처 → 숏 진입
    "outside":    "exit_warning", # 박스 이탈 → 청산 유도
    "middle":     "no_signal",    # 박스 중간 → 대기
}
