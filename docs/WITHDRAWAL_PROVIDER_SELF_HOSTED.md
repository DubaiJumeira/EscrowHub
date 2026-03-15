# Self-hosted withdrawal provider (manual queue)

This service satisfies the signer HTTP contract required by `signer/withdrawal_provider.py`.

## What this implementation does
- Exposes `GET /health`
- Exposes `POST /v1/withdrawals`
- Exposes `POST /v1/withdrawals/reconcile`
- Enforces bearer-token authentication with `WITHDRAWAL_PROVIDER_TOKEN`
- Persists `idempotency_key` and `provider_ref` in a dedicated SQLite DB
- Validates supported assets strictly: `BTC`, `LTC`, `ETH`, `USDT`
- Validates withdrawal destination addresses before queueing a withdrawal
- Supports manual lifecycle updates through `scripts/withdrawal_provider_admin.py`

## What this implementation does not do
This provider **does not sign or broadcast blockchain transactions automatically**.
It is a production-safe manual queue that lets EscrowHub track and reconcile withdrawals while an operator executes the on-chain payout from a separate custody wallet.

That is the smallest safe implementation available from this repo because the repository does not contain a real multi-chain hot-wallet engine.

## Provider database
Set `WITHDRAWAL_PROVIDER_DB_PATH` to a path such as:

`/var/lib/escrowhub-withdrawal-provider/provider.db`

The DB contains the queued withdrawal request, lifecycle status, and an audit log.

## Example lifecycle
1. Signer calls `POST /v1/withdrawals`
2. Provider validates the request and returns `status=submitted`
3. Operator reviews queued withdrawals:
   `python3 scripts/withdrawal_provider_admin.py list --filter-status submitted`
4. Operator executes the payout externally and marks it broadcasted:
   `python3 scripts/withdrawal_provider_admin.py set-status --provider-ref <ref> --status broadcasted --txid <txid> --actual-network-fee <fee>`
5. If the actual on-chain fee differs from the estimate reserved by EscrowHub, include `--actual-network-fee`; EscrowHub will settle the delta automatically on reconcile/confirmation.
5. Signer reconcile loop calls `POST /v1/withdrawals/reconcile`
6. Provider returns the stored state so EscrowHub advances the withdrawal row
7. After finality, operator marks it confirmed:
   `python3 scripts/withdrawal_provider_admin.py set-status --provider-ref <ref> --status confirmed --txid <txid>`

## Required provider env
- `WITHDRAWAL_PROVIDER_TOKEN`
- `WITHDRAWAL_PROVIDER_BIND` (default `127.0.0.1`)
- `WITHDRAWAL_PROVIDER_PORT` (default `8787`)
- `WITHDRAWAL_PROVIDER_DB_PATH` (default `/var/lib/escrowhub-withdrawal-provider/provider.db`)

## EscrowHub signer env
- `WITHDRAWALS_ENABLED=true`
- `WITHDRAWAL_PROVIDER=http`
- `WITHDRAWAL_PROVIDER_URL=https://<your-domain>`
- `WITHDRAWAL_PROVIDER_TOKEN=<same token used by provider>`

## HTTPS recommendation
Run the provider on `127.0.0.1:8787` and put nginx in front of it with TLS termination.
The signer must use the public `https://` URL in production.
