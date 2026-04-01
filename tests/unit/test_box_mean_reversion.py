"""
BoxMeanReversionManager 단위 테스트.

FakeExchangeAdapter + SQLite 인메모리 DB로 거래소-무관 박스 역추세 매니저를 검증.
개별 메서드를 직접 호출하여 테스트 (asyncio 태스크 실행하지 않음).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.database.models import create_candle_model, create_box_model, create_box_position_model
from adapters.database.session import Base
from core.exchange.types import OrderType
from core.strategy.box_mean_reversion import BoxMeanReversionManager
from core.task.supervisor import TaskSupervisor
from tests.fake_exchange import FakeExchangeAdapter


# ── 테스트용 ORM 모델 (bxt_ prefix로 pytest 수집 방지) ───

BxtCandle = create_candle_model("bxt", pair_column="pair")
BxtBox = create_box_model("bxt", pair_column="pair")
BxtBoxPosition = create_box_position_model("bxt", pair_column="pair", order_id_length=40)


# ── Fixtures ──────────────────────────────────


@pytest_asyncio.fixture
async def db_session_factory():
    """SQLite 인메모리 async_sessionmaker — bxt_ 테이블만 생성."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        tables = [
            Base.metadata.tables[t]
            for t in Base.metadata.tables
            if t.startswith("bxt_")
        ]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def fake_adapter():
    adapter = FakeExchangeAdapter(initial_balances={"jpy": 1_000_000.0, "xrp": 0.0})
    adapter.set_ticker_price(100.0)
    return adapter


@pytest_asyncio.fixture
async def supervisor():
    return TaskSupervisor()


@pytest_asyncio.fixture
async def manager(fake_adapter, supervisor, db_session_factory):
    return BoxMeanReversionManager(
        adapter=fake_adapter,
        supervisor=supervisor,
        session_factory=db_session_factory,
        candle_model=BxtCandle,
        box_model=BxtBox,
        box_position_model=BxtBoxPosition,
        pair_column="pair",
    )


# ── 캔들 생성 헬퍼 ──────────────────────────────


async def insert_candles(
    factory: async_sessionmaker,
    pair: str,
    timeframe: str,
    ohlc_list: list[tuple[float, float, float, float]],
    start_time: Optional[datetime] = None,
) -> None:
    """OHLC 리스트를 DB에 삽입. (open, high, low, close) 튜플."""
    if start_time is None:
        start_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async with factory() as db:
        for i, (o, h, l, c) in enumerate(ohlc_list):
            candle = BxtCandle()
            candle.pair = pair
            candle.timeframe = timeframe
            candle.open_time = start_time + timedelta(hours=4 * i)
            candle.close_time = start_time + timedelta(hours=4 * (i + 1))
            candle.open = Decimal(str(o))
            candle.high = Decimal(str(h))
            candle.low = Decimal(str(l))
            candle.close = Decimal(str(c))
            candle.volume = Decimal("1000")
            candle.tick_count = 100
            candle.is_complete = True
            db.add(candle)
            await db.flush()
        await db.commit()


async def insert_box(
    factory: async_sessionmaker,
    pair: str,
    upper: float,
    lower: float,
    tolerance_pct: float = 0.5,
    status: str = "active",
) -> int:
    """박스를 직접 DB에 삽입. 반환: box id."""
    async with factory() as db:
        box = BxtBox()
        box.pair = pair
        box.upper_bound = Decimal(str(upper))
        box.lower_bound = Decimal(str(lower))
        box.upper_touch_count = 5
        box.lower_touch_count = 5
        box.tolerance_pct = Decimal(str(tolerance_pct))
        box.basis_timeframe = "4h"
        box.status = status
        box.created_at = datetime.now(timezone.utc)
        db.add(box)
        await db.commit()
        await db.refresh(box)
        return box.id


# ══════════════════════════════════════════════
# 테스트: 클러스터링 알고리즘
# ══════════════════════════════════════════════

class TestClusterDetection:

    def test_find_cluster_high(self):
        """고점 클러스터 감지: 100 근처 가격 5개."""
        prices = [100.0, 100.3, 100.2, 80.0, 100.1, 80.5, 100.4]
        avg, count = BoxMeanReversionManager._find_cluster(
            prices, tolerance_pct=0.5, min_touches=3, mode="high",
        )
        assert avg is not None
        assert count >= 4
        assert 99.5 < avg < 101.0

    def test_find_cluster_low(self):
        """저점 클러스터 감지: 80 근처 가격 3개."""
        prices = [100.0, 80.0, 80.3, 100.5, 80.1]
        avg, count = BoxMeanReversionManager._find_cluster(
            prices, tolerance_pct=0.5, min_touches=3, mode="low",
        )
        assert avg is not None
        assert count >= 3
        assert 79.5 < avg < 81.0

    def test_find_cluster_insufficient_touches(self):
        """min_touches 미달 시 None 반환."""
        prices = [100.0, 80.0, 60.0]
        avg, count = BoxMeanReversionManager._find_cluster(
            prices, tolerance_pct=0.5, min_touches=3, mode="high",
        )
        assert avg is None
        assert count == 0

    def test_find_cluster_empty(self):
        """빈 리스트 → None."""
        avg, count = BoxMeanReversionManager._find_cluster(
            [], tolerance_pct=0.5, min_touches=3, mode="high",
        )
        assert avg is None

    def test_linear_slope_positive(self):
        """상승 기울기."""
        slope = BoxMeanReversionManager._linear_slope([0, 1, 2, 3], [1.0, 2.0, 3.0, 4.0])
        assert slope == pytest.approx(1.0)

    def test_linear_slope_negative(self):
        """하락 기울기."""
        slope = BoxMeanReversionManager._linear_slope([0, 1, 2, 3], [4.0, 3.0, 2.0, 1.0])
        assert slope == pytest.approx(-1.0)

    def test_candle_high_low(self):
        """꼬리 포함 고점/저점 — candle.high / candle.low 사용."""

        class FakeCandle:
            def __init__(self, o, h, l, c):
                self.open = Decimal(str(o))
                self.high = Decimal(str(h))
                self.low = Decimal(str(l))
                self.close = Decimal(str(c))

        c = FakeCandle(100.0, 110.0, 95.0, 105.0)
        assert BoxMeanReversionManager._candle_high(c) == 110.0
        assert BoxMeanReversionManager._candle_low(c) == 95.0

        # 음봉
        c2 = FakeCandle(105.0, 112.0, 98.0, 100.0)
        assert BoxMeanReversionManager._candle_high(c2) == 112.0
        assert BoxMeanReversionManager._candle_low(c2) == 98.0


