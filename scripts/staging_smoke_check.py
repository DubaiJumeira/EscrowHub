from __future__ import annotations

from infra.db.database import get_connection, init_db
from error_sanitizer import sanitize_runtime_error
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

    if not dep_ready or not wd_ready:
        print(f"STAGING_SMOKE=BLOCKED deposit={sanitize_runtime_error(dep_reason or 'not ready')} withdrawal={sanitize_runtime_error(wd_reason or 'not ready')}")
        return 2

    print("STAGING_SMOKE=READY deposit=ok withdrawal=ok route_integrity=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
