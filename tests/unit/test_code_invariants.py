"""
T-INV: 코드 불변식 정적 검증.

실행 없이 소스코드를 읽어 구조적 규칙을 검증한다.

T-INV-01: GMO Coin sign_path에 '?' 미포함 (쿼리스트링이 sign에 섞이면 인증 실패)
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# tests/unit/test_code_invariants.py → parents[2] = trading-engine/
ENGINE_ROOT = REPO_ROOT  # trading-engine 루트 자체


def _read(rel: str) -> str:
    return (ENGINE_ROOT / rel).read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────
# T-INV-01: GMO Coin sign_path에 '?' 미포함
# ──────────────────────────────────────────────────────────────

class TestSignPathNoQueryString:
    """sign_path = "..." 형태의 문자열 리터럴에 '?'가 없어야 한다."""

    def test_gmo_coin_client_sign_paths_no_question_mark(self):
        source = _read("adapters/gmo_coin/client.py")
        # sign_path = "/v1/xxx" 형태 추출
        paths = re.findall(r'sign_path\s*=\s*"([^"]*)"', source)
        assert paths, "sign_path 변수가 하나도 없음 — 파일 경로를 확인하세요"
        bad = [p for p in paths if "?" in p]
        assert not bad, (
            f"GMO Coin sign_path에 쿼리스트링('?')이 포함된 경우 발견: {bad}\n"
            "sign_path와 request_path를 분리해야 합니다."
        )
