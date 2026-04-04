"""
core/analysis/session_filter.py

FX 시장 세션 필터 — 허용 세션 진입 차단 + 런던 오픈 블랙아웃.

세션 정의 (UTC):
  sydney:   22:00~06:00 (일자 넘김)
  tokyo:    00:00~09:00
  london:   08:00~17:00
  newyork:  13:00~22:00

겹침 허용: 런던오픈(08~09) = tokyo+london, 런던-뉴욕(13~17) = london+newyork.

사용:
    from core.analysis.session_filter import is_allowed_session, get_current_sessions
"""
from __future__ import annotations

from datetime import datetime, timezone

# UTC 기준 세션 시작/종료 시간 (start, end)
# end < start 이면 일자 넘김 (sydney)
SESSION_HOURS_UTC: dict[str, tuple[int, int]] = {
    "sydney":  (22, 6),
    "tokyo":   (0, 9),
    "london":  (8, 17),
    "newyork": (13, 22),
}


def get_current_sessions(dt: datetime | None = None) -> list[str]:
    """
    현재 활성 세션 목록 반환 (겹침 허용).

    Args:
        dt: 기준 시각 (None이면 현재). tzinfo가 없으면 UTC로 간주.

    Returns:
        활성 세션 이름 목록. 예: ["london", "newyork"]
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    hour = dt.hour
    active = []
    for name, (start, end) in SESSION_HOURS_UTC.items():
        if start < end:
            # 정상: start~end (당일)
            if start <= hour < end:
                active.append(name)
        else:
            # 일자 넘김: start~24 or 0~end
            if hour >= start or hour < end:
                active.append(name)
    return active


def is_allowed_session(params: dict, dt: datetime | None = None) -> bool:
    """
    전략 파라미터의 allowed_sessions 기준으로 현재 세션 허용 여부 판단.

    Args:
        params: 전략 파라미터. "allowed_sessions" 키 (list[str]).
        dt:     기준 시각 (None이면 현재).

    Returns:
        True = 진입 허용, False = 차단.
        allowed_sessions가 빈 배열이면 항상 True (필터 비활성).
    """
    allowed: list[str] = params.get("allowed_sessions", [])
    if not allowed:
        return True  # 필터 비활성
    current = get_current_sessions(dt)
    return any(s in allowed for s in current)


def is_london_open_blackout(params: dict, dt: datetime | None = None) -> bool:
    """
    런던 오픈 직후 N분 진입 차단 여부.

    런던 오픈: 08:00 UTC.
    blackout_minutes 동안 (08:00~08:00+N분) 진입 차단.

    Args:
        params: 전략 파라미터. "london_open_blackout_minutes" 키 (int, 기본 0).
        dt:     기준 시각 (None이면 현재).

    Returns:
        True = 블랙아웃 중 (차단), False = 정상.
        blackout_minutes=0 이면 항상 False (비활성).
    """
    minutes = int(params.get("london_open_blackout_minutes", 0))
    if minutes <= 0:
        return False

    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    # 런던 오픈: 매일 08:00 UTC
    london_open_hour = 8
    if dt.hour == london_open_hour and dt.minute < minutes:
        return True
    return False
