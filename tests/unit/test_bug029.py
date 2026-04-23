"""
BUG-029: pyramid_count DB 컬럼 누락 핫픽스 테스트 (수정판)

조사 결과: GmoCoinTrendManager는 gmoc_cfd_positions 테이블을 사용
(main.py에서 cfd_position_model=models.cfd_position 전달)

P-01: create_cfd_position_model("gmoc")에 pyramid_count 컬럼 존재 (핵심)
P-02: create_trend_position_model("gmoc")에 pyramid_count 컬럼 존재 (예비)
P-03: create_trend_position_model("bf")에 pyramid_count 컬럼 존재 (예비)
P-04: cfd_position 모델 pyramid_count: server_default='0', NOT NULL
P-05: _update_position_in_db 소스에 entry_size= 사용 (entry_amount 아님)
P-06: _update_position_in_db 실행 시 commit 호출 확인
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.database.models import (
    create_cfd_position_model,
    create_trend_position_model,
)


# ──────────────────────────────────────────────────────────────
# P-01: gmoc cfd 모델에 pyramid_count 컬럼 존재 (핵심 테이블)
# ──────────────────────────────────────────────────────────────

def test_p01_gmoc_cfd_model_has_pyramid_count():
    """create_cfd_position_model('gmoc')에 pyramid_count 컬럼이 있어야 한다.
    GmoCoinTrendManager의 실제 _position_model = gmoc_cfd_positions."""
    GmocCfdPosition = create_cfd_position_model("gmoc", pair_column="pair")
    assert hasattr(GmocCfdPosition, "pyramid_count"), \
        "gmoc_cfd_positions 모델에 pyramid_count 컬럼이 없음"


# ──────────────────────────────────────────────────────────────
# P-02: gmoc trend 모델에 pyramid_count (예비 테이블)
# ──────────────────────────────────────────────────────────────

def test_p02_gmoc_trend_model_has_pyramid_count():
    """create_trend_position_model('gmoc')에 pyramid_count 컬럼이 있어야 한다."""
    GmocTrendPosition = create_trend_position_model("gmoc")
    assert hasattr(GmocTrendPosition, "pyramid_count"), \
        "gmoc_trend_positions 모델에 pyramid_count 컬럼이 없음"


# ──────────────────────────────────────────────────────────────
# P-03: bf trend 모델에 pyramid_count
# ──────────────────────────────────────────────────────────────

def test_p03_bf_trend_model_has_pyramid_count():
    """create_trend_position_model('bf')에 pyramid_count 컬럼이 있어야 한다."""
    BfTrendPosition = create_trend_position_model("bf")
    assert hasattr(BfTrendPosition, "pyramid_count"), \
        "bf_trend_positions 모델에 pyramid_count 컬럼이 없음"


# ──────────────────────────────────────────────────────────────
# P-04: cfd_position pyramid_count 컬럼 스펙
# ──────────────────────────────────────────────────────────────

def test_p04_cfd_pyramid_count_column_spec():
    """pyramid_count: server_default='0', nullable=False."""
    GmocCfdPosition = create_cfd_position_model("gmoc", pair_column="pair")
    col = GmocCfdPosition.__table__.c["pyramid_count"]
    assert not col.nullable, "pyramid_count은 NOT NULL이어야 한다"
    assert col.server_default is not None, "pyramid_count은 server_default가 있어야 한다"


# ──────────────────────────────────────────────────────────────
# P-05: _update_position_in_db entry_size 사용 (entry_amount 아님)
# ──────────────────────────────────────────────────────────────

def test_p05_update_uses_entry_size():
    """_update_position_in_db 소스에 entry_size= 사용.
    gmoc_cfd_positions 스키마는 entry_size 컬럼 (entry_amount 아님)."""
    from core.punisher.strategy.plugins.gmo_coin_trend import manager as gmoc_mgr
    src = inspect.getsource(gmoc_mgr.GmoCoinTrendManager._update_position_in_db)
    assert "entry_amount=" not in src, \
        "_update_position_in_db에 entry_amount= 가 남아있음 — cfd_positions 스키마는 entry_size 사용"
    assert "entry_size=" in src, "_update_position_in_db에 entry_size= 이 없음"


# ──────────────────────────────────────────────────────────────
# P-06: _update_position_in_db 실행 시 commit 호출 확인
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_p06_update_position_in_db_commits():
    """_update_position_in_db 호출 시 DB commit이 호출된다."""
    from core.punisher.strategy.plugins.gmo_coin_trend.manager import GmoCoinTrendManager

    mgr = GmoCoinTrendManager.__new__(GmoCoinTrendManager)
    mgr._log_prefix = "[GmocMgr]"
    mgr._position_model = create_cfd_position_model("gmoc", pair_column="pair")

    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()
    mgr._session_factory = MagicMock(return_value=mock_db)

    await mgr._update_position_in_db(
        product_code="btc_jpy",
        db_record_id=7,
        entry_price=12_460_755.0,
        size=0.009,
        stop_loss_price=12_342_973.142857,
        pyramid_count=2,
    )

    assert mock_db.commit.called, "commit이 호출되지 않음"
    assert mock_db.execute.called, "execute가 호출되지 않음"
