from __future__ import annotations

# WARNING: XRP watcher is quarantined and must not be wired into runtime while active asset scope is BTC/LTC/ETH/USDT only.
# Secure alternative: keep this module isolated behind explicit feature flags and full security review before any re-enable.

import logging

from infra.chain_adapters.xrp_rpc import XrpRpcAdapter
from infra.db.database import get_connection, init_db
from wallet_service import WalletService
from watchers.notify import notify_deposit_credited

LOGGER = logging.getLogger(__name__)


def run_once(destination_tag_user_map: dict[str, int]) -> int:
    conn = get_connection()
    init_db(conn)
    try:
        wallet = WalletService(conn)
        adapter = XrpRpcAdapter(destination_tag_user_map)
        credited = 0
        for dep in adapter.fetch_deposits():
            if wallet.credit_deposit_if_confirmed(dep.user_id, dep.asset, dep.amount, dep.txid, dep.unique_key, "XRP", dep.confirmations, dep.finalized):
                credited += 1
                notify_deposit_credited(conn, dep.user_id, dep.asset, dep.amount, wallet.available_balance(dep.user_id, dep.asset))
        conn.commit()
        return credited
    except Exception:
        conn.rollback()
        LOGGER.exception("xrp watcher failed")
        raise
    finally:
        conn.close()
