# EscrowHub (v1)

EscrowHub is a Telegram multi-tenant crypto escrow platform with:
- Ledger-based custody accounting
- Escrow lock/release flows with seller-paid fees (3% platform + 0-3% bot extra)
- Background chain watchers with scan cursor state
- Withdrawal reserve pipeline and isolated signer process (Vault transit capable)
- Dispute management with admin resolution and audit events
- Cold-wallet sweep job for platform revenue

See `docs/RUNBOOK.md` for operational setup.
