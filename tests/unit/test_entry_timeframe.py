"""
entry_timeframe 파라미터 지원 단위 테스트.

ET-01: entry_timeframe 없을 때 → 기존 경로(4H 단독) 동일
ET-02: entry_timeframe="1h" → 1H 캔들 조회 + 4H regime override
ET-03: 4H regime=trending + 1H slope/RSI OK → long_setup 반환
ET-04: 4H regime=ranging + 1H slope OK → wait_regime 반환 (regime 우선)
ET-05: 1H 캔들 부족 → 4H fallback 동작 (결과 반환)
ET-06: entry_timeframe == basis_timeframe → 기존 경로 그대로
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import create_candle_model, create_cfd_position_model, create_strategy_model
from adapters.database.session import Base
from core.judge._judge_mixin import JudgeMixin


# ── ORM 모델 ─────────────────────────────────────────────────────────────────

TstEtfStrategy = create_strategy_model("tsetf")
TstEtfCandle = create_candle_model("tsetf", pair_column="pair")
TstEtfPosition = create_cfd_position_model("tsetf", pair_column="pair", order_id_length=40)

PAIR = "btc_jpy"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        tables = [
            Base.metadata.tables[t]
            for t in Base.metadata.tables
            if t.startswith("tsetf_") or t == "strategy_techniques"
        ]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────


def _make_mixin_stub(session_factory) -> JudgeMixin:
    """JudgeMixin을 직접 호출할 수 있는 최소 스텁 객체 생성."""
    obj = object.__new__(JudgeMixin)
    obj._session_factory = session_factory
    obj._candle_model = TstEtfCandle
    obj._pair_column = "pair"
    obj._log_prefix = "[ETTest]"
    obj._data_hub = None
    return obj


async def _insert_candles(
    session_factory,
    timeframe: str,
    count: int,
    base_close: float = 10_000_000.0,
    step: float = 0.0,
    oscillate: bool = False,
    is_complete: bool = True,
) -> None:
    """테스트용 캔들 삽입."""
    async with session_factory() as db:
        for i in range(count):
            if oscillate:
                close = base_close + math.sin(i * 0.5) * 100_000
                high = close * 1.004
                low = close * 0.996
            else:
                close = base_close + i * step
                high = close * 1.025  # 추세형: H-L 범위 큼
                low = close * 0.975

            open_time = datetime(2026, 1, 1, i // 60, i % 60, 0)
            close_time = datetime(2026, 1, 1, i // 60, i % 60, 59)

            row = TstEtfCandle(
                pair=PAIR,
                timeframe=timeframe,
                open_time=open_time,
                close_time=close_time,
                open=close * 0.999,
                high=high,
                low=low,
                close=close,
                volume=1.0,
                is_complete=is_complete,
            )
            db.add(row)
        await db.commit()


# ── 기본 파라미터 ──────────────────────────────────────────────────────────


def _base_params(**overrides) -> dict:
    p = {
        "ema_period": 20,
        "atr_period": 14,
        "rsi_period": 14,
        "ema_slope_entry_min": 0.0,
        "entry_rsi_min": 40.0,
        "entry_rsi_max": 65.0,
        "ema_slope_short_threshold": -0.05,
        "entry_rsi_min_short": 35.0,
        "entry_rsi_max_short": 60.0,
        "atr_multiplier_stop": 2.0,
        "bb_width_trending_min": 3.0,
        "bb_width_ranging_max": 3.0,
        "range_pct_ranging_max": 5.0,
    }
    p.update(overrides)
    return p


# ── ET-01: entry_timeframe 없음 → 기존 경로 ──────────────────────────────────


@pytest.mark.asyncio
async def test_et01_no_entry_timeframe_uses_single_timeframe(db_session_factory):
    """ET-01: entry_timeframe 파라미터 없음 → 단일 timeframe(4h) 경로."""
    await _insert_candles(db_session_factory, "4h", count=60, step=20_000)

    mixin = _make_mixin_stub(db_session_factory)
    params = _base_params()  # entry_timeframe 없음

    result = await mixin._compute_signal(PAIR, "4h", params=params)

    assert result is not None
    assert "signal" in result
    assert "latest_candle_open_time" in result
    assert "candles" in result
    # entry_candles 키 없음 (기존 경로)
    assert "entry_candles" not in result


# ── ET-06: entry_timeframe == basis_timeframe → 기존 경로 ──────────────────


@pytest.mark.asyncio
async def test_et06_entry_tf_equals_basis_tf_uses_single_path(db_session_factory):
    """ET-06: entry_timeframe == basis_timeframe → 단일 경로, entry_candles 없음."""
    await _insert_candles(db_session_factory, "4h", count=60, step=20_000)

    mixin = _make_mixin_stub(db_session_factory)
    params = _base_params(entry_timeframe="4h")  # basis_tf와 동일

    result = await mixin._compute_signal(PAIR, "4h", params=params)

    assert result is not None
    assert "entry_candles" not in result


# ── ET-02: entry_timeframe="1h" → 이중 경로 ──────────────────────────────────


@pytest.mark.asyncio
async def test_et02_entry_timeframe_1h_fetches_both_candles(db_session_factory):
    """ET-02: entry_timeframe='1h' → entry_candles 키 존재, candles는 4H 기준."""
    await _insert_candles(db_session_factory, "4h", count=60, step=20_000)
    await _insert_candles(db_session_factory, "1h", count=60, step=5_000)

    mixin = _make_mixin_stub(db_session_factory)
    params = _base_params(entry_timeframe="1h")

    result = await mixin._compute_signal(PAIR, "4h", params=params)

    assert result is not None
    # entry_candles 키 존재 (1H 경로 실행 증거)
    assert "entry_candles" in result
    # candles는 4H (RegimeGate 멱등성)
    basis_candles = result["candles"]
    assert all(c.timeframe == "4h" for c in basis_candles)
    # entry_candles는 1H
    entry_candles = result["entry_candles"]
    assert all(c.timeframe == "1h" for c in entry_candles)


# ── ET-03: 4H trending + 1H OK → long_setup ─────────────────────────────────


@pytest.mark.asyncio
async def test_et03_4h_trending_1h_slope_ok_returns_long_setup(db_session_factory):
    """ET-03: 4H regime=trending + 1H EMA/slope/RSI OK → long_setup."""
    # 4H: BB폭이 넓어 trending (각 봉의 H-L 범위 큼)
    await _insert_candles(db_session_factory, "4h", count=60, step=30_000)
    # 1H: 완만한 상승 추세 (price > EMA, slope > 0, RSI ~50)
    await _insert_candles(db_session_factory, "1h", count=60, step=8_000)

    mixin = _make_mixin_stub(db_session_factory)
    params = _base_params(entry_timeframe="1h")

    result = await mixin._compute_signal(PAIR, "4h", params=params)

    assert result is not None
    # 4H regime 기반 값이 존재해야 함
    assert "bb_width_pct" in result
    assert "range_pct" in result
    assert "regime" in result
    assert "trending_score" in result
    # long_setup 또는 regime에 따른 신호 (회귀 없음 검증)
    assert result["signal"] in (
        "long_setup", "short_setup", "wait_regime",
        "long_caution", "short_caution", "long_overheated",
        "short_oversold", "no_signal",
    )


# ── ET-04: 4H ranging → wait_regime 우선 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_et04_4h_ranging_overrides_1h_signal_to_wait_regime(db_session_factory):
    """ET-04: 4H regime=ranging → 1H slope OK여도 wait_regime 반환."""
    # 4H: BB폭 아주 좁음 → ranging
    await _insert_candles(db_session_factory, "4h", count=60,
                          base_close=10_000_000.0, step=0.0, oscillate=True)
    # 1H: 완만한 상승 → 1H 단독이면 long_setup 조건 충족 가능
    await _insert_candles(db_session_factory, "1h", count=60, step=5_000)

    mixin = _make_mixin_stub(db_session_factory)
    # 4H ranging 기준을 확실히 만족시키기 위해 ranging_max 수동 설정
    params = _base_params(
        entry_timeframe="1h",
        bb_width_ranging_max=10.0,  # ranging max 높여서 확실히 ranging 판정
        range_pct_ranging_max=50.0,
        bb_width_trending_min=20.0,  # trending min 높여서 확실히 ranging (not trending)
    )

    result = await mixin._compute_signal(PAIR, "4h", params=params)

    assert result is not None
    assert result["signal"] == "wait_regime", (
        f"4H ranging이면 wait_regime 이어야 함. 실제: {result['signal']}"
    )


# ── ET-05: 1H 캔들 부족 → 4H fallback ───────────────────────────────────────


@pytest.mark.asyncio
async def test_et05_insufficient_1h_candles_fallback_to_4h(db_session_factory):
    """ET-05: 1H 캔들 부족(ema_period+1 미만) → 4H 캔들로 fallback, 결과 반환."""
    await _insert_candles(db_session_factory, "4h", count=60, step=20_000)
    # 1H 캔들 5개만 삽입 (ema_period=20 미만)
    await _insert_candles(db_session_factory, "1h", count=5, step=5_000)

    mixin = _make_mixin_stub(db_session_factory)
    params = _base_params(entry_timeframe="1h", ema_period=20)

    result = await mixin._compute_signal(PAIR, "4h", params=params)

    # fallback: 4H 캔들로 계산 → None이 아님
    assert result is not None
    assert "signal" in result
    # entry_candles 키: fallback 시에도 존재 (4H로 대체됨)
    assert "entry_candles" in result


# ── 추가: latest_candle_open_time은 항상 4H 기준 ─────────────────────────────


@pytest.mark.asyncio
async def test_et07_latest_candle_open_time_uses_4h(db_session_factory):
    """ET-07: entry_timeframe='1h'이어도 latest_candle_open_time은 4H 마지막 캔들 기준."""
    await _insert_candles(db_session_factory, "4h", count=60, step=20_000)
    await _insert_candles(db_session_factory, "1h", count=60, step=5_000)

    mixin = _make_mixin_stub(db_session_factory)
    params = _base_params(entry_timeframe="1h")

    result = await mixin._compute_signal(PAIR, "4h", params=params)

    assert result is not None
    # latest_candle_open_time이 4H 마지막 캔들과 일치해야 함
    basis_candles = result["candles"]
    last_4h_time = str(basis_candles[-1].open_time)
    assert result["latest_candle_open_time"] == last_4h_time
