from __future__ import annotations

import logging
import os
import time

from infra.db.database import get_connection, init_db
from wallet_service import WalletService
from watcher_status_service import upsert_watcher_status
from watchers.eth_watcher import run_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("run_eth_watcher")


def _address_map(conn) -> dict[str, int]:
    wallet = WalletService(conn)
    wallet.verify_address_derivation_consistency(sample_size=25)
    pairs = wallet.monitored_deposit_address_map(["ETH", "USDT"])
    return {k.lower(): v for k, v in pairs.items()}


def _validate_erc20_config() -> None:
    usdt = os.getenv("USDT_ERC20_CONTRACT", "").strip()
    if not usdt:
        LOGGER.warning("USDT_ERC20_CONTRACT is not set; USDT deposit events will be ignored")


def main() -> None:
    enabled = os.getenv("ETH_WATCHER_ENABLED", "true").lower() == "true"
    if not enabled:
        LOGGER.info("ETH watcher disabled by config")
        return
    interval = int(os.getenv("WATCHER_POLL_INTERVAL_SECONDS", "30"))
    LOGGER.info("starting ETH watcher loop with interval=%ss", interval)
    _validate_erc20_config()

    while True:
        conn = get_connection()
        init_db(conn)
        try:
            start = time.time()
            credited = run_once(_address_map(conn))
            upsert_watcher_status(conn, "eth_watcher", success=True)
            conn.commit()
            LOGGER.info("eth watcher cycle success credited=%s duration=%.2fs", credited, time.time() - start)
        except Exception as exc:
            conn.rollback()
            upsert_watcher_status(conn, "eth_watcher", success=False, error=str(exc))
            conn.commit()
            LOGGER.exception("eth watcher cycle failed")
        finally:
            conn.close()
        time.sleep(interval)


if __name__ == "__main__":
    main()
