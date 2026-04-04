"""
SnapshotCollector — P-1 전략 스냅샷 수집기.

T1 (포지션 청산 직후) / T2 (매 4H봉 + 무포지션) 트리거로
全 전략(active+proposed)의 Score를 계산하고 strategy_snapshots DB에 저장.

특징:
- 매니저 의존성 없음 — DB + 어댑터 + scoring.py + signals.py만 사용
- fail-safe — 개별 전략 실패 시 skip, 전체 중단 안 함
- T2 중복 방지 — 58분 최소 간격

참조: solution-design/DYNAMIC_STRATEGY_SWITCHING.md §P-1
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Type

from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from adapters.database.models import PaperTrade
from core.exchange.base import ExchangeAdapter
from core.strategy.scoring import (
    StrategyScore,
    calculate_box_score,
    calculate_trend_score,
)
from core.strategy.signals import compute_trend_signal

logger = logging.getLogger(__name__)

_T2_MIN_INTERVAL_SEC = 3500  # 58분 — T2 중복 방지 (4H봉 사이 2회 발동 방지)
_CANDLE_LIMIT = 60            # Score 계산용 최대 캔들 수


class SnapshotCollector:
    """全 전략(active+proposed) Score 계산 + strategy_snapshots DB 저장.

    의존성 주입:
        session_factory : AsyncSession 팩토리
        adapter         : ExchangeAdapter (get_ticker용)
        strategy_model  : gmo_strategies / bf_strategies ORM 클래스
        candle_model    : ORM 캔들 모델 (timeframe, is_complete, open_time 컬럼 필수)
        box_model       : ORM 박스 모델 (status, strategy_id, upper_bound, lower_bound)
        snapshot_model  : create_strategy_snapshot_model() 반환 ORM 클래스
        pair_column     : 캔들/박스 모델의 pair 컬럼명 ("pair" or "product_code")
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        adapter: ExchangeAdapter,
        strategy_model: Type,
        candle_model: Type,
        box_model: Type,
        snapshot_model: Type,
        pair_column: str = "pair",
        switch_recommender: Optional[Any] = None,
    ) -> None:
        self._session_factory = session_factory
        self._adapter = adapter
        self._strategy_model = strategy_model
        self._candle_model = candle_model
        self._box_model = box_model
        self._snapshot_model = snapshot_model
        self._pair_column = pair_column
        self._switch_recommender = switch_recommender
        self._last_t2_time: Optional[datetime] = None

    # ──────────────────────────────────────────
    # 공개 API
    # ──────────────────────────────────────────

    async def collect_all_snapshots(
        self, trigger_type: str, trigger_pair: str = ""
    ) -> List[StrategyScore]:
        """全 전략 Score 계산 + strategy_snapshots DB 저장.

        Args:
            trigger_type : "T1_position_close" | "T2_candle_close"
            trigger_pair : 트리거 발생 pair (로그용)

        Returns:
            저장된 StrategyScore 리스트 (실패 전략 제외)
        """
        # ── T2 중복 방지 ────────────────────────────
        if trigger_type == "T2_candle_close":
            now = datetime.now(timezone.utc)
            if self._last_t2_time is not None:
                elapsed = (now - self._last_t2_time).total_seconds()
                if elapsed < _T2_MIN_INTERVAL_SEC:
                    logger.debug(
                        f"[Snapshot] T2 중복 방지 — 마지막 실행 {elapsed:.0f}초 전 "
                        f"(최소 {_T2_MIN_INTERVAL_SEC}초)"
                    )
                    return []
            self._last_t2_time = now

        strategies = await self._fetch_active_proposed()
        if not strategies:
            logger.debug("[Snapshot] active/proposed 전략 없음, skip")
            return []

        snapshot_time = datetime.now(timezone.utc)
        strategy_score_pairs: List[Tuple[Any, StrategyScore]] = []

        for strategy in strategies:
            try:
                score = await self._compute_score(strategy, snapshot_time, trigger_type)
                if score is not None:
                    await self._save_snapshot(strategy, score, snapshot_time, trigger_type)
                    strategy_score_pairs.append((strategy, score))
            except Exception as e:
                sid = getattr(strategy, "id", "?")
                logger.warning(
                    f"[Snapshot] strategy_id={sid} Score 계산/저장 실패 — {e}"
                )

        results = [sc for _, sc in strategy_score_pairs]

        logger.info(
            f"[Snapshot] {trigger_type} pair={trigger_pair!r}: "
            f"{len(results)}/{len(strategies)} 스냅샷 완료"
        )

        # 스위칭 추천 평가 (fail-safe)
        if self._switch_recommender is not None and strategy_score_pairs:
            try:
                await self._switch_recommender.evaluate(trigger_type, strategy_score_pairs)
            except Exception as e:
                logger.warning(f"[Snapshot] SwitchRecommender 평가 실패: {e}")

        return results

    # ──────────────────────────────────────────
    # 내부: 전략 조회
    # ──────────────────────────────────────────

    async def _fetch_active_proposed(self) -> List[Any]:
        """DB에서 active+proposed 전략 전체 조회."""
        async with self._session_factory() as db:
            result = await db.execute(
                select(self._strategy_model).where(
                    or_(
                        self._strategy_model.status == "active",
                        self._strategy_model.status == "proposed",
                    )
                )
            )
            return list(result.scalars().all())

    # ──────────────────────────────────────────
    # 내부: Score 계산
    # ──────────────────────────────────────────

    async def _compute_score(
        self,
        strategy: Any,
        snapshot_time: datetime,
        trigger_type: str,
    ) -> Optional[StrategyScore]:
        """단일 전략 Score 계산."""
        params: Dict = strategy.parameters or {}
        pair = params.get("pair") or params.get("product_code") or ""
        trading_style = params.get("trading_style", "")

        if not pair or not trading_style:
            logger.debug(
                f"[Snapshot] strategy_id={strategy.id}: pair/style 없음 — skip"
            )
            return None

        # 캔들 조회
        timeframe = params.get("basis_timeframe", "4h")
        candles = await self._fetch_candles(pair, timeframe)
        if len(candles) < 20:
            logger.debug(
                f"[Snapshot] {pair} 캔들 부족 ({len(candles)}개 < 20) — skip"
            )
            return None

        # regime + 공통 시그널 데이터
        signal_data = compute_trend_signal(candles, params=params)
        if signal_data is None:
            return None

        regime: str = signal_data.get("regime", "unclear")

        # paper_trades 통계
        paper_count, win_rate = await self._get_paper_stats(strategy.id)

        # 전략 스타일별 Score
        if trading_style == "box_mean_reversion":
            return await self._score_box(
                pair, params, regime, signal_data, paper_count, win_rate
            )
        if trading_style in ("trend_following", "cfd_trend_following"):
            return self._score_trend(
                params, signal_data, regime, paper_count, win_rate, trading_style
            )

        logger.debug(
            f"[Snapshot] strategy_id={strategy.id}: 미지원 스타일={trading_style!r} — skip"
        )
        return None

    async def _score_box(
        self,
        pair: str,
        params: Dict,
        regime: str,
        signal_data: Dict,
        paper_count: int,
        win_rate: float,
    ) -> StrategyScore:
        """박스역추세 Score 계산. 박스 없으면 readiness=0."""
        current_price = float(signal_data.get("current_price") or 0.0)
        commission = float(params.get("trading_fee_rate", 0.001))
        near_bound_pct = float(params.get("near_bound_pct", 0.3))

        box = await self._fetch_active_box(pair)

        if box is None:
            return calculate_box_score(
                current_price=current_price,
                upper=0.0,
                lower=0.0,
                near_bound_pct=near_bound_pct,
                box_width_pct=0.0,
                regime=regime,
                commission_rate=commission,
                win_rate=win_rate if win_rate > 0 else 0.5,
                paper_trades=paper_count,
                extra_detail={
                    "box_exists": False,
                    "rsi": signal_data.get("rsi"),
                    "bb_width_pct": signal_data.get("bb_width_pct"),
                },
            )

        upper = float(box.upper_bound)
        lower = float(box.lower_bound)
        width_pct = (upper - lower) / lower * 100 if lower > 0 else 0.0

        return calculate_box_score(
            current_price=current_price,
            upper=upper,
            lower=lower,
            near_bound_pct=near_bound_pct,
            box_width_pct=width_pct,
            regime=regime,
            commission_rate=commission,
            win_rate=win_rate if win_rate > 0 else 0.5,
            paper_trades=paper_count,
            extra_detail={
                "box_id": box.id,
                "box_exists": True,
                "box_upper": upper,
                "box_lower": lower,
                "box_width_pct": round(width_pct, 4),
                "rsi": signal_data.get("rsi"),
                "bb_width_pct": signal_data.get("bb_width_pct"),
            },
        )

    def _score_trend(
        self,
        params: Dict,
        signal_data: Dict,
        regime: str,
        paper_count: int,
        win_rate: float,
        trading_style: str,
    ) -> StrategyScore:
        """추세추종 Score 계산."""
        signal = signal_data.get("signal", "no_signal")
        rsi = signal_data.get("rsi")
        atr = signal_data.get("atr")
        current_price = float(signal_data.get("current_price") or 0.0)

        atr_pct = atr / current_price * 100 if (atr and current_price > 0) else 0.0
        trailing_mult = float(params.get("trailing_stop_atr_initial", 2.0))
        entry_rsi_min = float(params.get("entry_rsi_min", 40.0))
        entry_rsi_max = float(params.get("entry_rsi_max", 65.0))

        return calculate_trend_score(
            signal=signal,
            rsi=rsi,
            entry_rsi_min=entry_rsi_min,
            entry_rsi_max=entry_rsi_max,
            atr_pct=atr_pct,
            trailing_multiplier=trailing_mult,
            regime=regime,
            win_rate=win_rate if win_rate > 0 else 0.34,
            paper_trades=paper_count,
            extra_detail={
                "signal": signal,
                "ema_slope_pct": signal_data.get("ema_slope_pct"),
                "atr": atr,
                "bb_width_pct": signal_data.get("bb_width_pct"),
                "trading_style": trading_style,
            },
        )

    # ──────────────────────────────────────────
    # 내부: DB 조회 헬퍼
    # ──────────────────────────────────────────

    async def _fetch_candles(self, pair: str, timeframe: str) -> List[Any]:
        """완성된 캔들 최근 N개 조회 (시간 오름차순)."""
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
                .order_by(desc(CandleModel.open_time))
                .limit(_CANDLE_LIMIT)
            )
            candles = list(result.scalars().all())
        candles.reverse()
        return candles

    async def _fetch_active_box(self, pair: str) -> Optional[Any]:
        """active 박스 조회. strategy_id IS NULL = active 전략 박스."""
        BoxModel = self._box_model
        pair_col = getattr(BoxModel, self._pair_column)
        async with self._session_factory() as db:
            result = await db.execute(
                select(BoxModel)
                .where(
                    and_(
                        pair_col == pair,
                        BoxModel.status == "active",
                        BoxModel.strategy_id.is_(None),
                    )
                )
                .order_by(desc(BoxModel.created_at))
                .limit(1)
            )
            return result.scalars().first()

    async def _get_paper_stats(self, strategy_id: int) -> Tuple[int, float]:
        """paper_trades에서 전략별 거래 수 + 승률. 실패 시 (0, 0.5) 반환."""
        try:
            wins_expr = func.sum(
                case((PaperTrade.paper_pnl_pct > 0, 1), else_=0)
            )
            async with self._session_factory() as db:
                result = await db.execute(
                    select(
                        func.count(PaperTrade.id).label("total"),
                        wins_expr.label("wins"),
                    ).where(
                        and_(
                            PaperTrade.strategy_id == strategy_id,
                            PaperTrade.exit_time.is_not(None),
                        )
                    )
                )
                row = result.first()
            total = int(row.total or 0)
            wins = int(row.wins or 0)
            win_rate = wins / total if total > 0 else 0.0
            return total, win_rate
        except Exception as e:
            logger.debug(
                f"[Snapshot] strategy_id={strategy_id} paper_stats 조회 실패 — {e}"
            )
            return 0, 0.0

    async def _has_open_paper_position(self, strategy_id: int) -> bool:
        """paper_trades에서 미청산 포지션 보유 여부."""
        try:
            async with self._session_factory() as db:
                result = await db.execute(
                    select(func.count(PaperTrade.id)).where(
                        and_(
                            PaperTrade.strategy_id == strategy_id,
                            PaperTrade.exit_time.is_(None),
                        )
                    )
                )
                count = result.scalar() or 0
            return count > 0
        except Exception:
            return False

    # ──────────────────────────────────────────
    # 내부: 스냅샷 저장
    # ──────────────────────────────────────────

    async def _save_snapshot(
        self,
        strategy: Any,
        score: StrategyScore,
        snapshot_time: datetime,
        trigger_type: str,
    ) -> None:
        """strategy_snapshots 테이블에 행 저장."""
        params: Dict = strategy.parameters or {}
        pair = params.get("pair") or params.get("product_code") or ""
        trading_style = params.get("trading_style", "")
        has_position = await self._has_open_paper_position(strategy.id)

        SnapshotModel = self._snapshot_model
        row = SnapshotModel()
        row.strategy_id = strategy.id
        row.pair = pair
        row.trading_style = trading_style
        row.trigger_type = trigger_type
        row.snapshot_time = snapshot_time
        row.score = score.score
        row.readiness = score.readiness
        row.edge = score.edge
        row.regime_fit = score.regime_fit
        row.regime = score.regime
        row.confidence = score.confidence
        row.has_position = has_position
        row.current_price = score.detail.get("current_price") or float(
            score.detail.get("box_upper", 0) or 0
        )
        row.detail = score.detail

        async with self._session_factory() as db:
            db.add(row)
            await db.commit()
