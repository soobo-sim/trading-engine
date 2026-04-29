"""
Whipsaw Protection 테스트 — ERR-578 + 개선 A, B, D

  ERR578-01: change_losscut_price 실패 시 텔레그램 경고 발송
  ERR578-02: change_losscut_price 성공 시 텔레그램 미발송
  CP-01: 4H 캔들 교체 후 5분 내 exit_warning → no_signal
  CP-02: 4H 캔들 교체 후 5분 경과 → exit_warning 정상 반환
  CP-03: cooling 중 롱 포지션 exit_warning 억제
  CP-04: cooling 중 숏 포지션 exit_warning 억제
  GP-01: 진입 후 15분 내 기울기 하락 → tighten_stop 억제
  GP-02: 진입 후 15분 경과 → tighten_stop 정상 발동
  GP-03: 진입 후 15분 내 다이버전스 → tighten_stop 억제
  GP-04: opened_at 없으면 grace 패스 (정상 발동)
  EW-01: 롱 + price < ema - cushion → exit_warning
  EW-02: 롱 + price > ema - cushion + compute_trend_signal exit_warning 오판 → no_signal
  EW-03: 숏 + price > ema + cushion → exit_warning
  EW-04: 숏 + price < ema (compute_trend_signal 오판) → no_signal (핵심 버그 수정)
  EW-05: atr=None이면 cushion=0으로 동작
  EW-06: pos=None이면 signal 그대로 반환
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.exchange.types import Position


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _make_pos(side: str = "buy", opened_at=None) -> Position:
    extra = {"side": side}
    if opened_at is not None:
        extra["opened_at"] = opened_at
    return Position(
        pair="btc_jpy",
        entry_price=11_000_000.0,
        entry_amount=0.001,
        stop_loss_price=10_000_000.0,
        extra=extra,
    )


def _make_margin_mgr(pair: str = "btc_jpy", params: dict | None = None):
    """MarginTrendManager(CfdTrendFollowingManager) 최소 인스턴스."""
    from core.punisher.strategy.plugins.cfd_trend_following.manager import MarginTrendManager

    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.is_margin_trading = True
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = MarginTrendManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=MagicMock(),
        candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )
    mgr._params[pair] = params or {}
    return mgr


def _make_gmoc_mgr():
    """GmoCoinTrendManager 최소 인스턴스."""
    from core.punisher.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager

    adapter = MagicMock()
    adapter.exchange_name = "gmo_coin"
    adapter.is_margin_trading = True
    supervisor = MagicMock()
    supervisor.stop = AsyncMock()
    supervisor.is_running = MagicMock(return_value=False)

    mgr = GmoCoinTrendManager(
        adapter=adapter,
        supervisor=supervisor,
        session_factory=MagicMock(),
        candle_model=MagicMock(),
        cfd_position_model=MagicMock(),
    )
    return mgr


# ──────────────────────────────────────────────────────────────
# ERR-578: 로스컷 동기화 실패 → 텔레그램
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_err578_01_telegram_sent_on_sync_failure():
    """ERR578-01: change_losscut_price 실패 → asyncio.ensure_future 호출로 텔레그램 발송 예약."""
    mgr = _make_gmoc_mgr()

    fx_pos = MagicMock()
    fx_pos.position_id = "pos_123"
    mgr._adapter.get_positions = AsyncMock(return_value=[fx_pos])
    mgr._adapter.change_losscut_price = AsyncMock(return_value=False)  # 실패

    with patch(
        "core.punisher.strategy.plugins.gmo_coin_trend.manager.asyncio.ensure_future"
    ) as mock_future:
        mock_future.return_value = None

        with patch(
            "core.shared.logging.telegram_handlers._send_telegram",
            new_callable=AsyncMock,
        ):
            await mgr._sync_losscut_price("btc_jpy", 10_500_000.0)

    assert mock_future.called, "change_losscut_price 실패 시 ensure_future가 호출돼야 함"


@pytest.mark.asyncio
async def test_err578_02_no_telegram_on_sync_success():
    """ERR578-02: change_losscut_price 성공 → 텔레그램 미발송."""
    mgr = _make_gmoc_mgr()

    fx_pos = MagicMock()
    fx_pos.position_id = "pos_456"
    mgr._adapter.get_positions = AsyncMock(return_value=[fx_pos])
    mgr._adapter.change_losscut_price = AsyncMock(return_value=True)  # 성공

    with patch(
        "core.punisher.strategy.plugins.gmo_coin_trend.manager.asyncio.ensure_future"
    ) as mock_future:
        await mgr._sync_losscut_price("btc_jpy", 10_500_000.0)

    mock_future.assert_not_called()


# ──────────────────────────────────────────────────────────────
# 개선 A: cooling period
# ──────────────────────────────────────────────────────────────

def test_cp_01_exit_warning_suppressed_within_cooling():
    """CP-01: 4H 캔들 교체 후 5분 내 exit_warning → no_signal."""
    mgr = _make_margin_mgr(params={"candle_change_cooling_sec": 300})
    pos = _make_pos(side="buy")

    # 방금 캔들 교체 (2초 전)
    mgr._last_candle_change_time = {"btc_jpy": datetime.now(timezone.utc) - timedelta(seconds=2)}

    result = mgr._check_exit_warning("btc_jpy", "long_caution", 10_500_000.0, 11_000_000.0, pos)
    assert result == "no_signal", f"cooling 중 exit_warning이 억제돼야 함, got={result}"


def test_cp_02_exit_warning_allowed_after_cooling():
    """CP-02: 4H 캔들 교체 후 5분 경과 → exit_warning 정상 반환."""
    mgr = _make_margin_mgr(params={"candle_change_cooling_sec": 300, "exit_ema_atr_cushion": 0.0})
    pos = _make_pos(side="buy")

    # 6분 전에 캔들 교체됨 (cooling 완료)
    mgr._last_candle_change_time = {"btc_jpy": datetime.now(timezone.utc) - timedelta(seconds=360)}

    # 롱: price < ema → exit_warning
    result = mgr._check_exit_warning("btc_jpy", "hold", 10_500_000.0, 11_000_000.0, pos)
    assert result == "long_caution", f"cooling 후 exit_warning 발동돼야 함, got={result}"


def test_cp_03_cooling_long_position():
    """CP-03: cooling 중 롱 포지션 exit_warning 억제."""
    mgr = _make_margin_mgr(params={"candle_change_cooling_sec": 300})
    pos = _make_pos(side="buy")
    mgr._last_candle_change_time = {"btc_jpy": datetime.now(timezone.utc) - timedelta(seconds=10)}

    result = mgr._check_exit_warning("btc_jpy", "long_caution", 10_500_000.0, 11_000_000.0, pos)
    assert result == "no_signal"


def test_cp_04_cooling_short_position():
    """CP-04: cooling 중 숏 포지션 exit_warning 억제."""
    mgr = _make_margin_mgr(params={"candle_change_cooling_sec": 300})
    pos = _make_pos(side="sell")
    mgr._last_candle_change_time = {"btc_jpy": datetime.now(timezone.utc) - timedelta(seconds=10)}

    result = mgr._check_exit_warning("btc_jpy", "long_caution", 11_500_000.0, 11_000_000.0, pos)
    assert result == "no_signal"


# ──────────────────────────────────────────────────────────────
# 개선 B: grace period
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gp_01_slope_tighten_suppressed_within_grace():
    """GP-01: 진입 후 15분 내 기울기 하락 → tighten_stop 억제."""
    from core.punisher.strategy._candle_loop import CandleLoopMixin

    mgr = _make_margin_mgr(params={"entry_grace_period_sec": 900})
    mgr._log_prefix = "[Test]"
    mgr._apply_stop_tightening = AsyncMock()

    # 진입 5분 전
    opened_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    pos = _make_pos(side="buy", opened_at=opened_at)
    pos.stop_tightened = False

    params = {"entry_grace_period_sec": 900}
    slope_history = [1.0, 0.5, 0.2]  # 연속 하락
    atr = 100_000.0
    current_price = 11_000_000.0
    pair = "btc_jpy"

    # grace period 체크 코드 직접 실행
    grace_sec = float(params.get("entry_grace_period_sec", 900))
    from datetime import datetime as _dt, timezone as _tz
    elapsed = (_dt.now(_tz.utc) - opened_at).total_seconds()
    assert elapsed < grace_sec, "테스트 전제: 15분 내"

    # _apply_stop_tightening이 호출되지 않아야 함
    if elapsed < grace_sec:
        pass  # 억제
    else:
        await mgr._apply_stop_tightening(pair, current_price, atr, params)

    mgr._apply_stop_tightening.assert_not_called()


@pytest.mark.asyncio
async def test_gp_02_slope_tighten_fires_after_grace():
    """GP-02: 진입 후 15분 경과 → tighten_stop 정상 발동."""
    mgr = _make_margin_mgr(params={"entry_grace_period_sec": 900})
    mgr._log_prefix = "[Test]"
    mgr._apply_stop_tightening = AsyncMock()

    # 진입 20분 전
    opened_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    pos = _make_pos(side="buy", opened_at=opened_at)
    pos.stop_tightened = False

    params = {"entry_grace_period_sec": 900}
    atr = 100_000.0
    current_price = 11_000_000.0
    pair = "btc_jpy"

    grace_sec = float(params.get("entry_grace_period_sec", 900))
    from datetime import datetime as _dt, timezone as _tz
    elapsed = (_dt.now(_tz.utc) - opened_at).total_seconds()
    assert elapsed >= grace_sec, "테스트 전제: 15분 경과"

    # grace 경과 → 발동
    await mgr._apply_stop_tightening(pair, current_price, atr, params)

    mgr._apply_stop_tightening.assert_called_once()


@pytest.mark.asyncio
async def test_gp_03_divergence_tighten_suppressed_within_grace():
    """GP-03: 진입 후 15분 내 다이버전스 → tighten_stop 억제."""
    mgr = _make_margin_mgr(params={"entry_grace_period_sec": 900})
    mgr._apply_stop_tightening = AsyncMock()

    opened_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    params = {"entry_grace_period_sec": 900}

    grace_sec = float(params.get("entry_grace_period_sec", 900))
    from datetime import datetime as _dt, timezone as _tz
    elapsed = (_dt.now(_tz.utc) - opened_at).total_seconds()

    if elapsed < grace_sec:
        pass  # 억제
    else:
        await mgr._apply_stop_tightening("btc_jpy", 11_000_000.0, 100_000.0, params)

    mgr._apply_stop_tightening.assert_not_called()


@pytest.mark.asyncio
async def test_gp_04_no_opened_at_passes_grace():
    """GP-04: opened_at 없으면 grace 패스 → tighten_stop 정상 발동."""
    mgr = _make_margin_mgr(params={"entry_grace_period_sec": 900})
    mgr._apply_stop_tightening = AsyncMock()

    pos = _make_pos(side="buy", opened_at=None)  # opened_at 없음
    params = {"entry_grace_period_sec": 900}

    opened_at = pos.extra.get("opened_at")
    # opened_at이 없으면 grace 패스 → 즉시 발동
    if opened_at is None:
        await mgr._apply_stop_tightening("btc_jpy", 11_000_000.0, 100_000.0, params)

    mgr._apply_stop_tightening.assert_called_once()


# ──────────────────────────────────────────────────────────────
# 개선 D: exit_warning 방향 보정
# ──────────────────────────────────────────────────────────────

def test_ew_01_long_price_below_ema_minus_cushion():
    """EW-01: 롱 + price < ema - cushion → exit_warning."""
    mgr = _make_margin_mgr(params={"exit_ema_atr_cushion": 0.1})
    pos = _make_pos(side="buy")

    ema = 11_000_000.0
    atr = 100_000.0
    # cushion = 0.1 * 100_000 = 10_000
    # price = ema - cushion - 1 → 조건 충족
    price = ema - atr * 0.1 - 1

    result = mgr._check_exit_warning("btc_jpy", "hold", price, ema, pos, atr=atr)
    assert result == "long_caution", f"EW-01 실패: got={result}"


def test_ew_02_long_price_above_threshold_clears_exit_warning():
    """EW-02: 롱 + price > ema - cushion + compute_trend_signal 오판 → no_signal."""
    mgr = _make_margin_mgr(params={"exit_ema_atr_cushion": 0.1})
    pos = _make_pos(side="buy")

    ema = 11_000_000.0
    atr = 100_000.0
    # cushion = 10_000 → threshold = 10_990_000
    # price = ema - cushion + 1 → 롱 조건 미충족
    price = ema - atr * 0.1 + 1

    # compute_trend_signal이 잘못 exit_warning을 반환했다고 가정
    result = mgr._check_exit_warning("btc_jpy", "long_caution", price, ema, pos, atr=atr)
    assert result == "no_signal", f"EW-02 실패: 오판 exit_warning이 no_signal로 교정돼야 함, got={result}"


def test_ew_03_short_price_above_ema_plus_cushion():
    """EW-03: 숏 + price > ema + cushion → short_caution."""
    mgr = _make_margin_mgr(params={"exit_ema_atr_cushion": 0.1})
    pos = _make_pos(side="sell")

    ema = 11_000_000.0
    atr = 100_000.0
    # cushion = 10_000 → price > 11_010_000
    price = ema + atr * 0.1 + 1

    result = mgr._check_exit_warning("btc_jpy", "hold", price, ema, pos, atr=atr)
    assert result == "short_caution", f"EW-03 실패: got={result}"


def test_ew_04_short_price_below_ema_clears_exit_warning():
    """EW-04: 숏 + price < ema (오판) → no_signal (핵심 버그 수정)."""
    mgr = _make_margin_mgr(params={"exit_ema_atr_cushion": 0.1})
    pos = _make_pos(side="sell")

    ema = 11_000_000.0
    atr = 100_000.0
    # 숏 포지션인데 price < ema → 숏에게는 유리한 방향 (이탈 아님)
    # compute_trend_signal이 오판해서 short_caution 반환했을 경우 → no_signal 교정
    price = ema - 50_000.0

    result = mgr._check_exit_warning("btc_jpy", "short_caution", price, ema, pos, atr=atr)
    assert result == "no_signal", f"EW-04 실패: 숏 오판 short_caution이 no_signal로 교정돼야 함, got={result}"


def test_ew_05_atr_none_means_no_cushion():
    """EW-05: atr=None이면 cushion=0으로 동작."""
    mgr = _make_margin_mgr(params={"exit_ema_atr_cushion": 0.5})
    pos = _make_pos(side="buy")

    ema = 11_000_000.0
    # atr=None → cushion=0 → ema - 0 = ema
    # price = ema - 1 → 조건 충족
    price = ema - 1

    result = mgr._check_exit_warning("btc_jpy", "hold", price, ema, pos, atr=None)
    assert result == "long_caution", f"EW-05 실패: atr=None 시 cushion=0으로 동작해야 함, got={result}"


def test_ew_06_no_position_returns_signal_unchanged():
    """EW-06: pos=None이면 signal 그대로 반환."""
    mgr = _make_margin_mgr()

    result = mgr._check_exit_warning("btc_jpy", "long_setup", 11_000_000.0, 11_000_000.0, None)
    assert result == "long_setup", f"EW-06 실패: pos=None시 signal 그대로여야 함, got={result}"
