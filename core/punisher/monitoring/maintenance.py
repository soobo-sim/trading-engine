"""
거래소 정기 메인터넌스 스케줄 유틸리티.

메인터넌스 시간대에는 SF-03(WS), SF-06(API) 오탐이 발생할 수 있으므로,
Telegram 경고 전송 전에 이 함수로 스킵 여부를 판정한다.

== 환경변수 설정 (GMO Coin 메인터넌스 시간 오버라이드) ==
  GMO_COIN_MAINTENANCE_WEEKDAY    : 요일 (0=월 ~ 6=일, 기본 5=토)
  GMO_COIN_MAINTENANCE_START      : 메인터넌스 시작 시각 HH:MM (기본 "09:00")
  GMO_COIN_MAINTENANCE_END        : 메인터넌스 종료 시각 HH:MM (기본 "11:00")
  GMO_COIN_MAINTENANCE_PREOPEN_MIN: 프레오픈 추가 대기 시간(분) (기본 10)
                                    실제 차단 종료 = END + PREOPEN_MIN
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def _parse_hhmm(s: str) -> time:
    """'HH:MM' 문자열 → time 객체. 파싱 실패 시 ValueError."""
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def _build_gmo_coin_schedule() -> list[tuple[int, time, time]]:
    """환경변수에서 GMO Coin 정기 메인터넌스 스케줄을 구성한다."""
    weekday = int(os.getenv("GMO_COIN_MAINTENANCE_WEEKDAY", "5"))
    start_str = os.getenv("GMO_COIN_MAINTENANCE_START", "09:00")
    end_str = os.getenv("GMO_COIN_MAINTENANCE_END", "11:00")
    preopen_min = int(os.getenv("GMO_COIN_MAINTENANCE_PREOPEN_MIN", "10"))

    start = _parse_hhmm(start_str)
    end_base = _parse_hhmm(end_str)
    # 프레오픈 시간 추가 (예: 11:00 + 10분 = 11:10)
    end_dt = datetime.combine(date.today(), end_base) + timedelta(minutes=preopen_min)
    end = end_dt.time()

    return [(weekday, start, end)]


# 거래소별 정기 메인터넌스 스케줄
# (weekday, start_time_jst, end_time_jst)
# weekday: 0=월 ~ 6=일
# GMO Coin: 매주 토요일 09:00~11:00 JST + 프레오픈 10분 = 11:10까지 차단
MAINTENANCE_SCHEDULES: dict[str, list[tuple[int, time, time]]] = {
    "gmo_coin": _build_gmo_coin_schedule(),
}


def is_maintenance_window(exchange: str, now_jst: datetime | None = None) -> bool:
    """현재 시각이 거래소 정기 메인터넌스 시간대인지 판별.

    Args:
        exchange: 거래소 식별자 (예: "gmofx", "bitflyer"). 대소문자 무관.
        now_jst:  판정 기준 시각. None이면 현재 시각 사용.

    Returns:
        True이면 메인터넌스 중.
    """
    if now_jst is None:
        now_jst = datetime.now(JST)

    schedules = MAINTENANCE_SCHEDULES.get(exchange.lower(), [])
    for weekday, start, end in schedules:
        if now_jst.weekday() == weekday:
            current_time = now_jst.time()
            if start <= current_time <= end:
                return True
    return False


def seconds_until_maintenance_end(exchange: str, now_jst: datetime | None = None) -> int:
    """메인터넌스 종료까지 남은 초를 반환한다.

    메인터넌스 중이 아니면 0을 반환.
    메인터넌스 중이지만 종료 시각 계산 불가면 3600(1시간)을 반환.

    Args:
        exchange: 거래소 식별자 (예: "gmofx"). 대소문자 무관.
        now_jst:  판정 기준 시각. None이면 현재 시각 사용.
    """
    if now_jst is None:
        now_jst = datetime.now(JST)

    schedules = MAINTENANCE_SCHEDULES.get(exchange.lower(), [])
    for weekday, start, end in schedules:
        if now_jst.weekday() == weekday:
            current_time = now_jst.time()
            if start <= current_time <= end:
                # 종료 시각까지 남은 초 계산
                end_dt = now_jst.replace(
                    hour=end.hour, minute=end.minute, second=0, microsecond=0
                )
                remaining = int((end_dt - now_jst).total_seconds())
                # 음수 방지 (end_dt가 이미 지났으면 0 반환)
                return max(remaining, 0) if remaining >= 0 else 3600
    return 0
