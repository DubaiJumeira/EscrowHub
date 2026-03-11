# Merge Notes

This package merges the working hardened base with selected fixes from `EscrowHub-v4-fixed.zip`.

Merged improvements:
- Atomic deposit crediting to prevent duplicate ledger credits under watcher races.
- Async price lookups in bot deposit/deal flows to avoid blocking the event loop.
- Safer mutual escrow cancellation flow using a dedicated `cancel_escrow_mutual()` path.
- Better dispute context scoping and recovery to avoid stale deal/dispute state leaks.
- Stronger rate-limit table pruning and seller response rate limiting.
- Cleaner self-hosted fee UI text aligned with v1 economics (1% deposit/withdrawal, 3% escrow, no provider fee).
- Bot handler fix for `/create_escrow` argument parsing.

Important retained fixes from the hardened base:
- Large Telegram IDs map safely into valid BIP32 account/index components.
- `local_hd` is allowed consistently in production when explicitly configured.
- BTC/LTC withdrawal address validation uses checksum-aware validation.
- LTC watcher support and fee-tracking schema changes remain included.

Deliberate choice:
- The attempted pre-key withdrawal idempotency change from the fixed zip was not kept as-is because it conflicts with the DB immutability trigger for `withdrawals.idempotency_key`. The merged version keeps row-bound idempotency keys assigned in the same transaction.
