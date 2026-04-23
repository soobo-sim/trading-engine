"""
Advisory Validator — P3 Lesson 인용 검증.

Rachel advisory의 reasoning에 recalled_lessons가 인용되어 있는지 확인.
LESSON_VALIDATION_MODE 환경변수로 strict / warn / off 모드 분리.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("core.judge.evolution.advisor_validator")

# L-YYYY-NNN 형식 패턴
LESSON_REF_PATTERN = re.compile(r"L-\d{4}-\d{3}")

# 환경변수: strict (거부) / warn (로그만) / off (무시)
_MODE = os.getenv("LESSON_VALIDATION_MODE", "warn").lower()


class AdvisoryValidationError(Exception):
    """reasoning에 필수 lesson 인용이 누락된 경우."""


def validate_lesson_citations(
    *,
    reasoning: str,
    recalled_lesson_ids: list[str],
) -> None:
    """recalled_lessons가 있으면 reasoning에 각 ID가 언급되어야 한다.

    LESSON_VALIDATION_MODE=strict  → 미인용 시 AdvisoryValidationError 발생
    LESSON_VALIDATION_MODE=warn    → 미인용 시 WARNING 로그만
    LESSON_VALIDATION_MODE=off     → 아무 것도 하지 않음

    Args:
        reasoning: advisory reasoning 텍스트
        recalled_lesson_ids: macro-brief에서 전달된 lesson ID 목록
    """
    if _MODE == "off":
        return

    if not recalled_lesson_ids:
        return  # 인용할 lesson이 없으면 검증 불필요

    cited = set(LESSON_REF_PATTERN.findall(reasoning))
    missing = set(recalled_lesson_ids) - cited

    if not missing:
        return  # 모두 인용됨

    msg = (
        f"reasoning에 다음 lesson을 인용하지 않음: {sorted(missing)}. "
        f"수용/거부 여부를 명시해야 합니다."
    )

    if _MODE == "strict":
        raise AdvisoryValidationError(msg)
    else:
        # warn (기본값)
        logger.warning("[Lesson Citation] %s", msg)
