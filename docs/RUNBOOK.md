# EscrowHub v1 Runbook

## Required environment variables
- `SQLITE_DB_PATH`
- `TELEGRAM_BOT_TOKEN`
- `ADMIN_USER_IDS`
- `APP_ENV` (`dev` or `production`)
- `DISPUTE_FEE_POLICY` (`waive_all` default)

### HD wallet derivation
- `HD_WALLET_SEED_HEX` (required)
- BTC BIP84 path: `m/84'/0'/{user_id}'/0/0`
- ETH BIP44 path: `m/44'/60'/{user_id}'/0/0`

⚠️ Changing `HD_WALLET_SEED_HEX` changes all derived deposit addresses.

### Chain/RPC
- `BLOCKSTREAM_BASE_URL`
- `BTC_CONFIRMATIONS`, `LTC_CONFIRMATIONS`
- `ETH_RPC_URL`, `ETH_CONFIRMATIONS`
- `USDT_ERC20_CONTRACT`, `USDC_ERC20_CONTRACT`
- `SOL_RPC_URL`
- `XRP_RPC_URL`

### Signer (Vault)
- `SIGNER_MODE=local|vault` (production requires `vault`)
- `VAULT_ADDR`, `VAULT_TOKEN`, `VAULT_NAMESPACE` (optional)
- `VAULT_TRANSIT_MOUNT` (default `transit`)
- `VAULT_ETH_KEY_NAME`

### Cold wallet addresses (addresses only; keys must stay offline)
- `COLD_WALLET_ADDRESS_BTC`
- `COLD_WALLET_ADDRESS_ETH`
- `COLD_WALLET_ADDRESS_SOL`
- `COLD_WALLET_ADDRESS_XRP`
- `COLD_WALLET_ADDRESS_LTC`
- `HOT_WALLET_TARGET_<ASSET>`
- `HOT_WALLET_BUFFER_<ASSET>`

## Security notes
- Never commit `HD_WALLET_SEED_HEX`.
- Never store private keys/xprvs/seeds in DB.
- Cold wallet private keys must remain offline.
- Signer handles hot-wallet/Vault-backed signing only.

## Vault Transit setup (ETH signing digest)
```bash
vault secrets enable transit
vault write -f transit/keys/escrowhub-eth type=ecdsa-p256
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

## Seed rotation
1. Create new environment and set new `HD_WALLET_SEED_HEX`.
2. Do not reuse old addresses across environments.
3. Migrate balances operationally before switching production traffic.
