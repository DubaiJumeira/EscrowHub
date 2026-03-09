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
- Signer reconciliation uses status-aware backoff (`submitted` shorter, `broadcasted` moderate, `signer_retry` slower) gated by `last_reconciled_at`.
- Withdrawal provider identity (`provider_origin`, `provider_ref`) is immutable once assigned and cannot be rebound across withdrawals.
- External withdrawal provider deployment is still required for true go-live; repo hardening does not replace real custody infrastructure.