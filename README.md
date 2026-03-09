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

See `docs/RUNBOOK.md` for setup and operations.
