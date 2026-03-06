# EscrowHub v1 Runbook

## Required environment variables
- `DATABASE_URL` (PostgreSQL URL, e.g. `postgresql+psycopg2://user:pass@host/db`)
- `TELEGRAM_BOT_TOKEN`
- `ADMIN_USER_IDS` (comma-separated Telegram IDs)
- `ETH_RPC_URL` (Alchemy/Infura RPC)
- `BLOCKSTREAM_BASE_URL` (optional override)
- `BTC_CONFIRMATIONS` / `LTC_CONFIRMATIONS` (default 3)
- `ETH_CONFIRMATIONS` (default 12)
- `SOL_RPC_URL`
- `XRP_RPC_URL`
- `SIGNER_PROVIDER` (`mock` or `vault`)
- `VAULT_ADDR`, `VAULT_TOKEN`, `VAULT_SIGN_PATH` (when `SIGNER_PROVIDER=vault`)

## Start backend bot
```bash
python bot.py
```

## Run watchers
```bash
python -c "from watchers.eth_watcher import run_once; print(run_once({}))"
python -c "from watchers.btc_watcher import run_once; print(run_once('BTC', {}))"
python -c "from watchers.btc_watcher import run_once; print(run_once('LTC', {}))"
python -c "from watchers.sol_watcher import run_once; print(run_once())"
python -c "from watchers.xrp_watcher import run_once; print(run_once())"
```

## Run signer worker
```bash
python -c "from infra.db.database import get_connection, init_db; from wallet_service import WalletService; from signer.signer_service import SignerService; c=get_connection(); init_db(c); w=WalletService(c); print(SignerService().process_pending_withdrawals(w)); c.commit(); c.close()"
```

## Notes
- Never store private keys in repository or bot process.
- Signer must run as isolated process/service.
- All balances are derived from ledger + lock rows.
