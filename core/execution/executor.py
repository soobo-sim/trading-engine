"""Backward-compat shim — canonical: core/punisher/execution/executor.py"""
from core.punisher.execution.executor import *  # noqa: F401,F403
from core.punisher.execution.executor import (  # noqa: F401
    IExecutor,
    RealExecutor,
    PaperExecutor,
    create_executor,
    _calc_pnl_pct,
)
