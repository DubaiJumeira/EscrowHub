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

    def total_balance(self, user_id: int, asset: str) -> Decimal:
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
            "SELECT amount FROM withdrawals WHERE user_id=? AND asset=? AND status='pending'",
            (user_id, asset.upper()),
        ).fetchall()
        locked = Decimal("0")
        for row in locks:
            locked += Decimal(row["amount"])
        for row in pending_withdrawals:
            locked += Decimal(row["amount"])
        return locked

    def available_balance(self, user_id: int, asset: str) -> Decimal:
        return self.total_balance(user_id, asset)
