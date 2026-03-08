from __future__ import annotations

import logging
import os


LOGGER = logging.getLogger(__name__)


def _as_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    environment = os.getenv("APP_ENV", "dev")
    database_url = os.getenv("DATABASE_URL", "postgresql://escrow:escrow@localhost:5432/escrow")
    encryption_key = os.getenv("ENCRYPTION_KEY", "")
    telegram_main_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    btc_watcher_enabled = os.getenv("BTC_WATCHER_ENABLED", "true")
    eth_watcher_enabled = os.getenv("ETH_WATCHER_ENABLED", "true")
    watcher_poll_interval_seconds = int(os.getenv("WATCHER_POLL_INTERVAL_SECONDS", "30"))

    bot_id = int(os.getenv("ESCROWHUB_BOT_ID", os.getenv("BOT_ID", "1")))

    moderator_username = os.getenv("MODERATOR_USERNAME", os.getenv("ESCROWHUB_MODERATOR_USERNAME", "")).lstrip("@")
    # Backward compatibility alias; prefer `moderator_username`.
    MODERATOR_USERNAME = moderator_username


if os.getenv("BOT_ID") and not os.getenv("ESCROWHUB_BOT_ID"):
    LOGGER.warning("BOT_ID is deprecated; use ESCROWHUB_BOT_ID")
if os.getenv("ESCROWHUB_MODERATOR_USERNAME") and not os.getenv("MODERATOR_USERNAME"):
    LOGGER.warning("ESCROWHUB_MODERATOR_USERNAME is deprecated; use MODERATOR_USERNAME")
if not Settings.moderator_username:
    LOGGER.warning("MODERATOR_USERNAME is not set; cancellation guidance will use fallback text")
