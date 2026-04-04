"""
거래소 정기 메인터넌스 스케줄 유틸리티.

메인터넌스 시간대에는 SF-03(WS), SF-06(API) 오탐이 발생할 수 있으므로,
Telegram 경고 전송 전에 이 함수로 스킵 여부를 판정한다.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

# 거래소별 정기 메인터넌스 스케줄
# (weekday, start_time_jst, end_time_jst)
# weekday: 0=월 ~ 6=일
# GMO FX: 매주 토요일 09:00~11:00 JST + 10분 여유
MAINTENANCE_SCHEDULES: dict[str, list[tuple[int, time, time]]] = {
    "gmofx": [
        (5, time(9, 0), time(11, 10)),
    ],
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
