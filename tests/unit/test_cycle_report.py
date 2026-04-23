"""
P5 — CycleReport + TelegramEvolutionHandler 테스트.

ER (Evolution Routing):  ER-01~ER-04
CV (Causality Validation): CV-01~CV-06
CR (CycleReport Service):  CS-01~CS-04
FT (Format):               FT-01~FT-02
RA (API):                  CA-01~CA-04
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
import pytest_asyncio

from api.schemas.evolution import CycleReportInput
from api.services.cycle_report_service import (
    CycleReportService,
    format_evolution_report,
    validate_causality,
)


# ── ER: 도메인 라우팅 ────────────────────────────────────────

class TestEvolutionDomainRouting:
    def test_evolution_prefix_not_in_judge_or_punisher(self):
        """ER-01: EVOLUTION_PREFIXES는 JUDGE/PUNISHER와 겹치지 않음."""
        from core.shared.logging.telegram_handlers import (
            EVOLUTION_PREFIXES, JUDGE_PREFIXES, PUNISHER_PREFIXES
        )
        assert not (EVOLUTION_PREFIXES & JUDGE_PREFIXES)
        assert not (EVOLUTION_PREFIXES & PUNISHER_PREFIXES)

    def test_evolution_handler_installed_correctly(self):
        """ER-02: TelegramEvolutionHandler import 가능."""
        from core.shared.logging.telegram_handlers import TelegramEvolutionHandler
        h = TelegramEvolutionHandler(bot_token="tok", chat_id="cid")
        assert isinstance(h, logging.Handler)

    def test_evolution_handler_ignores_judge_logger(self):
        """ER-03: judge 로거 이름은 evolution 핸들러에서 필터링."""
        from core.shared.logging.telegram_handlers import TelegramEvolutionHandler
        h = TelegramEvolutionHandler(bot_token="tok", chat_id="cid")
        record = logging.LogRecord(
            name="core.judge.decision",
            level=logging.INFO,
            pathname="", lineno=0, msg="test", args=(), exc_info=None,
        )
        # emit가 호출되어도 _send_telegram을 호출하지 않아야 한다
        with patch("asyncio.ensure_future") as mock_future:
            h.emit(record)
            mock_future.assert_not_called()

    def test_evolution_handler_routes_evolution_logger(self):
        """ER-04: core.judge.evolution 로거는 진화 채널로 라우팅."""
        from core.shared.logging.telegram_handlers import TelegramEvolutionHandler
        h = TelegramEvolutionHandler(bot_token="tok", chat_id="cid")
        record = logging.LogRecord(
            name="core.judge.evolution.hypotheses",
            level=logging.INFO,
            pathname="", lineno=0, msg="가설 등록", args=(), exc_info=None,
        )
        with patch("asyncio.ensure_future") as mock_future, \
             patch("asyncio.get_running_loop", return_value=MagicMock()):
            h.emit(record)
            mock_future.assert_called_once()


# ── CV: 인과 검증 ────────────────────────────────────────────

def _full_input(**overrides) -> CycleReportInput:
    defaults = dict(
        hypothesis_id="H-2026-001",
        mode="full",
        observation="지난 3일 8건 중 5건 손실 — 모두 slope 0.04 미만 진입.",
        hypothesis="ema_slope_entry_min을 0.04 → 0.07로 상향하면 승률 개선 기대.",
        validation="백테스트 45건 — sharpe 1.42, wr 64%.",
        application="sharpe 1.42 충족 — paper 7일 추적 시작.",
        evaluation="기준 통과 + 가드레일 미발동 시 adopted.",
        lesson="교훈 L-2026-XXX 자동 등록 예정.",
        references={"lessons": ["L-2026-001"], "tunables": ["trend.ema_slope_entry_min"]},
    )
    defaults.update(overrides)
    return CycleReportInput(**defaults)


class TestCausalityValidation:
    def test_full_mode_all_links_pass(self):
        """CV-01: 정상 full 보고서 → 모든 인과 통과."""
        inp = _full_input()
        checks = validate_causality(inp)
        assert all(checks.values()), f"Failing: {[k for k,v in checks.items() if not v]}"

    def test_no_signal_mode_skips_causality(self):
        """CV-02: no_signal 모드에서는 인과 검증 무시 (ValueError 없음)."""
        inp = CycleReportInput(
            mode="no_signal",
            observation="이번 3일 분석 결과 — 변경 신호 없음.",
        )
        checks = validate_causality(inp)
        # no_signal에서 서비스는 failing 체크를 안 함 — validate_causality는 실행되지만 서비스에서 무시
        assert isinstance(checks, dict)

    def test_obs_to_hyp_number_match(self):
        """CV-03: observation의 숫자가 hypothesis에 등장해야 obs_to_hyp=True."""
        inp = _full_input(
            observation="손실 5건 발생, 모두 slope 0.04 마지막에 진입.",
            hypothesis="5건 패턴 기반으로 slope 상향 제안.",
        )
        checks = validate_causality(inp)
        assert checks["obs_to_hyp"] is True

    def test_obs_to_hyp_number_mismatch(self):
        """CV-04: observation 수치가 hypothesis에 없으면 obs_to_hyp=False."""
        inp = _full_input(
            observation="손실 5건 발생, 모두 slope 0.04 마지막에 진입.",
            hypothesis="쿤전히 다른 엘 바이 뭐라고 하는 에레이 다.",  # '5'가 없음
        )
        checks = validate_causality(inp)
        assert checks["obs_to_hyp"] is False

    def test_val_to_app_metric_match(self):
        """CV-05: validation 수치가 application에 포함되면 val_to_app=True."""
        inp = _full_input(
            validation="sharpe 1.42 달성.",
            application="1.42 달성으로 paper 시작.",
        )
        checks = validate_causality(inp)
        assert checks["val_to_app"] is True

    def test_eval_to_lesson_keyword_match(self):
        """CV-06: lesson에 'L-' 또는 '교훈' 포함 시 eval_to_lesson=True."""
        inp = _full_input(lesson="교훈 L-2026-042 자동 등록 완료.")
        checks = validate_causality(inp)
        assert checks["eval_to_lesson"] is True


# ── CS: CycleReportService ───────────────────────────────────

class TestCycleReportService:
    @pytest.mark.asyncio
    async def test_build_full_report(self):
        """CS-01: full 모드 정상 입력 → report 생성."""
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=None)))
        svc = CycleReportService(mock_db)
        inp = _full_input()
        report = await svc.build_and_validate(inp)
        assert report.cycle_id.startswith("CR-")
        assert report.mode == "full"
        assert report.hypothesis_id == "H-2026-001"

    @pytest.mark.asyncio
    async def test_build_no_signal_report(self):
        """CS-02: no_signal 모드 → report.mode == 'no_signal'."""
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=None)))
        svc = CycleReportService(mock_db)
        inp = CycleReportInput(
            mode="no_signal",
            observation="이번 3일 분석 결과 — 변경 신호 없음.",
        )
        report = await svc.build_and_validate(inp)
        assert report.mode == "no_signal"

    @pytest.mark.asyncio
    async def test_invalid_causality_raises(self):
        """CS-03: full 모드에서 인과 단절 → ValueError."""
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=None)))
        svc = CycleReportService(mock_db)
        # 명시적으로 causality_self_check에 obs_to_hyp=False 주입
        inp = _full_input(
            causality_self_check={"obs_to_hyp": False, "hyp_to_val": True, "val_to_app": True, "app_to_eval": True, "eval_to_lesson": True},
        )
        with pytest.raises(ValueError, match="인과 단절"):
            await svc.build_and_validate(inp)

    @pytest.mark.asyncio
    async def test_persist_called(self):
        """CS-04: persist() 호출 시 DB execute가 실행됨."""
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=None)))
        svc = CycleReportService(mock_db)
        inp = _full_input()
        report = await svc.build_and_validate(inp)
        await svc.persist(report)
        assert mock_db.commit.called


# ── FT: 포맷 ────────────────────────────────────────────────

class TestFormatEvolutionReport:
    def test_full_includes_6_sections(self):
        """FT-01: full 보고서에는 1️⃣~6️⃣ 섹션이 모두 포함."""
        from api.services.cycle_report_service import CycleReportResponse
        from datetime import datetime, timezone, timedelta
        now = datetime.now(tz=timezone(timedelta(hours=9)))
        report = CycleReportResponse(
            cycle_id="CR-2026-001",
            cycle_at=now,
            hypothesis_id="H-2026-001",
            mode="full",
            observation="관찰",
            hypothesis="가설",
            validation="검증",
            application="적용",
            evaluation="평가",
            lesson="교훈",
            causality_self_check={},
            references={"lessons": [], "tunables": []},
        )
        text = format_evolution_report(report)
        for section in ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]:
            assert section in text

    def test_no_signal_short(self):
        """FT-02: no_signal 보고서는 짧은 1줄 메시지."""
        from api.services.cycle_report_service import CycleReportResponse
        from datetime import datetime, timezone, timedelta
        now = datetime.now(tz=timezone(timedelta(hours=9)))
        report = CycleReportResponse(
            cycle_id="CR-2026-002",
            cycle_at=now,
            hypothesis_id=None,
            mode="no_signal",
            observation="이번 3일 분석 결과 — 변경 신호 없음.",
            hypothesis="(없음)",
            validation="(없음)",
            application="(없음)",
            evaluation="(없음)",
            lesson="(없음)",
            causality_self_check={},
            references={},
        )
        text = format_evolution_report(report)
        assert "변경 신호 없음" in text
        assert "1️⃣" not in text  # full 섹션 없어야 함

    def test_detail_blocks_rendered(self):
        """FT-03: detail 필드 포함 시 파라미터 비교표 + 시장 컨텍스트 블록이 렌더링됨."""
        from api.services.cycle_report_service import CycleReportResponse
        from api.schemas.evolution import (
            CycleReportDetail, ParameterChangeSummary,
            TradeStatsSummary, MarketContextSummary, BacktestSummary,
        )
        from datetime import datetime, timezone, timedelta
        now = datetime.now(tz=timezone(timedelta(hours=9)))
        detail = CycleReportDetail(
            parameter_changes=[
                ParameterChangeSummary(
                    key="trend.atr_entry_min", label="최소ATR진입",
                    before=1.5, after=1.8, unit="%",
                    rationale="손실 5건 중 4건 ATR<1.5%"
                )
            ],
            trade_stats=TradeStatsSummary(
                total=8, wins=5, losses=3, win_rate_pct=62.5,
                pnl_jpy=12000, avg_pnl_jpy=1500, max_loss_jpy=-8000,
                losing_patterns=["저변동성 진입", "체제 전환 직전"],
                lesson_adherence_rate=0.80,
            ),
            market_context=MarketContextSummary(
                period="2026-04-20 ~ 2026-04-23",
                btc_range_jpy="¥12,000,000 ~ ¥13,500,000",
                atr_avg_pct=1.17,
                regime_changes=[],
                fng_start=42, fng_end=48, fng_label="Fear",
                vix=15.2, dxy=104.3,
                key_events=["FOMC 의사록 공개 (비둘기파)"],
                key_news=["BTC ETF 유입 5억달러"],
            ),
            backtest=BacktestSummary(
                period="90d", trades=120,
                sharpe_before=0.8, sharpe_after=1.2,
                wr_before_pct=55.0, wr_after_pct=61.0,
                max_dd_before_pct=12.0, max_dd_after_pct=9.5,
                avg_pnl_before_jpy=800, avg_pnl_after_jpy=1100,
                samantha_comment="범위 내. 변화폭 양호. 백테스트 통과 권고.",
            ),
        )
        report = CycleReportResponse(
            cycle_id="CR-2026-003",
            cycle_at=now,
            hypothesis_id="H-2026-001",
            mode="full",
            observation="관찰",
            hypothesis="가설",
            validation="검증",
            application="적용",
            evaluation="평가",
            lesson="교훈",
            causality_self_check={},
            references={},
            detail=detail,
        )
        text = format_evolution_report(report)
        # 파라미터 비교표 포함 확인
        assert "최소ATR진입" in text
        assert "1.5" in text
        assert "1.8" in text
        # 시장 컨텍스트 포함 확인
        assert "FNG" in text
        assert "42" in text
        # 거래 통계 포함 확인
        assert "62.5" in text
        # 백테스트 결과 포함 확인
        assert "1.2" in text  # sharpe_after
        assert "Samantha" in text


# ── CA: API ──────────────────────────────────────────────────

@pytest_asyncio.fixture
async def cr_db_factory():
    """SQLite in-memory — lessons + hypotheses + ai_judgments 테이블만."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from adapters.database import hypothesis_model, lesson_model  # noqa
    from adapters.database.session import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        wanted = {"lessons", "hypotheses", "ai_judgments"}
        target = [t for name, t in Base.metadata.tables.items() if name in wanted]
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=target))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _cr_build_client(factory):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.evolution import router
    from api.dependencies import get_db

    app = FastAPI()
    app.include_router(router)

    async def _override_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app, raise_server_exceptions=False)