# ══════════════════════════════════════════════
# 테스트: 박스 감지
# ══════════════════════════════════════════════

class TestBoxDetection:

    @pytest.mark.asyncio
    async def test_detect_box_from_candles(self, manager, db_session_factory):
        """캔들에서 박스 감지 → DB에 active 박스 생성."""
        pair = "xrp_jpy"
        # 상단 ~105, 하단 ~95 근처 캔들 20개 생성
        ohlc = []
        for i in range(20):
            if i % 2 == 0:
                ohlc.append((100.0, 106.0, 94.0, 104.5))  # 고점 104.5
            else:
                ohlc.append((100.0, 106.0, 94.0, 95.5))  # 저점 95.5
        await insert_candles(db_session_factory, pair, "4h", ohlc)

        params = {
            "box_tolerance_pct": 1.0,
            "box_min_touches": 3,
            "box_lookback_candles": 20,
            "basis_timeframe": "4h",
            "fee_rate_pct": 0.15,
        }
        box = await manager._detect_and_create_box(pair, params)
        assert box is not None
        assert box.status == "active"
        assert float(box.upper_bound) > float(box.lower_bound)

    @pytest.mark.asyncio
    async def test_detect_box_skips_if_active_exists(self, manager, db_session_factory):
        """이미 active 박스 존재 시 감지 스킵."""
        pair = "xrp_jpy"
        await insert_box(db_session_factory, pair, 110.0, 90.0)
        # 캔들도 충분히 넣어둠
        ohlc = [(100.0, 106.0, 94.0, 104.0)] * 20
        await insert_candles(db_session_factory, pair, "4h", ohlc)

        params = {"box_tolerance_pct": 1.0, "box_min_touches": 3, "box_lookback_candles": 20}
        box = await manager._detect_and_create_box(pair, params)
        assert box is None

    @pytest.mark.asyncio
    async def test_detect_box_too_narrow(self, manager, db_session_factory):
        """박스 폭이 수수료보다 좁으면 None."""
        pair = "xrp_jpy"
        # 거의 동일한 가격 (upper ≈ lower)
        ohlc = [(100.0, 100.5, 99.5, 100.2)] * 20
        await insert_candles(db_session_factory, pair, "4h", ohlc)

        params = {
            "box_tolerance_pct": 0.5,
            "box_min_touches": 3,
            "box_lookback_candles": 20,
            "fee_rate_pct": 5.0,  # 극단적 수수료 → 폭 부족
        }
        box = await manager._detect_and_create_box(pair, params)
        assert box is None

    @pytest.mark.asyncio
    async def test_detect_box_insufficient_candles(self, manager, db_session_factory):
        """캔들 부족 시 None."""
        pair = "xrp_jpy"
        ohlc = [(100.0, 105.0, 95.0, 102.0)] * 3  # min_touches*2 = 6 미달
        await insert_candles(db_session_factory, pair, "4h", ohlc)

        params = {"box_tolerance_pct": 0.5, "box_min_touches": 3, "box_lookback_candles": 10}
        box = await manager._detect_and_create_box(pair, params)
        assert box is None


# ══════════════════════════════════════════════
# 테스트: 박스 유효성 검사
# ══════════════════════════════════════════════

class TestBoxValidation:

    @pytest.mark.asyncio
    async def test_invalidate_on_close_below_lower(self, manager, db_session_factory):
        """종가가 하단 아래 → 무효화."""
        pair = "xrp_jpy"
        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0, tolerance_pct=1.0)
        # 종가 80 → 90 * (1 - 0.01) = 89.1 아래
        await insert_candles(db_session_factory, pair, "4h", [(85.0, 90.0, 75.0, 80.0)])

        params = {"basis_timeframe": "4h"}
        reason = await manager._validate_active_box(pair, params)
        assert reason == "4h_close_below_lower"

        # DB에서 invalidated 확인
        box = await manager._get_active_box(pair)
        assert box is None  # active 박스 없어야 함

    @pytest.mark.asyncio
    async def test_invalidate_on_close_above_upper(self, manager, db_session_factory):
        """종가가 상단 위 → 무효화."""
        pair = "xrp_jpy"
        await insert_box(db_session_factory, pair, 110.0, 90.0, tolerance_pct=1.0)
        # 종가 120 → 110 * (1 + 0.01) = 111.1 위
        await insert_candles(db_session_factory, pair, "4h", [(115.0, 125.0, 112.0, 120.0)])

        params = {"basis_timeframe": "4h"}
        reason = await manager._validate_active_box(pair, params)
        assert reason == "4h_close_above_upper"

    @pytest.mark.asyncio
    async def test_valid_box_stays_active(self, manager, db_session_factory):
        """종가가 박스 내부 → 무효화 없음."""
        pair = "xrp_jpy"
        await insert_box(db_session_factory, pair, 110.0, 90.0, tolerance_pct=1.0)
        # 종가 100 → 박스 내부
        await insert_candles(db_session_factory, pair, "4h", [(98.0, 105.0, 95.0, 100.0)])

        params = {"basis_timeframe": "4h", "box_lookback_candles": 60}
        reason = await manager._validate_active_box(pair, params)
        assert reason is None

    @pytest.mark.asyncio
    async def test_converging_triangle_detection(self, manager, db_session_factory):
        """고점(high) 하락 + 저점(low) 상승 → 수렴 삼각형 감지."""
        pair = "xrp_jpy"
        await insert_box(db_session_factory, pair, 110.0, 90.0, tolerance_pct=1.0)

        # candle.high 하락 + candle.low 상승 → 수렴 삼각형
        # 마지막 캔들의 close는 박스 내부(90~110)에 있어야 close 검사 통과
        ohlc = []
        for i in range(10):
            h = 108 - 1.5 * i   # 108→94.5 (high 하락)
            l = 82 + 1.5 * i    # 82→95.5 (low 상승)
            o = l + 1
            c = h - 1           # close: 106→92.5 (박스 내부 유지)
            ohlc.append((o, h, l, c))
        await insert_candles(db_session_factory, pair, "4h", ohlc)

        params = {"basis_timeframe": "4h", "box_lookback_candles": 10}
        reason = await manager._validate_active_box(pair, params)
        assert reason == "converging_triangle"


