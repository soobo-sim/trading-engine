"""Backward-compat shim — canonical: core.judge.snapshot_collector"""
from core.judge.snapshot_collector import *  # noqa: F401,F403
from core.judge.snapshot_collector import (  # noqa: F401
    SnapshotCollector,
    _T2_MIN_INTERVAL_SEC,
    _CANDLE_LIMIT,
)
