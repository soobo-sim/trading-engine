"""
체제(Regime) 시뮬레이션 — 실전 코드를 그대로 호출하여 파라미터 변경 효과를 사전 검증한다.

핵심 원칙:
  - compute_trend_signal() 반환값에서 bb_width_pct, range_pct, regime 추출 (별도 계산 금지)
  - RegimeGate.update_regime() 직접 호출 (streak 로직 별도 구현 금지)
  - compute_candle_limit() 으로 윈도우 크기 계산 (하드코딩 금지)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

from core.punisher.execution.regime_gate import RegimeGate
from core.shared.signals import compute_candle_limit, compute_trend_signal


@dataclass
class RegimeSnapshot:
    candle_time: str          # open_time ISO format
    bb_width_pct: float
    range_pct: float
    regime: str               # "trending" | "ranging" | "unclear"
    active_strategy: Optional[str]
    consecutive_count: int
    switched: bool            # 이 캔들에서 전략 전환 발생 여부


@dataclass
class RegimeSimResult:
    snapshots: List[RegimeSnapshot]
    total_candles: int
    regime_counts: dict
    switches: List[dict]      # [{"time": ..., "from": ..., "to": ...}]
    blocked_candles: int      # active_strategy=None인 캔들 수
    params_used: dict
    streak_required: int

    def to_dict(self) -> dict:
        return {
            "total_candles": self.total_candles,
            "regime_counts": self.regime_counts,
            "switches": self.switches,
            "blocked_candles": self.blocked_candles,
            "params_used": self.params_used,
            "streak_required": self.streak_required,
            "snapshots": [
                {
                    "candle_time": s.candle_time,
                    "bb_width_pct": s.bb_width_pct,
                    "range_pct": s.range_pct,
                    "regime": s.regime,
                    "active_strategy": s.active_strategy,
                    "consecutive_count": s.consecutive_count,
                    "switched": s.switched,
                }
                for s in self.snapshots
            ],
        }


def simulate_regime(
    candles: List[Any],
    params: dict,
    streak_required: int = 3,
) -> RegimeSimResult:
    """
    4H 캔들을 시간순으로 리플레이하며 체제 판정 + RegimeGate 전환을 시뮬레이션.

    Args:
        candles: 시간순 정렬된 캔들 객체 리스트 (.open_time, .close, .high, .low 필요)
        params: 전략 파라미터
        streak_required: RegimeGate 전환에 필요한 연속 캔들 수 (기본 3)
    """
    # 시뮬레이션 중 RegimeGate 로그 억제 (매 캔들 INFO 폭주 방지)
    rg_logger = logging.getLogger("core.punisher.execution.regime_gate")
    prev_level = rg_logger.level
    rg_logger.setLevel(logging.WARNING)

    try:
        limit = compute_candle_limit(params)           # 실전 코드 호출
        gate = RegimeGate("sim", streak_required=streak_required)  # 실전 클래스

        snapshots: List[RegimeSnapshot] = []
        switches: List[dict] = []

        for i in range(limit - 1, len(candles)):
            window = candles[i - limit + 1 : i + 1]   # 실전과 동일한 윈도우

            sig = compute_trend_signal(window, params)  # 실전 코드 호출
            if sig is None:
                continue

            bb_width_pct = sig["bb_width_pct"]
            range_pct = sig["range_pct"]
            regime = sig["regime"]

            candle_key = str(candles[i].open_time)
            prev_active = gate.active_strategy
            switched_to = gate.update_regime(           # 실전 코드 호출
                regime, bb_width_pct, range_pct, candle_key=candle_key
            )

            if switched_to is not None:
                switches.append({
                    "time": candle_key,
                    "from": prev_active,
                    "to": switched_to,
                })

            snapshots.append(RegimeSnapshot(
                candle_time=candle_key,
                bb_width_pct=round(bb_width_pct, 2),
                range_pct=round(range_pct, 2),
                regime=regime,
                active_strategy=gate.active_strategy,
                consecutive_count=gate.consecutive_count,
                switched=switched_to is not None,
            ))

        regime_counts: dict = {"trending": 0, "ranging": 0, "unclear": 0}
        blocked_candles = 0
        for s in snapshots:
            regime_counts[s.regime] = regime_counts.get(s.regime, 0) + 1
            if s.active_strategy is None:
                blocked_candles += 1

        return RegimeSimResult(
            snapshots=snapshots,
            total_candles=len(snapshots),
            regime_counts=regime_counts,
            switches=switches,
            blocked_candles=blocked_candles,
            params_used=params,
            streak_required=streak_required,
        )
    finally:
        rg_logger.setLevel(prev_level)
