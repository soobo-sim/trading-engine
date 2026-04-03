"""
FX 시장 세션 가드 유틸리티.

GMO FX 외국환: 평일(월~금) 24시간, 주말(토~일) 휴장.
- 금요일 클로즈: 토요일 06:50 JST (≈ 금요일 21:50 UTC)
- 월요일 오픈: 월요일 07:00 JST (≈ 일요일 22:00 UTC)

※ 메인터넌스 등 거래소 고유 스케줄은 GET /public/v1/status 로 확인.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# JST = UTC+9
_JST = timezone(timedelta(hours=9))

# 금요일 자동 청산 기본값: 금요일 21:00 JST
# 주말 갭 리스크 대비 — 토요일 새벽(06:50 마감)까지 기다리지 않고
# 금요일 밤에 능동적으로 청산하여 수익 보전 + 갭 리스크 회피
_DEFAULT_FRIDAY_CLOSE_HOUR_JST = 21  # 금요일 밤 9시
_DEFAULT_FRIDAY_CLOSE_MINUTE_JST = 0


def now_jst() -> datetime:
    """현재 JST 시각."""
    return datetime.now(_JST)


def is_fx_market_open(dt: datetime | None = None) -> bool:
    """
    FX 시장이 열려있는지 판정.

    열림: 월요일 07:00 JST ~ 토요일 06:50 JST
    닫힘: 토요일 06:50 JST ~ 월요일 07:00 JST

    Args:
        dt: 판정 대상 시각 (None이면 현재 시각)

    Returns:
        True = 시장 열림
    """
    jst = dt.astimezone(_JST) if dt else now_jst()
    weekday = jst.weekday()  # 0=Mon, 5=Sat, 6=Sun

    if weekday == 6:  # 일요일 → 22:00 UTC(= 07:00 JST Mon) 이전은 닫힘
        return False
    if weekday == 5:  # 토요일
        if jst.hour >= 7:  # 06:50 이후 → 닫힘 (7시 넉넉히)
            return False
        # 토요일 00:00~06:49 → 아직 금요일 연장 세션
        return True
    # 월~금
    if weekday == 0 and jst.hour < 7:
        return False  # 월요일 07:00 이전
    return True


def should_close_for_weekend(
    dt: datetime | None = None,
    close_hour_jst: int = _DEFAULT_FRIDAY_CLOSE_HOUR_JST,
    close_minute_jst: int = _DEFAULT_FRIDAY_CLOSE_MINUTE_JST,
) -> bool:
    """
    금요일 마감 전 포지션 청산 시점인지 판정.

    기본: 금요일 21:00 JST 이후 → True
    이 시점 이후에는 신규 진입 차단 + 기존 포지션 청산.
    주말 갭 리스크 대비 — 토요일 새벽 마감까지 기다리지 않고 능동적 청산.

    Args:
        dt: 판정 대상 시각 (None이면 현재 시각)
        close_hour_jst:   청산 시작 시각(JST) — 금요일 기준
        close_minute_jst: 분

    Returns:
        True = 주말 청산 시점
    """
    jst = dt.astimezone(_JST) if dt else now_jst()
    weekday = jst.weekday()

    # 토요일: 항상 True (시장 닫힘 임박 또는 닫힘)
    if weekday == 5:
        return True

    # 일요일: 항상 True (시장 닫힘)
    if weekday == 6:
        return True

    # 금요일: close_hour 이후 → True (능동적 주말 청산)
    if weekday == 4:
        if jst.hour > close_hour_jst:
            return True
        if jst.hour == close_hour_jst and jst.minute >= close_minute_jst:
            return True

    return False


def minutes_until_market_close(dt: datetime | None = None) -> int | None:
    """
    FX 시장 마감(토 06:50 JST)까지 남은 분.

    시장 닫혀있으면 None.
    """
    jst = dt.astimezone(_JST) if dt else now_jst()
    if not is_fx_market_open(jst):
        return None

    # 이번 주 토요일 06:50 JST
    days_until_sat = (5 - jst.weekday()) % 7
    if days_until_sat == 0 and jst.hour >= 7:
        return 0  # 이미 토요일 마감 지남
    close_time = jst.replace(hour=6, minute=50, second=0, microsecond=0) + timedelta(
        days=days_until_sat
    )
    diff = (close_time - jst).total_seconds()
    return max(0, int(diff / 60))
