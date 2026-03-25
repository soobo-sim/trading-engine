"""
Monitoring API — 사만다 15분 보고용 서버측 리포트.

GET /api/monitoring/report   — 완성된 보고 텍스트 + raw 데이터 반환
"""
import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import AppState, get_db, get_state
from api.services.monitoring import generate_box_report, generate_trend_report, generate_cfd_report
from api.services.monitoring_status import generate_trend_status
from core.monitoring.health import _last_report_time

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/monitoring", tags=["Monitoring"])


@router.get("/status", summary="대시보드용 구조화 모니터링 상태")
async def get_monitoring_status(
    pair: str = Query(..., description="페어 (e.g. xrp_jpy / BTC_JPY)"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """
    대시보드용 구조화 상태 응답. 전략 상태·진입조건·포지션·시장 데이터를 단일 JSON으로 반환.
    설계: DASHBOARD_MONITORING_API.md
    """
    StrategyModel = state.models.strategy
    result = await db.execute(
        select(StrategyModel).where(StrategyModel.status == "active")
    )
    active_strategies = result.scalars().all()

    strategy = None
    trading_style = None
    for s in active_strategies:
        params = s.parameters or {}
        s_pair = params.get("pair") or params.get("product_code")
        if s_pair == pair:
            strategy = s
            trading_style = params.get("trading_style")
            break

    if not strategy:
        raise HTTPException(
            status_code=404,
            detail={"error": f"pair={pair}에 해당하는 활성 전략 없음"},
        )

    if trading_style == "trend_following":
        return await generate_trend_status(
            pair=pair,
            prefix=state.prefix,
            pair_column=state.pair_column,
            strategy=strategy,
            adapter=state.adapter,
            trend_manager=state.trend_manager,
            candle_model=state.models.candle,
            trend_position_model=state.models.trend_position,
            db=db,
        )
    else:
        # 박스권/CFD는 추후 구현
        raise HTTPException(
            status_code=400,
            detail={"error": f"trading_style={trading_style}은 status API 미지원 (추후 구현)"},
        )


@router.get("/report", summary="사만다 15분 보고용 모니터링 리포트")
async def get_monitoring_report(
    pair: str = Query(..., description="페어 (e.g. xrp_jpy / BTC_JPY)"),
    test_alert_level: str | None = Query(None, description="테스트용 alert level override (warning|critical)"),
    reset_cooldown: bool = Query(False, description="webhook 쿨다운 리셋 (테스트용)"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    """
    서버가 시그널 계산 → 조건 판단 → telegram_text + memory_block 조립.
    사만다는 이 응답을 그대로 출력하면 된다.
    """
    # 1. 활성 전략 중 해당 pair의 전략 찾기
    StrategyModel = state.models.strategy
    result = await db.execute(
        select(StrategyModel).where(StrategyModel.status == "active")
    )
    active_strategies = result.scalars().all()

    strategy = None
    trading_style = None
    for s in active_strategies:
        params = s.parameters or {}
        s_pair = params.get("pair") or params.get("product_code")
        if s_pair == pair:
            strategy = s
            trading_style = params.get("trading_style")
            break

    if not strategy:
        raise HTTPException(
            status_code=404,
            detail={"error": f"pair={pair}에 해당하는 활성 전략 없음"},
        )

    # test_alert_level 검증
    if test_alert_level and test_alert_level not in ("warning", "critical"):
        raise HTTPException(
            status_code=400,
            detail={"error": "test_alert_level must be 'warning' or 'critical'"},
        )

    # 2. trading_style에 따라 분기
    if trading_style == "trend_following":
        report = await generate_trend_report(
            pair=pair,
            prefix=state.prefix,
            pair_column=state.pair_column,
            strategy=strategy,
            adapter=state.adapter,
            trend_manager=state.trend_manager,
            candle_model=state.models.candle,
            db=db,
            test_alert_level=test_alert_level,
            reset_cooldown=reset_cooldown,
        )
    elif trading_style == "box_mean_reversion":
        report = await generate_box_report(
            pair=pair,
            prefix=state.prefix,
            pair_column=state.pair_column,
            strategy=strategy,
            adapter=state.adapter,
            health_checker=state.health_checker,
            box_model=state.models.box,
            box_position_model=state.models.box_position,
            candle_model=state.models.candle,
            db=db,
            test_alert_level=test_alert_level,
            reset_cooldown=reset_cooldown,
        )
    elif trading_style == "cfd_trend_following":
        report = await generate_cfd_report(
            pair=pair,
            prefix=state.prefix,
            pair_column=state.pair_column,
            strategy=strategy,
            adapter=state.adapter,
            cfd_manager=state.cfd_manager,
            candle_model=state.models.candle,
            db=db,
            test_alert_level=test_alert_level,
            reset_cooldown=reset_cooldown,
        )
    else:
        raise HTTPException(
            status_code=400,
            detail={"error": f"trading_style={trading_style}은 아직 미지원"},
        )

    if not report.get("success"):
        raise HTTPException(status_code=503, detail=report)

    # SF-08: 보고 성공 시각 기록
    import time as _time
    _last_report_time["last"] = _time.time()

    # 안전장치 요약 추가
    try:
        safety_report = await state.health_checker.check_safety_only()
        ok_count = sum(1 for c in safety_report.checks if c.status in ("ok", "n/a"))
        total = len(safety_report.checks)

        if safety_report.status == "all_ok":
            summary = f"🛡️ 안전장치: ✅ 전체 정상 ({ok_count}/{total})"
        elif safety_report.status == "critical":
            critical_names = [c.name for c in safety_report.checks if c.status == "critical"]
            summary = f"🛡️ 안전장치: 🔴 {', '.join(critical_names)} ({ok_count}/{total}) — 즉시 확인 필요"
        else:
            warn_names = [c.name for c in safety_report.checks if c.status == "warning"]
            summary = f"🛡️ 안전장치: 🟡 {', '.join(warn_names)} ({ok_count}/{total})"

        report["safety"] = {
            "status": safety_report.status,
            "summary": summary,
            "checks": [asdict(c) for c in safety_report.checks],
        }
    except Exception as e:
        logger.error(f"안전장치 체크 실패: {e}")
        report["safety"] = {"status": "unknown", "summary": "🛡️ 안전장치: ❓ 체크 실패"}

    return report
