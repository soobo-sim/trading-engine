"""
세션 필터 유닛 테스트.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from core.analysis.session_filter import (
    get_current_sessions,
    is_allowed_session,
    is_london_open_blackout,
)

_UTC = timezone.utc
_JST = timezone(timedelta(hours=9))


def _dt(hour: int, minute: int = 0, tzinfo=_UTC) -> datetime:
    """테스트용 datetime 헬퍼."""
    return datetime(2026, 4, 3, hour, minute, 0, tzinfo=tzinfo)


# ── get_current_sessions ──────────────────────────────────────

class TestGetCurrentSessions:
    def test_tokyo_only(self):
        """04:00 UTC = 도쿄 단독 (sydney 종료 후)."""
        sessions = get_current_sessions(_dt(4))
        assert "tokyo" in sessions
        assert "london" not in sessions
        assert "newyork" not in sessions

    def test_london_only(self):
        """12:00 UTC = 런던 단독 (tokyo 종료 후, newyork 시작 전)."""
        sessions = get_current_sessions(_dt(12))
        assert "london" in sessions
        assert "tokyo" not in sessions
        assert "newyork" not in sessions

    def test_london_newyork_overlap(self):
        """15:00 UTC = 런던 + 뉴욕 겹침 (거래량 최대 구간)."""
        sessions = get_current_sessions(_dt(15))
        assert "london" in sessions
        assert "newyork" in sessions

    def test_tokyo_london_overlap(self):
        """08:30 UTC = 도쿄 + 런던 겹침."""
        sessions = get_current_sessions(_dt(8, 30))
        assert "tokyo" in sessions
        assert "london" in sessions

    def test_sydney_midnight(self):
        """23:00 UTC = 시드니만 활성. 도쿄는 아직 시작 전 (00:00 UTC 시작)."""
        sessions = get_current_sessions(_dt(23))
        assert "sydney" in sessions
        assert "tokyo" not in sessions

    def test_dead_zone(self):
        """20:00 UTC = 뉴욕 (13~22), 시드니 포함 전."""
        sessions = get_current_sessions(_dt(20))
        assert "newyork" in sessions
        # 22까지는 sydney 시작 안함
        assert "sydney" not in sessions

    def test_none_uses_now(self):
        """dt=None이면 현재 시각 기반. 예외 없이 list 반환."""
        result = get_current_sessions(None)
        assert isinstance(result, list)

    def test_jst_aware(self):
        """JST 17:00 = UTC 08:00 = 런던 오픈."""
        dt_jst = _dt(17, tzinfo=_JST)
        sessions = get_current_sessions(dt_jst)
        assert "london" in sessions

    def test_no_tzinfo_treated_as_utc(self):
        """tzinfo 없는 naive datetime → UTC로 처리."""
        dt_naive = datetime(2026, 4, 3, 10, 0, 0)  # 10:00 naive
        sessions = get_current_sessions(dt_naive)
        assert "london" in sessions


# ── is_allowed_session ────────────────────────────────────────

class TestIsAllowedSession:
    def test_empty_allowed_always_true(self):
        """allowed_sessions=[] → 필터 비활성, 항상 허용."""
        params = {"allowed_sessions": []}
        assert is_allowed_session(params, _dt(15)) is True

    def test_no_key_always_true(self):
        """allowed_sessions 키 없음 → 비활성."""
        assert is_allowed_session({}, _dt(15)) is True

    def test_tokyo_only_blocks_london(self):
        """allowed=['tokyo'], 런던 시간(12:00 UTC) → 차단."""
        params = {"allowed_sessions": ["tokyo"]}
        assert is_allowed_session(params, _dt(12)) is False

    def test_tokyo_only_allows_tokyo(self):
        """allowed=['tokyo'], 도쿄 시간(04:00 UTC) → 허용."""
        params = {"allowed_sessions": ["tokyo"]}
        assert is_allowed_session(params, _dt(4)) is True

    def test_multiple_allowed(self):
        """allowed=['tokyo', 'london'], 도쿄 시간 → 허용."""
        params = {"allowed_sessions": ["tokyo", "london"]}
        assert is_allowed_session(params, _dt(4)) is True

    def test_overlap_session_allowed(self):
        """allowed=['tokyo'], 08:30 UTC (도쿄+런던 겹침) → 허용 (tokyo 포함)."""
        params = {"allowed_sessions": ["tokyo"]}
        assert is_allowed_session(params, _dt(8, 30)) is True

    def test_unknown_session_name_false(self):
        """allowed=['unknown'], 어느 시간이든 차단 (unknown 세션 없음)."""
        params = {"allowed_sessions": ["unknown_session"]}
        assert is_allowed_session(params, _dt(12)) is False


# ── is_london_open_blackout ───────────────────────────────────

class TestIsLondonOpenBlackout:
    def test_disabled_when_zero(self):
        """blackout_minutes=0 → 비활성, 런던 오픈 시간도 False."""
        params = {"london_open_blackout_minutes": 0}
        assert is_london_open_blackout(params, _dt(8, 10)) is False

    def test_no_key_disabled(self):
        """키 없음 → 비활성."""
        assert is_london_open_blackout({}, _dt(8, 10)) is False

    def test_within_blackout(self):
        """blackout=30분, 08:10 UTC → 차단."""
        params = {"london_open_blackout_minutes": 30}
        assert is_london_open_blackout(params, _dt(8, 10)) is True

    def test_at_boundary_inclusive(self):
        """blackout=30분, 08:00 UTC → 차단 (경계 포함)."""
        params = {"london_open_blackout_minutes": 30}
        assert is_london_open_blackout(params, _dt(8, 0)) is True

    def test_after_blackout(self):
        """blackout=30분, 08:30 UTC → 차단 해제 (30분은 차단 안 함)."""
        params = {"london_open_blackout_minutes": 30}
        assert is_london_open_blackout(params, _dt(8, 30)) is False

    def test_not_london_hour(self):
        """blackout=60분, 09:10 UTC (런던 오픈 아님) → False."""
        params = {"london_open_blackout_minutes": 60}
        assert is_london_open_blackout(params, _dt(9, 10)) is False

    def test_jst_converts_correctly(self):
        """JST 17:15 = UTC 08:15, blackout=30 → 차단."""
        params = {"london_open_blackout_minutes": 30}
        dt_jst = _dt(17, 15, tzinfo=_JST)
        assert is_london_open_blackout(params, dt_jst) is True
