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
        row = self.conn.execute(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) v FROM ledger_entries WHERE account_type='USER' AND account_owner_id=? AND asset=?",
            (user_id, asset.upper()),
        ).fetchone()
        return Decimal(str(row["v"]))

    def locked_balance(self, user_id: int, asset: str) -> Decimal:
        l = self.conn.execute(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) v FROM escrow_locks WHERE user_id=? AND asset=? AND status='locked'",
            (user_id, asset.upper()),
        ).fetchone()["v"]
        w = self.conn.execute(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) v FROM withdrawals WHERE user_id=? AND asset=? AND status='pending'",
            (user_id, asset.upper()),
        ).fetchone()["v"]
        return Decimal(str(l)) + Decimal(str(w))

    def available_balance(self, user_id: int, asset: str) -> Decimal:
        return self.total_balance(user_id, asset) - self.locked_balance(user_id, asset)