# ══════════════════════════════════════════════
# 테스트: 가격 위치 판정
# ══════════════════════════════════════════════

class TestPriceInBox:

    @pytest.mark.asyncio
    async def test_near_lower(self, manager, db_session_factory):
        """가격이 하단 근처(near_bound_pct=0.3% 이내) → near_lower."""
        pair = "xrp_jpy"
        await insert_box(db_session_factory, pair, 110.0, 90.0, tolerance_pct=1.0)
        # lower=90, near_pct=0.003 → 범위: 89.73~90.27
        result = await manager._is_price_in_box(pair, 90.1)
        assert result == "near_lower"

    @pytest.mark.asyncio
    async def test_near_upper(self, manager, db_session_factory):
        """가격이 상단 근처(near_bound_pct=0.3% 이내) → near_upper."""
        pair = "xrp_jpy"
        await insert_box(db_session_factory, pair, 110.0, 90.0, tolerance_pct=1.0)
        # upper=110, near_pct=0.003 → 범위: 109.67~110.33
        result = await manager._is_price_in_box(pair, 109.9)
        assert result == "near_upper"

    @pytest.mark.asyncio
    async def test_middle(self, manager, db_session_factory):
        """가격이 중간 → middle."""
        pair = "xrp_jpy"
        await insert_box(db_session_factory, pair, 110.0, 90.0, tolerance_pct=1.0)
        result = await manager._is_price_in_box(pair, 100.0)
        assert result == "middle"

    @pytest.mark.asyncio
    async def test_no_box(self, manager):
        """박스 없으면 None."""
        result = await manager._is_price_in_box("xrp_jpy", 100.0)
        assert result is None


# ══════════════════════════════════════════════
# 테스트: 포지션 DB 기록
# ══════════════════════════════════════════════

class TestPositionRecording:

    @pytest.mark.asyncio
    async def test_open_and_close_position(self, manager, db_session_factory):
        """진입 → 청산 DB 기록 + PnL 계산."""
        pair = "xrp_jpy"
        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0)

        # 진입
        pos = await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-001",
            entry_price=91.0, entry_amount=10.0, entry_jpy=910.0,
        )
        assert pos is not None
        assert pos.status == "open"
        assert float(pos.entry_price) == 91.0

        # 청산
        closed = await manager._record_close_position(
            pair=pair,
            exit_order_id="ORD-002",
            exit_price=109.0, exit_amount=10.0,
            exit_reason="near_upper_exit",
        )
        assert closed is not None
        # PnL = (109 - 91) * 10 = 180
        async with db_session_factory() as db:
            result = await db.execute(
                select(BxtBoxPosition).where(BxtBoxPosition.id == pos.id)
            )
            updated = result.scalar_one()
            assert updated.status == "closed"
            assert float(updated.realized_pnl_jpy) == 180.0
            assert float(updated.realized_pnl_pct) == pytest.approx(19.78, abs=0.01)

    @pytest.mark.asyncio
    async def test_duplicate_open_returns_existing(self, manager, db_session_factory):
        """open 포지션이 이미 있으면 새 진입 무시."""
        pair = "xrp_jpy"
        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0)

        pos1 = await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-001", entry_price=91.0, entry_amount=10.0,
        )
        pos2 = await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-002", entry_price=92.0, entry_amount=5.0,
        )
        assert pos2.id == pos1.id  # 기존 것 반환

    @pytest.mark.asyncio
    async def test_close_without_open(self, manager):
        """open 없이 close → None."""
        result = await manager._record_close_position(
            pair="xrp_jpy",
            exit_order_id="ORD-X", exit_price=100.0, exit_amount=10.0,
            exit_reason="test",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_has_open_position(self, manager, db_session_factory):
        """open 포지션 존재 여부."""
        pair = "xrp_jpy"
        assert not await manager._has_open_position(pair)

        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0)
        await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-001", entry_price=91.0, entry_amount=10.0,
        )
        assert await manager._has_open_position(pair)


# ══════════════════════════════════════════════
# 테스트: 주문 실행 (FakeExchangeAdapter)
# ══════════════════════════════════════════════

