from __future__ import annotations

import logging
import os
import time

from error_sanitizer import sanitize_runtime_error
from infra.db.database import get_connection, init_db
from runtime_preflight import FatalStartupError, PreflightIntegrityError, run_startup_preflight
from wallet_service import WalletService
from watcher_status_service import upsert_watcher_status
from watchers.sol_watcher import run_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("run_sol_watcher")


def _address_map(conn) -> dict[str, int]:
    wallet = WalletService(conn)
    pairs = wallet.monitored_deposit_address_map(["SOL"])
    return {str(k).strip(): int(v) for k, v in pairs.items()}


def _validate_sol_config() -> None:
    rpc_url = os.getenv("SOL_RPC_URL", "").strip()
    if not rpc_url:
        LOGGER.warning("SOL_RPC_URL is not set; SOL deposit polling will fail if enabled")


def main() -> None:
    enabled = os.getenv("SOL_WATCHER_ENABLED", "false").lower() == "true"
    if not enabled:
        LOGGER.info("SOL watcher disabled by config")
        conn = get_connection()
        init_db(conn)
        try:
            upsert_watcher_status(conn, "sol_watcher", success=False, error="disabled by config", health="disabled")
            conn.commit()
        finally:
            conn.close()
        return

    interval = int(os.getenv("WATCHER_POLL_INTERVAL_SECONDS", "30"))
    LOGGER.info("starting SOL watcher loop with interval=%ss", interval)

    try:
        preflight = run_startup_preflight("sol_watcher")
    except (PreflightIntegrityError, FatalStartupError) as exc:
        reasons = tuple(getattr(getattr(exc, "status", None), "reasons", ()) or ())
        message = "; ".join(reasons) or str(exc)
        conn = get_connection()
        init_db(conn)
        try:
            upsert_watcher_status(
                conn,
                "sol_watcher",
                success=False,
                error=message,
                health="fatal_startup_blocked",
            )
            conn.commit()
        finally:
            conn.close()
        LOGGER.error(
            "sol watcher startup aborted by fatal preflight/configuration error: %s",
            message,
        )
        raise

    if preflight is not None and preflight.reasons:
        LOGGER.warning("sol watcher preflight degraded: %s", "; ".join(preflight.reasons))

    _validate_sol_config()

    while True:
        conn = get_connection()
        init_db(conn)
        try:
            start = time.time()
            credited = run_once(_address_map(conn))
            upsert_watcher_status(conn, "sol_watcher", success=True, health="ok")
            conn.commit()
            LOGGER.info("sol watcher cycle success credited=%s duration=%.2fs", credited, time.time() - start)
        except Exception as exc:
            conn.rollback()
            upsert_watcher_status(
                conn,
                "sol_watcher",
                success=False,
                error=sanitize_runtime_error(exc),
                health="transient_failure",
            )
            conn.commit()
            LOGGER.exception("sol watcher cycle failed")
        finally:
            conn.close()
        time.sleep(interval)


if __name__ == "__main__":
    main()
