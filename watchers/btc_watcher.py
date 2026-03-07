from __future__ import annotations

import logging

from infra.chain_adapters.btc_blockstream import BlockstreamUtxoAdapter
from infra.db.database import get_connection, init_db
from wallet_service import WalletService

LOGGER = logging.getLogger(__name__)


def run_once(asset: str, address_user_map: dict[str, int]) -> int:
    conn = get_connection()
    init_db(conn)
    try:
        wallet = WalletService(conn)
        adapter = BlockstreamUtxoAdapter(asset=asset, address_user_map=address_user_map)
        credited = 0
        for dep in adapter.fetch_deposits():
            if wallet.credit_deposit_if_confirmed(dep.user_id, dep.asset, dep.amount, dep.txid, dep.unique_key, asset.upper(), dep.confirmations, dep.finalized):
                credited += 1
        conn.commit()
        return credited
    except Exception:
        conn.rollback()
        LOGGER.exception("%s watcher failed", asset)
        raise
    finally:
        conn.close()
