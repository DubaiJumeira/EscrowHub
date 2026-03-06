# EscrowHub v1 Runbook

## Required environment variables
- `SQLITE_DB_PATH` (or migrate SQL to PostgreSQL externally)
- `TELEGRAM_BOT_TOKEN`
- `ADMIN_USER_IDS` (comma-separated)
- `APP_ENV` (`dev` or `production`)
- `DISPUTE_FEE_POLICY` (`waive_all` default)

### Chain/RPC
- `BLOCKSTREAM_BASE_URL`
- `BTC_CONFIRMATIONS`, `LTC_CONFIRMATIONS`
- `ETH_RPC_URL`, `ETH_CONFIRMATIONS`
- `USDT_ERC20_CONTRACT`, `USDC_ERC20_CONTRACT`
- `SOL_RPC_URL`
- `XRP_RPC_URL`

### Signer (Vault)
- `SIGNER_MODE=local|vault` (production must use `vault`)
- `VAULT_ADDR`, `VAULT_TOKEN`, `VAULT_NAMESPACE` (optional)
- `VAULT_TRANSIT_MOUNT` (default `transit`)
- `VAULT_ETH_KEY_NAME`

### Cold sweep
- `COLD_WALLET_ADDRESS_<ASSET>` (e.g. `COLD_WALLET_ADDRESS_ETH`)
- `HOT_WALLET_TARGET_<ASSET>`
- `HOT_WALLET_BUFFER_<ASSET>`

## Vault Transit setup (ETH signing digest)
```bash
vault secrets enable transit
vault write -f transit/keys/escrowhub-eth type=ecdsa-p256
```
Configure:
```bash
export SIGNER_MODE=vault
export VAULT_TRANSIT_MOUNT=transit
export VAULT_ETH_KEY_NAME=escrowhub-eth
```

## Start bot
```bash
python bot.py
```

## Watchers
```bash
python -c "from watchers.eth_watcher import run_once; print(run_once({'0xyourhotaddr':123}))"
python -c "from watchers.btc_watcher import run_once; print(run_once('BTC', {'bc1...':123}))"
python -c "from watchers.btc_watcher import run_once; print(run_once('LTC', {'ltc1...':123}))"
python -c "from watchers.sol_watcher import run_once; print(run_once({'solAddr':123}))"
python -c "from watchers.xrp_watcher import run_once; print(run_once('rHotWallet', {'123':123}))"
```

## Signer worker
```bash
python -c "from infra.db.database import get_connection, init_db; from wallet_service import WalletService; from signer.signer_service import SignerService; c=get_connection(); init_db(c); w=WalletService(c); print(SignerService().process_pending_withdrawals(w)); c.commit(); c.close()"
```

## Sweep job
```bash
python -c "from watchers.sweep_job import run_once; print(run_once())"
```
