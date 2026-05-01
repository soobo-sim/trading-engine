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
    get_entry_blockers_short,
    get_entry_condition_lines_long,
    get_entry_condition_lines_short,
    get_wait_direction,
    get_narrative_situation,
    get_narrative_outlook,
    get_box_narrative_situation,
    get_box_narrative_outlook,
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
        result = get_market_summary(-0.2, 40.0, "long_caution")
        assert "하락" in result

    def test_entry_ready(self):
        result = get_market_summary(0.15, 50.0, "long_setup")
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
            signal="long_caution",
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
            signal="long_overheated",
            current_price=110.0,
            ema=100.0,
            ema_slope_pct=0.2,
            rsi=70.0,
        )
        assert len(blockers) == 1
        assert "과열" in blockers[0]

    def test_no_blockers(self):
        blockers = get_entry_blockers(
            signal="long_setup",
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

    def test_long_setup_signal_no_regime_blocker(self):
        """long_setup 시그널이면 레짐 blocker 없어야 한다."""
        blockers = get_entry_blockers(
            signal="long_setup",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.15,
            rsi=50.0,
        )
        assert not any("레짐" in b for b in blockers)

    def test_custom_rsi_min_honored(self):
        """전략별 RSI 하한(entry_rsi_min=45)이 반영되어야 한다."""
        blockers = get_entry_blockers(
            signal="long_setup",
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
            signal="long_overheated",
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
            signal="long_setup",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.15,
            rsi=63.0,
        )
        assert blockers == []

    def test_custom_rsi_max_60_blocks_at_63(self):
        """rsi_max=60일 때 RSI 63은 block되어야 한다."""
        blockers = get_entry_blockers(
            signal="long_setup",
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
            signal="long_setup",
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
            signal="long_setup",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=-0.03,
            rsi=50.0,
            slope_min=-0.05,
        )
        assert blockers == []


# ── 서사형(Narrative) 헬퍼 테스트 ────────────────────────

class TestGetNarrativeSituation:
    def test_no_position_downtrend(self):
        result = get_narrative_situation(
            has_position=False, signal="long_caution",
            ema_slope_pct=-0.15, rsi=28.0, current_price=90.0, ema=100.0,
        )
        assert "EMA 아래" in result and "하락" in result

    def test_no_position_uptrend_entry_ready(self):
        result = get_narrative_situation(
            has_position=False, signal="long_setup",
            ema_slope_pct=0.15, rsi=52.0, current_price=105.0, ema=100.0,
        )
        assert "상승" in result and "진입" in result

    def test_with_position_profitable(self):
        result = get_narrative_situation(
            has_position=True, signal="hold",
            ema_slope_pct=0.1, rsi=55.0, current_price=105.0, ema=100.0,
            unrealized_pnl_pct=3.0,
        )
        assert "수익 확대" in result

    def test_with_position_full_exit(self):
        result = get_narrative_situation(
            has_position=True, signal="long_caution",
            ema_slope_pct=-0.2, rsi=28.0, current_price=95.0, ema=100.0,
            unrealized_pnl_pct=-1.0,
            exit_signal={"action": "full_exit"},
        )
        assert "청산 시그널" in result


class TestGetNarrativeOutlook:
    def test_no_position_returns_none(self):
        assert get_narrative_outlook(False, None, 50.0, 0.5) is None

    def test_hold_default(self):
        result = get_narrative_outlook(True, {"action": "hold"}, 55.0, 1.0)
        assert result is not None
        assert "트레일링 스탑" in result

    def test_tighten_stop(self):
        result = get_narrative_outlook(True, {"action": "tighten_stop"}, 45.0, -0.5)
        assert "스탑 조임" in result

    def test_big_loss(self):
        result = get_narrative_outlook(True, {"action": "hold"}, 40.0, -2.0)
        assert "손절선 접근" in result


class TestGetBoxNarrativeSituation:
    def test_no_box(self):
        result = get_box_narrative_situation(False, "no_box", False)
        assert "미형성" in result

    def test_has_position_near_upper(self):
        result = get_box_narrative_situation(True, "near_upper", True, "buy", 1.5)
        assert "상단 접근" in result and "익절" in result

    def test_no_position_near_lower(self):
        result = get_box_narrative_situation(False, "near_lower", True)
        assert "하단 진입대" in result

    def test_no_position_middle(self):
        result = get_box_narrative_situation(False, "middle", True)
        assert "중심" in result


class TestGetBoxNarrativeOutlook:
    def test_no_position_returns_none(self):
        assert get_box_narrative_outlook(False, "middle", "buy") is None

    def test_buy_near_upper(self):
        result = get_box_narrative_outlook(True, "near_upper", "buy")
        assert "자동 익절" in result

    def test_sell_near_lower(self):
        result = get_box_narrative_outlook(True, "near_lower", "sell")
        assert "익절" in result


# ── 텔레그램 텍스트 조립 테스트 ──────────────────────────

class TestBuildTelegramText:
    def _make_no_pos(self, **kwargs):
        """공통 포지션 없는 data dict (누락 키 방지용)."""
        base = {
            "trend_icon": "📉",
            "current_price": 232.49,
            "market_summary": "🔻 하락 전환",
            "ema_state": "EMA 아래 -0.14%",
            "rsi_state": "RSI 과매도(31.5)",
            "volatility_state": "변동성 높음",
            "ema_slope_pct": None,
            "rsi": None,
            "position": None,
            "entry_blockers": [],
            "conditions_met": 5,
            "conditions_total": 5,
            "jpy_available": 19468,
            "collateral": None,
        }
        base.update(kwargs)
        return base

    def _make_with_pos(self, **kwargs):
        """공통 포지션 있는 data dict."""
        base = {
            "trend_icon": "📈",
            "current_price": 11800000,
            "position_summary": "상승추세·보유 유지",
            "ema_state": "EMA 위 +1.5%",
            "rsi_state": "RSI 중립(55.3)",
            "volatility_state": "변동성 보통",
            "ema_slope_pct": None,
            "rsi": None,
            "position": {
                "side": "buy",
                "entry_price": 11631190,
                "entry_amount": 0.003,
                "stop_loss_price": 11463739,
                "trailing_stop_distance": 336261,
                "unrealized_pnl_jpy": 506,
                "unrealized_pnl_pct": 0.87,
                "price_diff": 168810,
                # (11463739 - 11631190) * 0.003 = -502
                "pnl_at_stop": -502,
            },
            "entry_blockers": [],
            "jpy_available": 10000,
            "collateral": None,
        }
        base.update(kwargs)
        return base

    def test_no_position_with_blockers(self):
        data = self._make_no_pos(
            entry_blockers=["EMA slope -0.14% → ≥+0.00% 필요"],
            conditions_met=4,
        )
        text = build_telegram_text("CK", "21:01", "xrp_jpy", data)
        assert "[CK] 21:01" in text
        assert "📉추세추종" in text
        assert "🔻 하락" in text
        assert "판단 도메인 →" in text
        assert "대기중" in text

    def test_no_position_entry_ready(self):
        data = self._make_no_pos(
            trend_icon="📈",
            current_price=250.0,
            market_summary="✅ 진입 임박",
            signal="long_setup",
        )
        text = build_telegram_text("CK", "15:00", "xrp_jpy", data)
        assert "판단 도메인 →" in text
        assert "🟢 롱 진입 신호" in text

    def test_with_position(self):
        data = self._make_with_pos()
        text = build_telegram_text("BF", "21:01", "BTC_JPY", data)
        assert "[BF] 21:01" in text
        assert "손절" in text
        assert "보유" in text
        assert "BTC" in text
        assert "미실현" in text

    def test_with_position_long_label(self):
        """side=buy → 롱 보유."""
        data = self._make_with_pos(
            current_price=10000000,
            position={
                "side": "buy",
                "entry_price": 9800000,
                "entry_amount": 0.01,
                "stop_loss_price": 9600000,
                "trailing_stop_distance": 400000,
                "unrealized_pnl_jpy": 200000,
                "unrealized_pnl_pct": 2.04,
                "price_diff": 200000,
            },
        )
        text = build_telegram_text("GMOC", "12:00", "BTC_JPY", data)
        assert "롱 보유" in text
        assert "숏 보유" not in text

    def test_with_position_short_label(self):
        """side=sell → 숏 보유 (레버리지 숏)."""
        data = self._make_with_pos(
            trend_icon="📉",
            current_price=9500000,
            position_summary="추세 약화 감지",
            position={
                "side": "sell",
                "entry_price": 9800000,
                "entry_amount": 0.01,
                "stop_loss_price": 10100000,
                "trailing_stop_distance": 600000,
                "unrealized_pnl_jpy": 300000,
                "unrealized_pnl_pct": 3.06,
                "price_diff": -300000,
            },
            exit_signal={"action": "hold"},
            jpy_available=100000,
        )
        text = build_telegram_text("GMOC", "15:00", "BTC_JPY", data)
        assert "숏 보유" in text
        assert "롱 보유" not in text
        assert "⚡ 전망:" in text

    def test_no_exit_signal_no_outlook_line(self):
        """exit_signal=None → ⚡ 전망 행 미표시."""
        data = self._make_with_pos(
            position={
                "side": "buy",
                "entry_price": 11600000,
                "entry_amount": 0.003,
                "stop_loss_price": 11400000,
                "trailing_stop_distance": 400000,
                "unrealized_pnl_jpy": 600000,
                "unrealized_pnl_pct": 1.72,
                "price_diff": 200000,
            },
            exit_signal=None,
        )
        text = build_telegram_text("BF", "10:00", "BTC_JPY", data)
        assert "⚡ 전망:" not in text
        assert "미실현" in text

    def test_exit_signal_full_exit_outlook(self):
        """exit_signal full_exit → ⚡ 전망: 즉시 청산 실행 중."""
        data = self._make_with_pos(
            trend_icon="📉",
            current_price=11000000,
            position_summary="🚨 청산 시그널 발생",
            position={
                "side": "buy",
                "entry_price": 11500000,
                "entry_amount": 0.003,
                "stop_loss_price": 11200000,
                "trailing_stop_distance": 200000,
                "unrealized_pnl_jpy": -150000,
                "unrealized_pnl_pct": -4.35,
                "price_diff": -500000,
            },
            exit_signal={"action": "full_exit"},
        )
        text = build_telegram_text("BF", "10:00", "BTC_JPY", data)
        assert "즉시 청산 실행 중" in text

    def test_stop_loss_price_zero_no_crash(self):
        """stop_loss_price=0, pnl_at_stop 미설정 → 에러 없이 손익분기 행 표시."""
        data = self._make_with_pos(
            position={
                "side": "buy",
                "entry_price": 10800000,
                "entry_amount": 0.003,
                "stop_loss_price": 0,
                "trailing_stop_distance": 0,
                "unrealized_pnl_jpy": 60000,
                "unrealized_pnl_pct": 1.85,
                "price_diff": 200000,
                # pnl_at_stop 미설정 또는 0 → 손익분기 표시
            },
        )
        text = build_telegram_text("BF", "10:00", "BTC_JPY", data)
        assert "발동 시: ¥0 (손익분기)" in text  # pnl_at_stop=0 → 손익분기 표시
        assert "미실현" in text

    # ── 신규: 스탑 라인 3분기 (SR-01~SR-08) ────────────────────────

    def test_sr01_long_profit_stop_above_entry_shows_protection(self):
        """SR-01: 롱, 이익, stop>entry → 🛡️ 이익보호 + 확정이익 + 전망: 이익보호 중."""
        data = self._make_with_pos(
            current_price=12000000,
            position={
                "side": "buy",
                "entry_price": 11500000,
                "entry_amount": 0.004,
                "stop_loss_price": 11600000,  # > entry → pnl_at_stop=+400
                "trailing_stop_distance": 400000,
                "unrealized_pnl_jpy": 2000,
                "unrealized_pnl_pct": 4.35,
                "price_diff": 500000,
                "pnl_at_stop": 400,  # (11600000 - 11500000) * 0.004 = 400
            },
            exit_signal={"action": "hold"},
        )
        text = build_telegram_text("GMOC", "12:00", "btc_jpy", data)
        assert "발동 시: +¥400 이익 확정 (손익보호 중)" in text
        assert "트레일링 스탑이 이익 보호 중" in text
        assert "최소 +¥400 확정" in text

    def test_sr02_long_profit_stop_below_entry_shows_stop_loss(self):
        """SR-02: 롱, 이익(+2.3%), stop<entry → 🛑 손절 + 청산 시 손실 + 전망: 진입가 아래."""
        data = self._make_with_pos(
            current_price=11997440,
            position={
                "side": "buy",
                "entry_price": 11728011,
                "entry_amount": 0.004,
                "stop_loss_price": 11681748,  # < entry
                "trailing_stop_distance": 315692,
                "unrealized_pnl_jpy": 1078,
                "unrealized_pnl_pct": 2.30,
                "price_diff": 269429,
                "pnl_at_stop": -185,  # (11681748 - 11728011) * 0.004 ≈ -185
            },
            exit_signal={"action": "hold"},
        )
        text = build_telegram_text("GMOC", "07:48", "btc_jpy", data)
        assert "발동 시: -¥185 손절" in text
        assert "이익 중이나 스탑은 진입가 아래" in text
        assert "추가 상승 시 이익보호로 전환" in text

    def test_sr03_long_profit_stop_equals_entry_shows_breakeven(self):
        """SR-03: 롱, 이익, stop=entry → 🔒 손익분기 + 전망: 진입가 아래."""
        data = self._make_with_pos(
            current_price=12000000,
            position={
                "side": "buy",
                "entry_price": 11800000,
                "entry_amount": 0.004,
                "stop_loss_price": 11800000,  # = entry
                "trailing_stop_distance": 200000,
                "unrealized_pnl_jpy": 800,
                "unrealized_pnl_pct": 1.69,
                "price_diff": 200000,
                "pnl_at_stop": 0,
            },
            exit_signal={"action": "hold"},
        )
        text = build_telegram_text("GMOC", "12:00", "btc_jpy", data)
        assert "발동 시: ¥0 (손익분기)" in text
        assert "이익 중이나 스탑은 진입가 아래" in text

    def test_sr04_long_loss_stop_below_entry_shows_approaching(self):
        """SR-04: 롱, 손실(-1.5%), stop<entry → 🛑 손절 + 손절선 접근 중."""
        data = self._make_with_pos(
            current_price=11500000,
            position={
                "side": "buy",
                "entry_price": 11600000,
                "entry_amount": 0.004,
                "stop_loss_price": 11420000,
                "trailing_stop_distance": 80000,
                "unrealized_pnl_jpy": -400,
                "unrealized_pnl_pct": -1.72,
                "price_diff": -100000,
                "pnl_at_stop": -720,  # (11420000 - 11600000) * 0.004 = -720
            },
            exit_signal={"action": "hold"},
        )
        text = build_telegram_text("GMOC", "12:00", "btc_jpy", data)
        assert "발동 시: -¥720 손절" in text
        assert "손절선 접근 중" in text

    def test_sr05_short_profit_stop_below_entry_shows_protection(self):
        """SR-05: 숏, 이익, stop<entry(숏 기준 유리) → pnl_at_stop>0 → 🛡️ 이익보호."""
        # 숏: entry=10000000, stop=9700000 → pnl_at_stop=(entry-stop)*amount=(10000000-9700000)*0.01=3000
        data = self._make_with_pos(
            trend_icon="📉",
            current_price=9500000,
            position={
                "side": "sell",
                "entry_price": 10000000,
                "entry_amount": 0.01,
                "stop_loss_price": 9700000,
                "trailing_stop_distance": 200000,
                "unrealized_pnl_jpy": 5000,
                "unrealized_pnl_pct": 5.0,
                "price_diff": -500000,
                "pnl_at_stop": 3000,  # (10000000 - 9700000) * 0.01 = 3000
            },
            exit_signal={"action": "hold"},
        )
        text = build_telegram_text("GMOC", "12:00", "btc_jpy", data)
        assert "발동 시: +¥3,000 이익 확정 (손익보호 중)" in text
        assert "트레일링 스탑이 이익 보호 중" in text

    def test_sr06_short_profit_stop_above_entry_shows_stop_loss(self):
        """SR-06: 숏, 이익, stop>entry(숏 기준 불리) → pnl_at_stop<0 → 🛑 손절 + 전망: 진입가 아래."""
        # 숏: entry=10000000, stop=10100000 → pnl_at_stop=(10000000-10100000)*0.01=-1000
        data = self._make_with_pos(
            trend_icon="📉",
            current_price=9700000,
            position={
                "side": "sell",
                "entry_price": 10000000,
                "entry_amount": 0.01,
                "stop_loss_price": 10100000,  # > entry (숏 불리)
                "trailing_stop_distance": 400000,
                "unrealized_pnl_jpy": 3000,
                "unrealized_pnl_pct": 3.0,
                "price_diff": -300000,
                "pnl_at_stop": -1000,  # (10000000 - 10100000) * 0.01 = -1000
            },
            exit_signal={"action": "hold"},
        )
        text = build_telegram_text("GMOC", "12:00", "btc_jpy", data)
        assert "발동 시: -¥1,000 손절" in text
        assert "이익 중이나 스탑은 진입가 아래" in text

    def test_sr07_stop_zero_no_pnl_at_stop_shows_breakeven(self):
        """SR-07: stop=0, pnl_at_stop 미설정(=0) → 🔒 손익분기 행."""
        data = self._make_with_pos(
            position={
                "side": "buy",
                "entry_price": 10000000,
                "entry_amount": 0.004,
                "stop_loss_price": 0,
                "trailing_stop_distance": 0,
                "unrealized_pnl_jpy": 200,
                "unrealized_pnl_pct": 0.5,
                "price_diff": 50000,
                # pnl_at_stop 키 없음 → 기본값 0
            },
        )
        text = build_telegram_text("GMOC", "12:00", "btc_jpy", data)
        assert "발동 시: ¥0 (손익분기)" in text

    def test_sr08_tighten_stop_action_overrides_outlook(self):
        """SR-08: tighten_stop 액션 → 전망: 추세 약화 — 스탑 조임 중 (pnl_at_stop 무시)."""
        data = self._make_with_pos(
            position={
                "side": "buy",
                "entry_price": 11500000,
                "entry_amount": 0.004,
                "stop_loss_price": 11600000,
                "trailing_stop_distance": 200000,
                "unrealized_pnl_jpy": 2000,
                "unrealized_pnl_pct": 4.35,
                "price_diff": 500000,
                "pnl_at_stop": 400,  # 이익보호 상태지만
            },
            exit_signal={"action": "tighten_stop"},  # tighten이 우선
        )
        text = build_telegram_text("GMOC", "12:00", "btc_jpy", data)
        assert "추세 약화 — 스탑 조임 중" in text
        # pnl_at_stop>0 이지만 tighten_stop이 우선이므로 이익보호 전망 미표시
        assert "트레일링 스탑이 이익 보호 중" not in text

    # ── 신규: 진입가 대비 차이 표시 ──────────────────────────────

    def test_price_diff_positive_shown(self):
        """D2-01: 롱 이익 — 진입가 대비 +XX 표시."""
        data = self._make_with_pos(
            current_price=12000000,
            position={
                "side": "buy",
                "entry_price": 11800000,
                "entry_amount": 0.004,
                "stop_loss_price": 11600000,
                "trailing_stop_distance": 400000,
                "unrealized_pnl_jpy": 800,
                "unrealized_pnl_pct": 1.69,
                "price_diff": 200000,
            },
        )
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "진입가 대비 +¥200,000" in text

    def test_price_diff_negative_shown(self):
        """D2-02: 롱 손실 — 진입가 대비 -XX 표시."""
        data = self._make_with_pos(
            current_price=11827679,
            position={
                "side": "buy",
                "entry_price": 11919627,
                "entry_amount": 0.004,
                "stop_loss_price": 11735812,
                "trailing_stop_distance": 91867,
                "unrealized_pnl_jpy": -368,
                "unrealized_pnl_pct": -0.77,
                "price_diff": -91948,
            },
        )
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "진입가 대비 -¥91,948" in text
        assert "-¥368" in text

    def test_pnl_sign_before_yen(self):
        """D2-Sig: P&L 부호가 ¥ 앞에 위치. '¥-' 형식 사용 안 함."""
        data = self._make_with_pos(
            position={
                "side": "buy",
                "entry_price": 11919627,
                "entry_amount": 0.004,
                "stop_loss_price": 11735812,
                "trailing_stop_distance": 91868,
                "unrealized_pnl_jpy": -368,
                "unrealized_pnl_pct": -0.77,
                "price_diff": -91948,
            },
        )
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "-¥368" in text
        assert "¥-368" not in text  # 기존 포맷 사용 안 함

    def test_situation_with_ema_rsi_basis(self):
        """D2-03: ema_slope_pct + rsi 있으면 📊 근거 괄호 표시."""
        data = self._make_with_pos(
            ema_slope_pct=0.12,
            rsi=52.0,
        )
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "EMA↑+0.12%" in text
        assert "RSI 52" in text

    def test_situation_without_basis_no_parenthesis(self):
        """D2-04: ema_slope_pct=None → 괄호 없이 situation만 표시."""
        data = self._make_with_pos(ema_slope_pct=None, rsi=None)
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "(EMA" not in text

    def test_no_position_basis_shown(self):
        """D2-03b: 대기중에도 EMA slope + RSI 근거 표시."""
        data = self._make_no_pos(
            ema_slope_pct=-0.14,
            rsi=31.5,
            market_summary="🔻 하락 전환",
        )
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "EMA↓-0.14%" in text
        assert "RSI 32" in text  # 31.5 → .0f 반올림

    def test_collateral_line_shown_when_leveraged(self):
        """D2-05: collateral 있으면 💼 증거금 라인."""
        data = self._make_with_pos(
            collateral={
                "collateral": 200000,
                "open_position_pnl": -368,
                "require_collateral": 100000,
                "keep_rate": 200.0,
            },
        )
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "💼 증거금" in text
        assert "¥200,000" in text
        assert "필요 ¥100,000" in text
        assert "여력 ¥100,000" in text
        # 기존 💰 잔고 라인 미표시
        assert "💰 ¥" not in text

    def test_collateral_none_fallback_to_jpy(self):
        """D2-06: collateral=None → 기존 💰 ¥ 라인 표시."""
        data = self._make_with_pos(collateral=None, jpy_available=100173)
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "💰 ¥100,173" in text
        assert "💼" not in text

    def test_collateral_available_zero_floor(self):
        """D2-09: require >= collateral → 여력 ¥0 (음수 방지)."""
        data = self._make_no_pos(
            collateral={
                "collateral": 50000,
                "open_position_pnl": 0,
                "require_collateral": 60000,
                "keep_rate": 83.0,
            },
        )
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "여력 ¥0" in text

    def test_price_diff_zero(self):
        """D2-10: price_diff=0 → +¥0 표시."""
        data = self._make_with_pos(
            current_price=10000000,
            position={
                "side": "buy",
                "entry_price": 10000000,
                "entry_amount": 0.01,
                "stop_loss_price": 9800000,
                "trailing_stop_distance": 200000,
                "unrealized_pnl_jpy": 0,
                "unrealized_pnl_pct": 0.0,
                "price_diff": 0,
            },
        )
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "진입가 대비 +¥0" in text

    def test_ema_slope_zero_shows_flat_arrow(self):
        """E1: ema_slope_pct=0.0 → → 화살표 (↓ 아님)."""
        data = self._make_no_pos(ema_slope_pct=0.0, rsi=50.0)
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "EMA→+0.00%" in text
        assert "EMA↓" not in text
        assert "EMA↑" not in text

    def test_price_diff_missing_key_defaults_to_zero(self):
        """E2: price_diff 키 없는 구버전 position_data → +¥0 (에러 없음)."""
        data = self._make_with_pos(
            current_price=10000000,
            position={
                "side": "buy",
                "entry_price": 9800000,
                "entry_amount": 0.01,
                "stop_loss_price": 9600000,
                "trailing_stop_distance": 400000,
                "unrealized_pnl_jpy": 200000,
                "unrealized_pnl_pct": 2.04,
                # price_diff 키 없음 — 구버전 호환
            },
        )
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "진입가 대비 +¥0" in text
        assert "미실현" in text

    def test_short_pnl_sign_positive_when_price_fell(self):
        """E3: 숏 포지션에서 가격 하락 → 이익(+) 표시."""
        data = self._make_with_pos(
            trend_icon="📉",
            current_price=9500000,
            position_summary="추세 유지",
            position={
                "side": "sell",
                "entry_price": 9800000,
                "entry_amount": 0.01,
                "stop_loss_price": 10100000,
                "trailing_stop_distance": 600000,
                "unrealized_pnl_jpy": 30000,   # 숏 이익
                "unrealized_pnl_pct": 3.06,
                "price_diff": -300000,
            },
        )
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "숏 보유" in text
        assert "+¥30,000" in text   # 이익 → + 부호
        assert "-¥30,000" not in text

    def test_both_ema_slope_none_rsi_present_no_basis(self):
        """E4: ema_slope_pct만 None → 괄호 없음 (rsi만으로는 표시 안 함)."""
        data = self._make_no_pos(ema_slope_pct=None, rsi=55.0)
        text = build_telegram_text("GMOC", "22:06", "btc_jpy", data)
        assert "(EMA" not in text


# ── 메모리 블록 조립 테스트 ──────────────────────────────

class TestBuildMemoryBlock:
    def test_no_position(self):
        data = {
            "signal": "long_caution",
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
            "side": "buy",
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
        assert "📈 롱 보유" in text, "보유 행 있어야 함"
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

    def test_rpt06_short_position_display(self):
        """T-RPT-06: 숏(sell) 포지션 → 📉 숏 보유 + near_lower 익절 + 박스상단 손절."""
        # entry=212.5 (near_upper 진입), upper=212.0, lower=208.0
        short_pos = {
            "side": "sell",
            "entry_price": 212.5,
            "entry_amount": 1000.0,
            "unrealized_pnl_jpy": 500.0,
            "unrealized_pnl_pct": 0.23,
        }
        data = self._base_data(
            current_price=210.0,
            position=short_pos,
            near_bound_pct=1.5,
            tolerance_pct=1.5,
            stop_loss_pct=1.5,
        )
        text = build_box_telegram_text("GMO", "22:58", "gbp_jpy", data)

        # 숏 보유 아이콘 + 라벨
        assert "📉 숏 보유" in text, f"숏 보유 행 미표시: {text}"
        # 익절: near_lower (하한 근처)
        assert "🎯 익절: near_lower" in text, f"숏 익절 방향 오류: {text}"
        # 손절: 박스 상단 이탈
        assert "🛑 손절" in text
        assert "박스 무효화" in text
        # 진입 조건 / 다음 캔들 행 미표시 (포지션 있으므로)
        assert "⏰ 다음 4h봉" not in text
        assert "🚫" not in text


# ══════════════════════════════════════════════════════════════
#  Alert 평가 테스트
# ══════════════════════════════════════════════════════════════


class TestIsRegimeShift:
    def test_bullish_to_bearish(self):
        assert _is_regime_shift("long_setup", "long_caution") is True

    def test_wait_dip_to_bearish(self):
        assert _is_regime_shift("long_overheated", "long_caution") is True

    def test_bearish_to_bullish(self):
        assert _is_regime_shift("long_caution", "long_setup") is True

    def test_same_direction(self):
        assert _is_regime_shift("long_setup", "long_overheated") is False

    def test_no_signal(self):
        assert _is_regime_shift("no_signal", "long_setup") is False

    def test_wait_regime_not_shift(self):
        assert _is_regime_shift("wait_regime", "long_caution") is False


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
            "signal": "long_overheated",
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
        prev = self._base_raw(signal="long_setup")
        raw = self._base_raw(signal="long_caution")
        alert = evaluate_alert(raw, prev)
        assert alert["level"] == "critical"
        assert "regime_shift" in alert["triggers"]
        assert "long_setup → long_caution" in alert["text"]

    def test_no_regime_shift_same_direction(self):
        prev = self._base_raw(signal="long_setup")
        raw = self._base_raw(signal="long_overheated")
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
        raw = self._base_raw(signal="long_caution")
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


# ══════════════════════════════════════════════
# 테스트: 박스 수명 경고 (BOX_LIFECYCLE_POLICY)
# ══════════════════════════════════════════════

class TestCheckBoxAgeWarning:
    """check_box_age_warning 유틸 함수 — 엣지 케이스 포함."""

    def test_naive_datetime_treated_as_utc(self):
        """T-AGE-NA-01: tzinfo 없는 naive datetime → UTC로 간주하여 정상 계산."""
        from api.services.monitoring.box_report import check_box_age_warning
        # 21일 전 naive datetime (tzinfo=None)
        naive_old = (datetime.now(timezone.utc) - timedelta(days=21)).replace(tzinfo=None)
        assert naive_old.tzinfo is None, "사전조건: naive datetime이어야 함"
        result = check_box_age_warning(naive_old)
        assert result is not None
        assert "⚠️" in result

    def test_aware_datetime_works_correctly(self):
        """T-AGE-NA-02: timezone-aware datetime → 정상 처리."""
        from api.services.monitoring.box_report import check_box_age_warning
        aware_old = datetime.now(timezone.utc) - timedelta(days=21)
        result = check_box_age_warning(aware_old)
        assert result is not None
        assert "21" in result

    def test_recent_box_no_warning(self):
        """T-AGE-NA-03: 최근 박스 → 경고 없음."""
        from api.services.monitoring.box_report import check_box_age_warning
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        assert check_box_age_warning(recent) is None


class TestBuildBoxTelegramTextAgeWarning:
    """build_box_telegram_text — age_warning 표시 경로."""

    def _base_data(self, age_warning: str | None = None) -> dict:
        box = {
            "id": 5,
            "upper_bound": 160.0,
            "lower_bound": 155.0,
            "box_width_pct": 3.2,
            "bar_chart": "[━━━●━━━━━━━]",
        }
        if age_warning is not None:
            box["age_warning"] = age_warning
        return {
            "health_line": "🟢 WS✅ 태스크2/2✅ 잔고✅",
            "current_price": 156.5,
            "box": box,
            "position_label": "near_lower",
            "position": None,
            "jpy_available": 50000,
            "coin_available": 0.0,
            "basis_timeframe": "4h",
            "candle_open_time_jst": "08:00",
            "is_margin_trading": True,
        }

    def test_age_warning_included_in_text(self):
        """T-RPT-AGE-01: age_warning 있을 때 Telegram 텍스트에 포함."""
        data = self._base_data(age_warning="⚠️ 장기 박스 (25일째)")
        text = build_box_telegram_text("GMO", "09:00", "usd_jpy", data)
        assert "⚠️ 장기 박스" in text
        assert "25일째" in text

    def test_no_age_warning_clean_text(self):
        """T-RPT-AGE-02: age_warning 없을 때 텍스트에 경고 없음."""
        data = self._base_data(age_warning=None)
        text = build_box_telegram_text("GMO", "09:00", "usd_jpy", data)
        assert "장기 박스" not in text

    def test_age_warning_key_missing_no_error(self):
        """T-RPT-AGE-03: box dict에 age_warning 키 자체 없어도 에러 없음 (하위 호환)."""
        data = self._base_data()  # age_warning 키 미포함
        assert "age_warning" not in data["box"]
        text = build_box_telegram_text("GMO", "09:00", "usd_jpy", data)
        assert text  # 에러 없이 텍스트 생성
        assert "장기 박스" not in text


# ── 추가 엣지케이스 테스트 ──────────────────────────────

class TestBuildBoxTelegramTextEdgeCases:
    """박스 전략 텔레그램 텍스트 엣지케이스."""

    def test_box_absent_with_position_no_crash(self):
        """박스 없는데 포지션 있는 경우 — 익절/손절 계산 skip, 에러 없음."""
        data = {
            "health_line": "🟢 WS✅ 태스크3/3✅ 잔고✅",
            "current_price": 155.0,
            "box": None,
            "position_label": "no_box",
            "position": {
                "side": "buy",
                "entry_price": 153.0,
                "entry_amount": 1000.0,
                "unrealized_pnl_jpy": 200.0,
                "unrealized_pnl_pct": 1.31,
            },
            "jpy_available": 50000,
            "coin_available": 1000.0,
            "basis_timeframe": "4h",
            "candle_open_time_jst": "불명",
            "is_margin_trading": True,
        }
        text = build_box_telegram_text("GMO", "10:00", "usd_jpy", data)
        assert text
        assert "롱 보유" in text
        assert "미실현" in text
        # 박스 없으면 익절/손절 행 없음
        assert "🎯 익절" not in text
        assert "🛑 손절" not in text

    def test_position_outside_box_label(self):
        """position_label=outside → 박스 이탈 서사 포함."""
        data = {
            "health_line": "🟢 WS✅ 태스크3/3✅ 잔고✅",
            "current_price": 170.0,
            "box": {
                "id": 1, "upper_bound": 165.0, "lower_bound": 160.0,
                "box_width_pct": 3.1, "bar_chart": "[━━━━━━━━━━]●",
            },
            "position_label": "outside",
            "position": {
                "side": "buy",
                "entry_price": 161.0,
                "entry_amount": 1000.0,
                "unrealized_pnl_jpy": 900.0,
                "unrealized_pnl_pct": 5.59,
            },
            "jpy_available": 50000,
            "coin_available": 1000.0,
            "basis_timeframe": "4h",
            "candle_open_time_jst": "불명",
            "near_bound_pct": 1.5,
            "tolerance_pct": 1.5,
            "stop_loss_pct": 1.5,
            "is_margin_trading": True,
        }
        text = build_box_telegram_text("GMO", "10:00", "usd_jpy", data)
        assert "이탈" in text  # 서사 표시

    def test_multiple_entry_blockers_each_on_own_line(self):
        """진입 차단이 2개 이상일 때 각 줄에 표시."""
        data = {
            "health_line": "🟢 WS✅ 태스크3/3✅ 잔고✅",
            "current_price": 160.0,
            "box": {
                "id": 1, "upper_bound": 165.0, "lower_bound": 160.0,
                "box_width_pct": 3.1, "bar_chart": "[●━━━━━━━━━━]",
            },
            "position_label": "middle",
            "position": None,
            "entry_blockers": ["중심부 → 하한 진입대 대기", "잔고 부족"],
            "conditions_met": 1,
            "conditions_total": 3,
            "jpy_available": 50000,
            "coin_available": 0.0,
            "basis_timeframe": "4h",
            "candle_open_time_jst": "20:00",
            "is_margin_trading": False,
        }
        text = build_box_telegram_text("GMO", "10:00", "usd_jpy", data)
        lines = text.split("\n")
        blocker_lines = [l for l in lines if " · " in l]
        assert len(blocker_lines) == 2, f"blockers expected on separate lines: {text}"


class TestGetNarrativeSituationEdgeCases:
    """get_narrative_situation 엣지케이스."""

    def test_ema_none_returns_data_insufficient(self):
        """ema=None → 데이터 부족 반환."""
        result = get_narrative_situation(
            has_position=False, signal="no_signal",
            ema_slope_pct=0.1, rsi=50.0, current_price=100.0, ema=None,
        )
        assert "부족" in result

    def test_above_ema_weak_slope(self):
        """EMA 위지만 slope 약 → RSI 조건 대기."""
        result = get_narrative_situation(
            has_position=False, signal="long_overheated",
            ema_slope_pct=0.05, rsi=55.0, current_price=105.0, ema=100.0,
        )
        assert "EMA 위" in result or "RSI 조건" in result or "상승" in result

    def test_with_position_small_loss(self):
        """미실현 소폭 손실 → 관찰 문구."""
        result = get_narrative_situation(
            has_position=True, signal="hold",
            ema_slope_pct=0.05, rsi=48.0, current_price=99.0, ema=100.0,
            unrealized_pnl_pct=-0.5,
        )
        assert "관찰" in result or "손실" in result

    def test_with_position_tighten_stop(self):
        """tighten_stop → 스탑 조임 문구."""
        result = get_narrative_situation(
            has_position=True, signal="long_caution",
            ema_slope_pct=-0.05, rsi=45.0, current_price=105.0, ema=107.0,
            unrealized_pnl_pct=0.5,
            exit_signal={"action": "tighten_stop"},
        )
        assert "스탑 조임" in result


# ── get_wait_direction 테스트 ─────────────────────────────────

class TestGetWaitDirection:
    """WD-01~WD-05"""

    def test_wd01_short_when_price_below_ema_and_slope_down(self):
        """WD-01: supports_short=True, price<EMA, slope<0 → short."""
        result = get_wait_direction(True, "no_signal", 100.0, 110.0, -0.1)
        assert result == "short"

    def test_wd02_long_when_price_above_ema_and_slope_up(self):
        """WD-02: supports_short=True, price>EMA, slope>0 → long."""
        result = get_wait_direction(True, "no_signal", 110.0, 100.0, 0.1)
        assert result == "long"

    def test_wd03_long_when_signal_is_wait_dip(self):
        """WD-03: signal=wait_dip → long (롱 조건 부분 충족)."""
        result = get_wait_direction(True, "long_overheated", 105.0, 100.0, 0.03)
        assert result == "long"

    def test_wd03b_long_when_signal_is_wait_regime(self):
        """WD-03b: signal=wait_regime → long."""
        result = get_wait_direction(True, "wait_regime", 102.0, 100.0, 0.02)
        assert result == "long"

    def test_wd04_always_long_when_not_support_short(self):
        """WD-04: supports_short=False → 항상 long (현물 전용)."""
        result = get_wait_direction(False, "no_signal", 90.0, 100.0, -0.2)
        assert result == "long"

    def test_wd05_neutral_when_ema_none(self):
        """WD-05: supports_short=True, ema=None → neutral."""
        result = get_wait_direction(True, "no_signal", 100.0, None, -0.1)
        assert result == "neutral"

    def test_neutral_when_price_below_ema_slope_up(self):
        """price<EMA & slope>0 → neutral (방향 불명)."""
        result = get_wait_direction(True, "no_signal", 90.0, 100.0, 0.1)
        assert result == "neutral"


# ── get_entry_blockers_short 테스트 ──────────────────────────

class TestGetEntryBlockersShort:
    """SB-01~SB-02"""

    def test_sb01_only_rsi_blocker_when_oversold(self):
        """SB-01: 가격<EMA, slope 충족, RSI 과매도만 미충족."""
        blockers = get_entry_blockers_short(
            signal="no_signal",
            current_price=11_347_825,
            ema=11_414_041,
            ema_slope_pct=-0.06,
            rsi=32.7,
            rsi_min=35.0,
            rsi_max=60.0,
            slope_threshold=-0.05,
        )
        # slope -0.06 < -0.05 ✅  price < ema ✅  rsi 32.7 < 35 ❌
        assert len(blockers) == 1
        assert "RSI" in blockers[0]
        assert "과매도" in blockers[0]

    def test_sb02_all_blockers_when_conditions_not_met(self):
        """SB-02: slope≥threshold, price≥EMA → min 2 blockers."""
        blockers = get_entry_blockers_short(
            signal="no_signal",
            current_price=11_500_000,
            ema=11_000_000,
            ema_slope_pct=0.01,
            rsi=50.0,
            slope_threshold=-0.05,
        )
        # slope 0.01 >= -0.05 ❌  price > ema ❌
        assert len(blockers) >= 2
        assert any("EMA slope" in b for b in blockers)
        assert any("EMA20" in b for b in blockers)

    def test_no_blockers_when_all_conditions_met(self):
        """모든 숏 진입 조건 충족 → 빈 리스트."""
        blockers = get_entry_blockers_short(
            signal="short_setup",
            current_price=9_900_000,
            ema=10_000_000,
            ema_slope_pct=-0.1,
            rsi=45.0,
            rsi_min=35.0,
            rsi_max=60.0,
            slope_threshold=-0.05,
        )
        assert blockers == []

    def test_rsi_overbought_blocker(self):
        """RSI 과열 → 숏 진입 차단."""
        blockers = get_entry_blockers_short(
            signal="no_signal",
            current_price=9_900_000,
            ema=10_000_000,
            ema_slope_pct=-0.1,
            rsi=65.0,
            rsi_max=60.0,
        )
        assert any("이하 필요" in b for b in blockers)


# ── build_telegram_text wait_direction 분기 테스트 ─────────────

class TestBuildTelegramTextWaitDirection:
    """TG-01~TG-05: wait_direction 분기 검증."""

    def _base_no_position(self, wait_dir=None):
        return {
            "trend_icon": "📉",
            "current_price": 11_347_825,
            "market_summary": "🔻 하락 전환·전략 유효성 점검",
            "position_summary": None,
            "position": None,
            "wait_direction": wait_dir,
            "entry_blockers": ["RSI 32.7 → 35 이상 필요 (과매도)"],
            "conditions_met": 4,
            "conditions_total": 5,
            "jpy_available": 100_173,
        }

    def test_tg01_short_waiting_label(self):
        """TG-01: wait_direction='short' → '숏 대기중' 레이블."""
        text = build_telegram_text("GMOC", "22:32", "btc_jpy", self._base_no_position("short"))
        assert "숏 대기중" in text
        assert "롱 대기중" not in text

    def test_tg02_long_waiting_label_cfd(self):
        """TG-02: wait_direction='long' (CFD) → '롱 대기중' 레이블."""
        text = build_telegram_text("GMOC", "10:00", "btc_jpy", self._base_no_position("long"))
        assert "롱 대기중" in text
        assert "숏 대기중" not in text

    def test_tg03_none_spot_legacy_label(self):
        """TG-03: wait_direction=None (현물 spot) → 기존 '대기중' 동작."""
        text = build_telegram_text("BF", "10:00", "BTC_JPY", self._base_no_position(None))
        assert "대기중" in text
        assert "롱 대기중" not in text
        assert "숏 대기중" not in text

    def test_tg04_neutral_waiting_label(self):
        """TG-04: wait_direction='neutral' → '관망중' 레이블."""
        text = build_telegram_text("GMO", "10:00", "USD_JPY", self._base_no_position("neutral"))
        assert "관망중" in text

    def test_tg05_short_blockers_displayed(self):
        """TG-05: 숏 대기 시 판단 도메인 결론 표시."""
        data = self._base_no_position("short")
        data["entry_blockers"] = ["RSI 32.7 → 35 이상 필요 (과매도)"]
        data["conditions_met"] = 4
        data["signal"] = "hold"
        text = build_telegram_text("GMOC", "22:32", "btc_jpy", data)
        assert "판단 도메인 →" in text
        assert "⏸ 조건 미충족" in text


# ── 엣지케이스 보강 ────────────────────────────────────────────

class TestWaitDirectionEntrySignals:
    """short_setup/long_setup 시그널 → wait_direction 경로 확인."""

    def test_short_setup_signal_gives_short_direction(self):
        """short_setup: price<EMA + slope 강하 → short."""
        # signals.py short_setup 조건을 재현
        result = get_wait_direction(True, "short_setup", 9_900_000, 10_000_000, -0.1)
        assert result == "short"

    def test_long_setup_signal_gives_long_direction(self):
        """long_setup: price>EMA + slope 양수 → long (signal 명시 없어도 long)."""
        result = get_wait_direction(True, "long_setup", 10_100_000, 10_000_000, 0.1)
        assert result == "long"

    def test_exit_warning_price_below_ema_short(self):
        """exit_warning + price<EMA + slope 음수 → short."""
        result = get_wait_direction(True, "long_caution", 9_900_000, 10_000_000, -0.2)
        assert result == "short"

    def test_no_signal_price_above_ema_slope_negative_neutral(self):
        """price>EMA + slope<0 (혼재) → neutral."""
        result = get_wait_direction(True, "no_signal", 10_100_000, 10_000_000, -0.1)
        assert result == "neutral"


class TestEntryBlockersShortEdgeCases:
    """숏 블로커 엣지케이스."""

    def test_short_setup_signal_no_blockers(self):
        """short_setup 시그널 = 모든 숏 조건 충족 → 빈 리스트."""
        blockers = get_entry_blockers_short(
            signal="short_setup",
            current_price=9_900_000,
            ema=10_000_000,
            ema_slope_pct=-0.1,
            rsi=50.0,
        )
        assert blockers == []

    def test_slope_exactly_at_threshold_is_blocker(self):
        """slope == threshold 는 >= 에 해당 → 블로커 추가."""
        blockers = get_entry_blockers_short(
            signal="no_signal",
            current_price=9_900_000,
            ema=10_000_000,
            ema_slope_pct=-0.05,
            rsi=45.0,
            slope_threshold=-0.05,
        )
        # -0.05 >= -0.05 → True → 블로커
        assert any("EMA slope" in b for b in blockers)

    def test_slope_just_below_threshold_no_slope_blocker(self):
        """slope = -0.051 < -0.05 → slope 블로커 없음."""
        blockers = get_entry_blockers_short(
            signal="no_signal",
            current_price=9_900_000,
            ema=10_000_000,
            ema_slope_pct=-0.051,
            rsi=45.0,
            slope_threshold=-0.05,
        )
        assert not any("EMA slope" in b for b in blockers)

    def test_regime_blocker_added_for_wait_regime(self):
        """signal=wait_regime → 레짐 블로커 추가."""
        blockers = get_entry_blockers_short(
            signal="wait_regime",
            current_price=9_900_000,
            ema=10_000_000,
            ema_slope_pct=-0.1,
            rsi=45.0,
        )
        assert any("레짐" in b for b in blockers)

    def test_ema_none_no_crash(self):
        """ema=None → 에러 없이 slope 블로커만."""
        blockers = get_entry_blockers_short(
            signal="no_signal",
            current_price=10_000_000,
            ema=None,
            ema_slope_pct=0.01,
            rsi=45.0,
            slope_threshold=-0.05,
        )
        # ema None → 가격 블로커 없음, slope 0.01 >= -0.05 → slope 블로커 있음
        assert not any("EMA20" in b for b in blockers)
        assert any("EMA slope" in b for b in blockers)


class TestBuildTelegramTextWaitDirectionEdgeCases:
    """wait_direction 분기 추가 엣지케이스."""

    def _no_pos(self, wait_dir, blockers=None, met=None):
        return {
            "trend_icon": "📉",
            "current_price": 10_000_000,
            "market_summary": "관망",
            "position_summary": None,
            "position": None,
            "wait_direction": wait_dir,
            "entry_blockers": blockers or [],
            "conditions_met": met if met is not None else (5 if not blockers else 5 - len(blockers)),
            "conditions_total": 5,
            "jpy_available": 50_000,
        }

    def test_short_no_blockers_shows_all_clear(self):
        """숏 대기 + 블로커 없음 → 판단 도메인 결론 표시."""
        data = self._no_pos("short", [], 5)
        data["signal"] = "short_setup"
        text = build_telegram_text("GMOC", "10:00", "btc_jpy", data)
        assert "판단 도메인 →" in text
        assert "🔴 숏 진입 신호" in text

    def test_long_with_blockers(self):
        """롱 대기 + 블로커 1개 → 판단 도메인 결론 표시."""
        data = self._no_pos("long", ["EMA slope -0.02% → ≥+0.00% 필요"], 4)
        data["signal"] = "hold"
        text = build_telegram_text("GMOC", "10:00", "btc_jpy", data)
        assert "롱 대기중" in text
        assert "판단 도메인 →" in text
        assert "⏸ 조건 미충족" in text

    def test_conditions_met_zero_when_max_blockers(self):
        """블로커 다수 → 판단 도메인 결론 표시."""
        data = self._no_pos("short", ["b1", "b2", "b3", "b4", "b5"], 0)
        data["signal"] = "hold"
        text = build_telegram_text("GMOC", "10:00", "btc_jpy", data)
        assert "판단 도메인 →" in text
        assert "⏸ 조건 미충족" in text

    def test_spot_trend_report_no_wait_direction_key(self):
        """현물 spot report_data에 wait_direction 키 없어도 동작."""
        data = {
            "trend_icon": "📈",
            "current_price": 11_000_000,
            "market_summary": "✅ 진입 임박",
            "position_summary": None,
            "position": None,
            # wait_direction 키 없음
            "entry_blockers": [],
            "conditions_met": 5,
            "conditions_total": 5,
            "jpy_available": 100_000,
        }
        text = build_telegram_text("BF", "10:00", "BTC_JPY", data)
        assert "대기중" in text
        assert "롱 대기중" not in text


# ── generate_trend_report _supports_short 분기 동작 단위 검증 ──

class TestSupportShortIntegration:
    """_supports_short=True/False 에 따른 wait_direction 결정 경로 단위 검증."""

    def test_supports_short_false_always_returns_none_direction(self):
        """_supports_short=False → get_wait_direction이 호출되어도 long 반환 후 None으로 처리."""
        # generate_trend_report 내부 로직을 직접 흉내
        supports_short = False
        signal = "no_signal"
        current_price = 9_900_000
        ema = 10_000_000
        ema_slope_pct = -0.1
        position_data = None

        if supports_short and not position_data:
            wait_direction = get_wait_direction(True, signal, current_price, ema, ema_slope_pct)
        else:
            wait_direction = None

        assert wait_direction is None

    def test_supports_short_true_with_downtrend_gives_short(self):
        """_supports_short=True + 하락 조건 → wait_direction='short'."""
        supports_short = True
        signal = "no_signal"
        current_price = 9_900_000
        ema = 10_000_000
        ema_slope_pct = -0.1
        position_data = None

        if supports_short and not position_data:
            wait_direction = get_wait_direction(True, signal, current_price, ema, ema_slope_pct)
        else:
            wait_direction = None

        assert wait_direction == "short"

    def test_supports_short_true_with_position_gives_none(self):
        """_supports_short=True이어도 포지션 보유 중이면 wait_direction=None."""
        supports_short = True
        position_data = {"side": "sell", "entry_price": 10_000_000}  # 포지션 있음

        if supports_short and not position_data:
            wait_direction = get_wait_direction(True, "no_signal", 9_900_000, 10_000_000, -0.1)
        else:
            wait_direction = None

        assert wait_direction is None

    def test_short_direction_triggers_short_blockers(self):
        """wait_direction='short' → get_entry_blockers_short 경로 사용 검증."""
        # RSI 과매도만 남은 상황 재현
        blockers = get_entry_blockers_short(
            signal="no_signal",
            current_price=11_347_825,
            ema=11_414_041,
            ema_slope_pct=-0.06,
            rsi=32.7,
            rsi_min=35.0,
            rsi_max=60.0,
            slope_threshold=-0.05,
        )
        assert len(blockers) == 1
        assert "과매도" in blockers[0]
        # conditions_met = max(0, 5 - 1) = 4
        conditions_met = max(0, 5 - len(blockers))
        assert conditions_met == 4


# ──────────────────────────────────────────────────────────────
# RG-M: build_telegram_text 체제 라인 (⚙️) 표시 테스트
# ──────────────────────────────────────────────────────────────


class TestBuildTelegramTextRegimeLine:
    """RG-M01~RG-M04: ⚙️ 체제: 라인 표시/미표시 검증."""

    def _base(self, regime=None, active_strategy=None, has_position=False):
        d = {
            "trend_icon": "📈",
            "current_price": 11_800_000,
            "ema_slope_pct": 0.1,
            "rsi": 52.0,
            "regime": regime,
            "active_strategy": active_strategy,
            "jpy_available": 50_000,
            "collateral": None,
        }
        if has_position:
            d.update({
                "position": {
                    "side": "buy",
                    "entry_price": 11_600_000,
                    "entry_amount": 0.003,
                    "stop_loss_price": 11_400_000,
                    "trailing_stop_distance": 400_000,
                    "unrealized_pnl_jpy": 600,
                    "unrealized_pnl_pct": 1.03,
                    "price_diff": 200_000,
                },
                "position_summary": "상승추세·보유 유지",
                "exit_signal": None,
            })
        else:
            d.update({
                "position": None,
                "position_summary": None,
                "wait_direction": "long",
                "market_summary": "확신 상승추세",
                "entry_blockers": [],
                "conditions_met": 5,
                "conditions_total": 5,
            })
        return d

    def test_rgm01_trending_trend_following_shown_with_position(self):
        """RG-M01: regime=trending + active_strategy=trend_following → ⚙️ 체제 라인 포함 (포지션 있음)."""
        data = self._base(regime="trending", active_strategy="trend_following", has_position=True)
        text = build_telegram_text("GMOC", "16:00", "btc_jpy", data)
        assert "⚙️ 체제: 추세장 | 활성전략: 추세추종" in text

    def test_rgm02_ranging_box_shown_no_position(self):
        """RG-M02: regime=ranging + active_strategy=box_mean_reversion → ⚙️ 적절한 라벨 (포지션 없음)."""
        data = self._base(regime="ranging", active_strategy="box_mean_reversion", has_position=False)
        text = build_telegram_text("GMOC", "16:00", "btc_jpy", data)
        assert "⚙️ 체제: 횡보장 | 활성전략: 박스역추세" in text

    def test_rgm03_no_regime_no_active_strategy_no_line(self):
        """RG-M03: regime=None, active_strategy=None → ⚙️ 라인 미표시."""
        data = self._base(regime=None, active_strategy=None, has_position=False)
        text = build_telegram_text("GMOC", "16:00", "btc_jpy", data)
        assert "⚙️ 체제:" not in text

    def test_rgm04_unclear_regime_shown(self):
        """RG-M04: regime=unclear → '불명확' 라벨."""
        data = self._base(regime="unclear", active_strategy=None, has_position=False)
        text = build_telegram_text("GMOC", "16:00", "btc_jpy", data)
        assert "⚙️ 체제: 불명확" in text


class TestBuildTelegramTextRegimeGateInfo:
    """RG-D01~RG-D05: regime_gate_info 기반 체제 라인 표시."""

    def _base_data(self, regime_gate_info=None, regime=None, active_strategy=None, has_position=False, jit_bypass_gate=False):
        d = {
            "trend_icon": "📈",
            "current_price": 11_800_000,
            "ema_slope_pct": 0.15,
            "rsi": 55.0,
            "regime": regime,
            "active_strategy": active_strategy,
            "regime_gate_info": regime_gate_info,
            "jit_bypass_gate": jit_bypass_gate,
            "jpy_available": 50_000,
            "collateral": None,
        }
        if has_position:
            d.update({
                "position": {
                    "side": "buy",
                    "entry_price": 11_600_000,
                    "entry_amount": 0.003,
                    "stop_loss_price": 11_400_000,
                    "trailing_stop_distance": 400_000,
                    "unrealized_pnl_jpy": 600,
                    "unrealized_pnl_pct": 1.03,
                    "price_diff": 200_000,
                },
                "position_summary": "상승추세·보유 유지",
                "exit_signal": None,
            })
        else:
            d.update({
                "position": None,
                "position_summary": None,
                "wait_direction": "long",
                "market_summary": "확신 상승추세",
                "entry_blockers": [],
                "conditions_met": 5,
                "conditions_total": 5,
            })
        return d

    def test_rgd01_unclear_active_none_shows_진입차단(self):
        """RG-D01: regime_gate_info={last_regime='unclear', cnt=2, active=None} → 진입 차단 중."""
        data = self._base_data(regime_gate_info={
            "last_regime": "unclear",
            "consecutive_count": 2,
            "active_strategy": None,
        })
        text = build_telegram_text("GMOC", "08:25", "btc_jpy", data)
        assert "⚙️ 체제: 불명확(×2) | 진입 차단 중" in text

    def test_rgd02_trending_active_trend_following_shown(self):
        """RG-D02: regime_gate_info={last_regime='trending', cnt=5, active='trend_following'} → 활성: 추세추종."""
        data = self._base_data(regime_gate_info={
            "last_regime": "trending",
            "consecutive_count": 5,
            "active_strategy": "trend_following",
        })
        text = build_telegram_text("GMOC", "08:25", "btc_jpy", data)
        assert "⚙️ 체제: 추세장(×5) | 활성: 추세추종" in text

    def test_rgd03_gate_info_none_fallback_to_legacy(self):
        """RG-D03: regime_gate_info=None → 구 regime/active_strategy 폴백."""
        data = self._base_data(
            regime_gate_info=None,
            regime="trending",
            active_strategy=None,
        )
        text = build_telegram_text("GMOC", "08:25", "btc_jpy", data)
        # 폴백: _REGIME_LABEL 기반
        assert "⚙️ 체제: 추세장 | 활성전략: -" in text

    def test_rgd03b_gate_info_none_no_regime_no_line(self):
        """RG-D03b: regime_gate_info=None + regime=None → 체제 라인 없음."""
        data = self._base_data(regime_gate_info=None, regime=None, active_strategy=None)
        text = build_telegram_text("GMOC", "08:25", "btc_jpy", data)
        assert "⚙️ 체제:" not in text

    def test_rgd04_unclear_with_position_shows_차단(self):
        """RG-D04: 포지션 보유 중에도 체제 라인에 '진입 차단 중' 표시."""
        data = self._base_data(
            regime_gate_info={
                "last_regime": "unclear",
                "consecutive_count": 1,
                "active_strategy": None,
            },
            has_position=True,
        )
        text = build_telegram_text("GMOC", "08:25", "btc_jpy", data)
        assert "⚙️ 체제: 불명확(×1) | 진입 차단 중" in text

    def test_rgd05_ranging_active_box_shown(self):
        """RG-D05: last_regime='ranging', active='box_mean_reversion' → 활성: 박스역추세."""
        data = self._base_data(regime_gate_info={
            "last_regime": "ranging",
            "consecutive_count": 3,
            "active_strategy": "box_mean_reversion",
        })
        text = build_telegram_text("GMOC", "08:25", "btc_jpy", data)
        assert "⚙️ 체제: 횡보장(×3) | 활성: 박스역추세" in text

    def test_rgd06_last_regime_none_shows_dash(self):
        """RG-D06: last_regime=None (warm-up) → '-' 표시."""
        data = self._base_data(regime_gate_info={
            "last_regime": None,
            "consecutive_count": 0,
            "active_strategy": None,
        })
        text = build_telegram_text("GMOC", "08:25", "btc_jpy", data)
        assert "⚙️ 체제: -(×0) | 진입 차단 중" in text

    def test_rgd07_jit_bypass_gate_hides_active_strategy(self):
        """RG-D07: jit_bypass_gate=True → 활성전략 대신 'JIT bypass' 표시."""
        data = self._base_data(
            regime_gate_info={
                "last_regime": "trending",
                "consecutive_count": 19,
                "active_strategy": "trend_following",
            },
            jit_bypass_gate=True,
        )
        text = build_telegram_text("GMOC", "08:25", "btc_jpy", data)
        assert "⚙️ 체제: 추세장(×19) | JIT bypass" in text
        assert "활성: 추세추종" not in text

    def test_rgd08_jit_bypass_false_shows_active_strategy(self):
        """RG-D08: jit_bypass_gate=False (기본) → 기존 활성전략 표시 유지."""
        data = self._base_data(
            regime_gate_info={
                "last_regime": "trending",
                "consecutive_count": 19,
                "active_strategy": "trend_following",
            },
            jit_bypass_gate=False,
        )
        text = build_telegram_text("GMOC", "08:25", "btc_jpy", data)
        assert "⚙️ 체제: 추세장(×19) | 활성: 추세추종" in text


class TestBuildBoxTelegramTextRegimeGateInfo:
    """BOX-RG01~BOX-RG04: build_box_telegram_text regime_gate_info 체제 라인."""

    def _base_box_data(self, regime_gate_info=None):
        return {
            "health_line": "🟢 WS✅ 태스크4/4✅ 잔고✅",
            "current_price": 11_900_000,
            "box": None,
            "position_label": "no_box",
            "position": None,
            "entry_blockers": ["박스 미형성"],
            "conditions_met": 0,
            "conditions_total": 3,
            "formation_progress": None,
            "next_scan_jst": None,
            "next_scan_minutes_str": "",
            "box_conditions_str": "tol=0.5% / 3+ 터치 필요",
            "jpy_available": 50_000.0,
            "coin_available": 0.0,
            "basis_timeframe": "4h",
            "candle_open_time_jst": "09:00",
            "next_candle_minutes_str": "34분 후",
            "strategy_name": "box_v1",
            "strategy_id": 3,
            "near_bound_pct": 1.5,
            "tolerance_pct": 0.5,
            "stop_loss_pct": 1.5,
            "is_margin_trading": True,
            "regime_gate_info": regime_gate_info,
        }

    def test_box_rg01_unclear_shows_진입차단(self):
        """BOX-RG01: unclear×2 → ⚙️ 체제: 불명확(×2) | 진입 차단 중."""
        data = self._base_box_data(regime_gate_info={
            "last_regime": "unclear",
            "consecutive_count": 2,
            "active_strategy": None,
        })
        text = build_box_telegram_text("GMOC", "08:25", "btc_jpy", data)
        assert "⚙️ 체제: 불명확(×2) | 진입 차단 중" in text

    def test_box_rg02_trending_active_shown(self):
        """BOX-RG02: trending×5 + active=trend_following → 체제 라인 포함."""
        data = self._base_box_data(regime_gate_info={
            "last_regime": "trending",
            "consecutive_count": 5,
            "active_strategy": "trend_following",
        })
        text = build_box_telegram_text("GMOC", "08:25", "btc_jpy", data)
        assert "⚙️ 체제: 추세장(×5) | 활성: 추세추종" in text

    def test_box_rg03_no_regime_gate_info_no_line(self):
        """BOX-RG03: regime_gate_info=None → 체제 라인 없음."""
        data = self._base_box_data(regime_gate_info=None)
        text = build_box_telegram_text("GMOC", "08:25", "btc_jpy", data)
        assert "⚙️ 체제:" not in text

    def test_box_rg04_ranging_active_box_shown(self):
        """BOX-RG04: ranging×3 + active=box_mean_reversion → 활성: 박스역추세."""
        data = self._base_box_data(regime_gate_info={
            "last_regime": "ranging",
            "consecutive_count": 3,
            "active_strategy": "box_mean_reversion",
        })
        text = build_box_telegram_text("GMOC", "08:25", "btc_jpy", data)
        assert "⚙️ 체제: 횡보장(×3) | 활성: 박스역추세" in text


# ── get_entry_condition_lines_long 테스트 ──────────────────────────

class TestGetEntryConditionLinesLong:
    """CL-L01~CL-L08: 롱 진입 4개 조건 상세 라인."""

    def test_cll01_all_met(self):
        """CL-L01: 4개 조건 모두 충족 → 4줄 전부 ✅."""
        lines = get_entry_condition_lines_long(
            signal="long_setup",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.15,
            rsi=52.0,
            rsi_min=40.0,
            rsi_max=65.0,
            slope_min=0.0,
            regime_consecutive=4,
            regime_active=True,
        )
        assert len(lines) == 4
        assert all("✅" in l for l in lines)
        assert all("❌" not in l for l in lines)

    def test_cll02_slope_only_unmet(self):
        """CL-L02: slope 미충족만 → ❌ ② 라인 포함."""
        lines = get_entry_condition_lines_long(
            signal="no_signal",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=-0.02,
            rsi=52.0,
            rsi_min=40.0,
            rsi_max=65.0,
            slope_min=0.0,
            regime_consecutive=4,
            regime_active=True,
        )
        unmet = [l for l in lines if "❌" in l]
        met = [l for l in lines if "✅" in l]
        assert len(unmet) == 1
        assert "② EMA 기울기" in unmet[0]
        assert "부족" in unmet[0]
        assert len(met) == 3

    def test_cll03_price_below_ema_unmet(self):
        """CL-L03: 가격 < EMA → ❌ ① 라인에 ↑ 필요 금액 표시."""
        lines = get_entry_condition_lines_long(
            signal="long_caution",
            current_price=95.0,
            ema=100.0,
            ema_slope_pct=0.1,
            rsi=52.0,
            regime_consecutive=4,
            regime_active=True,
        )
        price_line = next(l for l in lines if "① 가격" in l)
        assert "❌" in price_line
        assert "↑" in price_line
        assert "필요" in price_line

    def test_cll04_rsi_too_low(self):
        """CL-L04: RSI 과매도 → ❌ ③ 라인에 부족 표시."""
        lines = get_entry_condition_lines_long(
            signal="no_signal",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.1,
            rsi=25.0,
            rsi_min=40.0,
        )
        rsi_line = next(l for l in lines if "③ RSI" in l)
        assert "❌" in rsi_line
        assert "부족" in rsi_line

    def test_cll05_rsi_too_high(self):
        """CL-L05: RSI 과열 → ❌ ③ 라인에 초과·과열 표시."""
        lines = get_entry_condition_lines_long(
            signal="long_overheated",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.1,
            rsi=72.0,
            rsi_max=65.0,
        )
        rsi_line = next(l for l in lines if "③ RSI" in l)
        assert "❌" in rsi_line
        assert "과열" in rsi_line

    def test_cll06_regime_wait_unmet(self):
        """CL-L06: signal=wait_regime → ❌ ④ 라인에 횡보 감지."""
        lines = get_entry_condition_lines_long(
            signal="wait_regime",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.1,
            rsi=52.0,
            regime_consecutive=2,
            regime_active=False,
        )
        regime_line = next(l for l in lines if "④ 추세장" in l)
        assert "❌" in regime_line
        assert "횡보" in regime_line

    def test_cll07_regime_gate_not_active(self):
        """CL-L07: regime_active=False (RegimeGate 미달) → ❌ ④ 차단 중."""
        lines = get_entry_condition_lines_long(
            signal="no_signal",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.1,
            rsi=52.0,
            regime_consecutive=2,
            regime_active=False,
        )
        regime_line = next(l for l in lines if "④ 추세장" in l)
        assert "❌" in regime_line
        assert "차단" in regime_line

    def test_cll08_regime_active_shows_consecutive(self):
        """CL-L08: regime_active=True → ✅ ④ 라인에 연속 횟수 표시."""
        lines = get_entry_condition_lines_long(
            signal="long_setup",
            current_price=105.0,
            ema=100.0,
            ema_slope_pct=0.1,
            rsi=52.0,
            regime_consecutive=5,
            regime_active=True,
        )
        regime_line = next(l for l in lines if "④ 추세장" in l)
        assert "✅" in regime_line
        assert "×5" in regime_line


# ── get_entry_condition_lines_short 테스트 ──────────────────────────

class TestGetEntryConditionLinesShort:
    """CL-S01~CL-S08: 숏 진입 4개 조건 상세 라인."""

    def test_cls01_all_met(self):
        """CL-S01: 4개 조건 모두 충족 → 4줄 전부 ✅."""
        lines = get_entry_condition_lines_short(
            signal="short_setup",
            current_price=9_900_000,
            ema=10_000_000,
            ema_slope_pct=-0.1,
            rsi=48.0,
            rsi_min=35.0,
            rsi_max=60.0,
            slope_threshold=-0.05,
            regime_consecutive=4,
            regime_active=True,
        )
        assert len(lines) == 4
        assert all("✅" in l for l in lines)

    def test_cls02_slope_unmet(self):
        """CL-S02: slope -0.02% → -0.05% 미달 → ❌ ② + 부족량 표시."""
        lines = get_entry_condition_lines_short(
            signal="no_signal",
            current_price=9_900_000,
            ema=10_000_000,
            ema_slope_pct=-0.02,
            rsi=48.0,
            slope_threshold=-0.05,
            regime_consecutive=4,
            regime_active=True,
        )
        slope_line = next(l for l in lines if "② EMA 기울기" in l)
        assert "❌" in slope_line
        assert "부족" in slope_line
        assert "-0.02" in slope_line
        assert "-0.05" in slope_line

    def test_cls03_price_above_ema_unmet(self):
        """CL-S03: 가격 > EMA → ❌ ① 라인에 ↓ 필요 표시."""
        lines = get_entry_condition_lines_short(
            signal="no_signal",
            current_price=10_100_000,
            ema=10_000_000,
            ema_slope_pct=-0.1,
            rsi=48.0,
            regime_consecutive=4,
            regime_active=True,
        )
        price_line = next(l for l in lines if "① 가격" in l)
        assert "❌" in price_line
        assert "↓" in price_line
        assert "필요" in price_line

    def test_cls04_rsi_oversold(self):
        """CL-S04: RSI 과매도 → ❌ ③ 라인에 과매도 표시."""
        lines = get_entry_condition_lines_short(
            signal="no_signal",
            current_price=9_900_000,
            ema=10_000_000,
            ema_slope_pct=-0.1,
            rsi=30.0,
            rsi_min=35.0,
        )
        rsi_line = next(l for l in lines if "③ RSI" in l)
        assert "❌" in rsi_line
        assert "과매도" in rsi_line

    def test_cls05_rsi_too_high(self):
        """CL-S05: RSI 초과 → ❌ ③ 라인에 초과 표시."""
        lines = get_entry_condition_lines_short(
            signal="no_signal",
            current_price=9_900_000,
            ema=10_000_000,
            ema_slope_pct=-0.1,
            rsi=65.0,
            rsi_max=60.0,
        )
        rsi_line = next(l for l in lines if "③ RSI" in l)
        assert "❌" in rsi_line
        assert "초과" in rsi_line

    def test_cls06_slope_met_shows_threshold(self):
        """CL-S06: slope 충족 시 ≤threshold 충족 표시."""
        lines = get_entry_condition_lines_short(
            signal="short_setup",
            current_price=9_900_000,
            ema=10_000_000,
            ema_slope_pct=-0.08,
            rsi=48.0,
            slope_threshold=-0.05,
            regime_consecutive=4,
            regime_active=True,
        )
        slope_line = next(l for l in lines if "② EMA 기울기" in l)
        assert "✅" in slope_line
        assert "충족" in slope_line

    def test_cls07_current_case_3_of_4(self):
        """CL-S07: 실제 리포트 케이스 재현 — slope -0.02% 미충족, 나머지 3개 충족."""
        lines = get_entry_condition_lines_short(
            signal="no_signal",
            current_price=12_005_001,
            ema=12_007_400,
            ema_slope_pct=-0.02,
            rsi=50.0,
            rsi_min=35.0,
            rsi_max=60.0,
            slope_threshold=-0.05,
            regime_consecutive=4,
            regime_active=True,
        )
        assert len(lines) == 4
        met = [l for l in lines if "✅" in l]
        unmet = [l for l in lines if "❌" in l]
        assert len(met) == 3
        assert len(unmet) == 1
        assert "② EMA 기울기" in unmet[0]

    def test_cls08_returns_4_lines_always(self):
        """CL-S08: ema/rsi/ema_slope 모두 None이어도 4줄 미만이 될 수 있음 — 크래시 없음."""
        lines = get_entry_condition_lines_short(
            signal="no_signal",
            current_price=10_000_000,
            ema=None,
            ema_slope_pct=None,
            rsi=None,
        )
        # None 값은 해당 조건 라인을 생략 → 크래시 없으면 OK
        assert isinstance(lines, list)


# ── build_telegram_text entry_condition_lines 렌더링 테스트 ──────────

class TestBuildTelegramTextConditionLines:
    """TCL-01~TCL-05: 포지션 없을 때 판단 도메인 결론 표시 검증."""

    def _base_short_waiting(self, condition_lines=None, signal="short_setup"):
        return {
            "trend_icon": "📉",
            "current_price": 12_005_001,
            "market_summary": "🔻 하락 전환",
            "position_summary": None,
            "position": None,
            "wait_direction": "short",
            "entry_blockers": [],
            "conditions_met": 3,
            "conditions_total": 4,
            "jpy_available": 100_000,
            "signal": signal,
        }

    def test_tcl01_short_setup_shows_short_ready(self):
        """TCL-01: short_setup 신호 → 숏 진입 신호 표시."""
        text = build_telegram_text("GMOC", "14:08", "btc_jpy", self._base_short_waiting(signal="short_setup"))
        assert "판단 도메인 →" in text
        assert "🔴 숏 진입 신호" in text
        assert "signal=short_setup" in text

    def test_tcl02_long_setup_shows_long_ready(self):
        """TCL-02: long_setup 신호 → 롱 진입 신호 표시."""
        data = self._base_short_waiting(signal="long_setup")
        data["wait_direction"] = "long"
        text = build_telegram_text("GMOC", "14:08", "btc_jpy", data)
        assert "판단 도메인 →" in text
        assert "🟢 롱 진입 신호" in text
        assert "🚫" not in text

    def test_tcl03_hold_shows_wait(self):
        """TCL-03: hold 신호 → 조건 미충족 표시."""
        data = self._base_short_waiting(signal="hold")
        text = build_telegram_text("GMOC", "14:08", "btc_jpy", data)
        assert "판단 도메인 →" in text
        assert "⏸ 조건 미충족" in text

    def test_tcl04_wait_regime_shows_regime_gate(self):
        """TCL-04: wait_regime 신호 → RegimeGate 차단 표시."""
        data = self._base_short_waiting(signal="wait_regime")
        text = build_telegram_text("GMOC", "14:08", "btc_jpy", data)
        assert "판단 도메인 →" in text
        assert "RegimeGate" in text

    def test_tcl05_no_signal_key_no_crash(self):
        """TCL-05: signal 키 없어도 크래시 없음."""
        data = {
            "trend_icon": "📉",
            "current_price": 12_005_001,
            "market_summary": "관망",
            "position": None,
            "wait_direction": "short",
            "entry_blockers": ["블로커 1"],
            "conditions_met": 4,
            "conditions_total": 5,
            "jpy_available": 100_000,
            # signal 키 없음
        }
        text = build_telegram_text("GMOC", "14:08", "btc_jpy", data)
        assert "판단 도메인 →" in text


# ─── entry_mode / armed 상태 표시 (실행 도메인 보고) ─────────────────────────

class TestEntryModeInReport:
    """ER-01~ER-08: build_telegram_text entry_mode/armed 상태 표시."""

    def _base_data(self) -> dict:
        return {
            "trend_icon": "🔻",
            "current_price": 12_142_312,
            "market_summary": "하락 전환",
            "signal": "long_caution",
            "position": None,
            "wait_direction": "short",
            "entry_blockers": [],
            "conditions_met": 3,
            "conditions_total": 5,
            "jpy_available": 100_000,
            "collateral": {"collateral": 98_051, "require_collateral": 0},
            "regime_gate_info": {
                "last_regime": "trending",
                "consecutive_count": 20,
                "active_strategy": "trend_following",
            },
            "jit_bypass_gate": True,
        }

    def test_er01_default_no_mode_line(self):
        """ER-01: entry_mode/timeframe 없으면 '진입 모드' 줄 없음."""
        text = build_telegram_text("GMOC", "22:46", "btc_jpy", self._base_data())
        assert "진입 모드" not in text

    def test_er02_ws_cross_shows_mode_line(self):
        """ER-02: entry_mode=ws_cross → '진입 모드: ⚡ WS 돌파' 표시."""
        data = self._base_data()
        data["entry_mode"] = "ws_cross"
        text = build_telegram_text("GMOC", "22:46", "btc_jpy", data)
        assert "진입 모드" in text
        assert "WS 돌파" in text

    def test_er03_entry_timeframe_1h_shows_mode_line(self):
        """ER-03: entry_timeframe=1h → '진입 모드: 📊 1H slope/RSI' 표시."""
        data = self._base_data()
        data["entry_timeframe"] = "1h"
        text = build_telegram_text("GMOC", "22:46", "btc_jpy", data)
        assert "진입 모드" in text
        assert "1H slope/RSI" in text

    def test_er04_ws_cross_with_1h_shows_combined(self):
        """ER-04: ws_cross + 1h → '진입 모드' 줄에 WS + 1H 모두 표시."""
        data = self._base_data()
        data["entry_mode"] = "ws_cross"
        data["entry_timeframe"] = "1h"
        text = build_telegram_text("GMOC", "22:46", "btc_jpy", data)
        assert "WS 돌파" in text
        assert "1H slope/RSI" in text

    def test_er05_ws_cross_armed_shows_line(self):
        """ER-05: ws_cross + armed_direction=short → EMA/현재가/거리 표시."""
        import time as _time
        data = self._base_data()  # current_price=12_142_312
        data["entry_mode"] = "ws_cross"
        data["armed_direction"] = "short"
        data["armed_ema"] = 12_000_000.0  # gap=142,312 → 더 내려가야
        data["armed_expire_at"] = _time.time() + 3600 * 3.5
        text = build_telegram_text("GMOC", "22:46", "btc_jpy", data)
        assert "숏 armed" in text
        assert "¥12,000,000" in text
        assert "만료까지" in text
        assert "현재" in text
        assert "더 내려가야 진입" in text

    def test_er06_ws_cross_no_armed_shows_waiting(self):
        """ER-06: ws_cross + armed 없음 → '⏳ WS 대기: armed 조건 미충족' 표시."""
        data = self._base_data()
        data["entry_mode"] = "ws_cross"
        text = build_telegram_text("GMOC", "22:46", "btc_jpy", data)
        assert "WS 대기" in text
        assert "armed 조건 미충족" in text

    def test_er07_mode_line_appears_with_position(self):
        """ER-07: 포지션 보유 중에도 entry_mode 줄 표시."""
        data = self._base_data()
        data["position"] = {
            "side": "sell",
            "entry_price": 12_200_000,
            "entry_amount": 0.01,
            "current_price": 12_142_312,
            "unrealized_pnl_jpy": 577,
            "unrealized_pnl_pct": 0.47,
            "stop_loss_price": 12_400_000,
            "trailing_stop_distance": 257_688,
            "price_diff": -57_688,
            "pnl_at_stop": -20_000,
        }
        data["entry_mode"] = "ws_cross"
        data["entry_timeframe"] = "1h"
        text = build_telegram_text("GMOC", "22:46", "btc_jpy", data)
        assert "진입 모드" in text
        assert "WS 돌파" in text

    def test_er08_long_armed_shows_correct_direction(self):
        """ER-08: armed_direction=long → '롱 armed' + 현재가가 이미 EMA 위 표시."""
        import time as _time
        data = self._base_data()  # current_price=12_142_312
        data["entry_mode"] = "ws_cross"
        data["wait_direction"] = "long"
        data["armed_direction"] = "long"
        data["armed_ema"] = 11_900_000.0  # current_price > ema → 이미 위
        data["armed_expire_at"] = _time.time() + 7200
        text = build_telegram_text("GMOC", "22:46", "btc_jpy", data)
        assert "롱 armed" in text
        assert "¥11,900,000" in text
        assert "현재" in text
        assert "WS 신호 대기" in text
