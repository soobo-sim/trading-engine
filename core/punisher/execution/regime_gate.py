"""
RegimeGate — 4H 체제 기반 전략 진입 게이트.

4H 캔들 경계에서 update_regime()을 호출하면 regime 이력을 관리하고,
3캔들 연속 동일 regime 시 active_strategy를 전환한다.

로그 프리픽스: [Punisher-Layer][RegimeGate]
"""
from __future__ import annotations

import itertools
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[Punisher-Layer][RegimeGate]"
_DEFAULT_STREAK_REQUIRED = 3  # 전환에 필요한 연속 캔들 수
_DEFAULT_RESTORE_REQUIRED = 2  # unclear 후 복원에 필요한 연속 캔들 수 (< streak_required)

# regime → 허용 전략 매핑
_REGIME_TO_STRATEGY: dict[str, str] = {
    "trending": "trend_following",
    "ranging": "box_mean_reversion",
}

# 전략 이름 → 표시 레이블
_STRATEGY_LABEL: dict[str, str] = {
    "trend_following": "추세추종",
    "box_mean_reversion": "박스역추세",
}


class RegimeGate:
    """4H 체제 기반 전략 진입 게이트.

    상태는 인메모리. 재시작 시 최초 3캔들 warm-up 필요.
    warm-up 중에는 active_strategy=None → 양쪽 모두 진입 차단.
    """

    def __init__(
        self,
        pair: str,
        streak_required: int = _DEFAULT_STREAK_REQUIRED,
        restore_required: int = _DEFAULT_RESTORE_REQUIRED,
    ) -> None:
        self._pair = pair
        self._active_strategy: str | None = None
        self._streak_required = streak_required
        self._restore_required = min(restore_required, streak_required)  # streak 이하로 제한
        self._regime_history: list[str] = []  # 최대 streak_required개 유지
        self._last_switch_at: datetime | None = None
        self._switch_count: int = 0
        self._consecutive_count: int = 0  # 동일 regime 연속 횟수 (전체 이력)
        self._consecutive_regime: str | None = None  # 연속 카운트 중인 regime
        self._last_candle_key: str | None = None  # 마지막 갱신 캔들 키 (중복 호출 방지)

    # ── 진입 허용 판정 ──────────────────────────────────────────

    def should_allow_entry(self, manager_type: str) -> bool:
        """현재 active_strategy와 manager_type이 일치하면 True.

        active_strategy가 None이면 False (warm-up 또는 unclear).

        Args:
            manager_type: "trend_following" | "box_mean_reversion"
        Returns:
            진입 허용 여부
        """
        return self._active_strategy == manager_type

    # ── 4H 캔들 경계에서 호출 ──────────────────────────────────

    def update_regime(
        self,
        regime: str,
        bb_width_pct: float = 0.0,
        range_pct: float = 0.0,
        *,
        candle_key: str | None = None,
    ) -> str | None:
        """regime 이력에 append → streak 확인 → 전환 판정.

        동작 순서:
        0. candle_key가 _last_candle_key와 동일하면 → None 반환 (중복 호출 스킵)
           (두 매니저가 같은 gate를 공유하므로 같은 캔들을 2번 처리하지 않도록)
        1. regime_history에 regime append (최대 streak_required개 유지)
        2. 최근 streak_required개가 전부 동일한지 확인
        3-a. 전부 동일 + _REGIME_TO_STRATEGY에 있음 + 현재 active와 다름 → 전환
        3-b. 전부 동일 + 현재 active와 같음 → 전환 없음 (INFO 로그)
        3-c. streak 미달 → 전환 없음 (INFO 로그)
        3-d. unclear 포함 → active_strategy에 영향 없음 (INFO 로그, 진입 차단)

        Args:
            regime: "trending" | "ranging" | "unclear"
            bb_width_pct: 볼린저밴드 폭 % (로그용)
            range_pct: 가격 범위 % (로그용)
            candle_key: 4H 캔들 open_time 키. 동일 키로 중복 호출 시 스킵.
                        None이면 멱등성 체크 스킵 (하위 호환).
        Returns:
            전환된 전략 이름 또는 None
        """
        # 0. 동일 캔들 중복 호출 방지
        if candle_key is not None and candle_key == self._last_candle_key:
            return None

        # 1. history append + 최대 크기 유지
        self._last_candle_key = candle_key
        self._regime_history.append(regime)
        if len(self._regime_history) > self._streak_required:
            self._regime_history.pop(0)

        # 동일 regime 연속 횟수 갱신
        if regime == self._consecutive_regime:
            self._consecutive_count += 1
        else:
            self._consecutive_regime = regime
            self._consecutive_count = 1

        history_str = str(self._regime_history)
        streak_len = len(self._regime_history)

        # warm-up 미완료
        if streak_len < self._streak_required:
            logger.info(
                f"{_LOG_PREFIX} {self._pair}: 4H 체제 판정 갱신\n"
                f"  현재 regime={regime} (BB폭 {bb_width_pct:.1f}%, 가격범위 {range_pct:.1f}%) — {regime} 연속 {self._consecutive_count}회\n"
                f"  이력: {history_str} → warm-up 중 ({streak_len}/{self._streak_required}캔들)\n"
                f"  진입 차단 유지"
            )
            return None

        # 2. streak 확인
        unique = set(self._regime_history)

        # 3-d. unclear 포함 — active_strategy 변경하지 않지만 진입은 unclear 기간 동안 차단 가능
        if "unclear" in unique:
            # unclear가 현재 캔들이면 active를 None으로 (양쪽 차단)
            if self._regime_history[-1] == "unclear":
                prev_active = self._active_strategy
                self._active_strategy = None
                logger.info(
                    f"{_LOG_PREFIX} {self._pair}: 4H 체제 판정 갱신\n"
                    f"  현재 regime=unclear (BB폭 {bb_width_pct:.1f}%, 가격범위 {range_pct:.1f}%) — unclear 연속 {self._consecutive_count}회\n"
                    f"  이력: {history_str} → unclear 끼임, streak 리셋\n"
                    f"  활성전략: {prev_active} → None (양쪽 진입 차단)"
                )
                return None

            # 현재 캔들이 U가 아님 — 꼬리 연속 카운트로 조기 복원 판정
            tail_regime = self._regime_history[-1]
            tail_streak = sum(
                1 for _ in
                itertools.takewhile(
                    lambda r: r == tail_regime,
                    reversed(self._regime_history),
                )
            )
            target_strategy = _REGIME_TO_STRATEGY.get(tail_regime)

            if target_strategy and tail_streak >= self._restore_required:
                # 복원 조건 충족 (restore_required 연속)
                prev_strategy = self._active_strategy
                self._active_strategy = target_strategy
                self._last_switch_at = datetime.now(timezone.utc)
                self._switch_count += 1
                trend_allow = "✅ 진입허용" if target_strategy == "trend_following" else "🚫 진입차단"
                box_allow = "✅ 진입허용" if target_strategy == "box_mean_reversion" else "🚫 진입차단"
                prev_label = _STRATEGY_LABEL.get(prev_strategy, str(prev_strategy)) if prev_strategy else "없음"
                new_label = _STRATEGY_LABEL.get(target_strategy, target_strategy)
                logger.warning(
                    f"{_LOG_PREFIX} {self._pair}: 4H 체제 판정 갱신\n"
                    f"  현재 regime={tail_regime} (BB폭 {bb_width_pct:.1f}%, 가격범위 {range_pct:.1f}%) — {tail_regime} 연속 {self._consecutive_count}회\n"
                    f"  이력: {history_str} → unclear 포함이나 꼬리 {tail_streak}연속 (restore_required={self._restore_required})\n"
                    f"  ⭐⭐⭐⭐ 전략 조기 복원: {prev_label} → {new_label}\n"
                    f"  TrendManager {trend_allow}, BoxManager {box_allow}"
                )
                return target_strategy

            # 아직 복원 기준 미달 — 차단 유지
            logger.info(
                f"{_LOG_PREFIX} {self._pair}: 4H 체제 판정 갱신\n"
                f"  현재 regime={regime} (BB폭 {bb_width_pct:.1f}%, 가격범위 {range_pct:.1f}%) — {regime} 연속 {self._consecutive_count}회\n"
                f"  이력: {history_str} → unclear 포함, 꼬리 {tail_streak}/{self._restore_required}연속 (복원 기준 미달)\n"
                f"  현재 활성: {self._active_strategy} 유지 (차단 계속)"
            )
            return None

        # 3-c. streak 미달 (모두 동일하지 않음)
        if len(unique) > 1:
            streak_count = sum(1 for r in reversed(self._regime_history) if r == regime)
            logger.info(
                f"{_LOG_PREFIX} {self._pair}: 4H 체제 판정 갱신\n"
                f"  현재 regime={regime} (BB폭 {bb_width_pct:.1f}%, 가격범위 {range_pct:.1f}%) — {regime} 연속 {self._consecutive_count}회\n"
                f"  이력: {history_str} → streak {streak_count}/{self._streak_required}, 전환 조건 미충족\n"
                f"  현재 활성: {self._active_strategy} 유지"
            )
            return None

        # 여기까지 오면: len(unique) == 1, "unclear" 없음 → 동일 regime 3캔들 연속
        confirmed_regime = self._regime_history[0]
        target_strategy = _REGIME_TO_STRATEGY.get(confirmed_regime)

        if target_strategy is None:
            # 매핑에 없는 regime (예상 외)
            logger.warning(
                f"{_LOG_PREFIX} {self._pair}: 알 수 없는 regime={confirmed_regime} → 전환 스킵"
            )
            return None

        # 3-b. 이미 동일 전략
        if self._active_strategy == target_strategy:
            logger.info(
                f"{_LOG_PREFIX} {self._pair}: 4H 체제 판정 갱신\n"
                f"  현재 regime={confirmed_regime} (BB폭 {bb_width_pct:.1f}%, 가격범위 {range_pct:.1f}%) — {confirmed_regime} 연속 {self._consecutive_count}회\n"
                f"  이력: {history_str} → 3캔들 연속 {confirmed_regime}\n"
                f"  현재 활성: {self._active_strategy} 유지 (이미 최적 전략)"
            )
            return None

        # 3-a. 전환 발생
        prev_strategy = self._active_strategy
        self._active_strategy = target_strategy
        self._last_switch_at = datetime.now(timezone.utc)
        self._switch_count += 1

        trend_allow = "✅ 진입허용" if target_strategy == "trend_following" else "🚫 진입차단"
        box_allow = "✅ 진입허용" if target_strategy == "box_mean_reversion" else "🚫 진입차단"
        prev_label = _STRATEGY_LABEL.get(prev_strategy, str(prev_strategy)) if prev_strategy else "없음"
        new_label = _STRATEGY_LABEL.get(target_strategy, target_strategy)

        logger.warning(
            f"{_LOG_PREFIX} {self._pair}: 4H 체제 판정 갱신\n"
            f"  현재 regime={confirmed_regime} (BB폭 {bb_width_pct:.1f}%, 가격범위 {range_pct:.1f}%) — {confirmed_regime} 연속 {self._consecutive_count}회\n"
            f"  이력: {history_str} → 3캔들 연속 {confirmed_regime}\n"
            f"  ⭐⭐⭐⭐ 전략 전환: {prev_label} → {new_label}\n"
            f"  TrendManager {trend_allow}, BoxManager {box_allow}"
        )

        return target_strategy

    # ── 프로퍼티 ────────────────────────────────────────────────

    @property
    def active_strategy(self) -> str | None:
        """현재 활성 전략. None이면 양쪽 모두 진입 차단."""
        return self._active_strategy

    @property
    def regime_history(self) -> list[str]:
        """최근 regime 이력 (읽기 전용 복사본)."""
        return list(self._regime_history)

    @property
    def switch_count(self) -> int:
        """총 전환 횟수."""
        return self._switch_count

    @property
    def last_switch_at(self) -> datetime | None:
        """마지막 전환 시각 (UTC)."""
        return self._last_switch_at

    @property
    def consecutive_count(self) -> int:
        """현재 regime 연속 횟수."""
        return self._consecutive_count

    @property
    def last_candle_key(self) -> str | None:
        """마지막 갱신된 캔들 키 (읽기 전용)."""
        return self._last_candle_key

    # ── 직렬화 / 복원 ────────────────────────────────────────────

    def to_dict(self) -> dict:
        """영속화용 상태 직렬화.

        Returns:
            pair, active_strategy, regime_history, last_switch_at,
            switch_count, consecutive_count, consecutive_regime,
            last_candle_key 를 담은 dict.
        """
        return {
            "pair": self._pair,
            "active_strategy": self._active_strategy,
            "regime_history": list(self._regime_history),
            "last_switch_at": self._last_switch_at,
            "switch_count": self._switch_count,
            "consecutive_count": self._consecutive_count,
            "consecutive_regime": self._consecutive_regime,
            "last_candle_key": self._last_candle_key,
            "streak_required": self._streak_required,
            "restore_required": self._restore_required,
        }

    def restore(self, state: dict) -> None:
        """DB에서 읽어온 state dict로 내부 상태를 복원한다.

        빈 dict이면 즉시 return (초기 상태 유지).
        pair는 생성자에서 고정되므로 복원 대상에서 제외.

        Args:
            state: to_dict() 동일 구조의 dict. 빈 dict이면 스킵.
        """
        if not state:
            return

        self._streak_required = int(state.get("streak_required", _DEFAULT_STREAK_REQUIRED))
        self._restore_required = min(
            int(state.get("restore_required", _DEFAULT_RESTORE_REQUIRED)),
            self._streak_required,
        )
        self._active_strategy = state.get("active_strategy")
        history = state.get("regime_history", [])
        self._regime_history = list(history)[-self._streak_required:]
        self._last_switch_at = state.get("last_switch_at")
        self._switch_count = int(state.get("switch_count") or 0)
        self._consecutive_count = int(state.get("consecutive_count") or 0)
        self._consecutive_regime = state.get("consecutive_regime")
        self._last_candle_key = state.get("last_candle_key")

        active_label = (
            _STRATEGY_LABEL.get(self._active_strategy, self._active_strategy)
            if self._active_strategy
            else "없음"
        )
        logger.info(
            f"{_LOG_PREFIX} {self._pair}: DB에서 상태 복원\n"
            f"  활성전략: {active_label} | 이력: {self._regime_history}\n"
            f"  candle_key: {self._last_candle_key}"
        )
