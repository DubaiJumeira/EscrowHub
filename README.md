# EscrowHub

Refactored to support a custody **user balance wallet** model.

## Final fee rules
- Platform fee: 3% mandatory
- Bot extra fee: 0% to 3% per tenant
- Seller pays all fees on escrow completion
- Platform gets exactly 3%; bot owner gets exactly bot extra fee

## Core modules
- `escrow_service.py` (business logic)
- `bot.py` (Telegram handlers)
- `wallet_service.py` (ledger, locks, deposits, withdrawals, payout address sweeps)
- `fee_service.py`
- `price_service.py`
- `tenant_service.py`

## Watchers / Signer
- `watchers/eth_watcher.py` (implemented skeleton + idempotent credit path)
- `watchers/btc_watcher.py`, `watchers/sol_watcher.py`, `watchers/xrp_watcher.py` (placeholders)
- `signer/signer_service.py` (isolated signer boundary)

## Network labels
- USDT (ERC-20)
- USDC (ERC-20)

## Tests
```bash
python -m pytest -q
```
