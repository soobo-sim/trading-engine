"""
core/analysis/intermarket.py

FRED 매크로 지표 기반 방향 바이어스 + 스트레스 판단 (F-04 알파 팩터).

coinmarket-data(:8002)의 /api/intermarket/latest 엔드포인트를
1시간 TTL 인메모리 캐시로 조회하여:
  - 방향 바이어스: get_direction_bias(pair) → "bullish" | "bearish" | "neutral"
  - 매크로 스트레스: is_macro_stress() → bool

tick 루프(_entry_monitor) 안에서 호출되므로 캐시 필수.
API 호출 실패 = graceful degradation (neutral / 스트레스 아님으로 허용, 경고 로그).

바이어스 로직:
  USD_JPY:
    금리차(DGS10-DGS2) 상승추세 → bullish (달러 강세)
    금리차 하락추세 → bearish
  GBP_JPY / EUR_JPY:
    VIX > 20 → bearish (리스크 오프 → JPY 강세)
    달러 인덱스(DTWEXBGS) 상승추세 → USD 강세지만 JPY 크로스는 neutral
  공통:
    VIX > vix_stress_threshold → is_macro_stress() = True

추세 판단: days 이내 평균 대비 현재값 (단순 기울기)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 3600  # 1시간 (FRED 데이터는 일간 → 1시간 캐시 충분)
_TREND_DAYS = 5         # 5일 평균으로 추세 판단

# 페어별 바이어스에 사용할 주요 series
_PAIR_SERIES: dict[str, list[str]] = {
    "usd_jpy": ["DGS10", "DGS2", "T10Y2Y", "VIXCLS"],
    "gbp_jpy": ["VIXCLS", "DTWEXBGS"],
    "eur_jpy": ["VIXCLS", "DTWEXBGS"],
}


class IntermarketClient:
    """
    FRED 매크로 지표 조회 클라이언트.

    사용:
        client = IntermarketClient(trading_data_url="http://trading-data:8002")
        bias, conf, reasons = await client.get_direction_bias("usd_jpy", params)
        stressed = await client.is_macro_stress(params)
    """

    def __init__(self, trading_data_url: str) -> None:
        self._base_url = trading_data_url.rstrip("/")
        # (series_data, fetched_at)
        self._cache: Optional[tuple[dict, datetime]] = None

    # ── Public API ────────────────────────────────────────────

    async def get_direction_bias(
        self, pair: str, params: dict
    ) -> tuple[str, float, list[str]]:
        """
        페어의 방향 바이어스 판단.

        Returns:
            (bias, confidence, reasons)
            bias: "bullish" | "bearish" | "neutral"
            confidence: 0.0~1.0
            reasons: 판단 근거 목록 (로그용)
        """
        if not params.get("intermarket_bias_enabled", False):
            return "neutral", 0.0, ["disabled"]

        series = await self._get_latest()
        if series is None:
            return "neutral", 0.0, ["api_unavailable"]

        pair_lower = pair.lower()
        return self._calc_bias(pair_lower, series, params)

    async def is_macro_stress(self, params: dict) -> bool:
        """
        VIX 기반 매크로 스트레스 판단.
        VIX > vix_stress_threshold → True.
        API 실패 시 False (graceful degradation).
        """
        series = await self._get_latest()
        if series is None:
            return False

        vix = series.get("VIXCLS")
        if vix is None:
            return False

        threshold = float(params.get("vix_stress_threshold", 25.0))
        return vix > threshold

    # ── Internal — 바이어스 계산 ─────────────────────────────

    def _calc_bias(
        self, pair: str, series: dict, params: dict
    ) -> tuple[str, float, list[str]]:
        """페어별 바이어스 계산 로직."""
        reasons: list[str] = []
        score = 0.0  # +1 = bullish 신호, -1 = bearish 신호
        signals = 0

        vix = series.get("VIXCLS")

        if pair == "usd_jpy":
            # 금리 스프레드(10Y-2Y) 수준 → 양수면 bullish
            t10y2y = series.get("T10Y2Y")
            if t10y2y is not None:
                signals += 1
                if t10y2y > 0.2:
                    score += 1
                    reasons.append(f"수익률 커브 정상화(T10Y2Y={t10y2y:.2f}%) → USD 강세")
                elif t10y2y < -0.1:
                    score -= 1
                    reasons.append(f"역전된 수익률 커브(T10Y2Y={t10y2y:.2f}%) → USD 약세 위험")

            # 10Y 금리 수준
            dgs10 = series.get("DGS10")
            if dgs10 is not None:
                signals += 1
                if dgs10 > 4.0:
                    score += 0.5
                    reasons.append(f"미국 10Y 금리 높음({dgs10:.2f}%) → JPY 캐리 매력")
                elif dgs10 < 3.0:
                    score -= 0.5
                    reasons.append(f"미국 10Y 금리 낮음({dgs10:.2f}%) → 캐리 약화")

        elif pair in ("gbp_jpy", "eur_jpy"):
            # VIX 리스크 지표 → 높으면 JPY 강세 → bearish
            if vix is not None:
                signals += 1
                if vix > 20:
                    score -= 1
                    reasons.append(f"VIX 상승({vix:.1f}) → 리스크 오프 → JPY 강세 → bearish")
                elif vix < 15:
                    score += 0.5
                    reasons.append(f"VIX 낮음({vix:.1f}) → 리스크 온 → JPY 약세")

            # DXY 트렌드
            dxy = series.get("DTWEXBGS")
            if dxy is not None:
                signals += 1
                # DXY 강세 시 크로스 페어는 복합 효과 → neutral 유지
                reasons.append(f"DXY={dxy:.1f} (크로스 페어 영향 중립)")

        else:
            # 미지원 페어 → 바이어스 없음
            return "neutral", 0.0, ["unsupported_pair"]

        if signals == 0:
            return "neutral", 0.0, ["no_data"]

        # VIX 스트레스 공통 보정 — signals > 0인 경우에만 반영
        if vix is not None and vix > float(params.get("vix_stress_threshold", 25.0)):
            score -= 0.5
            reasons.append(f"VIX 스트레스({vix:.1f} > threshold) → 전반적 bearish")

        confidence = min(abs(score) / max(signals, 1), 1.0)
        if score > 0.3:
            bias = "bullish"
        elif score < -0.3:
            bias = "bearish"
        else:
            bias = "neutral"

        return bias, round(confidence, 2), reasons

    # ── Internal — 데이터 조회 ────────────────────────────────

    async def _get_latest(self) -> Optional[dict]:
        """
        coinmarket-data /api/intermarket/latest 에서 데이터 조회.
        1시간 TTL 캐시 적용. 실패 시 None 반환 (graceful degradation).
        """
        now = datetime.now(tz=timezone.utc)
        if self._cache is not None:
            data, fetched_at = self._cache
            if (now - fetched_at).total_seconds() < _CACHE_TTL_SEC:
                return data

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._base_url}/api/intermarket/latest")
                resp.raise_for_status()
                payload = resp.json()
            series = payload.get("series", {})
            # None 값 제거
            series = {k: v for k, v in series.items() if v is not None}
            self._cache = (series, now)
            return series
        except Exception as e:
            logger.warning(f"[IntermarketClient] API 조회 실패 (graceful): {e}")
            return None


def create_intermarket_client() -> IntermarketClient:
    """
    환경변수 TRADING_DATA_URL에서 URL 읽어 IntermarketClient 생성.
    (FRED_API_KEY 미설정이어도 클라이언트는 생성됨 — coinmarket-data가 graceful 처리)
    """
    url = os.environ.get("TRADING_DATA_URL", "http://trading-data:8002")
    return IntermarketClient(trading_data_url=url)
