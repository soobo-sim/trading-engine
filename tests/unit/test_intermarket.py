"""
IntermarketClient 유닛 테스트 (F-04 알파 팩터).
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.judge.analysis.intermarket import IntermarketClient


@pytest.fixture
def client() -> IntermarketClient:
    return IntermarketClient(trading_data_url="http://mock-coinmarket:8002")


def _inject_cache(client: IntermarketClient, series: dict) -> None:
    """캐시에 직접 데이터 주입 (API 호출 없이 테스트)."""
    from datetime import datetime, timezone
    client._cache = (series, datetime.now(tz=timezone.utc))


# ── get_direction_bias ────────────────────────────────────────

class TestGetDirectionBias:
    @pytest.mark.asyncio
    async def test_disabled_returns_neutral(self, client):
        """intermarket_bias_enabled=False(기본) → neutral."""
        bias, conf, reasons = await client.get_direction_bias("usd_jpy", {})
        assert bias == "neutral"
        assert "disabled" in reasons

    @pytest.mark.asyncio
    async def test_usd_jpy_high_spread_bullish(self, client):
        """T10Y2Y 높음 + DGS10 높음 → USD_JPY bullish."""
        _inject_cache(client, {"T10Y2Y": 1.0, "DGS10": 4.5, "VIXCLS": 15.0})
        bias, conf, reasons = await client.get_direction_bias(
            "usd_jpy", {"intermarket_bias_enabled": True}
        )
        assert bias == "bullish"
        assert conf > 0

    @pytest.mark.asyncio
    async def test_usd_jpy_inverted_curve_bearish(self, client):
        """T10Y2Y 역전(-0.5%) + DGS10 낮음 → USD_JPY bearish."""
        _inject_cache(client, {"T10Y2Y": -0.5, "DGS10": 2.5, "VIXCLS": 18.0})
        bias, conf, reasons = await client.get_direction_bias(
            "usd_jpy", {"intermarket_bias_enabled": True}
        )
        assert bias == "bearish"

    @pytest.mark.asyncio
    async def test_gbp_jpy_high_vix_bearish(self, client):
        """VIX=30 → GBP_JPY bearish (리스크 오프)."""
        _inject_cache(client, {"VIXCLS": 30.0, "DTWEXBGS": 120.0})
        bias, conf, reasons = await client.get_direction_bias(
            "gbp_jpy", {"intermarket_bias_enabled": True}
        )
        assert bias == "bearish"

    @pytest.mark.asyncio
    async def test_gbp_jpy_low_vix_bullish(self, client):
        """VIX=12 → 리스크 온 → GBP_JPY bullish."""
        _inject_cache(client, {"VIXCLS": 12.0, "DTWEXBGS": 118.0})
        bias, conf, reasons = await client.get_direction_bias(
            "gbp_jpy", {"intermarket_bias_enabled": True}
        )
        assert bias == "bullish"

    @pytest.mark.asyncio
    async def test_api_failure_returns_neutral(self, client):
        """API 실패 → graceful → neutral."""
        bias, conf, reasons = await client.get_direction_bias(
            "usd_jpy", {"intermarket_bias_enabled": True}
        )
        assert bias == "neutral"
        assert "api_unavailable" in reasons

    @pytest.mark.asyncio
    async def test_eur_jpy_high_vix_bearish(self, client):
        """EUR_JPY도 GBP_JPY와 동일 로직 — VIX=25 → bearish."""
        _inject_cache(client, {"VIXCLS": 25.0, "DTWEXBGS": 119.0})
        bias, conf, reasons = await client.get_direction_bias(
            "eur_jpy", {"intermarket_bias_enabled": True}
        )
        assert bias == "bearish"

    @pytest.mark.asyncio
    async def test_unsupported_pair_neutral(self, client):
        """지원하지 않는 페어 → neutral + unsupported_pair."""
        _inject_cache(client, {"VIXCLS": 30.0, "DGS10": 4.5})
        bias, conf, reasons = await client.get_direction_bias(
            "btc_jpy", {"intermarket_bias_enabled": True}
        )
        assert bias == "neutral"
        assert "unsupported_pair" in reasons

    @pytest.mark.asyncio
    async def test_usd_jpy_neutral_zone(self, client):
        """T10Y2Y=0.05(neutral zone, 0.2 미만) + DGS10=3.5(낮지도 높지도 않음) → neutral."""
        _inject_cache(client, {"T10Y2Y": 0.05, "DGS10": 3.5})
        bias, conf, reasons = await client.get_direction_bias(
            "usd_jpy", {"intermarket_bias_enabled": True}
        )
        assert bias == "neutral"

    @pytest.mark.asyncio
    async def test_usd_jpy_no_data_neutral(self, client):
        """T10Y2Y, DGS10 모두 없음 → no_data → neutral."""
        _inject_cache(client, {"VIXCLS": 18.0})  # USD_JPY가 사용하지 않는 데이터만
        bias, conf, reasons = await client.get_direction_bias(
            "usd_jpy", {"intermarket_bias_enabled": True}
        )
        assert bias == "neutral"
        assert "no_data" in reasons

    @pytest.mark.asyncio
    async def test_usd_jpy_vix_stress_reduces_bullish(self, client):
        """bullish 신호 + VIX 스트레스 → score 감소."""
        # T10Y2Y=1.0 → score+1, DGS10=4.5 → score+0.5
        # VIX=28 > threshold=25 → score-0.5 → 최종 score=1.0, 여전히 bullish
        _inject_cache(client, {"T10Y2Y": 1.0, "DGS10": 4.5, "VIXCLS": 28.0})
        bias, conf, reasons = await client.get_direction_bias(
            "usd_jpy", {"intermarket_bias_enabled": True, "vix_stress_threshold": 25.0}
        )
        assert bias == "bullish"
        # VIX 스트레스 reason 포함 확인
        assert any("스트레스" in r for r in reasons)

    @pytest.mark.asyncio
    async def test_pair_lowercase_normalized(self, client):
        """페어 대소문자 무관 — USD_JPY도 usd_jpy와 동일하게 처리."""
        _inject_cache(client, {"T10Y2Y": 1.0, "DGS10": 4.5})
        bias_lower, _, _ = await client.get_direction_bias(
            "usd_jpy", {"intermarket_bias_enabled": True}
        )
        bias_upper, _, _ = await client.get_direction_bias(
            "USD_JPY", {"intermarket_bias_enabled": True}
        )
        assert bias_lower == bias_upper


# ── is_macro_stress ───────────────────────────────────────────

class TestIsMacroStress:
    @pytest.mark.asyncio
    async def test_vix_above_threshold_stress(self, client):
        """VIX=28 > threshold=25 → True."""
        _inject_cache(client, {"VIXCLS": 28.0})
        result = await client.is_macro_stress({"vix_stress_threshold": 25.0})
        assert result is True

    @pytest.mark.asyncio
    async def test_vix_below_threshold_no_stress(self, client):
        """VIX=20 < threshold=25 → False."""
        _inject_cache(client, {"VIXCLS": 20.0})
        result = await client.is_macro_stress({"vix_stress_threshold": 25.0})
        assert result is False

    @pytest.mark.asyncio
    async def test_vix_exactly_at_threshold_no_stress(self, client):
        """VIX=25 == threshold=25 → False (초과가 아니므로)."""
        _inject_cache(client, {"VIXCLS": 25.0})
        result = await client.is_macro_stress({"vix_stress_threshold": 25.0})
        assert result is False

    @pytest.mark.asyncio
    async def test_vix_missing_no_stress(self, client):
        """VIX 데이터 없음 → False (graceful)."""
        _inject_cache(client, {"DGS10": 4.0})
        result = await client.is_macro_stress({})
        assert result is False

    @pytest.mark.asyncio
    async def test_api_failure_no_stress(self, client):
        """API 실패 → False (graceful degradation)."""
        result = await client.is_macro_stress({})
        assert result is False

    @pytest.mark.asyncio
    async def test_custom_threshold(self, client):
        """커스텀 임계값 적용."""
        _inject_cache(client, {"VIXCLS": 20.0})
        assert await client.is_macro_stress({"vix_stress_threshold": 18.0}) is True
        assert await client.is_macro_stress({"vix_stress_threshold": 22.0}) is False


