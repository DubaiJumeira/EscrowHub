# EscrowHub v1 Runbook

## Required environment variables
- `TELEGRAM_BOT_TOKEN`
- `ADMIN_USER_IDS` (comma-separated Telegram IDs)
- `APP_ENV` (`dev` or `production`)
- `SQLITE_DB_PATH` (or external DB wiring)
- `HD_WALLET_SEED_HEX` (**required for HD derivation/signing**)
- `XRP_HOT_WALLET_ADDRESS` (**required in production for XRP deposits**)
- `ETH_RPC_URL` (Alchemy/Infura RPC)
- `BLOCKSTREAM_BASE_URL` (optional override)
- `BTC_CONFIRMATIONS` / `LTC_CONFIRMATIONS` (default 3)
- `ETH_CONFIRMATIONS` (default 12)
- `SOL_RPC_URL`
- `XRP_RPC_URL`
- `SIGNER_PROVIDER` (`hd`, `mock`, or `vault`)
- `VAULT_ADDR`, `VAULT_TOKEN`, `VAULT_SIGN_PATH` (when `SIGNER_PROVIDER=vault`)

## Seed backup policy (critical)
- Never commit or store `HD_WALLET_SEED_HEX` in code or DB.
- Store seed backup in offline encrypted secret manager/HSM process.
- Rotate operational access credentials, not seed itself, unless emergency recovery protocol is triggered.

## Start bot backend
```bash
python bot.py
```

## Run watchers
```bash
python -c "from watchers.eth_watcher import run_once; print(run_once({}))"
python -c "from watchers.btc_watcher import run_once; print(run_once('BTC', {}))"
python -c "from watchers.btc_watcher import run_once; print(run_once('LTC', {}))"
python -c "from watchers.xrp_watcher import run_once; print(run_once({'12345': 12345}))"
python -c "from watchers.sol_watcher import run_once; print(run_once())"
```

## Run signer worker
```bash
python -c "from infra.db.database import get_connection, init_db; from wallet_service import WalletService; from signer.signer_service import SignerService; c=get_connection(); init_db(c); w=WalletService(c); print(SignerService().process_pending_withdrawals(w)); c.commit(); c.close()"
```

## Security notes
- Telegram/backend process must never store private keys.
- Signer should run as a separate process.
- In production, missing `hdwallet`/solana libs or missing XRP hot wallet env must fail fast.
