from __future__ import annotations

from error_sanitizer import sanitize_runtime_error
from infra.db.database import get_connection, init_db
from runtime_preflight import run_startup_preflight
from signer.signer_service import SignerService
from wallet_service import WalletService


def main() -> int:
    conn = get_connection()
    try:
        init_db(conn)
        wallet = WalletService(conn)
        dep_ready, dep_reason = wallet.address_provider.is_ready()
        signer = SignerService()
        wd_ready, wd_reason = signer.readiness()
    except Exception as exc:
        # WARNING: smoke harness fails closed on uncertainty and never attempts on-chain actions.
        print(f"STAGING_SMOKE=BLOCKED reason={sanitize_runtime_error(exc)}")
        return 2
    finally:
        conn.close()

    route_ok = False
    route_detail = "unknown"
    try:
        bot_pf = run_startup_preflight("bot")
        signer_pf = run_startup_preflight("signer")
        watcher_pf = run_startup_preflight("watcher")
        route_ok = bool(bot_pf.route_integrity_ready and signer_pf.route_integrity_ready and watcher_pf.route_integrity_ready)
        route_detail = "ok" if route_ok else "route_integrity_unavailable"
    except Exception as exc:
        # WARNING: route/startup integrity uncertainty is treated as BLOCKED to prevent false-green staging signals.
        route_ok = False
        route_detail = sanitize_runtime_error(exc)

    if not route_ok or not dep_ready or not wd_ready:
        print(
            "STAGING_SMOKE=BLOCKED "
            f"route_integrity={sanitize_runtime_error(route_detail or 'not ready')} "
            f"deposit={sanitize_runtime_error(dep_reason or 'not ready')} "
            f"withdrawal={sanitize_runtime_error(wd_reason or 'not ready')}"
        )
        return 2

    print("STAGING_SMOKE=READY route_integrity=ok deposit=ok withdrawal=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
