"""
Decision Layer — AI 에이전트 응답 DTO + SignalSnapshot 직렬화.

앨리스 → 사만다 → 레이첼 3단 판단 체인에서 각 에이전트의 입/출력 계약.
LLM structured output의 JSON → 이 DTO로 파싱하여 타입 안전하게 전달한다.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from core.data.dto import SignalSnapshot


# ──────────────────────────────────────────────────────────────
# Agent Response DTO
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AliceProposal:
    """앨리스(정직한 낙관주의자) 제안서.

    LLM structured output → ALICE_RESPONSE_SCHEMA → 이 DTO로 파싱.

    action:
      "entry_long"  — 롱 진입 제안
      "entry_short" — 숏 진입 제안
      "hold"        — 진입 않음 (적극적 판단)
    """
    action: str                        # "entry_long" | "entry_short" | "hold"
    confidence: float                  # 0.0~1.0
    stop_loss: Optional[float]
    take_profit: Optional[float]
    situation_summary: str             # 현재 상황 1~2문장 요약
    reasoning: tuple[str, ...]         # 판단 근거 (팩터별 1줄)
    risk_factors: tuple[str, ...]      # 리스크 요인
    pessimistic_scenario: str          # 비관 시나리오 (자기검증)


@dataclass(frozen=True)
class SamanthaAudit:
    """사만다(구조적 반론자) 감사 보고서.

    verdict:
      "agree"       — 앨리스 제안에 동의
      "conditional" — 조건부 동의 (크기 축소, SL 확대 등)
      "oppose"      — 반대 (구체적 근거 포함)
    """
    verdict: str                       # "agree" | "conditional" | "oppose"
    confidence_adjustment: float       # 사만다가 제안하는 확신도 (앨리스 값 대체)
    max_size_pct: Optional[float]      # 크기 제한. None → 앨리스 유지
    worst_case_jpy: float              # 최악 시나리오 손실 JPY
    reasoning: str                     # 감사 결과 요약
    missed_risks: tuple[str, ...]      # 앨리스가 놓친 리스크


@dataclass(frozen=True)
class RachelVerdict:
    """레이첼(논증 품질 심판) 최종 판정.

    final_action:
      "execute"          — 앨리스 제안 실행
      "hold"             — 보류
      "modified_execute" — 사만다 조건 수용 후 실행
    """
    final_action: str                  # "execute" | "hold" | "modified_execute"
    final_confidence: float
    final_size_pct: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    alice_grade: str                   # "data" | "pattern" | "inference"
    samantha_grade: str                # "data" | "pattern" | "inference"
    adopted_side: str                  # "alice" | "samantha" | "compromise"
    reasoning: str                     # 판정 요약
    failure_probability: str           # "이 판단이 틀릴 가능성" 1줄


# ──────────────────────────────────────────────────────────────
# SignalSnapshot → 자연어 직렬화
# ──────────────────────────────────────────────────────────────

def serialize_snapshot(snapshot: SignalSnapshot) -> str:
    """SignalSnapshot → LLM에 전달할 마크다운 포맷.

    각 섹션(기술지표 / 매크로 / 뉴스 / 센티먼트 / 경제이벤트 / 포지션 / 교훈)을
    마크다운으로 직렬화한다. 데이터 없는 섹션은 "데이터 없음"으로 명시.
    """
    lines: list[str] = []

    # ── 1. 기술 지표 ──────────────────────────────────────────
    lines.append("## 기술 지표")
    lines.append(f"- 페어: {snapshot.pair} ({snapshot.exchange})")
    lines.append(f"- 현재가: {snapshot.current_price:,.2f}")
    lines.append(f"- 시그널: {snapshot.signal}")
    if snapshot.ema is not None:
        lines.append(f"- EMA20: {snapshot.ema:,.4f}")
    if snapshot.ema_slope_pct is not None:
        lines.append(f"- EMA 기울기: {snapshot.ema_slope_pct:+.4f}%/봉")
    if snapshot.rsi is not None:
        lines.append(f"- RSI: {snapshot.rsi:.1f}")
    if snapshot.atr is not None:
        lines.append(f"- ATR: {snapshot.atr:,.4f}")
    if snapshot.regime is not None:
        lines.append(f"- 체제(Regime): {snapshot.regime}")
    if snapshot.stop_loss_price is not None:
        lines.append(f"- ATR 기반 SL: {snapshot.stop_loss_price:,.4f}")
    exit_sig = snapshot.exit_signal or {}
    exit_action = exit_sig.get("action", "hold")
    exit_reason = exit_sig.get("reason", "")
    if exit_action != "hold":
        lines.append(f"- Exit 시그널: {exit_action} ({exit_reason})")

    # ── 2. 매크로 ────────────────────────────────────────────
    lines.append("\n## 매크로 데이터")
    if snapshot.macro is not None:
        m = snapshot.macro
        if m.us_10y is not None:
            lines.append(f"- 미국 10년물 금리: {m.us_10y:.3f}%")
        if m.us_2y is not None:
            lines.append(f"- 미국 2년물 금리: {m.us_2y:.3f}%")
        if m.vix is not None:
            lines.append(f"- VIX: {m.vix:.2f}")
        if m.dxy is not None:
            lines.append(f"- DXY: {m.dxy:.3f}")
        if m.fetched_at is not None:
            lines.append(f"- 조회 시각: {m.fetched_at.strftime('%Y-%m-%d %H:%M UTC')}")
    else:
        lines.append("- 데이터 없음")

    # ── 3. 뉴스 ─────────────────────────────────────────────
    lines.append("\n## 최근 뉴스")
    if snapshot.news:
        for n in snapshot.news[:5]:  # 최대 5건
            sentiment_str = ""
            if n.sentiment_score is not None:
                sentiment_str = f" [감성: {n.sentiment_score:+.2f}]"
            pub = n.published_at.strftime("%m/%d %H:%M")
            lines.append(f"- [{pub}] {n.title} ({n.source}){sentiment_str}")
    else:
        lines.append("- 뉴스 없음")

    # ── 4. 센티먼트 ──────────────────────────────────────────
    lines.append("\n## 시장 센티먼트")
    if snapshot.sentiment is not None:
        s = snapshot.sentiment
        lines.append(f"- 지수: {s.score}/100 ({s.classification})")
        lines.append(f"- 출처: {s.source}")
    else:
        lines.append("- 데이터 없음")

    # ── 5. 경제 이벤트 ───────────────────────────────────────
    lines.append("\n## 향후 24시간 경제 이벤트")
    if snapshot.upcoming_events:
        for ev in snapshot.upcoming_events:
            dt_str = ev.datetime_jst.strftime("%m/%d %H:%M JST")
            fc_str = f" 예상={ev.forecast}" if ev.forecast else ""
            pv_str = f" 이전={ev.previous}" if ev.previous else ""
            lines.append(f"- [{ev.importance}] {dt_str} {ev.name} ({ev.currency}){fc_str}{pv_str}")
    else:
        lines.append("- 예정 이벤트 없음")

    # ── 6. 포지션 ────────────────────────────────────────────
    lines.append("\n## 현재 포지션")
    if snapshot.position is not None:
        p = snapshot.position
        pnl_str = ""
        if p.entry_price is not None:
            pnl_pct = (snapshot.current_price - p.entry_price) / p.entry_price * 100
            pnl_str = f" (미실현 P&L: {pnl_pct:+.2f}%)"
        lines.append(f"- 진입가: {p.entry_price:,.4f}{pnl_str}")
        lines.append(f"- 수량: {p.entry_amount}")
        if p.stop_loss_price is not None:
            lines.append(f"- SL: {p.stop_loss_price:,.4f}")
        lines.append(f"- 스탑 타이트닝 적용: {p.stop_tightened}")
    else:
        lines.append("- 포지션 없음")

    # ── 7. 과거 교훈 ─────────────────────────────────────────
    lines.append("\n## 과거 교훈 (유사 상황)")
    if snapshot.relevant_lessons:
        for lesson in snapshot.relevant_lessons[:3]:  # 최대 3건
            outcome_str = "✓ 수익" if lesson.outcome == "win" else "✗ 손실"
            tags = ", ".join(lesson.situation_tags) if lesson.situation_tags else ""
            lines.append(f"- [{outcome_str}] {lesson.lesson_text}")
            if tags:
                lines.append(f"  (태그: {tags})")
    else:
        lines.append("- 관련 교훈 없음")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# JSON Schema (OpenAI structured output)
# ──────────────────────────────────────────────────────────────

ALICE_RESPONSE_SCHEMA: dict = {
    "name": "alice_proposal",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["entry_long", "entry_short", "hold"],
                "description": "제안 액션"
            },
            "confidence": {
                "type": "number",
                "description": "확신도 0.0~1.0"
            },
            "stop_loss": {
                "type": ["number", "null"],
                "description": "손절가. hold 시 null"
            },
            "take_profit": {
                "type": ["number", "null"],
                "description": "목표가. null 허용"
            },
            "situation_summary": {
                "type": "string",
                "description": "현재 상황 1~2문장 요약"
            },
            "reasoning": {
                "type": "array",
                "items": {"type": "string"},
                "description": "판단 근거 (팩터별 1줄)"
            },
            "risk_factors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "리스크 요인"
            },
            "pessimistic_scenario": {
                "type": "string",
                "description": "비관 시나리오 (자기검증)"
            }
        },
        "required": [
            "action", "confidence", "stop_loss", "take_profit",
            "situation_summary", "reasoning", "risk_factors", "pessimistic_scenario"
        ],
        "additionalProperties": False
    }
}

SAMANTHA_RESPONSE_SCHEMA: dict = {
    "name": "samantha_audit",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["agree", "conditional", "oppose"],
                "description": "감사 결론"
            },
            "confidence_adjustment": {
                "type": "number",
                "description": "사만다 제안 확신도 (앨리스 값 대체)"
            },
            "max_size_pct": {
                "type": ["number", "null"],
                "description": "포지션 크기 상한. null → 앨리스 유지"
            },
            "worst_case_jpy": {
                "type": "number",
                "description": "최악 시나리오 손실 JPY 금액 (양수)"
            },
            "reasoning": {
                "type": "string",
                "description": "감사 결과 요약"
            },
            "missed_risks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "앨리스가 놓친 리스크"
            }
        },
        "required": [
            "verdict", "confidence_adjustment", "max_size_pct",
            "worst_case_jpy", "reasoning", "missed_risks"
        ],
        "additionalProperties": False
    }
}

RACHEL_RESPONSE_SCHEMA: dict = {
    "name": "rachel_verdict",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "final_action": {
                "type": "string",
                "enum": ["execute", "hold", "modified_execute"],
                "description": "최종 판정"
            },
            "final_confidence": {
                "type": "number",
                "description": "최종 확신도 0.0~1.0"
            },
            "final_size_pct": {
                "type": "number",
                "description": "최종 포지션 크기 0.0~0.8"
            },
            "stop_loss": {
                "type": ["number", "null"]
            },
            "take_profit": {
                "type": ["number", "null"]
            },
            "alice_grade": {
                "type": "string",
                "enum": ["data", "pattern", "inference"],
                "description": "앨리스 논증 등급"
            },
            "samantha_grade": {
                "type": "string",
                "enum": ["data", "pattern", "inference"],
                "description": "사만다 논증 등급"
            },
            "adopted_side": {
                "type": "string",
                "enum": ["alice", "samantha", "compromise"],
                "description": "채택한 쪽"
            },
            "reasoning": {
                "type": "string",
                "description": "판정 요약"
            },
            "failure_probability": {
                "type": "string",
                "description": "이 판단이 틀릴 가능성 1줄"
            }
        },
        "required": [
            "final_action", "final_confidence", "final_size_pct",
            "stop_loss", "take_profit",
            "alice_grade", "samantha_grade", "adopted_side",
            "reasoning", "failure_probability"
        ],
        "additionalProperties": False
    }
}
