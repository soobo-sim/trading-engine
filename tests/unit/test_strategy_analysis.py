"""
strategy_analysis API unit tests.
설계서: trader-common/solution-design/STRATEGY_ANALYSIS_SYSTEM.md §2~3
"""
from __future__ import annotations

import pytest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────

def _make_report(**kwargs):
    defaults = dict(
        id=1, exchange="gmofx", currency_pair="USD_JPY",
        report_type="daily",
        reported_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
        chart_start=datetime(2026, 3, 25, 9, 0, tzinfo=timezone.utc),
        chart_end=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
        strategy_active=True, strategy_id=7,
        final_decision="approved",
        final_rationale="추세 명확",
        next_review=None,
        created_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
        analyses=[],
    )
    defaults.update(kwargs)
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


def _make_analysis(**kwargs):
    defaults = dict(
        id=1, report_id=1, agent_name="alice",
        summary="상승 추세 / EMA↑ RSI 52",
        structured_data={"trend": "uptrend", "confidence": 85},
        full_text="## Alice 분석\n...",
        created_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


def _make_reflection(**kwargs):
    defaults = dict(
        id=1, reflection_date=date(2026, 3, 31),
        agent_name="alice", period_type="short",
        period_start=date(2026, 3, 24), period_end=date(2026, 3, 31),
        missed_data=[{"indicator": "RSI divergence", "impact": "진입 2H 지연"}],
        data_improvement=None, effective_decisions=None,
        action_items=None, strategy_performance=None,
        created_at=datetime(2026, 3, 31, 9, 0, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


# ──────────────────────────────────────────────────────────────
# Pydantic 유효성 검증
# ──────────────────────────────────────────────────────────────

def test_p01_invalid_report_type_rejected():
    from pydantic import ValidationError
    from api.routes.strategy_analysis import ReportCreate
    with pytest.raises(ValidationError, match="report_type"):
        ReportCreate(
            exchange="gmofx", currency_pair="USD_JPY",
            report_type="hourly",
            reported_at=datetime(2026, 4, 1, 9, 0),
        )


def test_p02_invalid_final_decision_rejected():
    from pydantic import ValidationError
    from api.routes.strategy_analysis import ReportCreate
    with pytest.raises(ValidationError, match="final_decision"):
        ReportCreate(
            exchange="gmofx", currency_pair="USD_JPY",
            report_type="daily",
            reported_at=datetime(2026, 4, 1, 9, 0),
            final_decision="maybe",
        )


def test_p03_invalid_agent_name_in_analysis_rejected():
    from pydantic import ValidationError
    from api.routes.strategy_analysis import AgentAnalysisCreate
    with pytest.raises(ValidationError, match="agent_name"):
        AgentAnalysisCreate(
            agent_name="bob",
            summary="테스트",
            structured_data={},
        )


def test_p04_all_valid_report_types_accepted():
    from api.routes.strategy_analysis import ReportCreate
    for rt in ["daily", "weekly", "monthly"]:
        obj = ReportCreate(
            exchange="gmofx", currency_pair="USD_JPY",
            report_type=rt,
            reported_at=datetime(2026, 4, 1, 9, 0),
        )
        assert obj.report_type == rt


def test_p05_all_valid_agent_names_accepted():
    from api.routes.strategy_analysis import AgentAnalysisCreate
    for agent in ["alice", "samantha", "rachel"]:
        obj = AgentAnalysisCreate(agent_name=agent, summary="테스트 요약", structured_data={})
        assert obj.agent_name == agent


def test_p06_invalid_reflection_agent_name_rejected():
    from pydantic import ValidationError
    from api.routes.strategy_analysis import ReflectionCreate
    with pytest.raises(ValidationError, match="agent_name"):
        ReflectionCreate(
            reflection_date=date(2026, 3, 31),
            agent_name="monica",
            period_type="short",
        )


def test_p07_invalid_period_type_rejected():
    from pydantic import ValidationError
    from api.routes.strategy_analysis import ReflectionCreate
    with pytest.raises(ValidationError, match="period_type"):
        ReflectionCreate(
            reflection_date=date(2026, 3, 31),
            agent_name="alice",
            period_type="yearly",
        )


def test_p08_all_valid_period_types_accepted():
    from api.routes.strategy_analysis import ReflectionCreate
    for pt in ["short", "medium", "long"]:
        obj = ReflectionCreate(
            reflection_date=date(2026, 3, 31),
            agent_name="alice",
            period_type=pt,
        )
        assert obj.period_type == pt


# ──────────────────────────────────────────────────────────────
# 서비스 — chart_start/chart_end 자동 계산
# ──────────────────────────────────────────────────────────────

def test_s01_chart_range_daily():
    from api.services.strategy_analysis_service import _compute_chart_range
    reported = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
    start, end = _compute_chart_range(reported, "daily")
    assert (reported - start).days == 7
    assert end == reported


def test_s02_chart_range_weekly():
    from api.services.strategy_analysis_service import _compute_chart_range
    reported = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
    start, end = _compute_chart_range(reported, "weekly")
    assert (reported - start).days == 14


def test_s03_chart_range_monthly():
    from api.services.strategy_analysis_service import _compute_chart_range
    reported = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
    start, end = _compute_chart_range(reported, "monthly")
    assert (reported - start).days == 30


# ──────────────────────────────────────────────────────────────
# 서비스 — 변환 헬퍼
# ──────────────────────────────────────────────────────────────

def test_s04_report_to_dict_basic():
    from api.services.strategy_analysis_service import _report_to_dict
    r = _make_report()
    d = _report_to_dict(r, include_analyses=False)
    assert d["id"] == 1
    assert d["currency_pair"] == "USD_JPY"
    assert d["final_decision"] == "approved"
    assert "analyses" not in d


def test_s05_report_to_dict_with_analyses():
    from api.services.strategy_analysis_service import _report_to_dict
    r = _make_report(analyses=[_make_analysis()])
    d = _report_to_dict(r, include_analyses=True)
    assert len(d["analyses"]) == 1
    assert d["analyses"][0]["agent_name"] == "alice"


def test_s06_reflection_to_dict():
    from api.services.strategy_analysis_service import _reflection_to_dict
    r = _make_reflection()
    d = _reflection_to_dict(r)
    assert d["agent_name"] == "alice"
    assert d["period_type"] == "short"
    assert d["missed_data"] == [{"indicator": "RSI divergence", "impact": "진입 2H 지연"}]


# ──────────────────────────────────────────────────────────────
# POST /api/strategy-analysis/reports
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c01_create_report_success():
    from api.routes.strategy_analysis import create_report, ReportCreate
    body = ReportCreate(
        exchange="gmofx",
        currency_pair="USD_JPY",
        report_type="daily",
        reported_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
        strategy_active=True,
        strategy_id=7,
        final_decision="approved",
        final_rationale="추세 명확",
        analyses=[
            {"agent_name": "alice", "summary": "상승 추세", "structured_data": {"confidence": 85}},
        ],
    )
    mock_db = AsyncMock()
    expected = {"id": 1, "currency_pair": "USD_JPY", "analyses": []}

    with patch("api.routes.strategy_analysis.svc.create_report", new=AsyncMock(return_value=expected)):
        result = await create_report(body=body, db=mock_db)

    assert result["id"] == 1
    assert result["currency_pair"] == "USD_JPY"


@pytest.mark.asyncio
async def test_c02_create_report_duplicate_returns_409():
    from api.routes.strategy_analysis import create_report, ReportCreate
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError
    body = ReportCreate(
        exchange="gmofx",
        currency_pair="USD_JPY",
        report_type="daily",
        reported_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
    )
    mock_db = AsyncMock()

    with patch(
        "api.routes.strategy_analysis.svc.create_report",
        side_effect=IntegrityError("dup", {}, None),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await create_report(body=body, db=mock_db)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["blocked_code"] == "DUPLICATE_REPORT"


# ──────────────────────────────────────────────────────────────
# GET /api/strategy-analysis/reports/latest
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_g01_get_latest_reports():
    from api.routes.strategy_analysis import get_latest_reports
    mock_db = AsyncMock()
    expected = [{"id": 1, "currency_pair": "USD_JPY"}, {"id": 2, "currency_pair": "EUR_JPY"}]

    with patch("api.routes.strategy_analysis.svc.get_latest_reports", new=AsyncMock(return_value=expected)):
        result = await get_latest_reports(exchange="gmofx", db=mock_db)

    assert len(result) == 2
    assert result[0]["currency_pair"] == "USD_JPY"


# ──────────────────────────────────────────────────────────────
# GET /api/strategy-analysis/reports
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_g02_list_reports_invalid_type_returns_400():
    from api.routes.strategy_analysis import list_reports
    from fastapi import HTTPException
    mock_db = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await list_reports(exchange=None, currency_pair=None, report_type="hourly", limit=50, db=mock_db)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["blocked_code"] == "INVALID_REPORT_TYPE"


@pytest.mark.asyncio
async def test_g03_list_reports_success():
    from api.routes.strategy_analysis import list_reports
    mock_db = AsyncMock()
    expected = [{"id": 1}]

    with patch("api.routes.strategy_analysis.svc.list_reports", new=AsyncMock(return_value=expected)):
        result = await list_reports(
            exchange="gmofx", currency_pair="USD_JPY",
            report_type="daily", limit=10, db=mock_db,
        )

    assert len(result) == 1


# ──────────────────────────────────────────────────────────────
# GET /api/strategy-analysis/reports/{id}
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_g04_get_report_not_found_returns_404():
    from api.routes.strategy_analysis import get_report
    from fastapi import HTTPException
    mock_db = AsyncMock()

    with patch("api.routes.strategy_analysis.svc.get_report", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await get_report(report_id=999, db=mock_db)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_g05_get_report_invalid_id_returns_400():
    from api.routes.strategy_analysis import get_report
    from fastapi import HTTPException
    mock_db = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await get_report(report_id=0, db=mock_db)
    assert exc_info.value.status_code == 400


# ──────────────────────────────────────────────────────────────
# POST /api/strategy-analysis/reflections
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r01_create_reflection_success():
    from api.routes.strategy_analysis import create_reflection, ReflectionCreate
    body = ReflectionCreate(
        reflection_date=date(2026, 3, 31),
        agent_name="alice",
        period_type="short",
        missed_data=[{"indicator": "RSI divergence", "impact": "진입 2H 지연"}],
    )
    mock_db = AsyncMock()
    expected = {"id": 1, "agent_name": "alice", "period_type": "short"}

    with patch("api.routes.strategy_analysis.svc.create_reflection", new=AsyncMock(return_value=expected)):
        result = await create_reflection(body=body, db=mock_db)

    assert result["id"] == 1
    assert result["agent_name"] == "alice"


@pytest.mark.asyncio
async def test_r02_create_reflection_duplicate_returns_409():
    from api.routes.strategy_analysis import create_reflection, ReflectionCreate
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError
    body = ReflectionCreate(
        reflection_date=date(2026, 3, 31),
        agent_name="alice",
        period_type="short",
    )
    mock_db = AsyncMock()

    with patch(
        "api.routes.strategy_analysis.svc.create_reflection",
        side_effect=IntegrityError("dup", {}, None),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await create_reflection(body=body, db=mock_db)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["blocked_code"] == "DUPLICATE_REFLECTION"


# ──────────────────────────────────────────────────────────────
# GET /api/strategy-analysis/reflections
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r03_list_reflections_invalid_agent_returns_400():
    from api.routes.strategy_analysis import list_reflections
    from fastapi import HTTPException
    mock_db = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await list_reflections(agent_name="monica", period_type=None, limit=50, db=mock_db)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["blocked_code"] == "INVALID_AGENT_NAME"


@pytest.mark.asyncio
async def test_r04_list_reflections_invalid_period_returns_400():
    from api.routes.strategy_analysis import list_reflections
    from fastapi import HTTPException
    mock_db = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await list_reflections(agent_name=None, period_type="yearly", limit=50, db=mock_db)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["blocked_code"] == "INVALID_PERIOD_TYPE"


@pytest.mark.asyncio
async def test_r05_list_reflections_success():
    from api.routes.strategy_analysis import list_reflections
    mock_db = AsyncMock()
    expected = [{"id": 1, "agent_name": "alice"}]

    with patch("api.routes.strategy_analysis.svc.list_reflections", new=AsyncMock(return_value=expected)):
        result = await list_reflections(agent_name="alice", period_type="short", limit=10, db=mock_db)

    assert len(result) == 1


# ──────────────────────────────────────────────────────────────
# GET /api/strategy-analysis/reflections/{id}
# ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r06_get_reflection_not_found_returns_404():
    from api.routes.strategy_analysis import get_reflection
    from fastapi import HTTPException
    mock_db = AsyncMock()

    with patch("api.routes.strategy_analysis.svc.get_reflection", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await get_reflection(reflection_id=999, db=mock_db)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_r07_get_reflection_invalid_id_returns_400():
    from api.routes.strategy_analysis import get_reflection
    from fastapi import HTTPException
    mock_db = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await get_reflection(reflection_id=-1, db=mock_db)
    assert exc_info.value.status_code == 400


# ──────────────────────────────────────────────────────────────
# 보강: 엣지 케이스 + Step 5 통합 검증
# ──────────────────────────────────────────────────────────────

def test_p09_summary_too_short_rejected():
    """summary min_length=5 미달 → ValidationError."""
    from pydantic import ValidationError
    from api.routes.strategy_analysis import AgentAnalysisCreate
    with pytest.raises(ValidationError, match="summary"):
        AgentAnalysisCreate(agent_name="alice", summary="짧", structured_data={})


def test_p10_conditional_decision_accepted():
    """final_decision='conditional' 허용 확인."""
    from api.routes.strategy_analysis import ReportCreate
    obj = ReportCreate(
        exchange="gmofx", currency_pair="USD_JPY",
        report_type="daily",
        reported_at=datetime(2026, 4, 1, 9, 0),
        final_decision="conditional",
    )
    assert obj.final_decision == "conditional"


def test_s07_report_to_dict_analyses_none_safe():
    """analyses가 None이어도 include_analyses=True 에러 없음."""
    from api.services.strategy_analysis_service import _report_to_dict
    r = _make_report(analyses=None)
    d = _report_to_dict(r, include_analyses=True)
    assert d["analyses"] == []


def test_s08_report_to_dict_no_optional_fields():
    """nullable 컬럼 전부 None이어도 KeyError 없음."""
    from api.services.strategy_analysis_service import _report_to_dict
    r = _make_report(
        chart_start=None, chart_end=None,
        strategy_id=None, final_decision=None,
        final_rationale=None, next_review=None,
        created_at=None, reported_at=None,
        analyses=[],
    )
    d = _report_to_dict(r, include_analyses=False)
    assert d["chart_start"] is None
    assert d["final_decision"] is None


@pytest.mark.asyncio
async def test_c03_create_report_telegram_task_called():
    """POST 성공 시 asyncio.create_task가 1회 호출됨."""
    from api.routes.strategy_analysis import create_report, ReportCreate

    body = ReportCreate(
        exchange="gmofx",
        currency_pair="GBP_JPY",
        report_type="weekly",
        reported_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
        final_decision="hold",
        analyses=[],
    )
    mock_db = AsyncMock()
    mock_task = MagicMock()

    with (
        patch("api.routes.strategy_analysis.svc.create_report", new=AsyncMock(return_value={"id": 2})),
        patch("api.routes.strategy_analysis.asyncio.create_task", mock_task),
    ):
        result = await create_report(body=body, db=mock_db)

    assert result == {"id": 2}
    mock_task.assert_called_once()


@pytest.mark.asyncio
async def test_c04_create_report_duplicate_no_telegram():
    """409 시 telegramcreate_task 미호출."""
    from api.routes.strategy_analysis import create_report, ReportCreate
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    body = ReportCreate(
        exchange="gmofx", currency_pair="USD_JPY",
        report_type="daily",
        reported_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
    )
    mock_db = AsyncMock()
    mock_task = MagicMock()

    with (
        patch("api.routes.strategy_analysis.svc.create_report", side_effect=IntegrityError("dup", {}, None)),
        patch("api.routes.strategy_analysis.asyncio.create_task", mock_task),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await create_report(body=body, db=mock_db)

    assert exc_info.value.status_code == 409
    mock_task.assert_not_called()
