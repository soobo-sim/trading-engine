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


# ──────────────────────────────────────────────────────────────
# 전략 비교 structured_data 계약 검증
# (REPORTING_SYSTEM_DESIGN.md §3.2.1 §7 strategy_comparison 갱신 반영)
# ──────────────────────────────────────────────────────────────

class TestStrategyComparisonStructuredData:
    """주간/월간 보고 structured_data에 strategy_comparison 필드 계약."""

    def _analysis_with(self, data: dict):
        return _make_analysis(structured_data=data)

    def test_sd01_strategy_comparison_keys_present(self):
        """strategy_comparison 배열의 각 항목이 필수 키를 포함."""
        comparison = [
            {
                "strategy_id": 4,
                "status": "active",
                "pair": "GBP_JPY",
                "style": "box_mean_reversion",
                "regime_fit": "✅",
                "paper_progress": None,
                "rationale": "횡보장 지속, 박스 유효",
            },
            {
                "strategy_id": 2,
                "status": "proposed",
                "pair": "GBP_JPY",
                "style": "trend_following",
                "regime_fit": "❌",
                "paper_progress": "3/20",
                "rationale": "횡보장에서 손실 예상",
            },
        ]
        data = {
            "strategy_comparison": comparison,
            "best_strategy_id": 4,
            "best_strategy_rationale": "박스역추세가 현 횡보장에서 유일하게 양의 EV",
            "switch_recommended": False,
        }
        a = self._analysis_with(data)
        sd = a.structured_data
        assert "strategy_comparison" in sd
        assert "best_strategy_id" in sd
        assert "best_strategy_rationale" in sd
        assert sd["best_strategy_rationale"] != ""
        assert len(sd["strategy_comparison"]) == 2

    def test_sd02_best_strategy_rationale_is_non_empty_string(self):
        """best_strategy_rationale은 비어있지 않아야 한다."""
        data = {
            "strategy_comparison": [],
            "best_strategy_id": 4,
            "best_strategy_rationale": "EMA slope 양전환으로 추세추종이 박스보다 EV 우위",
            "switch_recommended": False,
        }
        a = self._analysis_with(data)
        rationale = a.structured_data.get("best_strategy_rationale", "")
        assert isinstance(rationale, str) and len(rationale) > 0

    def test_sd03_rebacktest_weekly_structure(self):
        """weekly 보고 structured_data에 rebacktest_weekly 키 구조 검증."""
        rebacktest = [
            {
                "strategy_id": 4,
                "style": "box_mean_reversion",
                "period": "2026-03-29~04-04",
                "candles": 42,
                "pnl": 2850,
                "win_rate": 42.0,
                "sharpe": 1.2,
                "optimal_params": {"stop_loss_pct": 1.5},
                "vs_live_gap_pct": -5.3,
                "conclusion": "실전과 괴리 미미, 파라미터 유지",
            }
        ]
        data = {"rebacktest_weekly": rebacktest}
        a = self._analysis_with(data)
        rb = a.structured_data["rebacktest_weekly"][0]
        assert rb["candles"] == 42
        assert "conclusion" in rb
        assert "vs_live_gap_pct" in rb

    def test_sd04_rebacktest_monthly_gap_exceeds_threshold(self):
        """monthly 재백테스팅에서 괴리 15%p+ 시 conclusion에 조정 언급 필수."""
        rebacktest = [
            {
                "strategy_id": 4,
                "style": "box_mean_reversion",
                "period": "2026-03-01~04-01",
                "candles": 180,
                "pnl": 1000,
                "win_rate": 35.0,
                "sharpe": 0.5,
                "optimal_params": {},
                "vs_live_gap_pct": -18.5,
                "conclusion": "괴리 18.5%p → ema_period 15→12 조정 제안",
            }
        ]
        data = {"rebacktest_monthly": rebacktest}
        a = self._analysis_with(data)
        rb = a.structured_data["rebacktest_monthly"][0]
        assert abs(rb["vs_live_gap_pct"]) >= 15.0
        # 괴리 15p+ 시 conclusion이 비어있으면 안 됨
        assert rb["conclusion"] != ""

    def test_sd05_strategy_comparison_regime_fit_values(self):
        """regime_fit은 ✅ 또는 ❌이어야 한다."""
        valid_fits = {"✅", "❌"}
        comparison = [
            {"strategy_id": 1, "status": "active", "pair": "USD_JPY",
             "style": "trend_following", "regime_fit": "✅",
             "paper_progress": None, "rationale": "추세 명확"},
            {"strategy_id": 2, "status": "proposed", "pair": "USD_JPY",
             "style": "box_mean_reversion", "regime_fit": "❌",
             "paper_progress": "0/20", "rationale": "추세장 부적합"},
        ]
        a = self._analysis_with({"strategy_comparison": comparison})
        for item in a.structured_data["strategy_comparison"]:
            assert item["regime_fit"] in valid_fits


