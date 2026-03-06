from __future__ import annotations

import json
import logging
import os
import time

from infra.db.database import get_connection, init_db
from watcher_status_service import upsert_watcher_status
from watchers.eth_watcher import run_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("run_eth_watcher")


def _address_map() -> dict[str, int]:
    raw = os.getenv("ETH_ADDRESS_USER_MAP", "{}")
    try:
        data = json.loads(raw)
        return {str(k): int(v) for k, v in data.items()}
    except Exception:
        LOGGER.warning("invalid ETH_ADDRESS_USER_MAP, using empty map")
        return {}


def main() -> None:
    enabled = os.getenv("ETH_WATCHER_ENABLED", "true").lower() == "true"
    if not enabled:
        LOGGER.info("ETH watcher disabled by config")
        return
    interval = int(os.getenv("WATCHER_POLL_INTERVAL_SECONDS", "30"))
    LOGGER.info("starting ETH watcher loop with interval=%ss", interval)

    while True:
        conn = get_connection()
        init_db(conn)
        try:
            start = time.time()
            credited = run_once(_address_map())
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
