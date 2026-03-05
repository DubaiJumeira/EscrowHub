# Telegram Multi-tenant Crypto Escrow Platform Architecture

## High-level architecture (production direction)

This codebase is refactored toward a multi-tenant architecture where many Telegram bots share one backend escrow and wallet engine.

### Components

1. **Telegram Layer**
   - `apps/bot_main`: main platform bot handlers and command/menu flows.
   - `apps/bot_tenant_router`: resolves incoming bot/update context to tenant configuration (`bots` table).

2. **Core Domain Services**
   - `core/escrow_engine`: escrow lifecycle and state transitions (`pending`, `active`, `completed`, `cancelled`, `disputed`).
   - `core/wallet_engine`: internal ledger-oriented balances (`available`, `locked`) and transaction mutations.
   - `core/fees`: fee policy with fixed base 3% + tenant service fee split.
   - `core/pricing`: price abstraction for minimum USD enforcement ($40 floor).
   - `core/reputation`: user reliability stats.

3. **Infrastructure Layer**
   - `infra/db`: schema/models and persistence boundaries.
   - `infra/chain_adapters`: chain abstraction per asset family (BTC/ETH/LTC/SOL/XRP + ERC-20).
   - Optional queue + workers for deposit monitoring and withdrawal broadcasting.

### Request flow (create escrow)

1. User invokes create escrow command from tenant bot.
2. TenantRouter resolves tenant config (`bot_service_fee_percent`, owner, status).
3. EscrowService validates:
   - supported asset
   - amount > 0
   - USD value >= $40 via `PriceService`
   - buyer balance sufficiency via `WalletService`
4. WalletService locks buyer funds.
5. Escrow record + participants + initial event persisted.
6. On release, locked funds are transformed into seller payout + fee revenues.

### Multi-tenant model

- Each Telegram bot is a tenant record in `bots`.
- Tenant-specific service fee is configured by bot owner.
- Shared platform rules apply globally:
  - base escrow fee 3%
  - service-fee split 30% platform / 70% bot owner

---

## Folder structure

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

---

## ERD description and schema

Implemented SQL schema: `infra/db/schema.sql`.

### Main entities

- `users`: platform identity and moderation flags.
- `bots`: tenant bot config and ownership.
- `wallet_addresses`: per-user per-asset deposit routing.
- `balances`: denormalized available/locked balances.
- `ledger_entries`: immutable accounting records + idempotency keys.
- `escrows`: escrow contracts with fees and statuses.
- `escrow_participants`: roles in each escrow.
- `escrow_events`: state transition audit trail.
- `disputes`: dispute lifecycle and outcomes.
- `withdrawals`: payout lifecycle.
- `admin_actions`: moderation and operations audit.
- `reputation_stats`: denormalized trust metrics.

---

## Core services pseudocode

### `EscrowService.create_escrow()`

```python
def create_escrow(tenant_bot_id, buyer_id, seller_id, amount, asset, description, bot_service_fee_percent):
    validate_minimum_usd(price_service, asset, amount, minimum_usd=40)
    fee = fee_service.calculate_fee(amount, bot_service_fee_percent)
    wallet_service.lock_funds(buyer_id, asset, amount, escrow_id)
    escrow = persist_escrow(status="active", fee_breakdown=fee, ...)
    persist_event(escrow.id, from_status="pending", to_status="active")
    return escrow
```

### `EscrowService.release()`

```python
def release(escrow_id):
    escrow = get_escrow(escrow_id)
    assert escrow.status == "active"
    unlock_and_debit_buyer_locked(escrow.amount)
    credit_seller(escrow.fee_breakdown.seller_payout)
    credit_platform_revenue(escrow.fee_breakdown.platform_revenue)
    credit_owner_revenue(escrow.fee_breakdown.owner_revenue)
    mark_escrow_completed()
```

### `EscrowService.dispute()`

```python
def dispute(escrow_id, reason):
    escrow = get_escrow(escrow_id)
    assert escrow.status == "active"
    create_dispute(escrow_id, reason)
    mark_escrow_disputed()
```

### `FeeService.calculate_fee()`

```python
def calculate_fee(amount, bot_service_fee_percent):
    escrow_fee = amount * 3%
    bot_service_fee = amount * bot_service_fee_percent
    platform_revenue, owner_revenue = split_revenue(bot_service_fee, 30, 70)
    total_fees = escrow_fee + bot_service_fee
    seller_payout = amount - total_fees
    return FeeBreakdown(...)
```

### `FeeService.split_revenue(platform_share=30%, owner_share=70%)`

```python
def split_revenue(service_fee_revenue):
    platform = service_fee_revenue * 30%
    owner = service_fee_revenue - platform
    return platform, owner
```

### `WalletService.credit_deposit()`

```python
def credit_deposit(user_id, asset, amount, tx_ref, idempotency_key):
    assert idempotency_key is unique
    balances[user_id, asset].available += amount
    append_ledger_entry(direction="credit", entry_type="deposit", ...)
```

### `WalletService.lock_funds()`

```python
def lock_funds(user_id, asset, amount, escrow_id, idempotency_key):
    require available >= amount
    available -= amount
    locked += amount
    append_ledger_entry(entry_type="lock", ...)
```

### `WalletService.debit_withdrawal()`

```python
def debit_withdrawal(user_id, asset, amount, withdrawal_id, idempotency_key):
    require available >= amount
    available -= amount
    append_ledger_entry(entry_type="withdrawal", ...)
```

---

## Telegram handlers and flows

Implemented command/menu scaffolding in `apps/bot_main/handlers.py`:

- `/start` with required menu layout:
  - Row1 `[Profile] [Escrow Menu]`
  - Row2 `[Check User]`
  - Row3 `[Support Team]`
- `/profile`: user id + reputation + balance guidance.
- `/create_escrow`: create + lock escrow flow.
- `/check_user`: trade/dispute/reputation summary.
- `/support`: support contact + ticket hint.

---

## Security improvement checklist

- [ ] Bot token handling: store encrypted token and hash in DB; avoid plaintext logs.
- [ ] Encrypt secrets at rest using KMS/HSM or env key management.
- [ ] Rate limit by user + chat + command (anti-spam/abuse).
- [ ] Idempotency keys for deposits, withdrawals, webhook handlers.
- [ ] Replay protection for webhook/update processing.
- [ ] Strict state transition checks for escrow/dispute workflows.
- [ ] Asset/address validation per chain.
- [ ] Audit logging for all admin and funds-affecting actions.
- [ ] Alerting on unusual velocity or balance drift.
- [ ] Segregated hot/cold wallet design for production.

---

## Staged refactor plan

1. **Stage 1 (done in this commit)**
   - Introduce layered architecture modules.
   - Introduce fee, pricing, escrow and wallet service boundaries.
   - Add migration-ready schema and architecture doc.
   - Keep legacy `bot.py` and `escrow_service.py` as compatibility wrappers.

2. **Stage 2**
   - Replace in-memory stores with repositories + PostgreSQL.
   - Add async workers for chain deposit indexing and withdrawal broadcasting.

3. **Stage 3**
   - Add admin panel/API for disputes, account freeze, revenue analytics.
   - Harden observability, idempotency store, anti-fraud and policy checks.
