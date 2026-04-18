"""Backward-compat shim — canonical: core/judge/execution/approval.py"""
from core.judge.execution.approval import *  # noqa: F401,F403
from core.judge.execution.approval import (  # noqa: F401
    IApprovalGate,
    TelegramApprovalGate,
    AutoApprovalGate,
)
