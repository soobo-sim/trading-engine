"""
JIT Advisory 감사 로그 헬퍼.

모든 JIT 자문 호출(성공/실패)을 jit_advisories 테이블에 기록한다.
실패해도 거래를 막지 않는다 — 로깅 실패는 경고만.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from adapters.database.jit_advisory_model import JITAdvisory
from .models import JITAdvisoryRequest, JITAdvisoryResponse

logger = logging.getLogger(__name__)


async def save_jit_audit(
    session: AsyncSession,
    request: Optional[JITAdvisoryRequest],
    response: Optional[JITAdvisoryResponse],
    final_action: str,
    final_size_pct: Optional[float],
    error: Optional[str],
    log_prefix: str = "[Judge-Layer][????][Advisory]",
) -> None:
    """JIT 자문 결과를 DB에 기록. 실패해도 예외 전파 안 함."""
    try:
        _req_id = request.request_id if request else "N/A"
        _pair = request.pair if request else "?"
        row = JITAdvisory(
            request_id=_req_id,
            pair=_pair,
            exchange=request.exchange if request else "?",
            trading_style=request.trading_style if request else "?",
            proposed_action=request.proposed_action if request else "?",
            rule_signal=request.rule_signal if request else "?",
            rule_confidence=request.rule_confidence if request else 0.0,
            rule_size_pct=request.rule_size_pct if request else 0.0,
            rule_reasoning=request.rule_reasoning if request else "",

            jit_decision=response.decision if response else None,
            jit_confidence=response.confidence if response else None,
            jit_reasoning=response.reasoning if response else None,
            jit_size_pct=response.adjusted_size_pct if response else None,
            jit_model=response.model if response else None,
            jit_latency_ms=response.latency_ms if response else None,
            jit_error=error,

            final_action=final_action,
            final_size_pct=final_size_pct,
        )
        session.add(row)
        await session.commit()

        # ── DB 저장 완료 INFO 로그 ─────────────────────────────
        _jit_dec = response.decision if response else "FAIL"
        _latency = f"{response.latency_ms}ms" if response and response.latency_ms else "-"
        _size_str = f"{final_size_pct:.0%}" if final_size_pct is not None else "-"
        logger.info(
            f"{log_prefix} {_pair} ✔ DB 저장 완료"
            f" — req={_req_id} jit={_jit_dec} final={final_action}"
            f" size={_size_str} latency={_latency}"
        )
    except Exception as e:
        logger.warning(f"{log_prefix} 감사 로그 저장 실패 (무시): {e}")
        try:
            await session.rollback()
        except Exception:
            pass
