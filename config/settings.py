from __future__ import annotations

import logging
import os


LOGGER = logging.getLogger(__name__)


def _as_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    environment = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "dev")).strip() or "dev"
    is_production = environment.lower() == "production"
    sqlite_db_path = os.getenv("SQLITE_DB_PATH", os.getenv("DATABASE_URL", "")).strip()
    encryption_key = os.getenv("ENCRYPTION_KEY", "")
    telegram_main_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    btc_watcher_enabled = os.getenv("BTC_WATCHER_ENABLED", "true")
    eth_watcher_enabled = os.getenv("ETH_WATCHER_ENABLED", "true")
    sol_watcher_enabled = os.getenv("SOL_WATCHER_ENABLED", "false")
    watcher_poll_interval_seconds = int(os.getenv("WATCHER_POLL_INTERVAL_SECONDS", "30"))

    bot_id = int(os.getenv("ESCROWHUB_BOT_ID", os.getenv("BOT_ID", "1")))

    moderator_ids = {
        int(v.strip())
        for v in os.getenv("MODERATOR_TELEGRAM_IDS", os.getenv("MODERATOR_IDS", "")).split(",")
        if v.strip().isdigit()
    }

    supported_assets = ("BTC", "LTC", "ETH", "USDT", "SOL")
    deposit_platform_fee_percent = os.getenv("DEPOSIT_PLATFORM_FEE_PERCENT", "1").strip()
    withdrawal_platform_fee_percent = os.getenv("WITHDRAWAL_PLATFORM_FEE_PERCENT", "1").strip()
    withdrawal_network_fee_btc = os.getenv("WITHDRAWAL_NETWORK_FEE_BTC", "0.00002000").strip()
    withdrawal_network_fee_ltc = os.getenv("WITHDRAWAL_NETWORK_FEE_LTC", "0.00010000").strip()
    withdrawal_network_fee_eth = os.getenv("WITHDRAWAL_NETWORK_FEE_ETH", "0.00050000").strip()
    withdrawal_network_fee_usdt = os.getenv("WITHDRAWAL_NETWORK_FEE_USDT", "3.000000").strip()
    withdrawal_network_fee_sol = os.getenv("WITHDRAWAL_NETWORK_FEE_SOL", "0.000005000").strip()
    withdrawal_minimum_usd = os.getenv("WITHDRAWAL_MINIMUM_USD", "10").strip()
    escrow_platform_fee_percent = os.getenv("ESCROW_PLATFORM_FEE_PERCENT", "3").strip()
    provider_fee_enabled = _as_bool("PROVIDER_FEE_ENABLED", "false")
    withdrawal_daily_limit_usd = os.getenv("WITHDRAWAL_DAILY_LIMIT_USD", "5000").strip()
    withdrawal_min_interval_seconds = int(os.getenv("WITHDRAWAL_MIN_INTERVAL_SECONDS", "30"))
    allow_dev_bot_bootstrap = _as_bool("ALLOW_DEV_BOT_BOOTSTRAP", "false")
    withdrawals_enabled = _as_bool("WITHDRAWALS_ENABLED", "false")
    eth_max_blocks_per_run = int(os.getenv("ETH_MAX_BLOCKS_PER_RUN", "500"))
    encryption_kdf_iterations = int(os.getenv("ENCRYPTION_KDF_ITERATIONS", "600000"))
    support_handle = os.getenv("SUPPORT_HANDLE", "").strip()
    btc_xpub = os.getenv("BTC_XPUB", "").strip()
    ltc_xpub = os.getenv("LTC_XPUB", "").strip()
    eth_xpub = os.getenv("ETH_XPUB", "").strip()


if os.getenv("BOT_ID") and not os.getenv("ESCROWHUB_BOT_ID"):
    LOGGER.warning("BOT_ID is deprecated; use ESCROWHUB_BOT_ID")
if Settings.is_production and not Settings.sqlite_db_path:
    raise RuntimeError("SQLITE_DB_PATH or DATABASE_URL is required in production")
if not Settings.moderator_ids:
    LOGGER.warning("No moderator IDs configured; dispute moderation controls are disabled")

if Settings.support_handle and not Settings.support_handle.startswith("@"):
    LOGGER.warning("SUPPORT_HANDLE should start with @; normalizing at runtime")

if Settings.moderator_ids and not os.getenv("ADMIN_USER_IDS", "").strip():
    LOGGER.warning("ADMIN_USER_IDS is empty while moderators are configured; admin and moderator duties are intentionally distinct")
