"""Backward-compat shim — canonical: core/shared/data/dto.py"""
from core.shared.data.dto import *  # noqa: F401,F403
from core.shared.data.dto import modify_decision  # noqa: F401 — 함수는 * import에 안 잡힐 수 있음
