"""Backward-compat shim — canonical: core/shared/logging/telegram_handlers.py"""
from core.shared.logging.telegram_handlers import *  # noqa: F401,F403
from core.shared.logging.telegram_handlers import (  # noqa: F401
    setup_telegram_logging,
    shutdown_telegram_logging,
    TelegramDigestHandler,
    TelegramTransactionHandler,
    TelegramAlertHandler,
    JUDGE_PREFIXES,
    PUNISHER_PREFIXES,
    _get_domain,
    _send_telegram,
    _handlers,
)
