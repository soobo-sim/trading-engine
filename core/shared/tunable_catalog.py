"""
Tunable 카탈로그 — 진화 대상 요소의 단일 진실 소스.

에이전트가 만질 수 있는 모든 요소를 화이트리스트로 관리.
카탈로그 외 변경은 hypotheses_service 가 거부한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

LayerType = Literal["A", "B", "C", "D", "E"]
RiskLevel = Literal["low", "medium", "high", "escalation_only"]
Autonomy = Literal["auto", "escalation"]
ValueType = Literal["float", "int", "str", "bool", "json", "prompt"]


@dataclass(frozen=True)
class TunableSpec:
    """진화 대상 요소의 단일 명세."""

    key: str                              # 고유 키 (snake_case, dot.path 허용)
    layer: LayerType                      # A=파라미터 / B=룰 / C=빈도 / D=데이터 / E=프롬프트
    value_type: ValueType
    default: Any
    min: Any | None = None                # 수치형만 의미. str/json은 None
    max: Any | None = None
    allowed_values: list[Any] | None = None  # enum 형식
    owner: str = ""                       # 코드/MD 위치 (예: gmoc_strategies.parameters)
    risk_level: RiskLevel = "low"
    autonomy: Autonomy = "auto"
    description: str = ""                 # 한국어 1~2줄
    affects: list[str] = field(default_factory=list)
    db_table: str | None = None           # DB에 저장된 경우 테이블명
    db_path: str | None = None            # JSONB 안의 경로

    def __post_init__(self) -> None:
        # autonomy=escalation → risk_level=escalation_only 강제
        if self.autonomy == "escalation" and self.risk_level != "escalation_only":
            raise ValueError(
                f"{self.key}: autonomy=escalation requires risk_level=escalation_only"
            )

    def validate_value(self, new_value: Any) -> tuple[bool, str | None]:
        """값 범위 검증. (ok, error_msg) 반환."""
        if self.allowed_values is not None and new_value not in self.allowed_values:
            return False, (
                f"{self.key}: value {new_value!r} not in allowed_values"
                f" {self.allowed_values}"
            )
        if self.min is not None:
            try:
                if new_value < self.min:
                    return False, f"{self.key}: {new_value} < min {self.min}"
            except TypeError:
                pass
        if self.max is not None:
            try:
                if new_value > self.max:
                    return False, f"{self.key}: {new_value} > max {self.max}"
            except TypeError:
                pass
        return True, None


class TunableCatalog:
    """모든 TunableSpec 의 in-memory 레지스트리 (싱글톤 패턴)."""

    _specs: dict[str, TunableSpec] = {}

    @classmethod
    def register(cls, spec: TunableSpec) -> None:
        if spec.key in cls._specs:
            raise ValueError(f"Duplicate tunable key: {spec.key}")
        cls._specs[spec.key] = spec

    @classmethod
    def register_many(cls, specs: list[TunableSpec]) -> None:
        for spec in specs:
            cls.register(spec)

    @classmethod
    def get(cls, key: str) -> TunableSpec | None:
        return cls._specs.get(key)

    @classmethod
    def list_all(cls) -> list[TunableSpec]:
        return sorted(cls._specs.values(), key=lambda s: (s.layer, s.key))

    @classmethod
    def list_by_layer(cls, layer: LayerType) -> list[TunableSpec]:
        return [s for s in cls.list_all() if s.layer == layer]

    @classmethod
    def list_by_autonomy(cls, autonomy: Autonomy) -> list[TunableSpec]:
        return [s for s in cls.list_all() if s.autonomy == autonomy]

    @classmethod
    def validate_change(cls, key: str, new_value: Any) -> tuple[bool, str | None]:
        """변경 가능 여부 검증. (ok, error_msg) 반환."""
        spec = cls.get(key)
        if spec is None:
            return False, f"Unknown tunable key: {key!r} — 카탈로그에 없는 변경은 불허"
        return spec.validate_value(new_value)

    @classmethod
    def count_by_layer(cls) -> dict[str, int]:
        result: dict[str, int] = {}
        for spec in cls._specs.values():
            result[spec.layer] = result.get(spec.layer, 0) + 1
        return result

    @classmethod
    def _reset(cls) -> None:
        """테스트 전용 — 레지스트리 초기화."""
        cls._specs.clear()
