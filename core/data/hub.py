"""Backward-compat shim — canonical: core/shared/data/hub.py"""
from core.shared.data.hub import *  # noqa: F401,F403
from core.shared.data.hub import IDataHub, DataHub  # noqa: F401 — Protocol은 * import에서 누락 가능