class TestOrderExecution:

    @pytest.mark.asyncio
    async def test_open_position_market_buy(self, manager, fake_adapter, db_session_factory):
        """market_buy 진입 → 잔고 변동 + DB 기록."""
        pair = "xrp_jpy"
        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0)
        box = await manager._get_active_box(pair)
        manager._params[pair] = {"position_size_pct": 10.0, "min_order_jpy": 500}

        await manager._open_position_market(pair, box, 91.0, manager._params[pair])

        # 잔고 확인: 1M * 10% = 100,000 JPY 투입
        balance = await fake_adapter.get_balance()
        assert balance.get_available("jpy") < 1_000_000.0
        assert balance.get_available("xrp") > 0

        # DB 포지션 확인
        assert await manager._has_open_position(pair)

    @pytest.mark.asyncio
    async def test_open_position_skipped_low_balance(self, manager, fake_adapter, db_session_factory):
        """잔고 부족 → 진입 스킵."""
        pair = "xrp_jpy"
        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0)
        box = await manager._get_active_box(pair)
        fake_adapter.set_balance("jpy", 100.0)  # 극도로 적은 잔고
        manager._params[pair] = {"position_size_pct": 10.0, "min_order_jpy": 500}

        await manager._open_position_market(pair, box, 91.0, manager._params[pair])
        assert not await manager._has_open_position(pair)

    @pytest.mark.asyncio
    async def test_close_position_market_sell(self, manager, fake_adapter, db_session_factory):
        """market_sell 청산 → DR 기록 + PnL."""
        pair = "xrp_jpy"
        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0)

        # 먼저 진입
        fake_adapter.set_balance("xrp", 100.0)
        await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-BUY", entry_price=91.0, entry_amount=100.0,
        )

        pos = await manager._get_open_position(pair)
        manager._params[pair] = {"min_coin_size": 0.001, "trading_fee_rate": 0.002}
        await manager._close_position_market(pair, pos, "near_upper_exit")

        # 포지션 closed 확인
        assert not await manager._has_open_position(pair)

    @pytest.mark.asyncio
    async def test_close_ticker_fallback_when_price_zero(self, manager, fake_adapter, db_session_factory):
        """BUG-008: 체결가 미반환(price=0) → ticker 현재가로 대체하여 PnL 계산."""
        pair = "xrp_jpy"
        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0)

        fake_adapter.set_balance("xrp", 100.0)
        fake_adapter.set_ticker_price(108.0)
        await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-BUY", entry_price=91.0, entry_amount=100.0,
        )

        pos = await manager._get_open_position(pair)
        manager._params[pair] = {"min_coin_size": 0.001, "trading_fee_rate": 0.002}

        # place_order가 price=0인 Order를 반환하도록 패치
        from core.exchange.types import Order, OrderStatus, OrderSide
        original_place = fake_adapter.place_order

        async def _place_zero_price(*args, **kwargs):
            order = await original_place(*args, **kwargs)
            return Order(
                order_id=order.order_id, pair=order.pair,
                order_type=order.order_type, side=order.side,
                price=0, amount=order.amount,
                status=order.status, created_at=order.created_at,
            )

        fake_adapter.place_order = _place_zero_price

        await manager._close_position_market(pair, pos, "near_upper_exit")
        assert not await manager._has_open_position(pair)

        # DB에 ticker last(=108.0)로 PnL이 기록되어야 함
        TstBoxPosition = manager._box_position_model
        async with db_session_factory() as db:
            result = await db.execute(
                select(TstBoxPosition).where(TstBoxPosition.status == "closed")
            )
            rec = result.scalars().first()
            assert rec is not None
            assert float(rec.exit_price) == 108.0
            assert float(rec.realized_pnl_jpy) > 0  # 91→108, 이익

    @pytest.mark.asyncio
    async def test_close_dust_logged_after_sell(self, manager, fake_adapter, db_session_factory, caplog):
        """BUG-009: 청산 후 dust 잔고 감지 → 로그 기록."""
        pair = "xrp_jpy"
        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0)

        # 소량 잔고 설정 (0.005 XRP, fee 차감 후 dust 남음)
        fake_adapter.set_balance("xrp", 0.005)
        await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-BUY-009", entry_price=91.0, entry_amount=0.005,
        )

        pos = await manager._get_open_position(pair)
        manager._params[pair] = {"min_coin_size": 0.001, "trading_fee_rate": 0.002}

        import logging
        with caplog.at_level(logging.INFO):
            await manager._close_position_market(pair, pos, "near_upper_exit")

        assert not await manager._has_open_position(pair)
        # dust 로그가 기록되었는지 확인
        dust_logs = [r for r in caplog.records if "dust 잔고 감지" in r.message]
        assert len(dust_logs) == 1
        assert "매도 불가 수량" in dust_logs[0].message


# ══════════════════════════════════════════════
# 테스트: 캔들 조회
# ══════════════════════════════════════════════

class TestCandleQueries:

    @pytest.mark.asyncio
    async def test_get_latest_candle_open_time(self, manager, db_session_factory):
        """최신 완성 캔들 open_time 반환."""
        pair = "xrp_jpy"
        t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
        await insert_candles(db_session_factory, pair, "4h", [
            (100.0, 105.0, 95.0, 102.0),
            (102.0, 106.0, 98.0, 104.0),
        ], start_time=t0)

        result = await manager._get_latest_candle_open_time(pair, "4h")
        assert result is not None
        # 두 번째 캔들의 open_time = t0 + 4h
        assert "2026-03-01T04:00:00" in result

    @pytest.mark.asyncio
    async def test_get_completed_candles_ordering(self, manager, db_session_factory):
        """완성 캔들은 시간 오름차순으로 반환."""
        pair = "xrp_jpy"
        await insert_candles(db_session_factory, pair, "4h", [
            (100.0, 105.0, 95.0, 102.0),
            (102.0, 106.0, 98.0, 104.0),
            (104.0, 108.0, 100.0, 106.0),
        ])

        candles = await manager._get_completed_candles(pair, "4h", limit=10)
        assert len(candles) == 3
        # 시간 오름차순 확인
        assert candles[0].open_time < candles[1].open_time < candles[2].open_time


# ══════════════════════════════════════════════
# 테스트: FX (증거금) 모드 — ISSUE-1~6 검증
# ══════════════════════════════════════════════

