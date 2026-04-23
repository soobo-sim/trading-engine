"""
P1 — Tunable 카탈로그 테스트.

TC-01~TC-05: TunableSpec 데이터 모델 검증
TC-06~TC-08: TunableCatalog 등록/조회 검증
TC-09~TC-12: 초기 등록 30개 키 무결성
TC-13~TC-15: API 응답 구조 검증 (FastAPI TestClient)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ── 테스트 픽스처 ────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_catalog():
    """각 테스트 전 카탈로그를 초기화해 독립성 보장."""
    from core.shared.tunable_catalog import TunableCatalog
    # 원본 상태 저장
    original = dict(TunableCatalog._specs)
    TunableCatalog._reset()
    yield
    # 복원
    TunableCatalog._specs.clear()
    TunableCatalog._specs.update(original)


@pytest.fixture
def populated_catalog():
    """표준 30개 키가 등록된 카탈로그."""
    from core.shared.tunable_catalog import TunableCatalog
    from core.shared.tunable_registry import register_all
    register_all()
    return TunableCatalog


# ── TC-01~TC-05: TunableSpec 데이터 모델 ─────────────────────


class TestTunableSpec:
    def test_valid_spec_creation(self):
        """TC-01: 올바른 TunableSpec 생성."""
        from core.shared.tunable_catalog import TunableSpec
        spec = TunableSpec(
            key="test.value",
            layer="A",
            value_type="float",
            default=1.5,
            min=0.5,
            max=3.0,
            description="테스트 파라미터",
        )
        assert spec.key == "test.value"
        assert spec.layer == "A"
        assert spec.autonomy == "auto"

    def test_escalation_requires_escalation_only_risk(self):
        """TC-02: autonomy=escalation이면 risk_level=escalation_only 강제."""
        from core.shared.tunable_catalog import TunableSpec
        with pytest.raises(ValueError, match="risk_level=escalation_only"):
            TunableSpec(
                key="bad.key",
                layer="B",
                value_type="int",
                default=5,
                autonomy="escalation",
                risk_level="high",  # escalation_only여야 함
            )

    def test_validate_value_within_range(self):
        """TC-03: 범위 안 값은 valid."""
        from core.shared.tunable_catalog import TunableSpec
        spec = TunableSpec(
            key="test.range", layer="A", value_type="float",
            default=1.5, min=1.0, max=3.0,
        )
        ok, err = spec.validate_value(2.0)
        assert ok is True
        assert err is None

    def test_validate_value_below_min(self):
        """TC-04: min 미만 값은 invalid."""
        from core.shared.tunable_catalog import TunableSpec
        spec = TunableSpec(
            key="test.range", layer="A", value_type="float",
            default=1.5, min=1.0, max=3.0,
        )
        ok, err = spec.validate_value(0.5)
        assert ok is False
        assert "min" in err

    def test_validate_allowed_values(self):
        """TC-05: allowed_values에 없는 값은 invalid."""
        from core.shared.tunable_catalog import TunableSpec
        spec = TunableSpec(
            key="test.enum", layer="C", value_type="str",
            default="4h",
            allowed_values=["1h", "2h", "4h"],
        )
        ok, err = spec.validate_value("6h")
        assert ok is False
        assert "allowed_values" in err


# ── TC-06~TC-08: TunableCatalog 등록/조회 ───────────────────


class TestTunableCatalog:
    def test_register_and_get(self):
        """TC-06: register 후 get으로 조회 가능."""
        from core.shared.tunable_catalog import TunableCatalog, TunableSpec
        spec = TunableSpec(key="x.test", layer="A", value_type="float", default=1.0)
        TunableCatalog.register(spec)
        assert TunableCatalog.get("x.test") is spec

    def test_duplicate_key_raises(self):
        """TC-07: 동일 key 중복 등록 시 ValueError."""
        from core.shared.tunable_catalog import TunableCatalog, TunableSpec
        TunableCatalog.register(TunableSpec(key="dup.key", layer="A", value_type="int", default=1))
        with pytest.raises(ValueError, match="Duplicate"):
            TunableCatalog.register(TunableSpec(key="dup.key", layer="A", value_type="int", default=2))

    def test_unknown_key_returns_none(self):
        """TC-08: 없는 key는 None 반환."""
        from core.shared.tunable_catalog import TunableCatalog
        assert TunableCatalog.get("nonexistent") is None

    def test_validate_change_unknown_key(self):
        """TC-08b: 없는 key로 validate_change 호출 시 False."""
        from core.shared.tunable_catalog import TunableCatalog
        ok, err = TunableCatalog.validate_change("no.such.key", 1.0)
        assert ok is False
        assert "카탈로그에 없는" in err


# ── TC-09~TC-12: 초기 등록 30개 키 무결성 ────────────────────


class TestTunableRegistry:
    def test_all_specs_registered(self, populated_catalog):
        """TC-09: 초기 등록 키가 30개 이상."""
        specs = populated_catalog.list_all()
        assert len(specs) >= 30, f"기대 ≥30, 실제 {len(specs)}"

    def test_layer_a_count(self, populated_catalog):
        """TC-10: Layer A 키가 15개."""
        specs = populated_catalog.list_by_layer("A")
        assert len(specs) == 15

    def test_each_spec_has_description(self, populated_catalog):
        """TC-11: 모든 spec에 description이 있어야 함."""
        for spec in populated_catalog.list_all():
            assert spec.description, f"{spec.key} 에 description 없음"

    def test_escalation_specs_have_correct_risk(self, populated_catalog):
        """TC-12: autonomy=escalation인 spec은 risk_level=escalation_only."""
        for spec in populated_catalog.list_all():
            if spec.autonomy == "escalation":
                assert spec.risk_level == "escalation_only", (
                    f"{spec.key}: autonomy=escalation인데 risk_level={spec.risk_level}"
                )

    def test_count_by_layer(self, populated_catalog):
        """TC-12b: count_by_layer 반환값에 A~E 모두 포함."""
        counts = populated_catalog.count_by_layer()
        for layer in ("A", "B", "C", "D", "E"):
            assert layer in counts, f"Layer {layer} 누락"

    def test_validate_change_in_range(self, populated_catalog):
        """TC-12c: 카탈로그에 등록된 키에 범위 내 값 변경 성공."""
        ok, err = populated_catalog.validate_change("trend.trailing_stop_atr_initial", 2.0)
        assert ok is True
        assert err is None

    def test_validate_change_out_of_range(self, populated_catalog):
        """TC-12d: 범위 밖 값은 validate_change 실패."""
        ok, err = populated_catalog.validate_change("trend.trailing_stop_atr_initial", 99.0)
        assert ok is False


# ── TC-13~TC-15: API 응답 구조 ────────────────────────────────


class TestTunableAPI:
    @pytest.fixture
    def client(self, populated_catalog):
        """테스트용 FastAPI TestClient."""
        from fastapi import FastAPI
        from api.routes.evolution import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_get_tunables_returns_200(self, client):
        """TC-13: GET /api/tunables 200 응답."""
        resp = client.get("/api/tunables")
        assert resp.status_code == 200

    def test_get_tunables_structure(self, client):
        """TC-14: 응답에 total / tunables / by_layer_count 필드 포함."""
        data = client.get("/api/tunables").json()
        assert "total" in data
        assert "tunables" in data
        assert "by_layer_count" in data
        assert data["total"] == len(data["tunables"])

    def test_get_tunables_filter_by_layer(self, client):
        """TC-15: layer=A 필터 적용 시 Layer A 항목만 반환."""
        data = client.get("/api/tunables?layer=A").json()
        for t in data["tunables"]:
            assert t["layer"] == "A"

    def test_get_tunables_invalid_layer(self, client):
        """TC-15b: 잘못된 layer 파라미터 → 400."""
        resp = client.get("/api/tunables?layer=Z")
        assert resp.status_code == 400

    def test_get_tunables_filter_by_autonomy(self, client):
        """TC-15c: autonomy=escalation 필터 시 escalation 항목만."""
        data = client.get("/api/tunables?autonomy=escalation").json()
        for t in data["tunables"]:
            assert t["autonomy"] == "escalation"
