"""
PostAnalyzer — 거래 사후 분석 (Learning Loop Stage 2).

청산 완료 후 ai_judgments.outcome 업데이트 시 ENABLE_POST_ANALYSIS=true이면
LLM에 거래 결과를 전달하여 짧은 분석 텍스트를 생성, ai_judgments.post_analysis에 저장한다.

분석 결과는 미래 판단 개선에 활용된다 (04_LEARNING_LOOP.md §4).

사용법:
    analyzer = PostAnalyzer(
        llm_client=OpenAiLlmClient(api_key=..., default_model="gpt-4o-mini"),
        session_factory=session_factory,
        judgment_model=AiJudgment,
    )
    await analyzer.analyze(judgment_id=42, outcome="win", realized_pnl=3200.0, hold_duration_hours=4.2)

설계서: trader-common/docs/specs/ai-native/04_LEARNING_LOOP.md §4
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update as sa_update

logger = logging.getLogger(__name__)

_POST_ANALYSIS_SYSTEM_PROMPT = """당신은 트레이딩 전략 사후 분석 전문가입니다.
주어진 거래 기록(페어, 액션, 결과, PnL, 보유 시간, 신뢰도)을 분석하여
다음 판단에서 개선할 수 있는 핵심 인사이트를 200자 이내 일본어 또는 한국어로 작성하세요.
- 성공(win) 시: 무엇이 잘 작동했는가?
- 실패(loss) 시: 주요 실패 원인과 다음에 피해야 할 패턴은 무엇인가?"""

_POST_ANALYSIS_SCHEMA = {
    "name": "post_analysis",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "analysis": {
                "type": "string",
                "description": "사후 분석 텍스트 (200자 이내)",
            }
        },
        "required": ["analysis"],
        "additionalProperties": False,
    },
}


class PostAnalyzer:
    """거래 사후 분석기.

    Args:
        llm_client:      ILlmClient (OpenAiLlmClient 또는 Mock).
        session_factory: AsyncSession 팩토리.
        judgment_model:  AiJudgment ORM 클래스.
        model:           LLM 모델 override (없으면 llm_client 기본값).
    """

    def __init__(
        self,
        llm_client: Any,
        session_factory: Any,
        judgment_model: Any,
        model: str | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._session_factory = session_factory
        self._judgment_model = judgment_model
        self._model = model

    async def analyze(
        self,
        judgment_id: int,
        outcome: str,
        realized_pnl: float,
        hold_duration_hours: float,
    ) -> str | None:
        """거래 결과 → LLM 사후 분석 → ai_judgments.post_analysis 업데이트.

        Args:
            judgment_id:          ai_judgments.id
            outcome:              "win" | "loss"
            realized_pnl:         실현 손익 (JPY)
            hold_duration_hours:  보유 시간 (시간)

        Returns:
            분석 텍스트 (성공) 또는 None (실패).
            실패 시 WARNING 로그만 — 거래 흐름을 블록하지 않는다.
        """
        # 판단 기록에서 컨텍스트 읽기
        judgment = await self._fetch_judgment(judgment_id)
        if judgment is None:
            logger.warning(f"[PostAnalyzer] judgment_id={judgment_id} 없음 — 분석 스킵")
            return None

        user_prompt = self._build_prompt(judgment, outcome, realized_pnl, hold_duration_hours)

        try:
            result = await self._llm_client.chat(
                system_prompt=_POST_ANALYSIS_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response_schema=_POST_ANALYSIS_SCHEMA,
                model=self._model,
            )
            analysis_text = result.get("analysis", "")
        except Exception as e:
            logger.warning(f"[PostAnalyzer] LLM 호출 실패 (스킵): {e}")
            return None

        if not analysis_text:
            return None

        # DB 업데이트
        try:
            async with self._session_factory() as session:
                await session.execute(
                    sa_update(self._judgment_model)
                    .where(self._judgment_model.id == judgment_id)
                    .values(post_analysis=analysis_text)
                )
                await session.commit()
            logger.info(
                f"[PostAnalyzer] judgment_id={judgment_id} post_analysis 저장 완료 "
                f"({len(analysis_text)}자)"
            )
        except Exception as e:
            logger.warning(f"[PostAnalyzer] DB 업데이트 실패 (스킵): {e}")

        return analysis_text

    async def _fetch_judgment(self, judgment_id: int) -> Any | None:
        """ai_judgments DB에서 판단 기록 조회."""
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(self._judgment_model).where(
                        self._judgment_model.id == judgment_id
                    )
                )
                return result.scalars().first()
        except Exception as e:
            logger.warning(f"[PostAnalyzer] judgment 조회 실패: {e}")
            return None

    @staticmethod
    def _build_prompt(
        judgment: Any,
        outcome: str,
        realized_pnl: float,
        hold_duration_hours: float,
    ) -> str:
        """사후 분석용 LLM 프롬프트 생성."""
        alice_reasoning = judgment.alice_reasoning or {}
        alice_summary = str(alice_reasoning)[:300] if alice_reasoning else "N/A"

        return (
            f"**거래 기록**\n"
            f"- 페어: {judgment.pair}\n"
            f"- 거래소: {judgment.exchange}\n"
            f"- 판단 액션: {judgment.final_action}\n"
            f"- 판단 신뢰도: {judgment.final_confidence:.2f}\n"
            f"- 소스: {judgment.source}\n"
            f"- 결과: {outcome}\n"
            f"- 실현 손익: ¥{realized_pnl:+,.0f}\n"
            f"- 보유 시간: {hold_duration_hours:.1f}시간\n"
            f"- Alice 분석 요약: {alice_summary}\n\n"
            "위 거래에 대한 사후 분석을 200자 이내로 작성하세요."
        )
