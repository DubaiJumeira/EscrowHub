from __future__ import annotations

import logging

from infra.chain_adapters.xrp_rpc import XrpRpcAdapter
from infra.db.database import get_connection, init_db
from wallet_service import WalletService

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
        conn.commit()
        return credited
    except Exception:
        conn.rollback()
        LOGGER.exception("xrp watcher failed")
        raise
    finally:
        conn.close()
