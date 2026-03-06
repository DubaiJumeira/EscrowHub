# EscrowHub (v1)

Production-oriented Telegram escrow backend with:
- DB-persisted custody wallet balances (ledger-based)
- Multi-tenant escrow fee model (platform 3% + bot extra 0-3%)
- Blockchain watchers + idempotent deposit credits
- Isolated signer boundary for withdrawals
- Dispute flow with admin resolution + audit

See `docs/RUNBOOK.md` for setup and operations.
