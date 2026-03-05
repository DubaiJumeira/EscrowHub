# Telegram Crypto Escrow Platform (Multi-tenant Starter)

Production-oriented refactor for a Telegram escrow platform with shared backend engine.

## What is implemented now

- Multi-layer architecture (`apps`, `core`, `infra`, `config`, `docs`).
- Base escrow fee policy: **3%**.
- Tenant/bot service fee with revenue split:
  - **30% platform owner**
  - **70% tenant bot owner**
- Minimum escrow validation: **$40 USD equivalent** through `PriceService` abstraction.
- Ledger-style wallet service with available/locked balances and immutable-style entries.
- Escrow engine scaffolding for create/release/dispute flows.
- Migration-ready SQL schema for users, bots, escrows, ledger, disputes, withdrawals, admin actions.

## Required menu layout

`/start` menu in `apps/bot_main/handlers.py`:

- Row 1: `Profile`, `Escrow Menu`
- Row 2: `Check User`
- Row 3: `Support Team`

## Supported assets (design-level)

BTC, ETH, LTC, USDT, USDC, SOL, XRP.

Chain integration is abstracted via adapters in `infra/chain_adapters/` with a mock implementation for development.

## Project structure

```text
/apps/bot_main
/apps/bot_tenant_router
/core/escrow_engine
/core/wallet_engine
/core/fees
/core/reputation
/core/pricing
/infra/db
/infra/chain_adapters
/config
/docs
/tests
```

## Run tests

```bash
python -m pytest -q
```

## Run the Telegram app entrypoint

```bash
export TELEGRAM_BOT_TOKEN="<your-token>"
python bot.py
```

## Notes

- `bot.py` and `escrow_service.py` are preserved as compatibility entrypoints while refactoring to modular services.
- Do not hardcode secrets; use environment variables and secret management.
- See `docs/ARCHITECTURE.md` for architecture details, pseudocode, schema summary, and staged rollout plan.
