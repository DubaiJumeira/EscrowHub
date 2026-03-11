# Deposit-ready VPS notes

This package was adjusted for **single-VPS self-hosted deposits**.

## What changed
- Production deposit issuance can now use `ADDRESS_PROVIDER=local_hd`.
- `ADDRESS_PROVIDER=auto` will also use local HD issuance when `HD_WALLET_SEED_HEX` is present.
- `DATABASE_URL` is accepted as an alias for `SQLITE_DB_PATH`.
- `BTC_RPC_URL` is accepted as an alias for the Blockstream BTC watcher endpoint.
- Deposit UI only shows assets that have a watcher path configured.
  - BTC shown when `BTC_WATCHER_ENABLED=true`
  - ETH shown when `ETH_WATCHER_ENABLED=true`
  - USDT shown only when `USDT_ERC20_CONTRACT` is configured
  - LTC hidden by default in this repo
- Readiness checks no longer block on withdrawal provider config when `WITHDRAWALS_ENABLED=false`.

## Add these env vars to your file before launch
- `APP_ENV=production`
- `ENCRYPTION_KEY=<long random secret>`
- `ADDRESS_PROVIDER=local_hd`
- Keep `WITHDRAWALS_ENABLED=false` unless you have a real external withdrawal provider

## Important
- This makes the app **deposit-ready**, not withdrawal-ready.
- Because derivation happens inside the app process in `local_hd` mode, protect the VPS carefully and keep your seed backed up offline.


## v4 fee + multi-asset policy

- Deposit platform fee: **1%** (credited balance is net of fee)
- Withdrawal platform fee: **1%** (user balance is debited amount + fee)
- Escrow platform fee: **3%**
- Provider fee: **0%** in this self-hosted v1
- Blockchain network fees are paid separately by the user
- Supported assets in this build: **BTC, LTC, ETH, USDT (ERC-20)**
- ETH and USDT require a working `ETH_RPC_URL` (or `ETH_RPC_URLS`)
- LTC requires `LTC_WATCHER_ENABLED=true` and a LitecoinSpace-compatible API base URL