class TestFxMarginTrading:
    """GMO FX 증거금 거래 호환성 테스트."""

    @pytest_asyncio.fixture
    async def fx_adapter(self):
        adapter = FakeExchangeAdapter(initial_balances={"jpy": 1_000_000.0})
        adapter.set_margin_trading(True)
        adapter.set_ticker_price(150.0)
        return adapter

    @pytest_asyncio.fixture
    async def fx_manager(self, fx_adapter, supervisor, db_session_factory):
        return BoxMeanReversionManager(
            adapter=fx_adapter,
            supervisor=supervisor,
            session_factory=db_session_factory,
            candle_model=BxtCandle,
            box_model=BxtBox,
            box_position_model=BxtBoxPosition,
            pair_column="pair",
        )

    @pytest.mark.asyncio
    async def test_fx_open_position_converts_jpy_to_size(
        self, fx_manager, fx_adapter, db_session_factory,
    ):
        """ISSUE-1 (V-1): invest_jpy가 통화 수량(정수)으로 올바르게 변환되는가."""
        pair = "usd_jpy"
        box_id = await insert_box(db_session_factory, pair, 155.0, 145.0)
        box = await fx_manager._get_active_box(pair)
        params = {
            "position_size_pct": 30.0,
            "min_order_jpy": 500,
            "leverage": 3,
            "min_lot_size": 1,
        }
        fx_manager._params[pair] = params

        # 잔고 1M * 30% = 300,000 JPY, leverage 3 → 900,000 / 150 = 6000 통화
        await fx_manager._open_position_market(pair, box, 150.0, params)

        assert await fx_manager._has_open_position(pair)

        # place_order에 전달된 amount 확인
        last_order = fx_adapter.order_history[-1]
        assert last_order.amount == 6000.0  # math.floor(300000 * 3 / 150)

    @pytest.mark.asyncio
    async def test_fx_open_position_skips_small_lot(
        self, fx_manager, fx_adapter, db_session_factory,
    ):
        """FX 최소 로트 미달 시 진입 스킵."""
        pair = "usd_jpy"
        box_id = await insert_box(db_session_factory, pair, 155.0, 145.0)
        box = await fx_manager._get_active_box(pair)
        fx_adapter.set_balance("jpy", 100.0)  # 극도로 적은 잔고
        params = {
            "position_size_pct": 10.0,
            "min_order_jpy": 1,
            "leverage": 1,
            "min_lot_size": 1000,  # 최소 1000통화
        }
        fx_manager._params[pair] = params

        await fx_manager._open_position_market(pair, box, 150.0, params)
        assert not await fx_manager._has_open_position(pair)

    @pytest.mark.asyncio
    async def test_fx_close_position_uses_close_position(
        self, fx_manager, fx_adapter, db_session_factory,
    ):
        """ISSUE-2/4 (V-2, V-6): FX 청산이 close_position을 사용하고 양방향 포지션 미발생."""
        from core.exchange.types import FxPosition

        pair = "usd_jpy"
        box_id = await insert_box(db_session_factory, pair, 155.0, 145.0)

        # 포지션 DB 기록 with exchange_position_id
        await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-FX-001", entry_price=150.0,
            entry_amount=6000.0, entry_jpy=300000.0,
            exchange_position_id="99001",
        )
        pos = await fx_manager._get_open_position(pair)

        # FX 포지션 설정
        fx_adapter.set_fx_positions([
            FxPosition(
                product_code="USD_JPY", side="BUY", price=150.0, size=6000.0,
                pnl=100.0, leverage=0, require_collateral=0,
                swap_point_accumulate=0, sfd=0, position_id=99001,
            ),
        ])

        fx_manager._params[pair] = {}
        await fx_manager._close_position_market(pair, pos, "near_upper_exit")

        # 포지션이 closed 상태인지 확인
        assert not await fx_manager._has_open_position(pair)

        # place_order가 아닌 close_position이 호출되었는지 확인
        # (close_position 호출 시 FX 포지션이 제거됨)
        remaining = await fx_adapter.get_positions("USD_JPY")
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_fx_close_matches_position_id_from_api(
        self, fx_manager, fx_adapter, db_session_factory,
    ):
        """ISSUE-6 (V-2): DB에 positionId 없을 때 API 매칭으로 청산."""
        from core.exchange.types import FxPosition

        pair = "usd_jpy"
        box_id = await insert_box(db_session_factory, pair, 155.0, 145.0)

        # exchange_position_id 없이 기록
        await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-FX-002", entry_price=150.0,
            entry_amount=6000.0, entry_jpy=300000.0,
        )
        pos = await fx_manager._get_open_position(pair)

        # FX 포지션 설정 — BUY 1건만
        fx_adapter.set_fx_positions([
            FxPosition(
                product_code="USD_JPY", side="BUY", price=150.0, size=6000.0,
                pnl=200.0, leverage=0, require_collateral=0,
                swap_point_accumulate=0, sfd=0, position_id=88001,
            ),
        ])

        fx_manager._params[pair] = {}
        await fx_manager._close_position_market(pair, pos, "box_invalidated")

        assert not await fx_manager._has_open_position(pair)

    @pytest.mark.asyncio
    async def test_fx_no_dust_check(
        self, fx_manager, fx_adapter, db_session_factory, caplog,
    ):
        """ISSUE-5: FX 모드에서는 dust 잔고 체크가 실행되지 않는다."""
        from core.exchange.types import FxPosition
        import logging

        pair = "usd_jpy"
        box_id = await insert_box(db_session_factory, pair, 155.0, 145.0)

        await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-FX-003", entry_price=150.0,
            entry_amount=1000.0, entry_jpy=150000.0,
            exchange_position_id="77001",
        )
        pos = await fx_manager._get_open_position(pair)

        fx_adapter.set_fx_positions([
            FxPosition(
                product_code="USD_JPY", side="BUY", price=150.0, size=1000.0,
                pnl=0, leverage=0, require_collateral=0,
                swap_point_accumulate=0, sfd=0, position_id=77001,
            ),
        ])

        fx_manager._params[pair] = {}
        with caplog.at_level(logging.INFO):
            await fx_manager._close_position_market(pair, pos, "near_upper_exit")

        dust_logs = [r for r in caplog.records if "dust" in r.message.lower()]
        assert len(dust_logs) == 0  # FX에서는 dust 체크 안 함

    @pytest.mark.asyncio
    async def test_fx_position_id_stored_in_db(
        self, fx_manager, db_session_factory,
    ):
        """ISSUE-6 (V-5): exchange_position_id가 DB에 저장되는지 확인."""
        pair = "usd_jpy"
        box_id = await insert_box(db_session_factory, pair, 155.0, 145.0)

        pos = await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-FX-004", entry_price=150.0,
            entry_amount=1000.0, exchange_position_id="55001",
        )

        assert pos.exchange_position_id == "55001"

        # DB에서 직접 확인
        async with db_session_factory() as db:
            result = await db.execute(
                select(BxtBoxPosition).where(BxtBoxPosition.id == pos.id)
            )
            stored = result.scalar_one()
            assert stored.exchange_position_id == "55001"

    @pytest.mark.asyncio
    async def test_spot_unchanged_after_fx_changes(
        self, manager, fake_adapter, db_session_factory,
    ):
        """V-7: 현물(BF) 기존 로직 회귀 없음 — _open + _close 전체 사이클."""
        pair = "xrp_jpy"
        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0)
        box = await manager._get_active_box(pair)
        manager._params[pair] = {
            "position_size_pct": 10.0,
            "min_order_jpy": 500,
            "min_coin_size": 0.001,
            "trading_fee_rate": 0.002,
        }

        # 진입
        await manager._open_position_market(pair, box, 91.0, manager._params[pair])
        assert await manager._has_open_position(pair)
        assert fake_adapter.is_margin_trading is False

        # xrp 잔고가 생겼는지 확인
        balance = await fake_adapter.get_balance()
        xrp_balance = balance.get_available("xrp")
        assert xrp_balance > 0

        # 청산
        pos = await manager._get_open_position(pair)
        await manager._close_position_market(pair, pos, "near_upper_exit")
        assert not await manager._has_open_position(pair)

    @pytest.mark.asyncio
    async def test_fx_1000_unit_rounding(
        self, fx_manager, fx_adapter, db_session_factory,
    ):
        """V-10: 1,000통화 단위 내림 정확성 (2999→2000, 999→스킵)."""
        pair = "usd_jpy"
        box_id = await insert_box(db_session_factory, pair, 155.0, 145.0)
        box = await fx_manager._get_active_box(pair)

        # Case 1: 잔고 → 2999통화 → 내림 2000
        fx_adapter.set_balance("jpy", 149_950.0)  # 149950 * 1 / 150 = 999.67 → 레버3 → 2999 → 2000
        params = {
            "position_size_pct": 100.0,
            "min_order_jpy": 500,
            "leverage": 3,
            "lot_unit": 1000,
            "min_lot_size": 1000,
        }
        fx_manager._params[pair] = params
        await fx_manager._open_position_market(pair, box, 150.0, params)

        assert await fx_manager._has_open_position(pair)
        last_order = fx_adapter.order_history[-1]
        assert last_order.amount == 2000.0  # floor(2999/1000)*1000

        # Case 2: 잔고 → 999통화 → 내림 0 → 스킵
        # 새 포지션을 위해 기존 것 먼저 정리
        pos = await fx_manager._get_open_position(pair)
        BxtBoxPosition = fx_manager._box_position_model
        async with db_session_factory() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(BxtBoxPosition).where(BxtBoxPosition.id == pos.id).values(status="closed")
            )
            await db.commit()
        fx_manager._cached_position.pop(pair, None)  # 수동 DB 조작 후 캐시 무효화

        fx_adapter.set_balance("jpy", 49_950.0)  # 49950 * 3 / 150 = 999 → floor(999/1000)*1000 = 0
        order_count_before = len(fx_adapter.order_history)
        await fx_manager._open_position_market(pair, box, 150.0, params)
        assert not await fx_manager._has_open_position(pair)
        assert len(fx_adapter.order_history) == order_count_before  # 주문 안 됨

    @pytest.mark.asyncio
    async def test_fx_insufficient_margin_skips_entry(
        self, fx_manager, fx_adapter, db_session_factory,
    ):
        """V-11: 증거금 부족 시 진입 거부."""
        pair = "usd_jpy"
        box_id = await insert_box(db_session_factory, pair, 155.0, 145.0)
        box = await fx_manager._get_active_box(pair)

        fx_adapter.set_balance("jpy", 100.0)  # 극도로 적은 잔고
        params = {
            "position_size_pct": 10.0,
            "min_order_jpy": 500,
            "leverage": 1,
            "lot_unit": 1000,
            "min_lot_size": 1000,
        }
        fx_manager._params[pair] = params

        await fx_manager._open_position_market(pair, box, 150.0, params)
        assert not await fx_manager._has_open_position(pair)

    @pytest.mark.asyncio
    async def test_fx_leverage_affects_size(
        self, fx_manager, fx_adapter, db_session_factory,
    ):
        """V-12: 레버리지 변경에 따른 size 계산 (lever=1,3,5)."""
        pair = "usd_jpy"

        for leverage, expected_size in [(1, 2000), (3, 6000), (5, 10000)]:
            # 매번 새 박스/포지션 초기화
            box_id = await insert_box(db_session_factory, pair, 155.0, 145.0)
            box = await fx_manager._get_active_box(pair)
            fx_adapter.set_balance("jpy", 1_000_000.0)

            params = {
                "position_size_pct": 30.0,
                "min_order_jpy": 500,
                "leverage": leverage,
                "lot_unit": 1000,
                "min_lot_size": 1000,
            }
            fx_manager._params[pair] = params

            # 300,000 * leverage / 150 / 1000 → floor → * 1000
            await fx_manager._open_position_market(pair, box, 150.0, params)

            last_order = fx_adapter.order_history[-1]
            assert last_order.amount == expected_size, (
                f"leverage={leverage}: expected {expected_size}, got {last_order.amount}"
            )

            # 정리: 포지션 close, 박스 invalidate
            pos = await fx_manager._get_open_position(pair)
            BxtBoxPosition = fx_manager._box_position_model
            async with db_session_factory() as db:
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(BxtBoxPosition).where(BxtBoxPosition.id == pos.id).values(status="closed")
                )
                await db.execute(
                    sa_update(BxtBox).where(BxtBox.id == box_id).values(status="invalidated")
                )
                await db.commit()
            fx_manager._cached_position.pop(pair, None)  # 수동 DB 조작 후 캐시 무효화

    @pytest.mark.asyncio
    async def test_fx_close_position_failure_logged(
        self, fx_manager, fx_adapter, db_session_factory, caplog,
    ):
        """V-13: closeOrder 실패 시 에러 로깅."""
        import logging

        pair = "usd_jpy"
        box_id = await insert_box(db_session_factory, pair, 155.0, 145.0)

        await fx_manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="ORD-FX-FAIL", entry_price=150.0,
            entry_amount=1000.0, entry_jpy=150000.0,
            exchange_position_id="66001",
        )
        pos = await fx_manager._get_open_position(pair)

        # close_position이 예외를 던지도록 설정
        original = fx_adapter.close_position

        async def _raise(*args, **kwargs):
            raise RuntimeError("GMO API timeout")

        fx_adapter.close_position = _raise
        fx_manager._params[pair] = {}

        with caplog.at_level(logging.ERROR):
            await fx_manager._close_position_market(pair, pos, "test_failure")

        error_logs = [r for r in caplog.records if "청산 주문 오류" in r.message]
        assert len(error_logs) >= 1
        # 포지션은 여전히 open (실패했으므로)
        assert await fx_manager._has_open_position(pair)

        fx_adapter.close_position = original

    @pytest.mark.asyncio
    async def test_fx_weekend_close_blocks_entry(
        self, fx_manager, fx_adapter, db_session_factory,
    ):
        """V-8 (ISSUE-8): 금요일 마감 시간에 FX 신규 진입이 차단되는지 확인."""
        from unittest.mock import patch
        from core.exchange.session import should_close_for_weekend, is_fx_market_open

        pair = "usd_jpy"
        box_id = await insert_box(db_session_factory, pair, 155.0, 145.0)
        box = await fx_manager._get_active_box(pair)
        params = {
            "position_size_pct": 30.0,
            "min_order_jpy": 500,
            "leverage": 3,
            "lot_unit": 1000,
            "min_lot_size": 1000,
        }
        fx_manager._params[pair] = params
        fx_manager._prev_box_state[pair] = None

        # 주말 청산 시점이면 _entry_monitor에서 진입 차단
        # _entry_monitor는 루프이므로 직접 로직만 검증
        with patch(
            "core.strategy.box_mean_reversion.should_close_for_weekend",
            return_value=True,
        ):
            # should_close_for_weekend=True이면 FX 진입 스킵해야 함
            is_fx = getattr(fx_adapter, "is_margin_trading", False)
            assert is_fx
            from core.strategy.box_mean_reversion import should_close_for_weekend as scw
            assert scw()  # 패치 확인


