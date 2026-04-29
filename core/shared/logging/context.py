"""Judge 사이클 컨텍스트 — 사이클 ID를 asyncio 태스크 경계로 전파.

_candle_monitor() 진입 시 cycle_id를 설정하면
동일 태스크 내의 _compute_signal() → rule_based.decide()까지 자동 전파된다.

사용법:
    # 사이클 진입 (CandleLoopMixin._candle_monitor)
    from core.shared.logging.context import set_judge_cycle_id
    set_judge_cycle_id()           # 현재 JST 시각 기반 ID 자동 생성

    # 로그 출력 (rule_based, _judge_mixin 등)
    from core.shared.logging.context import get_judge_cycle_id
    cycle_id = get_judge_cycle_id()   # "" 이면 사이클 외부
"""
from __future__ import annotations

import contextvars

# 현재 Judge 사이클 ID.  "" = 사이클 외부
_judge_cycle_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "judge_cycle_id", default=""
)

# 4자리 단조 증가 카운터 (0001~9999 순환)
_cycle_counter: int = 0


def set_judge_cycle_id() -> str:
    """4자리 카운터 기반 cycle_id 생성 + 설정 후 반환.

    형식: 0001~9999 (9999 도달 후 0001로 순환).
    로그의 ts 필드에 시각이 이미 포함되므로 cycle_id는 사이클 식별만 담당한다.
    """
    global _cycle_counter
    _cycle_counter = (_cycle_counter % 9999) + 1
    cycle_id = f"{_cycle_counter:04d}"
    _judge_cycle_id.set(cycle_id)
    return cycle_id


def get_judge_cycle_id() -> str:
    """현재 asyncio 태스크의 cycle_id 반환.  사이클 외부이면 ""."""
    return _judge_cycle_id.get()
