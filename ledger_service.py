from __future__ import annotations

from decimal import Decimal


class LedgerService:
    def __init__(self, conn) -> None:
        self.conn = conn

    def add_entry(self, account_type: str, account_owner_id: int | None, user_id: int | None, asset: str, amount: Decimal, entry_type: str, ref_type: str, ref_id: int) -> None:
        self.conn.execute(
            """INSERT INTO ledger_entries(account_type,account_owner_id,user_id,asset,amount,entry_type,ref_type,ref_id)
               VALUES(?,?,?,?,?,?,?,?)""",
            (account_type, account_owner_id, user_id, asset.upper(), str(Decimal(amount)), entry_type, ref_type, ref_id),
        )

    def _net_user_ledger_balance(self, user_id: int, asset: str) -> Decimal:
        rows = self.conn.execute(
            "SELECT amount FROM ledger_entries WHERE account_type='USER' AND account_owner_id=? AND asset=?",
            (user_id, asset.upper()),
        ).fetchall()
        total = Decimal("0")
        for row in rows:
            total += Decimal(row["amount"])
        return total

    def locked_balance(self, user_id: int, asset: str) -> Decimal:
        locks = self.conn.execute(
            "SELECT amount FROM escrow_locks WHERE user_id=? AND asset=? AND status='locked'",
            (user_id, asset.upper()),
        ).fetchall()
        pending_withdrawals = self.conn.execute(
            "SELECT amount, platform_fee_amount, network_fee_amount FROM withdrawals WHERE user_id=? AND asset=? AND status='pending'",
            (user_id, asset.upper()),
        ).fetchall()
        reserved = Decimal("0")
        for row in locks:
            reserved += Decimal(row["amount"])
        for row in pending_withdrawals:
            reserved += Decimal(str(row["amount"] or "0")) + Decimal(str(row["platform_fee_amount"] if "platform_fee_amount" in row.keys() else None or "0")) + Decimal(str(row["network_fee_amount"] if "network_fee_amount" in row.keys() else None or "0"))
        return reserved

    def available_balance(self, user_id: int, asset: str) -> Decimal:
        # Net USER ledger includes reserve debits for active escrow locks and pending withdrawals.
        return self._net_user_ledger_balance(user_id, asset)

    def total_balance(self, user_id: int, asset: str) -> Decimal:
        # Economic total held by user = currently available + currently reserved obligations.
        return self.available_balance(user_id, asset) + self.locked_balance(user_id, asset)