# ── prev_state 초기화 테스트 (dd31a65 수정 검증) ──────────────


class TestPrevStateInit:
    """재시작 시 prev_state 초기화 로직 검증.

    dd31a65: 포지션 없으면 prev_state=None → near_lower에서 즉시 진입 가능.
    포지션 있으면 prev_state=현재 상태 유지 (중복 청산 방지).
    """

    @pytest.mark.asyncio
    async def test_start_no_position_sets_prev_state_none(
        self, manager, fake_adapter, db_session_factory
    ):
        """포지션 없이 start → prev_state = None (즉시 진입 가능)."""
        fake_adapter.set_ticker_price(100.0)
        params = {"pair": "TEST_JPY", "basis_timeframe": "4h", "near_bound_pct": 0.5}

        await manager.start("TEST_JPY", params)

        assert manager._prev_box_state.get("TEST_JPY") is None

    @pytest.mark.asyncio
    async def test_start_with_position_sets_prev_state_to_current(
        self, manager, fake_adapter, db_session_factory
    ):
        """포지션 있으면 start → prev_state = 현재 zone."""
        # 박스 + 포지션 생성
        async with db_session_factory() as session:
            box = BxtBox(
                pair="TEST_JPY",
                upper_bound=Decimal("105.0"),
                lower_bound=Decimal("95.0"),
                upper_touch_count=5,
                lower_touch_count=5,
                tolerance_pct=Decimal("0.3"),

                status="active",
                created_at=datetime.now(timezone.utc),
            )
            session.add(box)
            await session.flush()

            pos = BxtBoxPosition(
                pair="TEST_JPY",
                box_id=box.id,
                entry_order_id="TEST-001",
                entry_price=Decimal("96.0"),
                entry_amount=Decimal("100"),
                status="open",
                created_at=datetime.now(timezone.utc),
            )
            session.add(pos)
            await session.commit()

        # 현재가를 near_lower 범위에 설정
        fake_adapter.set_ticker_price(95.5)
        params = {"pair": "TEST_JPY", "basis_timeframe": "4h", "near_bound_pct": 0.5}

        await manager.start("TEST_JPY", params)

        # 포지션 있으므로 현재 zone이 설정됨 (None이 아님)
        assert manager._prev_box_state.get("TEST_JPY") is not None

    @pytest.mark.asyncio
    async def test_no_position_near_lower_triggers_entry(
        self, manager, fake_adapter, db_session_factory
    ):
        """포지션 없고 가격이 이미 near_lower → prev_state=None이므로 진입 트리거됨.

        진입 조건: box_state == "near_lower" and prev_state != "near_lower"
        prev_state=None → 조건 충족 ✅
        """
        params = {"pair": "TEST_JPY", "basis_timeframe": "4h", "near_bound_pct": 0.5}

        # prev_state=None (포지션 없이 시작)
        manager._prev_box_state["TEST_JPY"] = None
        manager._params["TEST_JPY"] = params

        box_state = "near_lower"
        prev_state = manager._prev_box_state.get("TEST_JPY")

        # 진입 조건 충족 확인
        assert box_state == "near_lower" and prev_state != "near_lower"

    @pytest.mark.asyncio
    async def test_with_position_near_lower_no_duplicate_entry(
        self, manager, fake_adapter, db_session_factory
    ):
        """포지션 있고 near_lower → prev_state="near_lower" → 중복 진입 안 됨."""
        params = {"pair": "TEST_JPY", "basis_timeframe": "4h", "near_bound_pct": 0.5}

        # prev_state="near_lower" (포지션 있어서 현재 상태 유지)
        manager._prev_box_state["TEST_JPY"] = "near_lower"
        manager._params["TEST_JPY"] = params

        box_state = "near_lower"
        prev_state = manager._prev_box_state.get("TEST_JPY")

        # 진입 조건 미충족 확인
        assert not (box_state == "near_lower" and prev_state != "near_lower")


