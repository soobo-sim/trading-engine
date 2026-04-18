"""Backward-compat shim — canonical: core.judge.scoring"""
from core.judge.scoring import *  # noqa: F401,F403
from core.judge.scoring import (  # noqa: F401
    StrategyScore,
    calculate_total_score,
    calculate_box_readiness,
    calculate_box_edge,
    calculate_trend_readiness,
    calculate_trend_edge,
    calculate_regime_fit,
    calculate_box_score,
    calculate_trend_score,
)
