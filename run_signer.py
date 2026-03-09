from __future__ import annotations

import logging
import os
import time

from infra.db.database import get_connection, init_db
from signer.signer_service import SignerService
from runtime_preflight import PreflightIntegrityError, run_startup_preflight
from wallet_service import WalletService
from watcher_status_service import upsert_watcher_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("run_signer")


def main() -> None:
    interval = int(os.getenv("WATCHER_POLL_INTERVAL_SECONDS", "30"))
    try:
        preflight = run_startup_preflight("signer")
    except PreflightIntegrityError as exc:
        # WARNING: startup fails closed when route-integrity checks detect tampering/collision risk.
        LOGGER.error("signer startup aborted by fatal integrity preflight: %s", "; ".join(exc.status.reasons) or str(exc))
        raise
    if preflight is not None and not preflight.signer_ready:
        LOGGER.warning("signer preflight degraded: %s", "; ".join(preflight.reasons) or "not ready")
    LOGGER.info("starting signer loop interval=%ss", interval)
    while True:
        conn = get_connection()
        init_db(conn)
        try:
            start = time.time()
            wallet = WalletService(conn)
            count = SignerService().process_withdrawals(wallet)
            upsert_watcher_status(conn, "signer_loop", success=True)
            conn.commit()
            LOGGER.info("signer cycle success processed=%s duration=%.2fs", count, time.time() - start)
        except Exception as exc:
            conn.rollback()
            upsert_watcher_status(conn, "signer_loop", success=False, error=str(exc))
            conn.commit()
            LOGGER.exception("signer cycle failed")
        finally:
            conn.close()
        time.sleep(interval)


if __name__ == "__main__":
    main()