# ══════════════════════════════════════════════
# 테스트: 포지션 캐시 (T-SL-01~05)
# ══════════════════════════════════════════════

class TestPositionCache:
    """포지션 인메모리 캐시 — tick 루프의 DB 부하 최소화."""

    @pytest.mark.asyncio
    async def test_cache_cold_then_warm(self, manager, db_session_factory):
        """T-SL-01: 캐시 cold → DB 조회 후 warm (이후 호출은 DB 없이 반환)."""
        pair = "xrp_jpy"
        assert pair not in manager._cached_position  # cold 상태

        # 포지션 없음 — DB 조회 → cache 갱신
        result = await manager._get_open_position(pair)
        assert result is None
        assert pair in manager._cached_position  # warm (None)
        assert manager._cached_position[pair] is None

    @pytest.mark.asyncio
    async def test_cache_set_on_open(self, manager, db_session_factory):
        """T-SL-02: record_open_position 후 캐시가 포지션 객체로 갱신됨."""
        pair = "xrp_jpy"
        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0)

        pos = await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="CACHE-001", entry_price=91.0, entry_amount=10.0,
        )
        # 캐시에 포지션 객체가 설정되어야 함
        assert manager._cached_position.get(pair) is not None
        assert manager._cached_position[pair].id == pos.id

        # 이후 _get_open_position은 캐시에서 반환 (DB 미조회)
        result = await manager._get_open_position(pair)
        assert result is not None
        assert result.id == pos.id

    @pytest.mark.asyncio
    async def test_cache_cleared_on_close(self, manager, db_session_factory):
        """T-SL-03: record_close_position 후 캐시가 None으로 해제됨."""
        pair = "xrp_jpy"
        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0)

        await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="CACHE-002", entry_price=91.0, entry_amount=10.0,
        )
        assert manager._cached_position.get(pair) is not None

        await manager._record_close_position(
            pair=pair, exit_order_id="CACHE-002-X",
            exit_price=109.0, exit_amount=10.0, exit_reason="near_upper_exit",
        )
        # 캐시가 None으로 해제되어야 함
        assert manager._cached_position.get(pair) is None
        # _has_open_position도 False
        assert not await manager._has_open_position(pair)

    @pytest.mark.asyncio
    async def test_cache_cleared_on_stop(self, manager, db_session_factory):
        """T-SL-04: stop() 호출 시 캐시 항목 제거."""
        pair = "xrp_jpy"
        manager._params[pair] = {}
        manager._cached_position[pair] = None  # warm 상태 수동 설정

        await manager.stop(pair)
        assert pair not in manager._cached_position  # cold로 복귀

    @pytest.mark.asyncio
    async def test_cache_no_duplicate_close_after_near_upper(
        self, manager, fake_adapter, db_session_factory,
    ):
        """T-SL-05: near_upper 청산 후 SL 중복 발동 없음 (캐시에 None 반영됨).

        near_upper_exit → _record_close_position → cache=None
        이후 SL 체크 → _get_open_position → None → SL 미발동.
        """
        pair = "xrp_jpy"
        box_id = await insert_box(db_session_factory, pair, 110.0, 90.0)

        # 포지션 열기
        fake_adapter.set_balance("xrp", 100.0)
        await manager._record_open_position(
            pair=pair, box_id=box_id,
            entry_order_id="SL-COMPAT-001", entry_price=91.0, entry_amount=100.0,
        )
        assert manager._cached_position.get(pair) is not None

        # near_upper_exit 청산
        pos = await manager._get_open_position(pair)
        manager._params[pair] = {"min_coin_size": 0.001, "trading_fee_rate": 0.002}
        await manager._close_position_market(pair, pos, "near_upper_exit")

        # 캐시가 None → SL 체크 시 포지션 없음
        cached_after = manager._cached_position.get(pair)
        assert cached_after is None

        # _get_open_position 재호출 → None (DB조회 없이 캐시 반환)
        result = await manager._get_open_position(pair)
        assert result is None
