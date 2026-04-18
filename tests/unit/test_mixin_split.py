"""
Phase 2 Mixin 분리 검증 테스트 (MS-01~MS-05).

DOMAIN_SPLIT_AGENT_REDESIGN.md Phase 2 성공 기준:
  MS-01: MRO 순서 확인
  MS-02: JudgeMixin._compute_signal 접근 가능
  MS-03: ExecutionMixin._handle_execution_result 접근 가능
  MS-04: 서브클래스 override 동작 (MarginTrendManager)
  MS-05: import 경로 불변
"""
import pytest


# ──────────────────────────────────────────
# MS-01: MRO 순서 확인
# ──────────────────────────────────────────

def test_ms01_mro_contains_all_mixins_in_order():
    """BaseTrendManager MRO에 CandleLoopMixin → JudgeMixin → ExecutionMixin 순서."""
    from core.strategy.base_trend import BaseTrendManager
    from core.strategy._candle_loop import CandleLoopMixin
    from core.strategy._judge_mixin import JudgeMixin
    from core.strategy._execution_mixin import ExecutionMixin

    mro = BaseTrendManager.__mro__
    names = [cls.__name__ for cls in mro]

    assert "CandleLoopMixin" in names
    assert "JudgeMixin" in names
    assert "ExecutionMixin" in names

    candle_idx = names.index("CandleLoopMixin")
    judge_idx = names.index("JudgeMixin")
    exec_idx = names.index("ExecutionMixin")

    assert candle_idx < judge_idx < exec_idx, (
        f"MRO 순서 오류: CandleLoop={candle_idx}, Judge={judge_idx}, Exec={exec_idx}"
    )


# ──────────────────────────────────────────
# MS-02: JudgeMixin._compute_signal 접근 가능
# ──────────────────────────────────────────

def test_ms02_judge_compute_signal_accessible_from_base():
    """BaseTrendManager 인스턴스에서 _compute_signal 메서드가 callable."""
    from core.strategy.base_trend import BaseTrendManager
    from core.strategy._judge_mixin import JudgeMixin

    # _compute_signal이 JudgeMixin에서 정의되어야 함
    assert hasattr(JudgeMixin, "_compute_signal")
    # BaseTrendManager도 MRO로 접근 가능해야 함
    assert hasattr(BaseTrendManager, "_compute_signal")
    # 동일 메서드 객체 (MRO가 JudgeMixin을 우선)
    assert BaseTrendManager._compute_signal is JudgeMixin._compute_signal


# ──────────────────────────────────────────
# MS-03: ExecutionMixin._handle_execution_result 접근 가능
# ──────────────────────────────────────────

def test_ms03_execution_handle_result_accessible_from_base():
    """BaseTrendManager 인스턴스에서 _handle_execution_result 메서드가 callable."""
    from core.strategy.base_trend import BaseTrendManager
    from core.strategy._execution_mixin import ExecutionMixin

    assert hasattr(ExecutionMixin, "_handle_execution_result")
    assert hasattr(BaseTrendManager, "_handle_execution_result")
    assert BaseTrendManager._handle_execution_result is ExecutionMixin._handle_execution_result


# ──────────────────────────────────────────
# MS-04: 서브클래스 override 동작
# ──────────────────────────────────────────

def test_ms04_subclass_override_check_exit_warning():
    """MarginTrendManager._check_exit_warning이 JudgeMixin 것을 정상 override."""
    from core.strategy._judge_mixin import JudgeMixin
    from core.strategy.plugins.cfd_trend_following.manager import MarginTrendManager

    if hasattr(MarginTrendManager, "_check_exit_warning"):
        # override된 경우 MarginTrendManager의 메서드가 JudgeMixin 것이 아니어야 함
        margin_method = MarginTrendManager.__dict__.get("_check_exit_warning")
        if margin_method is not None:
            # 서브클래스가 직접 정의한 메서드가 있다면 JudgeMixin과 달라야 함
            assert margin_method is not JudgeMixin._check_exit_warning, (
                "MarginTrendManager override가 JudgeMixin과 동일 — override 실패"
            )
    # override 없으면 JudgeMixin 메서드를 그대로 상속 (정상)
    assert hasattr(MarginTrendManager, "_check_exit_warning")


def test_ms04_gmo_coin_manager_inherits_base():
    """GmoCoinTrendManager가 BaseTrendManager를 상속하고 MRO에 Mixin이 포함됨."""
    from core.strategy.base_trend import BaseTrendManager
    from core.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager
    from core.strategy._candle_loop import CandleLoopMixin
    from core.strategy._judge_mixin import JudgeMixin
    from core.strategy._execution_mixin import ExecutionMixin

    assert issubclass(GmoCoinTrendManager, BaseTrendManager)
    mro = GmoCoinTrendManager.__mro__
    names = [cls.__name__ for cls in mro]
    assert "CandleLoopMixin" in names
    assert "JudgeMixin" in names
    assert "ExecutionMixin" in names


# ──────────────────────────────────────────
# MS-05: import 경로 불변
# ──────────────────────────────────────────

def test_ms05_import_path_unchanged():
    """from core.strategy.base_trend import BaseTrendManager 가 정상 동작."""
    from core.strategy.base_trend import BaseTrendManager  # noqa: F401
    assert BaseTrendManager is not None


def test_ms05_base_trend_module_is_thin():
    """base_trend.py가 Mixin을 포함하지 않고 import만 한다."""
    import inspect
    import core.strategy.base_trend as mod
    from core.strategy._candle_loop import CandleLoopMixin
    from core.strategy._judge_mixin import JudgeMixin
    from core.strategy._execution_mixin import ExecutionMixin

    # Mixin 클래스 자체는 base_trend 모듈에서 정의되지 않아야 함
    assert mod.CandleLoopMixin is CandleLoopMixin  # re-export 허용
    # _compute_signal은 base_trend 모듈에서 정의되지 않음
    source_file = inspect.getfile(CandleLoopMixin)
    assert "_candle_loop" in source_file

    source_file = inspect.getfile(JudgeMixin)
    assert "_judge_mixin" in source_file

    source_file = inspect.getfile(ExecutionMixin)
    assert "_execution_mixin" in source_file
