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

## HD derivation paths
- BTC: `m/84'/0'/{user_id}'/0/0` (BIP84 mainnet)
- LTC: `m/84'/2'/{user_id}'/0/0` (BIP84 coin type 2)
- ETH / USDT(ERC-20) / USDC(ERC-20): `m/44'/60'/{user_id}'/0/0`
- XRP: shared hot wallet + destination tag = `user_id`
- SOL: `m/44'/501'/{user_id}'/0'` (flagged for audited production hardening)

## Security note
Changing `HD_WALLET_SEED_HEX` changes all derived addresses. Only address/path metadata is stored in DB; private keys remain offline and are never persisted.

See `docs/RUNBOOK.md` for setup and operations.
