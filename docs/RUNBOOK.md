# EscrowHub v1 Runbook

## Required environment variables
- `TELEGRAM_BOT_TOKEN`
- `APP_ENV` (`dev` or `production`)
- `SQLITE_DB_PATH` (or external DB wiring)
- `ETH_RPC_URL` (Alchemy/Infura RPC)

## Runtime controls / limits
- `BTC_WATCHER_ENABLED` (`true`/`false`)
- `ETH_WATCHER_ENABLED` (`true`/`false`)
- `WATCHER_POLL_INTERVAL_SECONDS` (default `30`)
- `ETH_MAX_BLOCKS_PER_RUN` (default `500`)
- `WITHDRAWALS_ENABLED` (`false` by default; keep disabled unless a full secure signer/broadcaster is deployed)

## Wallet and derivation variables
- `HD_WALLET_SEED_HEX` (seed derivation in non-production/dev flows)
- `BTC_XPUB`
- `LTC_XPUB`
- `ETH_XPUB`

### xpub safety note
With the current path contract (`m/.../{user_id}'/...`), xpubs are **not derivation-compatible** because hardened user nodes cannot be derived from public keys. Startup preflight fails closed when xpub mode is configured.
Production bot/watcher startup also fails closed when new deposit issuance cannot be satisfied by an approved external derivation/address service.

Secure alternatives:
- external address service / HSM-backed derivation service, or
- explicit migration to a non-hardened xpub-compatible path scheme.

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

## Active assets
Only BTC, LTC, ETH, and USDT are supported in active runtime.

## Signer provider behavior
`SignerService` is fail-closed by default. Pending withdrawals are skipped unless `WITHDRAWALS_ENABLED=true`, and no fake txids or partial broadcaster behavior is allowed.

## Seed backup policy (critical)
- Never commit or store `HD_WALLET_SEED_HEX` in code or DB.
- Store seed backup in offline encrypted secret manager/HSM process.
- Rotating/changing `HD_WALLET_SEED_HEX` changes all derived addresses.
