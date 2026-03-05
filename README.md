# Telegram Crypto Escrow Platform

Updated escrow architecture with final fee rules and multi-tenant bot context.

## Final fee rules

- Platform mandatory base fee: **3%** (seller pays)
- Tenant bot optional extra fee: **0% to 3%**
- Total fee = platform fee + bot extra fee
- Distribution on release:
  - platform gets full base 3%
  - bot owner gets full extra fee
  - seller receives `amount - total_fee`

## Core modules

- `escrow_service.py` - escrow lifecycle/business logic
- `fee_service.py` - fee calculations and payout breakdown
- `wallet_service.py` - deposit addresses, internal ledger, locked balances, withdrawals via signer boundary
- `price_service.py` - price abstraction + $40 minimum escrow validation
- `tenant_service.py` - bot tenant config + fee settings
- `bot.py` - Telegram handlers and menu flows

## Supported assets

BTC, ETH, LTC, USDT, USDC, SOL, XRP

## Database

Schema is in `infra/db/schema.sql` and includes:
- bots tenant config
- escrows
- wallet addresses (+ XRP destination tags)
- immutable ledger entries
- deposits and withdrawals

## Run tests

```bash
python -m pytest -q
```
