"""
monitoring 패키지 — monitoring_report.py를 5개 모듈로 분리.

하위 호환을 위해 모든 공개 심볼을 re-export.
"""

# display helpers
from .display import (
    JST,
    get_trend_icon,
    get_rsi_state,
    get_ema_state,
    get_volatility_state,
    get_market_summary,
    get_position_summary,
    get_entry_blockers,
    get_entry_blockers_short,
    get_wait_direction,
    get_narrative_situation,
    get_narrative_outlook,
    get_box_narrative_situation,
    get_box_narrative_outlook,
)

# alert system
from .alerts import (
    ALERT_COOLDOWN_SEC,
    ALERT_COOLDOWN_EXTENDED_SEC,
    _prev_raw_cache,
    _last_alert_time,
    _consecutive_same,
    _last_alert_level,
    _trigger_rachel_analysis,
    _build_test_alert,
    evaluate_alert,
    _is_regime_shift,
    build_alert_text,
)

# trend (spot) report
from .trend_report import (
    build_telegram_text,
    build_memory_block,
    generate_trend_report,
)

# box report
from .box_report import (
    build_bar_chart,
    build_health_line,
    get_box_position_label,
    build_box_telegram_text,
    build_box_memory_block,
    generate_box_report,
)

# cfd report
from .cfd_report import generate_cfd_report

__all__ = [
    # display
    "JST",
    "get_trend_icon",
    "get_rsi_state",
    "get_ema_state",
    "get_volatility_state",
    "get_market_summary",
    "get_position_summary",
    "get_entry_blockers",
    "get_entry_blockers_short",
    "get_wait_direction",
    # alerts
    "ALERT_COOLDOWN_SEC",
    "ALERT_COOLDOWN_EXTENDED_SEC",
    "_prev_raw_cache",
    "_last_alert_time",
    "_consecutive_same",
    "_last_alert_level",
    "_trigger_rachel_analysis",
    "_build_test_alert",
    "evaluate_alert",
    "_is_regime_shift",
    "build_alert_text",
    # trend
    "build_telegram_text",
    "build_memory_block",
    "generate_trend_report",
    # box
    "build_bar_chart",
    "build_health_line",
    "get_box_position_label",
    "build_box_telegram_text",
    "build_box_memory_block",
    "generate_box_report",
    # cfd
    "generate_cfd_report",
]
