from __future__ import annotations

import logging
import os


LOGGER = logging.getLogger(__name__)


def _as_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    environment = os.getenv("APP_ENV", "dev")
    is_production = environment.lower() == "production"
    sqlite_db_path = os.getenv("SQLITE_DB_PATH", "").strip()
    encryption_key = os.getenv("ENCRYPTION_KEY", "")
    telegram_main_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    btc_watcher_enabled = os.getenv("BTC_WATCHER_ENABLED", "true")
    eth_watcher_enabled = os.getenv("ETH_WATCHER_ENABLED", "true")
    watcher_poll_interval_seconds = int(os.getenv("WATCHER_POLL_INTERVAL_SECONDS", "30"))

    bot_id = int(os.getenv("ESCROWHUB_BOT_ID", os.getenv("BOT_ID", "1")))

    moderator_ids = {
        int(v.strip())
        for v in os.getenv("MODERATOR_TELEGRAM_IDS", "").split(",")
        if v.strip().isdigit()
    }

    supported_assets = ("BTC", "LTC", "ETH", "USDT")
    withdrawal_daily_limit_usd = os.getenv("WITHDRAWAL_DAILY_LIMIT_USD", "5000").strip()
    withdrawal_min_interval_seconds = int(os.getenv("WITHDRAWAL_MIN_INTERVAL_SECONDS", "30"))
    allow_dev_bot_bootstrap = _as_bool("ALLOW_DEV_BOT_BOOTSTRAP", "false")
    withdrawals_enabled = _as_bool("WITHDRAWALS_ENABLED", "false")
    eth_max_blocks_per_run = int(os.getenv("ETH_MAX_BLOCKS_PER_RUN", "500"))
    encryption_kdf_iterations = int(os.getenv("ENCRYPTION_KDF_ITERATIONS", "600000"))
    btc_xpub = os.getenv("BTC_XPUB", "").strip()
    ltc_xpub = os.getenv("LTC_XPUB", "").strip()
    eth_xpub = os.getenv("ETH_XPUB", "").strip()


if os.getenv("BOT_ID") and not os.getenv("ESCROWHUB_BOT_ID"):
    LOGGER.warning("BOT_ID is deprecated; use ESCROWHUB_BOT_ID")
if Settings.is_production and not Settings.sqlite_db_path:
    raise RuntimeError("SQLITE_DB_PATH is required in production")
if not Settings.moderator_ids:
    LOGGER.warning("No moderator IDs configured; dispute moderation controls are disabled")
