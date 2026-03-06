from __future__ import annotations

import os


class Settings:
    environment = os.getenv("APP_ENV", "dev")
    sqlite_db_path = os.getenv("SQLITE_DB_PATH", "./escrowhub.db")
    telegram_main_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    admin_user_ids = os.getenv("ADMIN_USER_IDS", "")

    eth_rpc_url = os.getenv("ETH_RPC_URL", "")
    sol_rpc_url = os.getenv("SOL_RPC_URL", "")
    xrp_rpc_url = os.getenv("XRP_RPC_URL", "")

    signer_mode = os.getenv("SIGNER_MODE", "local")
    vault_addr = os.getenv("VAULT_ADDR", "")
