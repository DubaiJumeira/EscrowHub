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

- User deposit addresses per asset (`wallet_addresses`)
- XRP supports destination tag model
- Chain watchers (integration boundary) detect deposits and then call `credit_deposit`
- Internal immutable ledger entries track credits/debits/locks/releases
- Withdrawals call `SignerService` boundary so private keys are outside Telegram process

## Key modules

- `escrow_service.py`
- `fee_service.py`
- `wallet_service.py`
- `price_service.py`
- `tenant_service.py`
- `bot.py`
