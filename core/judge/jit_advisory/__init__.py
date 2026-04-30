"""
JIT Advisory 패키지.

엔진이 진입 결정 직전 openclaw Rachel agent에게 단발 동기 자문을 구하는 레이어.
설계서: trader-common/docs/proposals/active/JIT_ADVISORY_ARCHITECTURE.md
"""
from .gate import JITAdvisoryGate
from .models import JITAdvisoryRequest, JITAdvisoryResponse
from .client import JITAdvisoryClient

__all__ = [
    "JITAdvisoryGate",
    "JITAdvisoryRequest",
    "JITAdvisoryResponse",
    "JITAdvisoryClient",
]
