"""
T-INV: 코드 불변식 정적 검증.

실행 없이 소스코드를 읽어 구조적 규칙을 검증한다.

T-INV-01: GMO sign_path에 '?' 미포함 (쿼리스트링이 sign에 섞이면 인증 실패)
T-INV-02: stop_loss_pct 파라미터 키가 백테스트↔실매매 양쪽에서 동일
T-INV-03: exit_reason이 백테스트 engine과 실매매 manager 양쪽에서 사용됨
T-INV-04: near_bound_pct 파라미터 키가 백테스트↔실매매 양쪽에서 동일
T-INV-05: SL 기본값이 양쪽 모두 1.5 (%)
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# tests/unit/test_code_invariants.py → parents[2] = trading-engine/
ENGINE_ROOT = REPO_ROOT  # trading-engine 루트 자체


def _read(rel: str) -> str:
    return (ENGINE_ROOT / rel).read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────
# T-INV-01: GMO sign_path에 '?' 미포함
# ──────────────────────────────────────────────────────────────

class TestSignPathNoQueryString:
    """sign_path = "..." 형태의 문자열 리터럴에 '?'가 없어야 한다."""

    def test_gmo_client_sign_paths_no_question_mark(self):
        source = _read("adapters/gmo_fx/client.py")
        # sign_path = "/v1/xxx" 형태 추출
        paths = re.findall(r'sign_path\s*=\s*"([^"]*)"', source)
        assert paths, "sign_path 변수가 하나도 없음 — 파일 경로를 확인하세요"
        bad = [p for p in paths if "?" in p]
        assert not bad, (
            f"GMO sign_path에 쿼리스트링('?')이 포함된 경우 발견: {bad}\n"
            "sign_path와 request_path를 분리해야 합니다."
        )


# ──────────────────────────────────────────────────────────────
# T-INV-02: stop_loss_pct 파라미터 키 일치
# ──────────────────────────────────────────────────────────────

class TestStopLossPctKeyConsistency:
    """백테스트 engine과 실전 manager가 같은 파라미터 키를 사용."""

    def test_backtest_engine_uses_stop_loss_pct(self):
        source = _read("core/backtest/engine.py")
        assert 'stop_loss_pct' in source, (
            "backtest engine에서 'stop_loss_pct' 파라미터 키를 사용하지 않음"
        )

    def test_live_manager_uses_stop_loss_pct(self):
        source = _read("core/strategy/plugins/box_mean_reversion/manager.py")
        assert 'stop_loss_pct' in source, (
            "BoxManager에서 'stop_loss_pct' 파라미터 키를 사용하지 않음"
        )

    def test_sl_default_value_is_1_5_in_backtest(self):
        source = _read("core/backtest/engine.py")
        # params.get("stop_loss_pct", 1.5) 패턴
        match = re.search(r'params\.get\(["\']stop_loss_pct["\'],\s*([\d.]+)\)', source)
        assert match, "backtest engine에서 stop_loss_pct 기본값 패턴을 찾을 수 없음"
        assert float(match.group(1)) == 1.5, (
            f"backtest SL 기본값이 1.5가 아님: {match.group(1)}"
        )

    def test_sl_default_value_is_1_5_in_manager(self):
        source = _read("core/strategy/plugins/box_mean_reversion/manager.py")
        match = re.search(r'params\.get\(["\']stop_loss_pct["\'],\s*([\d.]+)\)', source)
        assert match, "BoxManager에서 stop_loss_pct 기본값 패턴을 찾을 수 없음"
        assert float(match.group(1)) == 1.5, (
            f"BoxManager SL 기본값이 1.5가 아님: {match.group(1)}"
        )

    def test_sl_defaults_match(self):
        """양쪽 기본값이 동일해야 함."""
        bt_src = _read("core/backtest/engine.py")
        mg_src = _read("core/strategy/plugins/box_mean_reversion/manager.py")

        bt_match = re.search(r'params\.get\(["\']stop_loss_pct["\'],\s*([\d.]+)\)', bt_src)
        mg_match = re.search(r'params\.get\(["\']stop_loss_pct["\'],\s*([\d.]+)\)', mg_src)

        assert bt_match and mg_match, "stop_loss_pct 기본값 패턴 미발견"
        assert bt_match.group(1) == mg_match.group(1), (
            f"SL 기본값 불일치: backtest={bt_match.group(1)}, manager={mg_match.group(1)}"
        )


# ──────────────────────────────────────────────────────────────
# T-INV-03: exit_reason 양방향 사용
# ──────────────────────────────────────────────────────────────

class TestExitReasonConsistency:
    """백테스트와 실매매 양쪽에서 exit_reason이 기록되어야 한다."""

    def test_backtest_records_exit_reason(self):
        source = _read("core/backtest/engine.py")
        assert "exit_reason" in source, "backtest engine에서 exit_reason 미사용"

    def test_live_manager_records_exit_reason(self):
        source = _read("core/strategy/plugins/box_mean_reversion/manager.py")
        assert "exit_reason" in source, "BoxManager에서 exit_reason 미사용"

    def test_price_stop_loss_reason_in_both(self):
        """price_stop_loss exit_reason이 양쪽에서 사용."""
        bt_src = _read("core/backtest/engine.py")
        mg_src = _read("core/strategy/plugins/box_mean_reversion/manager.py")
        assert "price_stop_loss" in bt_src, "backtest engine에서 price_stop_loss exit_reason 미사용"
        assert "price_stop_loss" in mg_src, "BoxManager에서 price_stop_loss exit_reason 미사용"


# ──────────────────────────────────────────────────────────────
# T-INV-04: near_bound_pct 파라미터 키 일치
# ──────────────────────────────────────────────────────────────

class TestNearBoundPctConsistency:
    def test_backtest_engine_uses_near_bound_pct(self):
        source = _read("core/backtest/engine.py")
        # near_bound_pct 또는 near_pct가 있어야 함
        assert "near_bound_pct" in source or "near_pct" in source, (
            "backtest engine에서 near bound 파라미터 키 미사용"
        )

    def test_live_manager_uses_near_bound_pct(self):
        source = _read("core/strategy/plugins/box_mean_reversion/manager.py")
        assert "near_bound_pct" in source or "near_pct" in source, (
            "BoxManager에서 near bound 파라미터 키 미사용"
        )


# ──────────────────────────────────────────────────────────────
# T-INV-05: 박스 시그널 함수가 공유 모듈에서 import됨
# ──────────────────────────────────────────────────────────────

class TestSharedBoxSignalImport:
    """백테스트와 실매매가 동일한 box_signals 모듈 함수를 사용해야 한다."""

    def test_backtest_imports_from_box_signals(self):
        source = _read("core/backtest/engine.py")
        assert "box_signals" in source, (
            "backtest engine이 core.strategy.box_signals를 import하지 않음"
        )

    def test_live_manager_imports_from_box_signals(self):
        source = _read("core/strategy/plugins/box_mean_reversion/manager.py")
        assert "box_signals" in source, (
            "BoxManager가 core.strategy.box_signals를 import하지 않음"
        )
