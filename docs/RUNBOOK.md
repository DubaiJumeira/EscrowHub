# EscrowHub v1 Runbook

## Required environment variables
- `TELEGRAM_BOT_TOKEN`
- `ADMIN_USER_IDS` (comma-separated Telegram IDs)
- `APP_ENV` (`dev` or `production`)
- `SQLITE_DB_PATH` (or external DB wiring)
- `HD_WALLET_SEED_HEX` (**required for HD derivation/signing**)
- `XRP_HOT_WALLET_ADDRESS` (**required in production for XRP deposits**)
- `ETH_RPC_URL` (Alchemy/Infura RPC)
- `BLOCKSTREAM_BASE_URL` (optional override)
- `BTC_CONFIRMATIONS` / `LTC_CONFIRMATIONS` (default 3)
- `ETH_CONFIRMATIONS` (default 12)
- `SOL_RPC_URL`
- `XRP_RPC_URL`
- `SIGNER_PROVIDER` (`hd`, `mock`, or `vault`)
- `VAULT_ADDR`, `VAULT_TOKEN`, `VAULT_SIGN_PATH` (when `SIGNER_PROVIDER=vault`)
- `BTC_WATCHER_ENABLED` (`true`/`false`)
- `ETH_WATCHER_ENABLED` (`true`/`false`)
- `WATCHER_POLL_INTERVAL_SECONDS` (default `30`)

## Service entrypoints (separate processes)
- `python run_bot.py`
- `python run_btc_watcher.py`
- `python run_eth_watcher.py`
- `python run_signer.py`

These run as independent long-running loops. Watchers and signer are **not** started inside `bot.py`.

## Watcher status persistence
`watcher_status` table tracks per-cycle health:
- watcher_name
- last_run_at
- last_success_at
- last_error
- consecutive_failures
- updated_at

Use bot admin command `/watcher_status` to read BTC/ETH watcher health.

## Derivation paths
- BTC: `m/84'/0'/{user_id}'/0/0`
- LTC: `m/84'/2'/{user_id}'/0/0`
- ETH/USDT/USDC: `m/44'/60'/{user_id}'/0/0`
- XRP: shared hot wallet + destination tag = user_id
- SOL: `m/44'/501'/{user_id}'/0'` (TODO for full audited production support)

Only address metadata (`address`, `derivation_index`, `derivation_path`, `destination_tag`) is stored in DB. No private keys are stored.

## Seed backup policy (critical)
- Never commit or store `HD_WALLET_SEED_HEX` in code or DB.
- Store seed backup in offline encrypted secret manager/HSM process.
- Rotating/changing `HD_WALLET_SEED_HEX` changes all derived addresses.

## systemd deployment examples
Service unit examples are provided in `deploy/systemd/`:
- `escrowhub-bot.service`
- `escrowhub-btc-watcher.service`
- `escrowhub-eth-watcher.service`
- `escrowhub-signer.service`
