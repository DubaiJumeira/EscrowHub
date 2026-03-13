# EscrowHub v1 Runbook

## Required environment variables
- `TELEGRAM_BOT_TOKEN`
- `APP_ENV` (`dev` or `production`)
- `SQLITE_DB_PATH` or `DATABASE_URL` (SQLite runtime path; required in production)
- `ENCRYPTION_KEY` (required in production)
- `ETH_RPC_URL` (Ethereum RPC)

## Runtime controls / limits
- `BTC_WATCHER_ENABLED` (`true`/`false`)
- `ETH_WATCHER_ENABLED` (`true`/`false`)
- `WATCHER_POLL_INTERVAL_SECONDS` (default `30`)
- `ETH_MAX_BLOCKS_PER_RUN` (default `500`)
- `WITHDRAWALS_ENABLED` (`false` by default; keep disabled unless a full secure signer/broadcaster is deployed)

## Wallet and derivation variables
- `HD_WALLET_SEED_HEX` (required for self-hosted deterministic deposit issuance)
- `ADDRESS_PROVIDER` (`auto`, `local_hd`, `http`, or `disabled`)
- `ADDRESS_PROVIDER_URL` (required when `ADDRESS_PROVIDER=http`)
- `ADDRESS_PROVIDER_TOKEN` (required bearer token in production when `ADDRESS_PROVIDER=http`)

### Deposit address provider modes
Production deposit issuance supports two modes:

1. `ADDRESS_PROVIDER=http`
   - Health check: `GET /health` returns JSON with `ready` boolean (and optional `error`).
   - Idempotent issuance: `POST /addresses/get-or-create` with `{"user_id": int, "asset": "BTC|LTC|ETH|USDT"}`.
   - Response must include immutable `address` and `provider_ref`.

2. `ADDRESS_PROVIDER=local_hd` (or `ADDRESS_PROVIDER=auto` with `HD_WALLET_SEED_HEX` present)
   - Deterministically derives per-user deposit routes from the local seed.
   - Intended for a single self-hosted VPS only.
   - Simpler to operate, but less secure than an external HSM/address service because derivation happens in app runtime.

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
Runtime ledger support covers BTC, LTC, ETH, and USDT.

Deposit UI defaults to assets that have an active watcher path:
- BTC when `BTC_WATCHER_ENABLED=true`
- ETH when `ETH_WATCHER_ENABLED=true`
- USDT only when `ETH_WATCHER_ENABLED=true` **and** `USDT_ERC20_CONTRACT` is configured
- LTC stays hidden unless you explicitly opt in, because this repo does not include a dedicated LTC watcher entrypoint

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

Covered checks: DB connectivity, schema init, route integrity, deposit provider readiness, optional withdrawal provider readiness when withdrawals are enabled, startup-fatal conditions, required production env vars, and single-node SQLite posture. `--allow-degraded` changes exit behavior only and never relabels the reported state.

## Staging smoke harness (non-destructive)
Use this to verify route/provider handshakes without chain transactions:

```bash
python scripts/staging_smoke_check.py
```

This harness is fail-closed on uncertainty and only validates service handshakes.

## Supported production assets and posture
- Assets: **BTC, LTC, ETH, USDT only**.
- SOL runtime scaffolding exists, but **SOL must remain disabled and unadvertised** until the first-enable checklist below is completed.
- Current production target: **single-node SQLite only**.
- Deposit issuance may use `ADDRESS_PROVIDER=local_hd` or `ADDRESS_PROVIDER=http`.
- Withdrawals still require an external withdrawal provider and should remain disabled unless that provider exists.

## Future SOL enable checklist (do not turn on yet)
Prepare these variables in `/etc/escrowhub/escrowhub.env` before any future SOL go-live:
- `SOL_WATCHER_ENABLED=false` until the final enable step.
- `SOL_RPC_URL=https://...` pointing at the production RPC endpoint.
- `SOL_CONFIRMATIONS_REQUIRED=32` (or your approved finality target).
- `SOL_MAX_SLOTS_PER_RUN=64` (tune only if backfill lag requires it).
- `SOL_START_SLOT=<recent finalized slot>` for the first production enable when no `sol_watcher` cursor exists yet.

First-enable bootstrap notes:
- The current SOL watcher loads its initial cursor from `max(SOL_START_SLOT, stored sol_watcher cursor)`.
- If no cursor exists, leaving `SOL_START_SLOT=0` will backfill from genesis, which is not acceptable for production enablement.
- Seed `SOL_START_SLOT` to a recent finalized slot immediately before first enable, verify readiness, then enable the watcher.
- After the watcher writes its first `sol_watcher` cursor, keep `SOL_START_SLOT` as a floor or remove it once operator policy is documented.

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


## No-go / rollback rule
- If `scripts/release_readiness_check.py` reports `BLOCKED`, launch is a no-go.
- Remediate blocked reasons, rerun readiness, and only proceed once status is `READY` (or explicitly approved `DEGRADED`).
- If a deployment is already live and readiness transitions to `BLOCKED`, roll back to the last known-good release and keep withdrawals/deposits gated until checks pass.


## `/watcher_status` normalization details
`/watcher_status` is admin-only and intentionally minimal. It normalizes BTC watcher, ETH watcher, signer, and deposit provider to:
- `ready` = fully healthy
- `degraded` = tolerated non-fatal issue
- `blocked` = fatal startup/preflight/config no-go
- `disabled` = intentionally off by config

Deposit provider classification:
- `disabled` when no deposit provider mode is configured
- `blocked` when production-required deposit provider config is missing/invalid (for example missing seed, missing URL/token, non-HTTPS in production, missing encryption key)
- `degraded` for tolerated partial provider unhealthy conditions
- `ready` only when provider readiness and issuance readiness are both healthy