class TestCycleReportAPI:
    def test_generate_no_signal_201(self, cr_db_factory):
        """CA-01: POST /api/cycle-reports/generate (no_signal) → 201."""
        client = _cr_build_client(cr_db_factory)
        resp = client.post("/api/cycle-reports/generate", json={
            "mode": "no_signal",
            "observation": "이번 3일 분석 결과 — 변경 신호 없음. 트레이드 없음.",
        })
        assert resp.status_code == 201

    def test_generate_full_causality_broken_422(self, cr_db_factory):
        """CA-02: 인과 단절 full 보고서 → 422."""
        client = _cr_build_client(cr_db_factory)
        resp = client.post("/api/cycle-reports/generate", json={
            "hypothesis_id": "H-2026-001",
            "mode": "full",
            "observation": "손실 3건 발생. slope 0.04 미만 진입 문제.",
            "hypothesis": "완전히 다른 얘기이므로 인과 단절 발생.",
            "validation": "sharpe 없음.",
            "application": "적용 없음.",
            "evaluation": "평가 없음.",
            "lesson": "없음.",
            "causality_self_check": {
                "obs_to_hyp": False,
                "hyp_to_val": False,
                "val_to_app": False,
                "app_to_eval": False,
                "eval_to_lesson": False,
            },
        })
        assert resp.status_code == 422

    def test_list_cycle_reports_200(self, cr_db_factory):
        """CA-03: GET /api/cycle-reports → 200."""
        client = _cr_build_client(cr_db_factory)
        resp = client.get("/api/cycle-reports")
        assert resp.status_code == 200
        assert "reports" in resp.json()

    def test_generate_returns_telegram_sent_field(self, cr_db_factory):
        """CA-04: 응답에 telegram_sent 필드가 있음."""
        client = _cr_build_client(cr_db_factory)
        resp = client.post("/api/cycle-reports/generate", json={
            "mode": "no_signal",
            "observation": "이번 3일 분석 결과 — 변경 신호 없음. 트레이드 없음.",
        })
        assert resp.status_code == 201
        assert "telegram_sent" in resp.json()

