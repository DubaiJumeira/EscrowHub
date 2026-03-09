# EscrowHub v1 Runbook

## Required environment variables
- `TELEGRAM_BOT_TOKEN`
- `APP_ENV` (`dev` or `production`)
- `SQLITE_DB_PATH` (SQLite runtime path; required in production)
- `ETH_RPC_URL` (Alchemy/Infura RPC)

## Runtime controls / limits
- `BTC_WATCHER_ENABLED` (`true`/`false`)
- `ETH_WATCHER_ENABLED` (`true`/`false`)
- `WATCHER_POLL_INTERVAL_SECONDS` (default `30`)
- `ETH_MAX_BLOCKS_PER_RUN` (default `500`)
- `WITHDRAWALS_ENABLED` (`false` by default; keep disabled unless a full secure signer/broadcaster is deployed)

## Wallet and derivation variables
- `HD_WALLET_SEED_HEX` (seed derivation in non-production/dev flows only)
- `ADDRESS_PROVIDER` (`http` or `disabled`)
- `ADDRESS_PROVIDER_URL` (required when `ADDRESS_PROVIDER=http`)
- `ADDRESS_PROVIDER_TOKEN` (optional bearer token)

### Deposit address provider contract
Production deposit issuance is externalized and fail-closed.
- Health check: `GET /health` returns JSON with `ready` boolean (and optional `error`).
- Idempotent issuance: `POST /addresses/get-or-create` with `{"user_id": int, "asset": "BTC|LTC|ETH|USDT"}`.
- Response must include immutable `address` and `provider_ref`.

Only bot startup evaluates deposit issuance readiness. If unavailable, bot runs in degraded mode and deposit issuance entrypoints fail closed with a controlled user message. Watchers and signer still start after DB checks.

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

Use bot admin command `/watcher_status` to read BTC/ETH watcher health, signer loop health, bot deposit-issuance readiness, and signer_retry backlog totals.

## Active assets
Only BTC, LTC, ETH, and USDT are supported in active runtime.

## Signer provider behavior
`SignerService` is fail-closed by default. Pending withdrawals are skipped unless `WITHDRAWALS_ENABLED=true`, and no fake txids or partial broadcaster behavior is allowed.

## Seed backup policy (critical)
- Never commit or store `HD_WALLET_SEED_HEX` in code or DB.
- Store seed backup in offline encrypted secret manager/HSM process.
- Rotating/changing `HD_WALLET_SEED_HEX` changes all derived addresses.


## Legacy entrypoint quarantine
`apps/bot_main/main.py` is blocked in production and requires explicit non-production opt-in (`ALLOW_LEGACY_BOT_MAIN=true`). Use `run_bot.py` in production.

## Withdrawal failure semantics
Withdrawals remain disabled by default. If enabled for controlled testing, ambiguous signer/provider failures are moved to `signer_retry` state (funds stay reserved) and are not auto-released until operator reconciliation.
