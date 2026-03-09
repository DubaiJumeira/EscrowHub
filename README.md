# EscrowHub (v1)

Production-oriented Telegram escrow backend with:
- DB-persisted custody wallet balances (ledger-based)
- Multi-tenant escrow fee model (platform 3% + bot extra 0-3%)
- Blockchain watchers + idempotent deposit credits
- Isolated signer boundary for withdrawals
- Dispute flow with admin resolution + audit

## Operational process model
Run each service separately:
- `run_bot.py`
- `run_btc_watcher.py`
- `run_eth_watcher.py`
- `run_signer.py`

Database mode: SQLite only. Configure `SQLITE_DB_PATH` for all environments (required in production).

`bot.py` remains Telegram-only and does not start watcher threads.

## HD derivation paths (active assets only)
- BTC: `m/84'/0'/{user_id}'/0/0` (BIP84 mainnet)
- LTC: `m/84'/2'/{user_id}'/0/0` (BIP84 coin type 2)
- ETH / USDT(ERC-20): `m/44'/60'/{user_id}'/0/0`

## Security note
Changing `HD_WALLET_SEED_HEX` changes all derived addresses. Only address/path metadata is stored in DB; private keys remain offline and are never persisted.

## xpub reality check (fail-closed)
Current stored derivation contract uses hardened user nodes (`m/.../{user_id}'/...`). Public extended keys cannot derive hardened child paths, so `BTC_XPUB`, `LTC_XPUB`, and `ETH_XPUB` are intentionally rejected at runtime in this architecture.

Secure alternatives:
- external address derivation service / HSM-backed derivation service, or
- planned explicit migration to an xpub-compatible non-hardened user index path.
- production startup preflight now also requires an approved external derivation/address service for new deposit issuance; otherwise startup fails closed.

Service preflight is role-aware: bot may run in degraded mode when new deposit issuance is unavailable; watchers and signer still run after DB init + derivation consistency checks. Deposit address entrypoints fail closed with a controlled message when issuance is unavailable.

`apps/bot_main/main.py` is a quarantined legacy entrypoint and is blocked in production; use `run_bot.py`.

Withdrawals remain disabled by default (`WITHDRAWALS_ENABLED=false`). When enabled, runtime uses external `WITHDRAWAL_PROVIDER=http` only.

See `docs/RUNBOOK.md` for setup and operations.


## Supported assets (strict)
Runtime, DB constraints, and tests support only: **BTC, LTC, ETH, USDT**.

## Withdrawal provider contract (production-safe)
Withdrawals use a typed signer boundary with deterministic idempotency keys (`wd:<id>:<hash>`), validated request payloads, and explicit result statuses:
- success-like: `submitted|broadcasted|confirmed` (`submitted` may omit txid; `broadcasted|confirmed` require txid)
- deterministic failure: `rejected|permanent_failure` (safe to release reservation)
- unresolved: `retryable|ambiguous|unknown` (forced to `signer_retry`, funds remain reserved)

Withdrawal rows persist: `provider_origin`, `provider_ref`, `idempotency_key`, `external_status`, `submitted_at`, `broadcasted_at`, `last_reconciled_at`, and optional sanitized `tx_metadata_json`. Unknown/malformed provider outcomes fail closed to `signer_retry`.


- Startup preflight is fail-closed for route-integrity/collision/tampering failures across bot, watchers, and signer; these conditions abort startup.
- Withdrawal idempotency keys are bound per withdrawal row (`wdrow:<id>:...`), allowing legitimate repeated identical business-field withdrawals.
- Signer reconciliation uses status-aware backoff (`submitted` shorter, `broadcasted` moderate, `signer_retry` slower) gated by `last_reconciled_at`.
- Withdrawal provider identity (`provider_origin`, `provider_ref`) is immutable once assigned and cannot be rebound across withdrawals.
- External withdrawal provider deployment is still required for true go-live; repo hardening does not replace real custody infrastructure.

## Production safety posture
- Fatal startup integrity/configuration errors fail closed and stop the process; `run_bot.py` must not restart-loop on these failures.
- Watcher/signer/operator status surfaces persist and display sanitized error summaries only (secrets/payloads redacted).
- Single-node SQLite is a supported production posture for this repo; multi-node shared-database topology is not the target architecture.
- External withdrawal/address providers remain the final go-live dependency and must be configured/operated securely.