# ──────────────────────────────────────────────────────────────
# 정신차리자 structured_data 계약 검증
# (REPORTING_SYSTEM_DESIGN.md §3.3 / WORKFLOW_4 §A~§K 반영)
# ──────────────────────────────────────────────────────────────

class TestWakeUpStructuredData:
    """R-7 정신차리자 alice structured_data 필수 키 계약."""

    def _alice_analysis(self, data: dict):
        return _make_analysis(agent_name="alice", structured_data=data)

    def test_wu01_optimal_params_and_diff_present(self):
        """§I 역산: optimal_params / optimal_pnl / actual_vs_optimal_diff_pct 존재."""
        data = {
            "cause": "EXIT_TIMING",
            "confidence": 0.8,
            "optimal_params": {"stop_loss_pct": 1.5, "trailing_stop_atr_initial": 2.0},
            "optimal_pnl": 2500,
            "actual_vs_optimal_diff_pct": -4.3,
        }
        a = self._alice_analysis(data)
        sd = a.structured_data
        assert "optimal_params" in sd
        assert "optimal_pnl" in sd
        assert "actual_vs_optimal_diff_pct" in sd
        assert isinstance(sd["optimal_params"], dict)

    def test_wu02_root_cause_codes_is_list(self):
        """§J 근본 원인: root_cause_codes는 리스트, root_cause_detail은 문자열."""
        data = {
            "root_cause_codes": ["ENTRY_TIMING", "INFO_GAP"],
            "root_cause_detail": "EMA 기울기가 약한데 진입. 당시 금리 발표 직전이라 변동성 일시 확대.",
        }
        a = self._alice_analysis(data)
        sd = a.structured_data
        assert isinstance(sd["root_cause_codes"], list)
        assert len(sd["root_cause_codes"]) >= 1
        assert isinstance(sd["root_cause_detail"], str)
        assert len(sd["root_cause_detail"]) > 10

    def test_wu03_action_items_structure(self):
        """§K 액션 아이템: who / action / when / done_when / status 필수 키."""
        required_keys = {"who", "action", "when", "done_when", "status"}
        data = {
            "action_items": [
                {
                    "who": "파라미터 조정",
                    "action": "ema_slope_entry_min 0.05→0.10 변경",
                    "when": "다음 체제 점검",
                    "done_when": "백테스트 WR +3%p 이상",
                    "status": "pending",
                }
            ]
        }
        a = self._alice_analysis(data)
        for item in a.structured_data["action_items"]:
            assert required_keys.issubset(item.keys()), f"누락 키: {required_keys - item.keys()}"

    def test_wu04_overfit_risk_valid_values(self):
        """overfit_risk는 low / medium / high 중 하나."""
        valid = {"low", "medium", "high"}
        for risk in valid:
            data = {"overfit_risk": risk}
            a = self._alice_analysis(data)
            assert a.structured_data["overfit_risk"] in valid

    def test_wu05_full_section_ijk_payload_accepted(self):
        """§I + §J + §K 전체가 한 structured_data에 공존 가능."""
        data = {
            "cause": "REGIME_MISMATCH",
            "confidence": 0.7,
            "optimal_params": {"ema_period": 12},
            "optimal_pnl": 1800,
            "actual_vs_optimal_diff_pct": -3.1,
            "root_cause_codes": ["REGIME_MISMATCH", "PARAM_SUBOPTIMAL"],
            "root_cause_detail": "체제 불확실 상태에서 진입. 당시 regime API가 ranging 판정했지만 실제 unclear 구간.",
            "action_items": [
                {"who": "앨리스", "action": "체제 confidence 임계값 검토",
                 "when": "주간 브리핑 전", "done_when": "WF 재검증 완료", "status": "pending"}
            ],
            "overfit_risk": "medium",
        }
        a = self._alice_analysis(data)
        sd = a.structured_data
        # 세 섹션 전부 접근 가능
        assert sd["optimal_params"] is not None
        assert len(sd["root_cause_codes"]) >= 1
        assert len(sd["action_items"]) >= 1
