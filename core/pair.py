"""통화 페어 정규화 유틸리티.

라젠카 내부에서 pair는 항상 소문자 (btc_jpy).
외부 거래소 API 호출 시에는 어댑터가 별도 변환한다.
"""
from __future__ import annotations


def normalize_pair(pair: str) -> str:
    """pair 문자열을 소문자 정규형으로 변환한다.

    DB 저장, 메모리 키, 비교, API 입력 수신 시 항상 이 함수를 통과시킨다.
    외부 거래소 API 호출에서 대문자가 필요한 경우는 어댑터(_pair_to_symbol)가 담당한다.

    Args:
        pair: 통화 페어 문자열. 예: "BTC_JPY", "btc_jpy", " Btc_Jpy "

    Returns:
        소문자 정규형. 예: "btc_jpy"
    """
    return pair.strip().lower()
