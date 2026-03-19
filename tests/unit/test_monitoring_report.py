"""
테스트 — 모니터링 리포트 서비스 + 라우트.

서비스 레이어 함수(표시용, 텍스트 조립)와
FastAPI 라우트 통합 테스트.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from api.services.monitoring_report import (
    get_trend_icon,
    get_rsi_state,
    get_ema_state,
    get_volatility_state,
    get_market_summary,
    get_position_summary,
    get_entry_blockers,
    build_telegram_text,
    build_memory_block,
)


# ── 표시용 함수 테스트 ───────────────────────────────────

class TestGetTrendIcon:
    def test_bullish(self):
        assert get_trend_icon(0.15) == "📈"

    def test_bearish(self):
        assert get_trend_icon(-0.10) == "📉"

    def test_flat(self):
        assert get_trend_icon(0.02) == "➡️"

    def test_none(self):
        assert get_trend_icon(None) == "❓"


class TestGetRsiState:
    def test_oversold(self):
        assert "과매도" in get_rsi_state(25.0)
        assert "25.0" in get_rsi_state(25.0)

    def test_overbought(self):
        assert "과열" in get_rsi_state(75.0)

    def test_neutral(self):
        assert "중립" in get_rsi_state(50.0)

    def test_none(self):
        assert "없음" in get_rsi_state(None)


class TestGetEmaState:
    def test_above_ema_positive_slope(self):
        result = get_ema_state(110.0, 100.0, 0.5)
        assert "EMA 위" in result
        assert "↑" in result

    def test_below_ema_negative_slope(self):
        result = get_ema_state(90.0, 100.0, -0.3)
        assert "EMA 아래" in result
        assert "↓" in result

    def test_data_missing(self):
        result = get_ema_state(100.0, None, None)
        assert "부족" in result


class TestGetVolatilityState:
    def test_high(self):
        assert "높음" in get_volatility_state(3.5)

    def test_medium(self):
        assert "보통" in get_volatility_state(2.0)

    def test_low(self):
        assert "낮음" in get_volatility_state(1.0)

    def test_none(self):
        assert "불명" in get_volatility_state(None)


class TestGetMarketSummary:
    def test_exit_warning(self):
        result = get_market_summary(-0.2, 40.0, "exit_warning")
        assert "하락" in result

    def test_entry_ready(self):
        result = get_market_summary(0.15, 50.0, "entry_ok")
        assert "진입" in result

    def test_pullback_wait(self):
        result = get_market_summary(0.05, 35.0, "no_signal")
        assert "대기" in result

    def test_trend_weakening(self):
        result = get_market_summary(-0.05, 50.0, "no_signal")
        assert "약화" in result

    def test_crash(self):
        result = get_market_summary(-0.2, 25.0, "no_signal")
        assert "급락" in result

    def test_downtrend(self):
        result = get_market_summary(-0.2, 50.0, "no_signal")
        assert "하락" in result

    def test_none_values(self):
        result = get_market_summary(None, None, "no_signal")
        assert "부족" in result


class TestGetPositionSummary:
    def test_full_exit(self):
        result = get_position_summary({"action": "full_exit"}, 50.0, 1.0)
        assert "청산" in result

    def test_tighten_stop(self):
        result = get_position_summary({"action": "tighten_stop"}, 50.0, 1.0)
        assert "타이트닝" in result

    def test_profitable(self):
        result = get_position_summary({"action": "hold"}, 55.0, 3.0)
        assert "수익" in result

    def test_hold(self):
        result = get_position_summary({"action": "hold"}, 50.0, -0.5)
        assert "관찰" in result


# ── entry_blockers 테스트 ────────────────────────────────

class TestGetEntryBlockers:
    def test_all_blockers(self):
        blockers = get_entry_blockers(
            signal="exit_warning",
            current_price=90.0,
            ema=100.0,
            ema_slope_pct=-0.15,
            rsi=25.0,
        )
        assert len(blockers) == 3
        assert any("양수 전환" in b for b in blockers)
        assert any("갭" in b for b in blockers)
        assert any("breakdown" in b for b in blockers)

    def test_rsi_too_high(self):
        blockers = get_entry_blockers(
            signal="wait_dip",
            current_price=110.0,
            ema=100.0,
            ema_slope_pct=0.2,
            rsi=70.0,
        )
        assert len(blockers) == 1
        assert "과열" in blockers[0]

    def test_no_blockers(self):
        blockers = get_entry_blockers(
            signal="entry_ok",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.15,
            rsi=50.0,
        )
        assert blockers == []

    def test_none_values(self):
        blockers = get_entry_blockers(
            signal="no_signal",
            current_price=100.0,
            ema=None,
            ema_slope_pct=None,
            rsi=None,
        )
        assert blockers == []


# ── 텔레그램 텍스트 조립 테스트 ──────────────────────────

class TestBuildTelegramText:
    def test_no_position_with_blockers(self):
        data = {
            "trend_icon": "📉",
            "current_price": 232.49,
            "market_summary": "🔻 하락 전환",
            "ema_state": "EMA 아래 -0.14%",
            "rsi_state": "RSI 과매도(31.5)",
            "volatility_state": "변동성 높음",
            "position": None,
            "entry_blockers": ["EMA slope -0.14% → 양수 전환 필요"],
            "jpy_available": 19468,
        }
        text = build_telegram_text("CK", "21:01", "xrp_jpy", data)
        assert "[CK] 21:01" in text
        assert "📉추세추종" in text
        assert "🔻 하락" in text
        assert "🚫" in text
        assert "대기중" in text

    def test_no_position_entry_ready(self):
        data = {
            "trend_icon": "📈",
            "current_price": 250.0,
            "market_summary": "✅ 진입 임박",
            "ema_state": "EMA 위 +0.15%",
            "rsi_state": "RSI 중립(50.0)",
            "volatility_state": "변동성 보통",
            "position": None,
            "entry_blockers": [],
            "jpy_available": 50000,
        }
        text = build_telegram_text("CK", "15:00", "xrp_jpy", data)
        assert "✅ 진입 조건 충족" in text

    def test_with_position(self):
        data = {
            "trend_icon": "📈",
            "current_price": 11800000,
            "position_summary": "상승추세·보유 유지",
            "ema_state": "EMA 위 +1.5%",
            "rsi_state": "RSI 중립(55.3)",
            "volatility_state": "변동성 보통",
            "position": {
                "entry_price": 11631190,
                "entry_amount": 0.003,
                "stop_loss_price": 11463739,
                "trailing_stop_distance": 336261,
                "unrealized_pnl_jpy": 506,
                "unrealized_pnl_pct": 0.87,
            },
            "entry_blockers": [],
            "jpy_available": 10000,
        }
        text = build_telegram_text("BF", "21:01", "BTC_JPY", data)
        assert "[BF] 21:01" in text
        assert "손절" in text
        assert "보유" in text
        assert "BTC" in text
        assert "미실현" in text


# ── 메모리 블록 조립 테스트 ──────────────────────────────

class TestBuildMemoryBlock:
    def test_no_position(self):
        data = {
            "signal": "exit_warning",
            "current_price": 232.49,
            "ema_state": "EMA 아래 -0.14%",
            "rsi_state": "RSI 과매도(31.5)",
            "volatility_state": "변동성 높음",
            "ema20": 235.53,
            "position": None,
            "entry_blockers": ["EMA slope -0.14% → 양수 전환 필요"],
            "jpy_available": 19468,
            "coin_available": 0.0,
            "strategy_name": "XRP 추세추종 v3",
            "strategy_id": 22,
        }
        block = build_memory_block("CK", "21:01", "xrp_jpy", data)
        assert "CK" in block
        assert "모니터링" in block
        assert "포지션 없음" in block
        assert "진입 차단" in block
        assert "JPY" in block

    def test_with_position(self):
        data = {
            "signal": "hold",
            "current_price": 11800000,
            "ema_state": "EMA 위 +1.5%",
            "rsi_state": "RSI 중립(55.3)",
            "volatility_state": "변동성 보통",
            "ema20": 11650000,
            "position": {
                "entry_price": 11631190,
                "entry_amount": 0.003,
                "stop_loss_price": 11463739,
                "trailing_stop_distance": 336261,
                "unrealized_pnl_jpy": 506,
                "unrealized_pnl_pct": 0.87,
            },
            "entry_blockers": [],
            "jpy_available": 70000,
            "coin_available": 0.003,
            "strategy_name": "BTC 추세추종 v2",
            "strategy_id": 7,
        }
        block = build_memory_block("BF", "21:01", "BTC_JPY", data)
        assert "BF" in block
        assert "보유" in block
        assert "손절" in block
        assert "미실현" in block
