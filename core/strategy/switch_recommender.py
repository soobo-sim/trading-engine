"""
SwitchRecommender — 전략 스위칭 추천 엔진.

전 전략 Score 비교 → 가장 유리한 proposed 전략이 active Score의 SCORE_MARGIN배를
초과하면 추천을 생성하고 DB에 저장한다.

안전장치:
    - 쿨다운 24시간
    - 일 최대 1회
    - 月 최대 4회
    - Score 마진 1.5배 (strict >)

SnapshotCollector가 collect_all_snapshots() 완료 후 evaluate()를 호출한다.
on_recommendation 콜백은 Step 4/4에서 Telegram 연동에 사용된다.

역할 분리:
    - SwitchRecommender: 동일 타입 내 파라미터 비교 추천 (trend_following ↔ trend_following 등)
    - RegimeGate (core/execution/regime_gate.py): cross-type 전환 담당
      (trend_following ↔ box_mean_reversion) — regime 기반 자동 전환

참조: solution-design/DYNAMIC_STRATEGY_SWITCHING.md §P-1
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional, Tuple, Type

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.strategy.scoring import StrategyScore

logger = logging.getLogger(__name__)


class SwitchRecommender:
    """전략 Score 기반 스위칭 추천 생성기.

    Args:
        session_factory     : AsyncSession 팩토리
        recommendation_model: create_switch_recommendation_model() 반환 ORM 클래스
        on_recommendation   : 추천 생성 후 호출할 비동기 콜백 (optional)
    """

    SCORE_MARGIN: float = 1.5   # proposed > active × 1.5 (strict)
    COOLDOWN_HOURS: int = 24
    DAILY_MAX: int = 1
    MONTHLY_MAX: int = 4

    def __init__(
        self,
        session_factory: async_sessionmaker,
        recommendation_model: Type,
        on_recommendation: Optional[Callable] = None,
    ) -> None:
        self._session_factory = session_factory
        self._recommendation_model = recommendation_model
        self._on_recommendation = on_recommendation

    # ──────────────────────────────────────────
    # 공개 API
    # ──────────────────────────────────────────

    async def evaluate(
        self,
        trigger_type: str,
        strategy_score_pairs: List[Tuple[Any, StrategyScore]],
    ) -> Optional[Any]:
        """전략-Score 페어 리스트를 받아 스위칭 추천 여부 판단.

        Args:
            trigger_type         : "T1_position_close" | "T2_candle_close"
            strategy_score_pairs : [(strategy_orm, StrategyScore), ...]

        Returns:
            생성된 추천 ORM 행, 또는 None (추천 없음)
        """
        if not strategy_score_pairs:
            return None

        # 1. active vs proposed 분리
        active_pairs = [
            (s, sc) for s, sc in strategy_score_pairs if s.status == "active"
        ]
        proposed_pairs = [
            (s, sc) for s, sc in strategy_score_pairs if s.status == "proposed"
        ]

        if not active_pairs or not proposed_pairs:
            logger.debug(
                f"[SwitchRec] skip — active={len(active_pairs)}, proposed={len(proposed_pairs)}"
            )
            return None

        # 2. active 中 최고 Score
        best_active, best_active_score = max(active_pairs, key=lambda x: x[1].score)
        threshold = best_active_score.score * self.SCORE_MARGIN

        # 3. threshold 초과하는 proposed 후보 (strict >)
        candidates = [(s, sc) for s, sc in proposed_pairs if sc.score > threshold]
        if not candidates:
            best_proposed_score = max(sc.score for _, sc in proposed_pairs)
            logger.debug(
                f"[SwitchRec] 추천 없음 — active={best_active_score.score:.3f} "
                f"threshold={threshold:.3f} best_proposed={best_proposed_score:.3f}"
            )
            return None

        best_proposed, best_proposed_score = max(candidates, key=lambda x: x[1].score)

        # 4. 안전장치
        guard_reason = await self._check_safety_guards()
        if guard_reason:
            logger.info(
                f"[SwitchRec] 추천 차단: {guard_reason} "
                f"(proposed={best_proposed.id}, score={best_proposed_score.score:.3f})"
            )
            return None

        # 5. 이유 생성 + DB 저장
        reason = self._generate_reason(best_proposed, best_proposed_score, best_active_score)
        rec = await self._save_recommendation(
            trigger_type=trigger_type,
            current_strategy=best_active,
            current_score=best_active_score,
            recommended_strategy=best_proposed,
            recommended_score=best_proposed_score,
            reason=reason,
        )

        # 6. 콜백 (Telegram 등) — fire-and-forget
        if self._on_recommendation is not None:
            try:
                await self._on_recommendation(rec)
            except Exception as e:
                logger.warning(f"[SwitchRec] on_recommendation 콜백 실패: {e}")

        return rec

    # ──────────────────────────────────────────
    # 내부: 안전장치
    # ──────────────────────────────────────────

    async def _check_safety_guards(self) -> Optional[str]:
        """차단 사유 반환. None이면 통과."""
        now = datetime.now(timezone.utc)
        Model = self._recommendation_model

        async with self._session_factory() as db:
            # 1. 쿨다운: 최근 24H 내 생성된 추천 존재?
            cutoff_24h = now - timedelta(hours=self.COOLDOWN_HOURS)
            r = await db.execute(
                select(func.count(Model.id)).where(Model.created_at >= cutoff_24h)
            )
            count_24h = r.scalar_one()
            if count_24h > 0:
                return f"24H 쿨다운 미경과 (최근 {count_24h}건)"

            # 2. 일 최대
            today_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
            r = await db.execute(
                select(func.count(Model.id)).where(Model.created_at >= today_utc)
            )
            if r.scalar_one() >= self.DAILY_MAX:
                return f"일 최대 {self.DAILY_MAX}회 초과"

            # 3. 月 최대
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            r = await db.execute(
                select(func.count(Model.id)).where(Model.created_at >= month_start)
            )
            if r.scalar_one() >= self.MONTHLY_MAX:
                return f"月 최대 {self.MONTHLY_MAX}회 초과"

        return None

    # ──────────────────────────────────────────
    # 내부: 이유 생성
    # ──────────────────────────────────────────

    def _generate_reason(
        self,
        strategy: Any,
        score: StrategyScore,
        active_score: StrategyScore,
    ) -> str:
        """사람이 읽을 수 있는 추천 이유 생성."""
        params = strategy.parameters or {}
        pair = params.get("pair") or params.get("product_code") or "?"
        style = params.get("trading_style", "?")
        detail = score.detail or {}

        if style == "box_mean_reversion":
            width = detail.get("box_width_pct", 0)
            return (
                f"{pair} 박스 readiness={score.readiness:.2f}, "
                f"width={width:.2f}%, regime={score.regime} | "
                f"score={score.score:.3f} > active={active_score.score:.3f}×{self.SCORE_MARGIN}"
            )
        if style in ("trend_following", "cfd_trend_following"):
            signal = detail.get("signal", "?")
            return (
                f"{pair} 추세 signal={signal}, readiness={score.readiness:.2f}, "
                f"regime={score.regime} | "
                f"score={score.score:.3f} > active={active_score.score:.3f}×{self.SCORE_MARGIN}"
            )
        return (
            f"{pair} score={score.score:.3f} > "
            f"active={active_score.score:.3f}×{self.SCORE_MARGIN}"
        )

    # ──────────────────────────────────────────
    # 내부: DB 저장
    # ──────────────────────────────────────────

    async def _save_recommendation(
        self,
        *,
        trigger_type: str,
        current_strategy: Any,
        current_score: StrategyScore,
        recommended_strategy: Any,
        recommended_score: StrategyScore,
        reason: str,
    ) -> Any:
        """추천을 DB에 저장하고 ORM 행을 반환."""
        now = datetime.now(timezone.utc)
        ratio = (
            recommended_score.score / current_score.score
            if current_score.score > 0
            else 999.0
        )

        async with self._session_factory() as db:
            row = self._recommendation_model(
                trigger_type=trigger_type,
                triggered_at=now,
                current_strategy_id=current_strategy.id,
                current_score=float(current_score.score),
                recommended_strategy_id=recommended_strategy.id,
                recommended_score=float(recommended_score.score),
                score_ratio=float(round(ratio, 4)),
                confidence=recommended_score.confidence,
                reason=reason,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            logger.info(
                f"[SwitchRec] 추천 생성 id={row.id}: "
                f"active={current_strategy.id}(score={current_score.score:.3f}) → "
                f"proposed={recommended_strategy.id}(score={recommended_score.score:.3f}) "
                f"ratio={ratio:.2f}x confidence={recommended_score.confidence}"
            )
            return row
