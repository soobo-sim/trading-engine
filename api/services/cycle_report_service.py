"""
CycleReportService — P5 evolution-cycle 보고서 생성 + 인과 검증 + 저장.

저장: ai_judgments 테이블에 kind="cycle_report" 로 INSERT.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.schemas.evolution import CycleReportInput, CycleReportResponse, CycleReportDetail
from api.schemas.evolution import (
    ParameterChangeSummary,
    TradeStatsSummary,
    MarketContextSummary,
    BacktestSummary,
)

JST = timezone(timedelta(hours=9))
logger = logging.getLogger("core.judge.evolution.cycle_report")


# ── 인과 검증 헬퍼 ─────────────────────────────────────────────

def _extract_numbers(text: str) -> list[str]:
    """텍스트에서 숫자 패턴 추출 (정수 / 소수 / % 포함)."""
    return re.findall(r"\d+(?:\.\d+)?%?", text)


def _extract_tunable_keys(text: str) -> list[str]:
    """텍스트에서 tunable key 패턴 추출 (word.word 형식)."""
    return re.findall(r"\b[a-z][a-z_]+\.[a-z][a-z_]+\b", text)


def _extract_metrics(text: str) -> dict[str, str]:
    """검증 텍스트에서 지표 추출 (sharpe, wr 등)."""
    metrics: dict[str, str] = {}
    patterns = [
        (r"sharpe\s*[=:≈]?\s*(\d+(?:\.\d+)?)", "sharpe"),
        (r"wr\s*[=:≈]?\s*(\d+(?:\.\d+)?)", "wr"),
        (r"win_rate\s*[=:≈]?\s*(\d+(?:\.\d+)?)", "win_rate"),
    ]
    for pattern, key in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            metrics[key] = m.group(1)
    return metrics


def validate_causality(inp: CycleReportInput) -> dict[str, bool]:
    """6단계 인과 일관성 검사. full 모드에서만 엄격 적용."""
    checks: dict[str, bool] = {}

    # 1→2: observation 수치가 hypothesis에 등장?
    obs_numbers = _extract_numbers(inp.observation)
    checks["obs_to_hyp"] = (
        not obs_numbers
        or any(n in inp.hypothesis for n in obs_numbers)
    )

    # 2→3: hypothesis 핵심 변경 키워드가 validation에 반영?
    hyp_keys = _extract_tunable_keys(inp.hypothesis)
    checks["hyp_to_val"] = (
        not hyp_keys
        or "sharpe" in inp.validation.lower()
        or "wr" in inp.validation.lower()
        or "win_rate" in inp.validation.lower()
    )

    # 3→4: validation 수치가 application에 언급?
    val_metrics = _extract_metrics(inp.validation)
    checks["val_to_app"] = (
        not val_metrics
        or any(v in inp.application for v in val_metrics.values())
        or "paper" in inp.application.lower()
        or "canary" in inp.application.lower()
        or "통과" in inp.application
    )

    # 4→5: application 단계가 evaluation 기준과 매칭?
    checks["app_to_eval"] = (
        "기준" in inp.evaluation
        or "통과" in inp.evaluation
        or "sharpe" in inp.evaluation.lower()
        or "가드레일" in inp.evaluation
    )

    # 5→6: evaluation 결과가 lesson에 반영?
    checks["eval_to_lesson"] = (
        "L-" in inp.lesson
        or "예정" in inp.lesson
        or "교훈" in inp.lesson
        or "없음" in inp.lesson
    )

    return checks


def _fmt_param_changes(changes: list[ParameterChangeSummary]) -> str:
    """파라미터 변경 비교표 렌더링."""
    if not changes:
        return ""
    lines = ["📊 파라미터 변경"]
    for ch in changes:
        before = f"{ch.before}{ch.unit}"
        after = f"{ch.after}{ch.unit}"
        lines.append(f"  {ch.label}: {before} → {after}")
        lines.append(f"    └ 근거: {ch.rationale}")
    return "\n".join(lines)


def _fmt_trade_stats(s: TradeStatsSummary) -> str:
    lines = [
        f"  {s.total}건 | 승리 {s.wins} / 손실 {s.losses} | 승률 {s.win_rate_pct:.1f}%",
        f"  손익: {s.pnl_jpy:+,}엔 | 평균: {s.avg_pnl_jpy:+,}엔 | 최대손실: {s.max_loss_jpy:,}엔",
    ]
    if s.lesson_adherence_rate is not None:
        lines.append(f"  교훈 준수율: {s.lesson_adherence_rate*100:.0f}%")
    if s.losing_patterns:
        lines.append("  손실 패턴:")
        for p in s.losing_patterns:
            lines.append(f"    • {p}")
    return "\n".join(lines)


def _fmt_market_context(c: MarketContextSummary) -> str:
    lines = [f"  기간: {c.period}"]
    lines.append(f"  BTC: {c.btc_range_jpy} | ATR 평균: {c.atr_avg_pct:.2f}%")
    if c.regime_changes:
        lines.append(f"  체제 변화: {' / '.join(c.regime_changes)}")
    else:
        lines.append("  체제 변화: 없음 (안정)")
    fng_text = ""
    if c.fng_end is not None:
        if c.fng_start is not None:
            fng_text = f"{c.fng_start} → {c.fng_end} ({c.fng_label})"
        else:
            fng_text = f"{c.fng_end} ({c.fng_label})"
        parts = [f"FNG: {fng_text}"]
        if c.vix:
            parts.append(f"VIX: {c.vix:.1f}")
        if c.dxy:
            parts.append(f"DXY: {c.dxy:.1f}")
        lines.append("  " + " | ".join(parts))
    if c.key_events:
        lines.append("  주요 이벤트:")
        for e in c.key_events:
            lines.append(f"    • {e}")
    if c.key_news:
        lines.append("  주요 뉴스:")
        for n in c.key_news:
            lines.append(f"    • {n}")
    return "\n".join(lines)


def _fmt_backtest(b: BacktestSummary) -> str:
    def arrow(after: float, before: float, higher_is_better: bool = True) -> str:
        better = after > before if higher_is_better else after < before
        return "✅" if better else "⚠️"

    lines = [f"  기간: {b.period} | {b.trades}건"]
    lines.append(f"  {'항목':<8} {'변경 전':>9} {'변경 후':>9}")
    lines.append(f"  {'Sharpe':<8} {b.sharpe_before:>9.2f} {b.sharpe_after:>9.2f}  {arrow(b.sharpe_after, b.sharpe_before)}")
    lines.append(f"  {'승률':<8} {b.wr_before_pct:>8.1f}% {b.wr_after_pct:>8.1f}%  {arrow(b.wr_after_pct, b.wr_before_pct)}")
    lines.append(f"  {'최대낙폭':<8} {b.max_dd_before_pct:>8.1f}% {b.max_dd_after_pct:>8.1f}%  {arrow(b.max_dd_after_pct, b.max_dd_before_pct, higher_is_better=False)}")
    if b.avg_pnl_before_jpy is not None and b.avg_pnl_after_jpy is not None:
        lines.append(f"  {'평균손익':<8} {b.avg_pnl_before_jpy:>+9,} {b.avg_pnl_after_jpy:>+9,}엔  {arrow(b.avg_pnl_after_jpy, b.avg_pnl_before_jpy)}")
    if b.samantha_comment:
        lines.append(f"  Samantha: {b.samantha_comment}")
    return "\n".join(lines)


def format_evolution_report(report: CycleReportResponse) -> str:
    """Telegram 메시지 포맷 — detail 있으면 상세 테이블 포함."""
    if report.mode == "no_signal":
        return (
            f"🧬 진화 사이클 #{report.cycle_id}\n"
            f"이번 3일 분석 결과 — 변경 신호 없음.\n"
            f"근거: {report.observation[:200]}"
        )
    if report.mode == "failed":
        return f"⚠️ 진화 사이클 #{report.cycle_id} 실패\n{report.observation}"

    d = report.detail
    lessons_ref = report.references.get("lessons", [])
    tunables_ref = report.references.get("tunables", [])

    # ── 1️⃣ 관찰 ──────────────────────────────────────────────
    obs_block = "1️⃣ 관찰\n"
    if d and d.market_context:
        obs_block += _fmt_market_context(d.market_context) + "\n\n"
    if d and d.trade_stats:
        obs_block += _fmt_trade_stats(d.trade_stats) + "\n\n"
    obs_block += report.observation

    # ── 2️⃣ 가설 ──────────────────────────────────────────────
    hyp_block = "2️⃣ 가설\n"
    if d and d.parameter_changes:
        hyp_block += _fmt_param_changes(d.parameter_changes) + "\n\n"
    hyp_block += report.hypothesis

    # ── 3️⃣ 검증 ──────────────────────────────────────────────
    val_block = "3️⃣ 검증\n"
    if d and d.backtest:
        val_block += _fmt_backtest(d.backtest) + "\n\n"
    val_block += report.validation

    return (
        f"🧬 진화 사이클 #{report.cycle_id}  ({report.cycle_at.strftime('%Y-%m-%d %H:%M')})\n"
        f"가설 ID: {report.hypothesis_id or '없음'}\n"
        f"\n{obs_block}\n"
        f"\n{hyp_block}\n"
        f"\n{val_block}\n"
        f"\n4️⃣ 적용\n{report.application}\n"
        f"\n5️⃣ 평가\n{report.evaluation}\n"
        f"\n6️⃣ 교훈\n{report.lesson}\n"
        f"\n참조: lessons={lessons_ref} | tunables={tunables_ref}"
    )


class CycleReportService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def build_and_validate(self, inp: CycleReportInput) -> CycleReportResponse:
        """인과 검증 후 CycleReportResponse 반환. 인과 단절 시 ValueError."""
        cycle_id = await self._next_cycle_id()
        now = datetime.now(tz=JST)

        causality = inp.causality_self_check or validate_causality(inp)

        if inp.mode == "full":
            failing = [k for k, ok in causality.items() if not ok]
            if failing:
                raise ValueError(f"인과 단절: {failing}")

        report = CycleReportResponse(
            cycle_id=cycle_id,
            cycle_at=now,
            hypothesis_id=inp.hypothesis_id,
            mode=inp.mode,
            observation=inp.observation,
            hypothesis=inp.hypothesis,
            validation=inp.validation,
            application=inp.application,
            evaluation=inp.evaluation,
            lesson=inp.lesson,
            causality_self_check=causality,
            references=inp.references,
            detail=inp.detail,
        )
        return report

    async def persist(self, report: CycleReportResponse) -> None:
        """ai_judgments 테이블에 kind='cycle_report' 로 저장."""
        try:
            from sqlalchemy import text
            payload = report.model_dump(mode="json")
            await self.db.execute(
                text(
                    "INSERT INTO ai_judgments (kind, pair, payload, judged_at) "
                    "VALUES (:kind, :pair, :payload, :judged_at)"
                ),
                {
                    "kind": "cycle_report",
                    "pair": "btc_jpy",
                    "payload": json.dumps(payload),
                    "judged_at": report.cycle_at,
                },
            )
            await self.db.commit()
        except Exception as exc:
            logger.warning("cycle_report persist failed: %s", exc)

    async def list(self, limit: int = 30) -> list[dict]:
        """최근 사이클 보고서 목록."""
        try:
            from sqlalchemy import text
            rows = (
                await self.db.execute(
                    text(
                        "SELECT payload, judged_at FROM ai_judgments "
                        "WHERE kind = 'cycle_report' "
                        "ORDER BY judged_at DESC LIMIT :limit"
                    ),
                    {"limit": limit},
                )
            ).all()
            return [json.loads(r[0]) for r in rows]
        except Exception as exc:
            logger.warning("cycle_report list failed: %s", exc)
            return []

    async def _next_cycle_id(self) -> str:
        year = datetime.now(tz=JST).year
        prefix = f"CR-{year}-"
        try:
            from sqlalchemy import text
            row = (
                await self.db.execute(
                    text(
                        "SELECT payload FROM ai_judgments "
                        "WHERE kind = 'cycle_report' "
                        "ORDER BY judged_at DESC LIMIT 1"
                    )
                )
            ).scalar()
            if row:
                last_id = json.loads(row).get("cycle_id", "")
                if last_id.startswith(prefix):
                    num = int(last_id.split("-")[-1]) + 1
                    return f"{prefix}{num:03d}"
        except Exception:
            pass
        return f"{prefix}001"
