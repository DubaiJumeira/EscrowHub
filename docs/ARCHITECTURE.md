# Architecture Update: Final Fee & Wallet Rules

## Fee policy

- `PLATFORM_FEE_PERCENT = 3%` mandatory on every escrow
- `BOT_EXTRA_FEE_PERCENT` per tenant in range `[0, 3]`
- Seller pays fees on release
- Payout formula:
  - `platform_fee = amount * 3%`
  - `bot_extra_fee = amount * bot_extra_fee_percent`
  - `seller_payout = amount - (platform_fee + bot_extra_fee)`

No 30/70 split is used anymore.

## Multi-tenant

Each escrow is bound to `bot_id`. Tenant config includes:
- `owner_user_id`
- `bot_extra_fee_percent`
- `bot_display_name`
- `support_contact`

## Wallet architecture (self-custody option B)

- User deposit addresses per active asset (BTC/LTC/ETH/USDT)
- Chain watchers (integration boundary) detect deposits and then call `credit_deposit`
- Internal immutable ledger entries track credits/debits/locks/releases
- Withdrawals call `SignerService` boundary so private keys are outside Telegram process

### Deposit issuance provider
Production deposit addresses are issued by a dedicated external `AddressProvider` boundary (separate from withdrawal signing). Production requires HTTPS provider URLs and non-empty provider bearer tokens.
Wallet rows persist immutable provider metadata (`provider_origin`, `provider_ref`) for audit and migration safety.

Non-production can still use legacy seed derivation for existing tests/dev flows. Production never derives new deposit addresses from local seed paths.

## Key modules

- `escrow_service.py`
- `fee_service.py`
- `wallet_service.py`
- `price_service.py`
- `tenant_service.py`
- `bot.py`


## Runtime startup/readiness behavior
- Database runtime is SQLite (`SQLITE_DB_PATH`) for all services.
- `run_startup_preflight` is service-scoped: watchers/signer require DB init + derivation consistency only.
- Bot checks deposit issuance readiness; if unavailable, bot remains online in degraded mode and deposit issuance handlers fail closed.

## Legacy entrypoint
`apps/bot_main/main.py` is quarantined and not production-capable. Production should run `run_bot.py`.

## Withdrawals
`WITHDRAWALS_ENABLED` remains fail-closed by default. Ambiguous signer/provider errors move withdrawals into `signer_retry` and do not release reserved balances automatically.


### Readiness and degraded modes
- Bot preflight validates DB safety + route integrity + deposit-provider readiness.
- Watcher preflight validates DB safety + route integrity; no issuance dependency.
- Signer preflight validates DB safety + typed withdrawal-provider readiness.

### Withdrawal state machine safety
Internal statuses: `pending -> submitted -> broadcasted -> confirmed` with fail paths `failed|signer_retry`.
- `failed` is only used for deterministic pre-broadcast failures.
- `signer_retry` is used for ambiguous/retryable/unknown outcomes and **never** auto-releases balances.
- Reconciliation loop re-queries external provider for `submitted|broadcasted|signer_retry`.
- Daily withdrawal limit accounting includes unresolved `pending|submitted|broadcasted|signer_retry` requests.


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


## Security controls (latest)
- Active-cancel handshake uses DB-backed `active_cancel_requests` state; callback payload identity is not trusted.
- Non-buyer release attempts are blocked in handler and service layer (defense in depth).
- Deposit routing is fail-closed below enforced minimum ($40.00) before route issuance.
- Dispute reasons are bounded (1000 chars) before persistence path.
- Dispute fanout targets all moderators with isolated send attempts and sanitized error logging.
- Runtime support contact comes from `SUPPORT_HANDLE`; unset values return safe fallback text.
- `/recover` restores only recoverable open deal context from DB after restart; unsafe reconstruction is rejected.

## Release-readiness architecture
EscrowHub now includes a dedicated fail-closed readiness assessor (`readiness_service.py`) and operator entrypoints:
- `scripts/release_readiness_check.py`
- `scripts/staging_smoke_check.py`

The readiness assessor reuses startup preflight (`run_startup_preflight`) and provider readiness (`WalletService.address_provider.is_ready`, `SignerService.readiness`) rather than duplicating unsafe logic.

### Readiness semantics
- `READY`: all critical checks healthy.
- `DEGRADED`: launch-critical checks passed but tolerated degradations/warnings remain.
- `BLOCKED`: launch must stop (non-zero exit). `--allow-degraded` only changes exit behavior for DEGRADED and never rewrites reported state.

### Startup-fatal behavior
Route-integrity and signer configuration failures remain startup-fatal and fail closed. Operator-facing status reasons are sanitized to avoid secret/token/payload leakage.

### `/watcher_status` surface
`/watcher_status` remains admin-only and minimal, reporting normalized states:
- `ready`
- `degraded`
- `blocked`
- `disabled`

No stack traces, secrets, or raw provider payloads are exposed.

Deposit provider state normalization uses shared helper logic and truthfully distinguishes `ready|degraded|blocked|disabled` (including config-disabled and startup-fatal blocked conditions).


### Watcher disabled-state hardening
BTC/ETH watcher entrypoints persist `health_state=disabled` before exiting when disabled by config.
`/watcher_status` also derives disabled directly from watcher-enable config flags as defense in depth, so stale persisted `ok` rows cannot surface false `ready`.
