"""
Hypothesis 생애주기 검증 헬퍼 — P4 Self-Evolution Loop.

ALLOWED_TRANSITIONS 매트릭스 + 단계별 필수 조건 검증.
"""
from __future__ import annotations

from typing import Any

# ── 상태 전이 매트릭스 ────────────────────────────────────────

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "proposed":    {"backtested", "rejected"},
    "backtested":  {"paper", "rejected"},
    "paper":       {"canary", "rejected"},
    "canary":      {"adopted", "rolled_back", "rejected"},
    "adopted":     set(),          # terminal
    "rejected":    set(),          # terminal
    "rolled_back": {"archived"},
    "archived":    set(),          # terminal
}

TERMINAL_STATES = frozenset(s for s, nexts in ALLOWED_TRANSITIONS.items() if not nexts)


def validate_transition(current: str, new: str) -> None:
    """전이 허용 여부를 확인. 불허 시 ValueError."""
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if new not in allowed:
        raise ValueError(
            f"Transition {current!r} → {new!r} not allowed. "
            f"Allowed: {sorted(allowed) or '(terminal)'}"
        )


def check_promotion_to_paper(h: Any) -> None:
    """backtested → paper 승격 조건 (standard 트랙)."""
    bt: dict = h.backtest_result or {}
    if bt.get("trades", 0) < 30:
        raise ValueError(f"backtest trades={bt.get('trades', 0)} < 30 — paper 승격 불가")
    sharpe = bt.get("sharpe", 0)
    baseline = (h.baseline_metrics or {}).get("sharpe", 0)
    if sharpe < baseline:
        raise ValueError(f"backtest sharpe={sharpe} < baseline={baseline}")


def check_promotion_to_canary(h: Any) -> None:
    """paper → canary 승격 조건 (standard 트랙)."""
    pr: dict = h.paper_result or {}
    if pr.get("trades", 0) < 5:
        raise ValueError(f"paper trades={pr.get('trades', 0)} < 5")
    wr = pr.get("win_rate", 0)
    baseline_wr = (h.baseline_metrics or {}).get("win_rate", 0)
    if wr < baseline_wr - 0.05:
        raise ValueError(
            f"paper win_rate={wr} < baseline-5pt={baseline_wr - 0.05:.3f}"
        )


def check_promotion_to_adopted(h: Any) -> None:
    """canary → adopted 조건."""
    cr: dict = h.canary_result or {}
    if cr.get("rollback_triggered"):
        raise ValueError("canary rollback_triggered=True — adopted 불가")
    if cr.get("trades", 0) < 3:
        raise ValueError(f"canary trades={cr.get('trades', 0)} < 3")
    sharpe = cr.get("sharpe") or 0
    baseline = (h.baseline_metrics or {}).get("sharpe", 0)
    if sharpe < baseline * 0.95:
        raise ValueError(f"canary sharpe={sharpe} < 95% baseline={baseline * 0.95:.3f}")
