from __future__ import annotations

import os
from decimal import Decimal

from infra.db.database import get_connection, init_db
from signer.signer_service import SignerService
from wallet_service import SUPPORTED_ASSETS, WalletService


def _target(asset: str) -> Decimal:
    return Decimal(os.getenv(f"HOT_WALLET_TARGET_{asset}", "0"))


def _buffer(asset: str) -> Decimal:
    return Decimal(os.getenv(f"HOT_WALLET_BUFFER_{asset}", "0"))


def _cold_addr(asset: str) -> str:
    return os.getenv(f"COLD_WALLET_ADDRESS_{asset}", "")


def run_once() -> int:
    conn = get_connection()
    init_db(conn)
    wallet = WalletService(conn)
    signer = SignerService()
    count = 0
    try:
        for asset in sorted(SUPPORTED_ASSETS):
            cold = _cold_addr(asset)
            if not cold:
                continue
            balance = wallet.account_revenue_balance("PLATFORM_REVENUE", None, asset)
            threshold = _target(asset) + _buffer(asset)
            if balance <= threshold:
                continue
            amount = balance - _target(asset)
            sweep_id = wallet.create_platform_sweep(asset, amount, cold)
            txid = signer._sign_with_asset(asset, f"sweep:{asset}:{amount}:{cold}")
            wallet.mark_sweep_broadcasted(sweep_id, txid)
            count += 1
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
