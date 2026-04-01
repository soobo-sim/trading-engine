"""
테스트 — 모니터링 리포트 서비스 + 라우트.

서비스 레이어 함수(표시용, 텍스트 조립)와
FastAPI 라우트 통합 테스트.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import httpx

from api.services.monitoring import (
    get_trend_icon,
    get_rsi_state,
    get_ema_state,
    get_volatility_state,
    get_market_summary,
    get_position_summary,
    get_entry_blockers,
    build_telegram_text,
    build_memory_block,
    build_bar_chart,
    build_health_line,
    get_box_position_label,
    build_box_telegram_text,
    build_box_memory_block,
    evaluate_alert,
    _is_regime_shift,
    build_alert_text,
    _build_test_alert,
    _prev_raw_cache,
    _trigger_rachel_analysis,
    _last_alert_time,
    ALERT_COOLDOWN_SEC,
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
        assert any("slope" in b.lower() for b in blockers)
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

    def test_wait_regime_is_blocked(self):
        """wait_regime 시그널이면 횡보 레짐 blocker가 추가되어야 한다."""
        blockers = get_entry_blockers(
            signal="wait_regime",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.15,
            rsi=50.0,
        )
        assert len(blockers) == 1
        assert "횡보 레짐" in blockers[0]
        assert "BB폭" in blockers[0]

    def test_entry_ok_signal_no_regime_blocker(self):
        """entry_ok 시그널이면 레짐 blocker 없어야 한다."""
        blockers = get_entry_blockers(
            signal="entry_ok",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.15,
            rsi=50.0,
        )
        assert not any("레짐" in b for b in blockers)

    def test_custom_rsi_min_honored(self):
        """전략별 RSI 하한(entry_rsi_min=45)이 반영되어야 한다."""
        blockers = get_entry_blockers(
            signal="entry_ok",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.15,
            rsi=43.0,
            rsi_min=45.0,
        )
        assert len(blockers) == 1
        assert "43.0" in blockers[0]
        assert "45" in blockers[0]

    def test_custom_rsi_max_honored(self):
        """GMO FX 전략 RSI 상한(entry_rsi_max=60)이 반영되어야 한다."""
        blockers = get_entry_blockers(
            signal="wait_dip",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.15,
            rsi=62.0,
            rsi_max=60.0,
        )
        assert len(blockers) == 1
        assert "62.0" in blockers[0]
        assert "60" in blockers[0]
        assert "과열" in blockers[0]

    def test_default_rsi_65_no_block_at_63(self):
        """기본 rsi_max=65일 때 RSI 63은 block 없음."""
        blockers = get_entry_blockers(
            signal="entry_ok",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.15,
            rsi=63.0,
        )
        assert blockers == []

    def test_custom_rsi_max_60_blocks_at_63(self):
        """rsi_max=60일 때 RSI 63은 block되어야 한다."""
        blockers = get_entry_blockers(
            signal="entry_ok",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.15,
            rsi=63.0,
            rsi_max=60.0,
        )
        assert len(blockers) == 1
        assert "63.0" in blockers[0]

    def test_wait_regime_combined_with_other_blockers(self):
        """wait_regime + RSI 과열 두 blocker 동시에."""
        blockers = get_entry_blockers(
            signal="wait_regime",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.15,
            rsi=70.0,
        )
        assert len(blockers) == 2
        assert any("레짐" in b for b in blockers)
        assert any("과열" in b for b in blockers)

    def test_slope_min_custom(self):
        """전략별 slope_min이 반영되어야 한다."""
        blockers = get_entry_blockers(
            signal="entry_ok",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.05,
            rsi=50.0,
            slope_min=0.1,
        )
        assert len(blockers) == 1
        assert "slope" in blockers[0].lower()
        assert "0.10" in blockers[0]

    def test_slope_min_negative_allowed(self):
        """slope_min=-0.05일 때 slope -0.03은 통과."""
        blockers = get_entry_blockers(
            signal="entry_ok",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=-0.03,
            rsi=50.0,
            slope_min=-0.05,
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
            "entry_blockers": ["EMA slope -0.14% → ≥+0.00% 필요"],
            "conditions_met": 4,
            "conditions_total": 5,
            "jpy_available": 19468,
        }
        text = build_telegram_text("CK", "21:01", "xrp_jpy", data)
        assert "[CK] 21:01" in text
        assert "📉추세추종" in text
        assert "🔻 하락" in text
        assert "🚫 4/5" in text
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
            "conditions_met": 5,
            "conditions_total": 5,
            "jpy_available": 50000,
        }
        text = build_telegram_text("CK", "15:00", "xrp_jpy", data)
        assert "✅ 5/5 진입 조건 충족" in text

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


# ══════════════════════════════════════════════════════════════
#  박스 전략 리포트 테스트
# ══════════════════════════════════════════════════════════════


class TestBuildBarChart:
    def test_price_below_box(self):
        assert build_bar_chart(30.0, 34.0, 36.0) == "●[━━━━━━━━━━]"

    def test_price_above_box(self):
        assert build_bar_chart(40.0, 34.0, 36.0) == "[━━━━━━━━━━]●"

    def test_price_at_lower(self):
        result = build_bar_chart(34.0, 34.0, 36.0)
        assert result.startswith("[●")

    def test_price_at_upper(self):
        result = build_bar_chart(36.0, 34.0, 36.0)
        assert result.endswith("●]")

    def test_price_at_middle(self):
        result = build_bar_chart(35.0, 34.0, 36.0)
        assert "●" in result
        assert result.startswith("[")
        assert result.endswith("]")


class TestBuildHealthLine:
    def test_all_healthy(self):
        report = SimpleNamespace(
            healthy=True,
            ws_connected=True,
            tasks={
                "box_monitor": {"alive": True, "restarts": 0},
                "entry_monitor": {"alive": True, "restarts": 0},
                "candle_monitor": {"alive": True, "restarts": 0},
            },
            position_balance=[],
        )
        result = build_health_line(report)
        assert "🟢" in result
        assert "WS✅" in result
        assert "3/3✅" in result
        assert "잔고✅" in result

    def test_ws_disconnected(self):
        report = SimpleNamespace(
            healthy=False,
            ws_connected=False,
            tasks={"t1": {"alive": True, "restarts": 0}},
            position_balance=[],
        )
        result = build_health_line(report)
        assert "🚨" in result
        assert "WS🔴" in result

    def test_task_dead_with_restarts(self):
        report = SimpleNamespace(
            healthy=False,
            ws_connected=True,
            tasks={
                "t1": {"alive": True, "restarts": 2},
                "t2": {"alive": False, "restarts": 1},
            },
            position_balance=[],
        )
        result = build_health_line(report)
        assert "1/2⚠️" in result
        assert "재시작3" in result

    def test_balance_issue(self):
        report = SimpleNamespace(
            healthy=True,
            ws_connected=True,
            tasks={"t1": {"alive": True, "restarts": 0}},
            position_balance=[{"currency": "xrp", "expected": 100, "actual": 90}],
        )
        result = build_health_line(report)
        assert "🚨" in result
        assert "잔고⚠️" in result


class TestGetBoxPositionLabel:
    def test_near_lower(self):
        assert get_box_position_label(34.15, 34.0, 36.0, 1.0) == "near_lower"

    def test_near_upper(self):
        assert get_box_position_label(35.85, 34.0, 36.0, 1.0) == "near_upper"

    def test_middle(self):
        assert get_box_position_label(35.0, 34.0, 36.0, 1.0) == "middle"

    def test_outside(self):
        assert get_box_position_label(40.0, 34.0, 36.0, 1.0) == "outside"


class TestBuildBoxTelegramText:
    def test_with_box_and_position(self):
        data = {
            "health_line": "🟢 WS✅ 태스크3/3✅ 잔고✅",
            "current_price": 35.50,
            "box": {
                "id": 5,
                "upper_bound": 35.60,
                "lower_bound": 34.60,
                "box_width_pct": 2.8,
                "bar_chart": "[━━━━━●━━━━━]",
            },
            "position_label": "middle",
            "position": {
                "entry_price": 34.80,
                "entry_amount": 100.0,
                "unrealized_pnl_jpy": 70,
                "unrealized_pnl_pct": 2.01,
            },
            "jpy_available": 5000,
            "coin_available": 100.0,
            "basis_timeframe": "4h",
            "candle_open_time_jst": "20:00",
        }
        text = build_box_telegram_text("CK", "22:30", "xrp_jpy", data)
        assert "[CK] 22:30" in text
        assert "📦박스" in text
        assert "🟢" in text
        assert "middle" in text
        assert "폭 2.8%" in text
        assert "하단" in text
        assert "상단" in text
        assert "보유" in text
        assert "미실현" in text

    def test_no_box(self):
        data = {
            "health_line": "🟢 WS✅ 태스크2/2✅ 잔고✅",
            "current_price": 35.50,
            "box": None,
            "position_label": "no_box",
            "position": None,
            "jpy_available": 5000,
            "coin_available": 0.0,
            "basis_timeframe": "4h",
            "candle_open_time_jst": "20:00",
        }
        text = build_box_telegram_text("CK", "22:30", "xrp_jpy", data)
        assert "📭박스 미형성" in text
        assert "포지션 미보유" in text

    def test_box_no_position(self):
        data = {
            "health_line": "🟢 WS✅ 태스크3/3✅ 잔고✅",
            "current_price": 34.65,
            "box": {
                "id": 5,
                "upper_bound": 35.60,
                "lower_bound": 34.60,
                "box_width_pct": 2.8,
                "bar_chart": "[●━━━━━━━━━━]",
            },
            "position_label": "near_lower",
            "position": None,
            "jpy_available": 10000,
            "coin_available": 0.0,
            "basis_timeframe": "4h",
            "candle_open_time_jst": "20:00",
        }
        text = build_box_telegram_text("CK", "22:30", "xrp_jpy", data)
        assert "near_lower" in text
        assert "포지션 미보유" in text


class TestBuildBoxMemoryBlock:
    def test_with_box_and_position(self):
        data = {
            "health_line": "🟢 WS✅ 태스크3/3✅ 잔고✅",
            "current_price": 35.50,
            "box": {
                "id": 5,
                "upper_bound": 35.60,
                "lower_bound": 34.60,
                "box_width_pct": 2.8,
            },
            "position_label": "middle",
            "position": {
                "entry_price": 34.80,
                "entry_amount": 100.0,
                "unrealized_pnl_jpy": 70,
                "unrealized_pnl_pct": 2.01,
            },
            "jpy_available": 5000,
            "coin_available": 100.0,
            "strategy_name": "XRP 박스권 v1",
            "strategy_id": 19,
        }
        block = build_box_memory_block("CK", "22:30", "xrp_jpy", data)
        assert "CK" in block
        assert "모니터링" in block
        assert "XRP 박스권 v1" in block
        assert "box_id: 5" in block
        assert "보유" in block
        assert "미실현" in block
        assert "JPY" in block

    def test_no_box(self):
        data = {
            "health_line": "🟢 WS✅ 태스크2/2✅ 잔고✅",
            "current_price": 35.50,
            "box": None,
            "position_label": "no_box",
            "position": None,
            "jpy_available": 5000,
            "coin_available": 0.0,
            "strategy_name": "XRP 박스권 v1",
            "strategy_id": 19,
        }
        block = build_box_memory_block("CK", "22:30", "xrp_jpy", data)
        assert "박스 미형성" in block
        assert "포지션 없음" in block


# ══════════════════════════════════════════════════════════════
#  T-RPT: 보고 모드 분기 테스트 (P0.7)
# ══════════════════════════════════════════════════════════════


class TestBoxTelegramTextModes:
    """T-RPT-01~05: 포지션 유무 / FX / SL 근접 분기 테스트."""

    def _box(self):
        return {
            "id": 7,
            "upper_bound": 212.0,
            "lower_bound": 208.0,
            "box_width_pct": 1.9,
            "bar_chart": "[━━━━●━━━━━━]",
        }

    def _pos(self, entry_price: float = 208.5, pnl_jpy: float = 150.0, pnl_pct: float = 0.07):
        return {
            "entry_price": entry_price,
            "entry_amount": 1000.0,
            "unrealized_pnl_jpy": pnl_jpy,
            "unrealized_pnl_pct": pnl_pct,
        }

    def _base_data(self, **overrides):
        data = {
            "health_line": "🟢 WS✅ 태스크3/3✅ 잔고✅",
            "current_price": 209.5,
            "box": self._box(),
            "position_label": "middle",
            "position": None,
            "jpy_available": 100000,
            "coin_available": 0.0,
            "basis_timeframe": "4h",
            "candle_open_time_jst": "20:00",
            "near_bound_pct": 1.5,
            "tolerance_pct": 1.5,
            "stop_loss_pct": 1.5,
            "is_margin_trading": False,
            "conditions_met": 1,
            "conditions_total": 3,
            "entry_blockers": ["중심부 → 하한 진입대 대기"],
        }
        data.update(overrides)
        return data

    def test_rpt01_no_position_shows_entry_conditions(self):
        """T-RPT-01: 포지션 없음 → 진입 조건 행 + 다음 4h봉 행 표시."""
        data = self._base_data()
        text = build_box_telegram_text("GMO", "22:58", "gbp_jpy", data)
        assert "🚫" in text or "✅" in text, "진입 조건 행 있어야 함"
        assert "⏰ 다음 4h봉" in text, "다음 4h봉 행 있어야 함"
        assert "포지션 미보유" in text

    def test_rpt02_has_position_shows_exit_not_entry(self):
        """T-RPT-02: 포지션 있음 → 익절/손절 표시 + 진입 조건/다음캔들 미표시."""
        data = self._base_data(position=self._pos())
        text = build_box_telegram_text("GMO", "22:58", "gbp_jpy", data)
        assert "🎯 익절" in text, "익절 행 있어야 함"
        assert "🛑 손절" in text, "손절 행 있어야 함"
        assert "📈 보유" in text, "보유 행 있어야 함"
        assert "미실현" in text
        # 진입 조건 행과 다음 캔들 행은 표시 안 됨
        assert "🚫" not in text, "포지션 있을 때 진입 조건 행 미표시"
        assert "⏰ 다음 4h봉" not in text, "포지션 있을 때 다음 캔들 행 미표시"

    def test_rpt03_fx_hides_coin_line(self):
        """T-RPT-03: FX(is_margin_trading=True) → coin 잔고 미표시."""
        data = self._base_data(is_margin_trading=True, coin_available=0.0)
        text = build_box_telegram_text("GMO", "22:58", "gbp_jpy", data)
        assert "gbp 0.00개" not in text, "FX에서 통화 현물 잔고 미표시"
        assert "JPY" in text, "JPY 잔고는 표시"

    def test_rpt04_has_position_tp_range_calculation(self):
        """T-RPT-04: 포지션 있음 + near_upper 근접 → 익절 가격 범위 표시."""
        # upper=212.0, near_bound_pct=1.5
        # tp_low = 212.0 * 0.985 = 208.82, tp_high = 212.0 * 1.015 = 215.18
        data = self._base_data(
            current_price=209.0,
            position=self._pos(entry_price=208.5),
            near_bound_pct=1.5,
        )
        text = build_box_telegram_text("GMO", "22:58", "gbp_jpy", data)
        assert "🎯 익절: near_upper" in text
        # 익절 가격 숫자 확인 (¥208.82 형식으로 반올림 차이 허용)
        assert "¥208." in text or "¥209." in text, f"익절 하단 가격 표시 확인: {text}"

    def test_rpt05_has_position_sl_price_in_text(self):
        """T-RPT-05: 포지션 있음 → 손절 가격 (박스 무효화 + 가격SL) 표시."""
        # entry=208.5, sl_pct=1.5 → sl=208.5*0.985=205.37
        # lower=208.0, tol=1.5 → inv=208.0*0.985=204.88
        data = self._base_data(
            current_price=208.1,
            position=self._pos(entry_price=208.5),
            stop_loss_pct=1.5,
            tolerance_pct=1.5,
        )
        text = build_box_telegram_text("GMO", "22:58", "gbp_jpy", data)
        assert "🛑 손절" in text
        assert "박스 무효화" in text
        assert "가격SL" in text
        assert "-1.5%" in text, "SL 퍼센트 표시 확인"


# ══════════════════════════════════════════════════════════════
#  Alert 평가 테스트
# ══════════════════════════════════════════════════════════════


class TestIsRegimeShift:
    def test_bullish_to_bearish(self):
        assert _is_regime_shift("entry_ok", "exit_warning") is True

    def test_wait_dip_to_bearish(self):
        assert _is_regime_shift("wait_dip", "exit_warning") is True

    def test_bearish_to_bullish(self):
        assert _is_regime_shift("exit_warning", "entry_ok") is True

    def test_same_direction(self):
        assert _is_regime_shift("entry_ok", "wait_dip") is False

    def test_no_signal(self):
        assert _is_regime_shift("no_signal", "entry_ok") is False

    def test_wait_regime_not_shift(self):
        assert _is_regime_shift("wait_regime", "exit_warning") is False


class TestEvaluateAlert:
    """evaluate_alert 함수의 각 트리거 조건 테스트."""

    def _base_raw(self, **overrides):
        raw = {
            "pair": "xrp_jpy",
            "current_price": 35.0,
            "rsi14": 50.0,
            "ema20": 34.5,
            "ema_slope_pct": 0.1,
            "candle_change_pct": 0.5,
            "candle_1h_change_pct": 0.3,
            "signal": "wait_dip",
            "position": None,
        }
        raw.update(overrides)
        return raw

    # --- None (평상시) ---

    def test_no_alert_normal_conditions(self):
        raw = self._base_raw()
        assert evaluate_alert(raw) is None

    # --- Critical: RSI 극단 ---

    def test_rsi_extreme_low(self):
        raw = self._base_raw(rsi14=15.0)
        alert = evaluate_alert(raw)
        assert alert is not None
        assert alert["level"] == "critical"
        assert "rsi_extreme_low" in alert["triggers"]
        assert "극단 과매도" in alert["text"]

    def test_rsi_extreme_high(self):
        raw = self._base_raw(rsi14=90.0)
        alert = evaluate_alert(raw)
        assert alert is not None
        assert alert["level"] == "critical"
        assert "rsi_extreme_high" in alert["triggers"]
        assert "극단 과열" in alert["text"]

    # --- Critical: 15분 급락/급등 ---

    def test_price_crash_15m(self):
        """15분 변동률 -3% 초과 → critical."""
        prev = self._base_raw(current_price=36.0)
        raw = self._base_raw(current_price=34.8)  # -3.33%
        alert = evaluate_alert(raw, prev)
        assert alert["level"] == "critical"
        assert "price_crash_15m" in alert["triggers"]
        assert "초급락" in alert["text"]

    def test_price_surge_15m(self):
        """15분 변동률 +3% 초과 → critical."""
        prev = self._base_raw(current_price=34.0)
        raw = self._base_raw(current_price=35.1)  # +3.24%
        alert = evaluate_alert(raw, prev)
        assert alert["level"] == "critical"
        assert "price_surge_15m" in alert["triggers"]
        assert "초급등" in alert["text"]

    def test_no_15m_alert_without_prev_raw(self):
        """prev_raw가 없으면 15분 트리거 안 뜨."""
        raw = self._base_raw()
        assert evaluate_alert(raw, None) is None

    def test_15m_within_normal_no_alert(self):
        """15분 변동률 ±1.5% 이내 → 알림 없음."""
        prev = self._base_raw(current_price=35.0)
        raw = self._base_raw(current_price=35.3)  # +0.86%
        assert evaluate_alert(raw, prev) is None

    # --- Critical: 1H 급락/급등 ---

    def test_price_crash_1h(self):
        raw = self._base_raw(candle_1h_change_pct=-6.5)
        alert = evaluate_alert(raw)
        assert alert["level"] == "critical"
        assert "price_crash_1h" in alert["triggers"]
        assert "급락" in alert["text"]

    def test_price_surge_1h(self):
        raw = self._base_raw(candle_1h_change_pct=5.5)
        alert = evaluate_alert(raw)
        assert alert["level"] == "critical"
        assert "price_surge_1h" in alert["triggers"]
        assert "급등" in alert["text"]

    # --- Critical: 포지션 위험 ---

    def test_position_at_risk(self):
        pos = {"unrealized_pnl_pct": -4.5, "entry_price": 36.0, "entry_amount": 100}
        raw = self._base_raw(position=pos)
        alert = evaluate_alert(raw)
        assert alert["level"] == "critical"
        assert "position_at_risk" in alert["triggers"]
        assert "-4.5% 손실" in alert["text"]

    def test_position_small_loss_no_alert(self):
        pos = {"unrealized_pnl_pct": -2.0}
        raw = self._base_raw(position=pos)
        assert evaluate_alert(raw) is None

    # --- Critical: 체제 전환 ---

    def test_regime_shift(self):
        prev = self._base_raw(signal="entry_ok")
        raw = self._base_raw(signal="exit_warning")
        alert = evaluate_alert(raw, prev)
        assert alert["level"] == "critical"
        assert "regime_shift" in alert["triggers"]
        assert "entry_ok → exit_warning" in alert["text"]

    def test_no_regime_shift_same_direction(self):
        prev = self._base_raw(signal="entry_ok")
        raw = self._base_raw(signal="wait_dip")
        assert evaluate_alert(raw, prev) is None

    # --- Warning: RSI 경고 ---

    def test_rsi_low_warning(self):
        raw = self._base_raw(rsi14=23.0)
        alert = evaluate_alert(raw)
        assert alert is not None
        assert alert["level"] == "warning"
        assert "rsi_low" in alert["triggers"]

    def test_rsi_high_warning(self):
        raw = self._base_raw(rsi14=82.0)
        alert = evaluate_alert(raw)
        assert alert["level"] == "warning"
        assert "rsi_high" in alert["triggers"]

    def test_rsi_extreme_low_excludes_warning(self):
        """RSI < 20이면 critical만 뜨고 warning rsi_low는 안 뜸."""
        raw = self._base_raw(rsi14=18.0)
        alert = evaluate_alert(raw)
        assert "rsi_extreme_low" in alert["triggers"]
        assert "rsi_low" not in alert["triggers"]

    def test_rsi_extreme_high_excludes_warning(self):
        """RSI > 85이면 critical만 뜨고 warning rsi_high는 안 뜸."""
        raw = self._base_raw(rsi14=88.0)
        alert = evaluate_alert(raw)
        assert "rsi_extreme_high" in alert["triggers"]
        assert "rsi_high" not in alert["triggers"]

    # --- Warning: 15분 변동성 ---

    def test_high_volatility_15m_warning(self):
        """±1.5~3% 15분 변동 → warning."""
        prev = self._base_raw(current_price=35.0)
        raw = self._base_raw(current_price=35.7)  # +2.0%
        alert = evaluate_alert(raw, prev)
        assert alert["level"] == "warning"
        assert "high_volatility_15m" in alert["triggers"]

    def test_high_volatility_15m_negative(self):
        """±1.5~3% 음수 변동 → warning."""
        prev = self._base_raw(current_price=35.0)
        raw = self._base_raw(current_price=34.3)  # -2.0%
        alert = evaluate_alert(raw, prev)
        assert "high_volatility_15m" in alert["triggers"]

    # --- Warning: 1H 변동성 ---

    def test_high_volatility_1h_warning(self):
        raw = self._base_raw(candle_1h_change_pct=-4.0)
        alert = evaluate_alert(raw)
        assert alert["level"] == "warning"
        assert "high_volatility_1h" in alert["triggers"]

    def test_high_volatility_1h_positive(self):
        raw = self._base_raw(candle_1h_change_pct=3.5)
        alert = evaluate_alert(raw)
        assert "high_volatility_1h" in alert["triggers"]

    def test_no_volatility_1h_alert_within_normal(self):
        """3% 이하면 1H 변동성 경고 안 뜸."""
        raw = self._base_raw(candle_1h_change_pct=2.9)
        assert evaluate_alert(raw) is None

    # --- Warning: EMA 갭 ---

    def test_large_ema_gap(self):
        raw = self._base_raw(current_price=30.0, ema20=35.0)
        alert = evaluate_alert(raw)
        assert alert is not None
        assert "large_ema_gap" in alert["triggers"]

    def test_small_ema_gap_no_alert(self):
        raw = self._base_raw(current_price=35.0, ema20=34.8)
        assert evaluate_alert(raw) is None

    # --- Warning: slope 전환 ---

    def test_slope_reversal_positive_to_negative(self):
        prev = self._base_raw(ema_slope_pct=0.15)
        raw = self._base_raw(ema_slope_pct=-0.10)
        alert = evaluate_alert(raw, prev)
        assert alert is not None
        assert "slope_reversal" in alert["triggers"]

    def test_slope_reversal_negative_to_positive(self):
        prev = self._base_raw(ema_slope_pct=-0.20)
        raw = self._base_raw(ema_slope_pct=0.05)
        alert = evaluate_alert(raw, prev)
        assert "slope_reversal" in alert["triggers"]

    def test_slope_same_sign_no_alert(self):
        prev = self._base_raw(ema_slope_pct=0.10)
        raw = self._base_raw(ema_slope_pct=0.05)
        assert evaluate_alert(raw, prev) is None

    # --- 복합 트리거 ---

    def test_multiple_critical_triggers(self):
        raw = self._base_raw(rsi14=15.0, candle_1h_change_pct=-8.0)
        alert = evaluate_alert(raw)
        assert alert["level"] == "critical"
        assert "rsi_extreme_low" in alert["triggers"]
        assert "price_crash_1h" in alert["triggers"]
        assert "🚨🚨🚨" in alert["text"]

    def test_warning_level_with_multiple_warnings(self):
        raw = self._base_raw(rsi14=23.0, candle_1h_change_pct=-3.5)
        alert = evaluate_alert(raw)
        assert alert["level"] == "warning"
        assert len(alert["triggers"]) >= 2

    def test_critical_overrides_warning(self):
        """critical + warning 동시 발생 시 level은 critical."""
        raw = self._base_raw(rsi14=18.0, candle_1h_change_pct=-3.5)
        alert = evaluate_alert(raw)
        assert alert["level"] == "critical"

    # --- Box 전략 (RSI/EMA 없음) ---

    def test_box_no_rsi_no_ema(self):
        raw = {
            "pair": "xrp_jpy",
            "current_price": 35.0,
            "candle_change_pct": 1.0,
            "position": None,
        }
        assert evaluate_alert(raw) is None

    def test_box_position_at_risk(self):
        raw = {
            "pair": "xrp_jpy",
            "current_price": 33.0,
            "candle_change_pct": 0.5,
            "position": {"unrealized_pnl_pct": -5.0},
        }
        alert = evaluate_alert(raw)
        assert alert["level"] == "critical"
        assert "position_at_risk" in alert["triggers"]

    # --- prev_raw None ---

    def test_no_prev_raw_no_regime_shift(self):
        """prev_raw이 None이면 regime_shift/slope_reversal는 안 뜸."""
        raw = self._base_raw(signal="exit_warning")
        alert = evaluate_alert(raw, None)
        # exit_warning 자체로는 critical 아님 (regime_shift 없이)
        assert alert is None or "regime_shift" not in alert.get("triggers", [])


class TestBuildAlertText:
    """build_alert_text 함수 테스트."""

    def test_critical_text_ck(self):
        raw = {"pair": "xrp_jpy", "current_price": 35.0}
        triggers = [
            ("critical", "rsi_extreme_low", "RSI 15.0 극단 과매도"),
            ("critical", "price_crash_1h", "1H -8.0% 급락"),
        ]
        text = build_alert_text(raw, triggers, "critical")
        assert "🚨🚨🚨" in text
        assert "[CK 긴급]" in text
        assert "xrp_jpy" in text
        assert "RSI 15.0 극단 과매도" in text
        assert "레이첼 심층분석" in text

    def test_critical_text_bf(self):
        raw = {"pair": "BTC_JPY", "current_price": 10200000}
        triggers = [("critical", "price_crash_1h", "1H -6.0% 급락")]
        text = build_alert_text(raw, triggers, "critical")
        assert "[BF 긴급]" in text
        assert "BTC_JPY" in text

    def test_warning_text(self):
        raw = {"pair": "xrp_jpy", "current_price": 35.0}
        triggers = [("warning", "rsi_low", "RSI 23.0 과매도")]
        text = build_alert_text(raw, triggers, "warning")
        assert "⚠️" in text
        assert "[CK 주의]" in text
        assert "RSI 23.0 과매도" in text
        assert "🚨" not in text


# ── 레이첼 Webhook 트리거 테스트 ─────────────────────────

class TestTriggerRachelAnalysis:
    """_trigger_rachel_analysis webhook 호출 + 쿨다운 테스트."""

    @pytest.fixture(autouse=True)
    def _clear_cooldown(self):
        _last_alert_time.clear()
        yield
        _last_alert_time.clear()

    @pytest.mark.asyncio
    async def test_critical_triggers_webhook(self, monkeypatch):
        """토큰 설정 + critical alert → webhook POST 호출."""
        monkeypatch.setattr("api.services.monitoring.alerts.RACHEL_WEBHOOK_TOKEN", "test-token")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "ok"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        alert = {
            "level": "critical",
            "triggers": ["rsi_extreme_low", "price_crash"],
            "text": "🚨🚨🚨 [CK 긴급] xrp_jpy\n¥35\nRSI 15.0 극단 과매도",
        }

        with patch("api.services.monitoring.alerts.httpx.AsyncClient", return_value=mock_client):
            await _trigger_rachel_analysis("xrp_jpy", alert)

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "hooks/market-alert" in call_kwargs[0][0]
        body = call_kwargs[1]["json"]
        assert "긴급 분석 요청" in body["message"]
        assert "즉시 실행하라" in body["message"]
        assert "테스트 모드" not in body["message"]
        assert body["name"] == "MarketAlert"
        assert body["deliver"] is True
        assert "Bearer test-token" in call_kwargs[1]["headers"]["Authorization"]

    @pytest.mark.asyncio
    async def test_no_token_skips_webhook(self, monkeypatch):
        """RACHEL_WEBHOOK_TOKEN 미설정 시 graceful skip."""
        monkeypatch.setattr("api.services.monitoring.alerts.RACHEL_WEBHOOK_TOKEN", "")

        alert = {"level": "critical", "triggers": ["price_crash"], "text": "crash"}

        with patch("api.services.monitoring.alerts.httpx.AsyncClient") as mock_cls:
            await _trigger_rachel_analysis("xrp_jpy", alert)
            mock_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_duplicate(self, monkeypatch):
        """15분 이내 동일 pair → 2번째 스킵."""
        monkeypatch.setattr("api.services.monitoring.alerts.RACHEL_WEBHOOK_TOKEN", "test-token")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "ok"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        alert = {"level": "critical", "triggers": ["price_crash"], "text": "crash"}

        with patch("api.services.monitoring.alerts.httpx.AsyncClient", return_value=mock_client):
            await _trigger_rachel_analysis("xrp_jpy", alert)
            await _trigger_rachel_analysis("xrp_jpy", alert)

        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_different_pair_not_blocked(self, monkeypatch):
        """다른 pair는 쿨다운 영향 안 받음."""
        monkeypatch.setattr("api.services.monitoring.alerts.RACHEL_WEBHOOK_TOKEN", "test-token")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "ok"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        alert = {"level": "critical", "triggers": ["price_crash"], "text": "crash"}

        with patch("api.services.monitoring.alerts.httpx.AsyncClient", return_value=mock_client):
            await _trigger_rachel_analysis("xrp_jpy", alert)
            await _trigger_rachel_analysis("BTC_JPY", alert)

        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_webhook_error_handled_gracefully(self, monkeypatch):
        """webhook 네트워크 오류 시 예외 안 나고 로그만."""
        monkeypatch.setattr("api.services.monitoring.alerts.RACHEL_WEBHOOK_TOKEN", "test-token")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        alert = {"level": "critical", "triggers": ["price_crash"], "text": "crash"}

        with patch("api.services.monitoring.alerts.httpx.AsyncClient", return_value=mock_client):
            await _trigger_rachel_analysis("xrp_jpy", alert)  # 예외 없이 완료


class TestBuildTestAlert:
    """테스트용 alert 생성 함수."""

    def test_critical_alert(self):
        raw = {"pair": "xrp_jpy", "current_price": 35.5, "rsi14": 45.0}
        alert = _build_test_alert(raw, "critical")
        assert alert["level"] == "critical"
        assert alert["triggers"] == ["test_forced_critical"]
        assert "CK" in alert["text"]
        assert "긴급 테스트" in alert["text"]
        assert "RSI 45.0" in alert["text"]

    def test_warning_alert(self):
        raw = {"pair": "BTC_JPY", "current_price": 15000000, "rsi14": 55.0}
        alert = _build_test_alert(raw, "warning")
        assert alert["level"] == "warning"
        assert alert["triggers"] == ["test_forced_warning"]
        assert "BF" in alert["text"]
        assert "주의 테스트" in alert["text"]

    def test_critical_bf_pair(self):
        raw = {"pair": "BTC_JPY", "current_price": 15000000, "rsi14": 30.0}
        alert = _build_test_alert(raw, "critical")
        assert "BF" in alert["text"]
        assert "긴급 테스트" in alert["text"]

    def test_no_rsi(self):
        raw = {"pair": "xrp_jpy", "current_price": 35.5, "rsi14": None}
        alert = _build_test_alert(raw, "critical")
        assert "RSI" not in alert["text"]
        assert alert["level"] == "critical"


class TestTriggerRachelTestMode:
    """테스트 모드 webhook 메시지 분기 테스트."""

    @pytest.fixture(autouse=True)
    def _clear_cooldown(self):
        _last_alert_time.clear()
        yield
        _last_alert_time.clear()

    @pytest.mark.asyncio
    async def test_test_mode_message(self, monkeypatch):
        """테스트 모드 시 webhook message에 테스트 모드 문구 포함."""
        monkeypatch.setattr("api.services.monitoring.alerts.RACHEL_WEBHOOK_TOKEN", "test-token")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "ok"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        alert = {
            "level": "critical",
            "triggers": ["test_forced_critical"],
            "text": "테스트 강제 트리거",
        }

        with patch("api.services.monitoring.alerts.httpx.AsyncClient", return_value=mock_client):
            await _trigger_rachel_analysis("BTC_JPY", alert, test=True)

        mock_client.post.assert_called_once()
        body = mock_client.post.call_args[1]["json"]
        assert "테스트 모드" in body["message"]
        assert "실행하지 말 것" in body["message"]
        assert "Rachel 긴급 테스트" in body["message"]

    @pytest.mark.asyncio
    async def test_real_mode_message(self, monkeypatch):
        """실전 모드 시 webhook message에 즉시 실행 문구 포함."""
        monkeypatch.setattr("api.services.monitoring.alerts.RACHEL_WEBHOOK_TOKEN", "test-token")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "ok"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        alert = {
            "level": "critical",
            "triggers": ["rsi_extreme_low"],
            "text": "RSI 기반 alert",
        }

        with patch("api.services.monitoring.alerts.httpx.AsyncClient", return_value=mock_client):
            await _trigger_rachel_analysis("BTC_JPY", alert, test=False)

        body = mock_client.post.call_args[1]["json"]
        assert "즉시 실행하라" in body["message"]
        assert "Rachel 긴급]" in body["message"]
        assert "테스트 모드" not in body["message"]

    @pytest.mark.asyncio
    async def test_reset_cooldown_allows_retry(self, monkeypatch):
        """쿨다운 리셋 후 재호출 가능."""
        monkeypatch.setattr("api.services.monitoring.alerts.RACHEL_WEBHOOK_TOKEN", "test-token")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "ok"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        alert = {"level": "critical", "triggers": ["test_forced_critical"], "text": "test"}

        with patch("api.services.monitoring.alerts.httpx.AsyncClient", return_value=mock_client):
            # 1차 호출
            await _trigger_rachel_analysis("BTC_JPY", alert)
            # 쿨다운 리셋
            _last_alert_time.pop("BTC_JPY", None)
            # 2차 호출 — 쿨다운 없으므로 성공
            await _trigger_rachel_analysis("BTC_JPY", alert)

        assert mock_client.post.call_count == 2
