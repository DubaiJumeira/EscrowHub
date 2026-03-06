from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ledger_service import LedgerService

SUPPORTED_ASSETS = {"BTC", "ETH", "LTC", "USDT", "USDC", "SOL", "XRP"}
NETWORK_LABELS = {
    "BTC": "BTC",
    "ETH": "ETH",
    "LTC": "LTC",
    "USDT": "USDT (ERC-20)",
    "USDC": "USDC (ERC-20)",
    "SOL": "SOL",
    "XRP": "XRP",
}


@dataclass
class DepositRoute:
    address: str
    asset: str
    chain_family: str
    destination_tag: str | None


class WalletService:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.ledger = LedgerService(conn)

    @staticmethod
    def _asset(asset: str) -> str:
        symbol = asset.upper()
        if symbol not in SUPPORTED_ASSETS:
            raise ValueError(f"unsupported asset: {symbol}")
        return symbol

    def _chain_family(self, asset: str) -> str:
        return "ETHEREUM" if self._asset(asset) in {"ETH", "USDT", "USDC"} else self._asset(asset)

    def _ensure_user_row(self, user_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO users(id, telegram_id, username, frozen) VALUES(?,?,?,0)", (user_id, user_id, None))

    def get_or_create_deposit_address(self, user_id: int, asset: str) -> DepositRoute:
        symbol = self._asset(asset)
        row = self.conn.execute("SELECT * FROM wallet_addresses WHERE user_id=? AND asset=?", (user_id, symbol)).fetchone()
        if row:
            return DepositRoute(row["address"], row["asset"], row["chain_family"], row["destination_tag"])
        if symbol == "XRP":
            address, tag = "xrp_platform_receive_address", str(user_id)
        else:
            address, tag = f"{symbol.lower()}_addr_{user_id}", None
        self.conn.execute(
            "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,derivation_index,destination_tag) VALUES(?,?,?,?,?,?)",
            (user_id, symbol, self._chain_family(symbol), address, user_id if symbol != "XRP" else None, tag),
        )
        return DepositRoute(address, symbol, self._chain_family(symbol), tag)

    def credit_deposit_if_confirmed(self, user_id: int, asset: str, amount: Decimal, txid: str, unique_key: str, chain_family: str, confirmations: int, finalized: bool) -> bool:
        self._ensure_user_row(user_id)
        try:
            cur = self.conn.execute(
                "INSERT INTO deposits(user_id,asset,amount,txid,unique_key,chain_family,confirmations,status) VALUES(?,?,?,?,?,?,?,?)",
                (user_id, self._asset(asset), str(Decimal(amount)), txid, unique_key, chain_family, confirmations, "credited" if finalized else "seen"),
            )
        except Exception:
            return False
        if finalized:
            self.ledger.add_entry("USER", user_id, user_id, asset, Decimal(amount), "DEPOSIT", "deposit", int(cur.lastrowid))
        return finalized

    def total_balance(self, user_id: int, asset: str) -> Decimal:
        return self.ledger.total_balance(user_id, self._asset(asset))

    def locked_balance(self, user_id: int, asset: str) -> Decimal:
        return self.ledger.locked_balance(user_id, self._asset(asset))

    def available_balance(self, user_id: int, asset: str) -> Decimal:
        return self.ledger.available_balance(user_id, self._asset(asset))

    def lock_for_escrow(self, escrow_id: int, user_id: int, asset: str, amount: Decimal) -> None:
        self._ensure_user_row(user_id)
        if self.available_balance(user_id, asset) < Decimal(amount):
            raise ValueError("insufficient available balance")
        self.conn.execute("INSERT INTO escrow_locks(escrow_id,user_id,asset,amount,status) VALUES(?,?,?,?,?)", (escrow_id, user_id, self._asset(asset), str(Decimal(amount)), "locked"))
        self.ledger.add_entry("USER", user_id, user_id, asset, Decimal("0"), "ESCROW_LOCK", "escrow", escrow_id)

    def release_escrow(self, escrow_id: int, seller_id: int, platform_fee: Decimal, bot_fee: Decimal, seller_payout: Decimal, bot_owner_id: int, asset: str) -> None:
        lock = self.conn.execute("SELECT * FROM escrow_locks WHERE escrow_id=?", (escrow_id,)).fetchone()
        if not lock or lock["status"] != "locked":
            raise ValueError("escrow lock missing")
        self.conn.execute("UPDATE escrow_locks SET status='released' WHERE escrow_id=?", (escrow_id,))
        self.ledger.add_entry("USER", int(lock["user_id"]), int(lock["user_id"]), asset, -Decimal(lock["amount"]), "ESCROW_RELEASE", "escrow", escrow_id)
        self.ledger.add_entry("USER", seller_id, seller_id, asset, Decimal(seller_payout), "ESCROW_RELEASE", "escrow", escrow_id)
        self.ledger.add_entry("PLATFORM_REVENUE", None, None, asset, Decimal(platform_fee), "PLATFORM_FEE", "escrow", escrow_id)
        self.ledger.add_entry("BOT_OWNER_REVENUE", bot_owner_id, bot_owner_id, asset, Decimal(bot_fee), "BOT_FEE", "escrow", escrow_id)

    def cancel_escrow_lock(self, escrow_id: int) -> None:
        lock = self.conn.execute("SELECT * FROM escrow_locks WHERE escrow_id=?", (escrow_id,)).fetchone()
        if not lock or lock["status"] != "locked":
            raise ValueError("escrow lock missing")
        self.conn.execute("UPDATE escrow_locks SET status='released' WHERE escrow_id=?", (escrow_id,))
        self.ledger.add_entry("USER", int(lock["user_id"]), int(lock["user_id"]), lock["asset"], Decimal("0"), "ADJUSTMENT", "escrow", escrow_id)

    def request_withdrawal(self, user_id: int, asset: str, amount: Decimal, destination_address: str):
        self._ensure_user_row(user_id)
        self._ensure_user_row(user_id)
        if self.available_balance(user_id, asset) < Decimal(amount):
            raise ValueError("insufficient available balance")
        cur = self.conn.execute(
            "INSERT INTO withdrawals(user_id,asset,amount,destination_address,status) VALUES(?,?,?,?,?)",
            (user_id, self._asset(asset), str(Decimal(amount)), destination_address, "pending"),
        )
        wid = int(cur.lastrowid)
        self.ledger.add_entry("USER", user_id, user_id, asset, -Decimal(amount), "WITHDRAWAL_RESERVE", "withdrawal", wid)
        return {"id": wid, "asset": self._asset(asset), "amount": Decimal(amount), "destination_address": destination_address}

    def pending_withdrawals(self):
        return self.conn.execute("SELECT * FROM withdrawals WHERE status='pending'").fetchall()

    def mark_withdrawal_broadcasted(self, withdrawal_id: int, txid: str) -> None:
        row = self.conn.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
        self.conn.execute("UPDATE withdrawals SET status='broadcasted', txid=? WHERE id=?", (txid, withdrawal_id))
        self.ledger.add_entry("USER", row["user_id"], row["user_id"], row["asset"], Decimal("0"), "WITHDRAWAL_SENT", "withdrawal", withdrawal_id)

    def account_revenue_balance(self, account_type: str, owner_id: int | None, asset: str) -> Decimal:
        rows = self.conn.execute("SELECT amount, account_owner_id FROM ledger_entries WHERE account_type=? AND asset=?", (account_type, self._asset(asset))).fetchall()
        total = Decimal("0")
        for r in rows:
            if account_type == "BOT_OWNER_REVENUE" and int(r["account_owner_id"]) != int(owner_id):
                continue
            total += Decimal(r["amount"])
        return total
