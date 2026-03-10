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
- `ADDRESS_PROVIDER_TOKEN` (required bearer token in production)

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

Use bot admin command `/watcher_status` to read minimal normalized BTC/ETH watcher, signer, and deposit-provider states (`ready|degraded|blocked|disabled`) with sanitized detail only.

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


### Withdrawal provider contract
- `WITHDRAWAL_PROVIDER=http` is production-supported mode.
- Production requires `WITHDRAWAL_PROVIDER_URL` (HTTPS) and `WITHDRAWAL_PROVIDER_TOKEN` (non-empty).
- Requests are submitted with deterministic idempotency keys and asset/address/amount/user binding.
- Reconciliation API is required (`/v1/withdrawals/reconcile`) using `provider_ref` and/or `idempotency_key`.
- Provider responses map to internal states: `pending|submitted|broadcasted|confirmed|failed|signer_retry` (fail-closed on malformed/unknown outcomes).

### Unresolved withdrawal operator workflow
- `/watcher_status` for minimal provider/watcher/signer health (`ready|degraded|blocked|disabled`) only.
- `/signer_retry_list` for signer_retry backlog summary.
- `/unresolved_withdrawals` for pending/submitted/broadcasted/signer_retry list.
- `/withdrawal_reconcile <withdrawal_id> CONFIRM [FORCE]` for explicit single-withdrawal reconciliation trigger (targeted; unrelated unresolved rows are not processed).
- `/signer_retry_action <withdrawal_id> <requeue|fail> CONFIRM` for explicit state action.


- Startup preflight is fail-closed for route-integrity/collision/tampering failures across bot, watchers, and signer; these conditions abort startup.
- Withdrawal idempotency keys are bound per withdrawal row (`wdrow:<id>:...`), allowing legitimate repeated identical business-field withdrawals.
- Signer reconciliation uses status-aware backoff (`submitted` shorter, `broadcasted` moderate, `signer_retry` slower): first attempt is delayed from state timestamp (`submitted_at`/`broadcasted_at`/creation), and all subsequent retries are gated by `last_reconciled_at`.
- Withdrawal provider identity (`provider_origin`, `provider_ref`) is immutable once assigned and cannot be rebound across withdrawals.
- External withdrawal provider deployment is still required for true go-live; repo hardening does not replace real custody infrastructure.

## Production safety posture
- Fatal startup integrity/configuration errors fail closed and stop the process; `run_bot.py` must not restart-loop on these failures.
- Watcher/signer/operator status surfaces persist and display sanitized error summaries only (secrets/payloads redacted).
- Single-node SQLite is a supported production posture for this repo; multi-node shared-database topology is not the target architecture.
- External withdrawal/address providers remain the final go-live dependency and must be configured/operated securely.

- Signer startup/provider misconfiguration now raises typed fatal startup errors and persists signer watcher health as `fatal_startup_blocked` before hard exit.

- Signer loop state meanings: `fatal startup blocked`, `running: withdrawals disabled`, `running: provider not ready`, `running: healthy`.


## Conversation recovery and flow safety
- `/check_user` is globally available and clears conflicting in-memory flow state before starting lookup/create-deal flow.
- `/recover` restores latest open escrow context (`pending|active|disputed`) from DB and refuses closed states.
- Active cancellation requests are tracked server-side and response callbacks fail closed when stale, replayed, forged, or self-responded.

## Deposit/dispute policy alignment
- Deposit minimum is enforced at address issuance path: **$40.00 USD minimum**.
- Dispute reason input is limited to 1000 characters.
- Every configured moderator is notified on dispute submit; one failed moderator send does not block others.

## Operator configuration
- Set `SUPPORT_HANDLE` (e.g. `@escrow_support`) to expose support contact in bot UI.
- `ADMIN_USER_IDS` and `MODERATOR_TELEGRAM_IDS` are separate role surfaces; configure both intentionally.

## Release go/no-go readiness check
Run before every production start:

```bash
python scripts/release_readiness_check.py
python scripts/release_readiness_check.py --json
python scripts/release_readiness_check.py --allow-degraded
```

Readiness states:
- `READY`: all launch-critical checks passed.
- `DEGRADED`: launch-critical checks passed, but tolerated warnings/degradations exist (for example single-node SQLite posture warning).
- `BLOCKED`: fail-closed hard stop; command exits non-zero regardless of flags.

Covered checks: DB connectivity, schema init, route integrity, deposit provider readiness, withdrawal provider readiness, signer readiness, startup-fatal conditions, required production env vars, and single-node SQLite posture. `--allow-degraded` changes exit behavior only and never relabels the reported state.

## Staging smoke harness (non-destructive)
Use this to verify route/provider handshakes without chain transactions:

```bash
python scripts/staging_smoke_check.py
```

This harness is fail-closed on uncertainty and only validates service handshakes.

## Supported production assets and posture
- Assets: **BTC, LTC, ETH, USDT only**.
- Current production target: **single-node SQLite only**.
- External deposit and withdrawal providers are mandatory for production.

## Staged go-live procedure
1. Migrate on a DB copy.
2. Execute release readiness check.
3. Validate deposit issuance handshake.
4. Validate withdrawal provider/signer health.
5. Execute staging smoke checks.
6. Start services.

## No-go / rollback guidance
If readiness is `BLOCKED` or smoke checks fail:
- Do not launch.
- Roll back config/provider changes to last known-good values.
- Restore DB snapshot only if migration/data integrity is uncertain.
- Re-run readiness + smoke checks before any relaunch.
