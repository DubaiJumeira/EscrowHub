from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
import re

from config.settings import Settings
from hd_wallet import HDWalletDeriver
from ledger_service import LedgerService
from price_service import StaticPriceService

SUPPORTED_ASSETS = set(Settings.supported_assets)
NETWORK_LABELS = {
    "BTC": "BTC",
    "ETH": "ETH",
    "LTC": "LTC",
    "USDT": "USDT (ERC-20)",
}


@dataclass
class DepositRoute:
    address: str
    asset: str
    chain_family: str
    destination_tag: str | None
    derivation_path: str | None


class WalletService:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.ledger = LedgerService(conn)
        self.hd = HDWalletDeriver()
        self.price_service = StaticPriceService({"BTC": Decimal("65000"), "ETH": Decimal("3500"), "LTC": Decimal("80"), "USDT": Decimal("1")})

    @staticmethod
    def _asset(asset: str) -> str:
        symbol = asset.upper()
        if symbol not in SUPPORTED_ASSETS:
            raise ValueError(f"unsupported asset: {symbol}")
        return symbol

    def _chain_family(self, asset: str) -> str:
        return "ETHEREUM" if self._asset(asset) in {"ETH", "USDT"} else self._asset(asset)

    def _ensure_user_row(self, user_ref: int) -> int:
        by_id = self.conn.execute("SELECT id FROM users WHERE id=?", (user_ref,)).fetchone()
        if by_id:
            return int(by_id["id"])
        by_tg = self.conn.execute("SELECT id FROM users WHERE telegram_id=?", (user_ref,)).fetchone()
        if by_tg:
            return int(by_tg["id"])
        cur = self.conn.execute("INSERT INTO users(telegram_id, username, frozen) VALUES(?,?,0)", (user_ref, None))
        return int(cur.lastrowid)

    def _assert_not_frozen(self, resolved_user_id: int) -> None:
        row = self.conn.execute("SELECT frozen FROM users WHERE id=?", (resolved_user_id,)).fetchone()
        if row and int(row["frozen"]):
            raise ValueError("account is frozen")

    def get_or_create_deposit_address(self, user_id: int, asset: str) -> DepositRoute:
        symbol = self._asset(asset)
        resolved_user_id = self._ensure_user_row(user_id)
        row = self.conn.execute("SELECT * FROM wallet_addresses WHERE user_id=? AND asset=?", (resolved_user_id, symbol)).fetchone()
        if row:
            return DepositRoute(row["address"], row["asset"], row["chain_family"], row["destination_tag"], row["derivation_path"])

        if symbol == "BTC":
            k = self.hd.derive_btc_address(resolved_user_id)
        elif symbol == "LTC":
            k = self.hd.derive_ltc_address(resolved_user_id)
        elif symbol in {"ETH", "USDT"}:
            k = self.hd.derive_eth_address(resolved_user_id)
        else:
            raise RuntimeError(f"unsupported production derivation for {symbol}")

        self.conn.execute(
            "INSERT INTO wallet_addresses(user_id,asset,chain_family,address,derivation_index,destination_tag,derivation_path) VALUES(?,?,?,?,?,?,?)",
            (resolved_user_id, symbol, self._chain_family(symbol), k.public_address, resolved_user_id, None, k.path),
        )
        return DepositRoute(k.public_address, symbol, self._chain_family(symbol), None, k.path)

    def credit_deposit_if_confirmed(self, user_id: int, asset: str, amount: Decimal, txid: str, unique_key: str, chain_family: str, confirmations: int, finalized: bool) -> bool:
        resolved_user_id = self._ensure_user_row(user_id)
        try:
            cur = self.conn.execute(
                "INSERT INTO deposits(user_id,asset,amount,txid,unique_key,chain_family,confirmations,status) VALUES(?,?,?,?,?,?,?,?)",
                (resolved_user_id, self._asset(asset), str(Decimal(amount)), txid, unique_key, chain_family, confirmations, "credited" if finalized else "seen"),
            )
        except sqlite3.IntegrityError:
            return False
        if finalized:
            self.ledger.add_entry("USER", resolved_user_id, resolved_user_id, asset, Decimal(amount), "DEPOSIT", "deposit", int(cur.lastrowid))
        return finalized

    def total_balance(self, user_id: int, asset: str) -> Decimal:
        resolved_user_id = self._ensure_user_row(user_id)
        return self.ledger.total_balance(resolved_user_id, self._asset(asset))

    def locked_balance(self, user_id: int, asset: str) -> Decimal:
        resolved_user_id = self._ensure_user_row(user_id)
        return self.ledger.locked_balance(resolved_user_id, self._asset(asset))

    def available_balance(self, user_id: int, asset: str) -> Decimal:
        resolved_user_id = self._ensure_user_row(user_id)
        return self.ledger.available_balance(resolved_user_id, self._asset(asset))

    def lock_for_escrow(self, escrow_id: int, user_id: int, asset: str, amount: Decimal) -> None:
        resolved_user_id = self._ensure_user_row(user_id)
        if self.available_balance(resolved_user_id, asset) < Decimal(amount):
            raise ValueError("insufficient available balance")
        self.conn.execute("INSERT INTO escrow_locks(escrow_id,user_id,asset,amount,status) VALUES(?,?,?,?,?)", (escrow_id, resolved_user_id, self._asset(asset), str(Decimal(amount)), "locked"))
        self.ledger.add_entry("USER", resolved_user_id, resolved_user_id, asset, -Decimal(amount), "ESCROW_LOCK", "escrow", escrow_id)

    def release_escrow(self, escrow_id: int, seller_id: int, platform_fee: Decimal, bot_fee: Decimal, seller_payout: Decimal, bot_owner_id: int, asset: str) -> None:
        lock = self.conn.execute("SELECT * FROM escrow_locks WHERE escrow_id=?", (escrow_id,)).fetchone()
        if not lock or lock["status"] != "locked":
            raise ValueError("escrow lock missing")
        self.conn.execute("UPDATE escrow_locks SET status='released' WHERE escrow_id=?", (escrow_id,))
        self.ledger.add_entry("USER", seller_id, seller_id, asset, Decimal(seller_payout), "ESCROW_RELEASE", "escrow", escrow_id)
        self.ledger.add_entry("PLATFORM_REVENUE", None, None, asset, Decimal(platform_fee), "PLATFORM_FEE", "escrow", escrow_id)
        self.ledger.add_entry("BOT_OWNER_REVENUE", bot_owner_id, bot_owner_id, asset, Decimal(bot_fee), "BOT_FEE", "escrow", escrow_id)

    def cancel_escrow_lock(self, escrow_id: int) -> None:
        lock = self.conn.execute("SELECT * FROM escrow_locks WHERE escrow_id=?", (escrow_id,)).fetchone()
        if not lock or lock["status"] != "locked":
            raise ValueError("escrow lock missing")
        self.conn.execute("UPDATE escrow_locks SET status='released' WHERE escrow_id=?", (escrow_id,))
        self.ledger.add_entry("USER", int(lock["user_id"]), int(lock["user_id"]), lock["asset"], Decimal(lock["amount"]), "ESCROW_UNLOCK", "escrow", escrow_id)

    def _validate_withdrawal_address(self, asset: str, destination_address: str) -> None:
        symbol = self._asset(asset)
        address = (destination_address or "").strip()
        if not address:
            raise ValueError("destination address is required")

        patterns = {
            "ETH": r"^0x[a-fA-F0-9]{40}$",
            "USDT": r"^0x[a-fA-F0-9]{40}$",
            "BTC": r"^(bc1[ac-hj-np-z02-9]{11,71}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$",
            "LTC": r"^(ltc1[ac-hj-np-z02-9]{11,71}|[LM3][a-km-zA-HJ-NP-Z1-9]{26,34})$",
        }
        if not re.match(patterns[symbol], address):
            network = NETWORK_LABELS.get(symbol, symbol)
            raise ValueError(f"Invalid destination address for {network}")

    def validate_withdrawal_address(self, asset: str, destination_address: str) -> None:
        self._validate_withdrawal_address(asset, destination_address)

    def _withdrawn_usd_last_24h(self, user_id: int) -> Decimal:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self.conn.execute(
            "SELECT asset, amount FROM withdrawals WHERE user_id=? AND status IN ('pending','broadcasted') AND created_at >= ?",
            (user_id, cutoff),
        ).fetchall()
        total = Decimal("0")
        for row in rows:
            amount = Decimal(str(row["amount"] or "0"))
            total += self.price_service.get_usd_value(str(row["asset"]), amount)
        return total

    def request_withdrawal(self, user_id: int, asset: str, amount: Decimal, destination_address: str):
        symbol = self._asset(asset)
        amt = Decimal(amount)
        if amt <= Decimal("0"):
            raise ValueError("amount must be positive")
        self._validate_withdrawal_address(symbol, destination_address)
        resolved_user_id = self._ensure_user_row(user_id)

        managed_tx = not bool(getattr(self.conn, "in_transaction", False))
        if managed_tx:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute("SAVEPOINT withdrawal_request")
        try:
            self._assert_not_frozen(resolved_user_id)
            min_interval = max(0, int(Settings.withdrawal_min_interval_seconds))
            if min_interval > 0:
                last = self.conn.execute(
                    "SELECT created_at FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 1",
                    (resolved_user_id,),
                ).fetchone()
                if last and last["created_at"]:
                    last_dt = datetime.strptime(last["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - last_dt).total_seconds() < min_interval:
                        raise ValueError("withdrawals are rate-limited; try again shortly")

            daily_limit = Decimal(Settings.withdrawal_daily_limit_usd)
            if self._withdrawn_usd_last_24h(resolved_user_id) + amt > daily_limit:
                raise ValueError("daily withdrawal limit exceeded")

            if self.ledger.available_balance(resolved_user_id, symbol) < amt:
                raise ValueError("insufficient available balance")
            cur = self.conn.execute(
                "INSERT INTO withdrawals(user_id,asset,amount,destination_address,status) VALUES(?,?,?,?,?)",
                (resolved_user_id, symbol, str(amt), destination_address, "pending"),
            )
            wid = int(cur.lastrowid)
            self.ledger.add_entry("USER", resolved_user_id, resolved_user_id, symbol, -amt, "WITHDRAWAL_RESERVE", "withdrawal", wid)
            if managed_tx:
                self.conn.commit()
            else:
                self.conn.execute("RELEASE SAVEPOINT withdrawal_request")
            return {"id": wid, "asset": symbol, "amount": amt, "destination_address": destination_address}
        except Exception:
            if managed_tx:
                self.conn.rollback()
            else:
                self.conn.execute("ROLLBACK TO SAVEPOINT withdrawal_request")
                self.conn.execute("RELEASE SAVEPOINT withdrawal_request")
            raise

    def pending_withdrawals(self):
        return self.conn.execute("SELECT * FROM withdrawals WHERE status='pending'").fetchall()

    def mark_withdrawal_broadcasted(self, withdrawal_id: int, txid: str) -> None:
        self.conn.execute("UPDATE withdrawals SET status='broadcasted', txid=? WHERE id=?", (txid, withdrawal_id))

    def mark_withdrawal_failed(self, withdrawal_id: int, reason: str) -> None:
        row = self.conn.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
        if not row:
            return
        self.conn.execute("UPDATE withdrawals SET status=?, txid=? WHERE id=?", ("failed", f"ERROR: {reason}"[:255], withdrawal_id))
        self.ledger.add_entry("USER", row["user_id"], row["user_id"], row["asset"], Decimal(row["amount"]), "WITHDRAWAL_RELEASE", "withdrawal", withdrawal_id)

    def account_revenue_balance(self, account_type: str, owner_id: int | None, asset: str) -> Decimal:
        symbol = self._asset(asset)
        if account_type == "BOT_OWNER_REVENUE":
            rows = self.conn.execute(
                "SELECT amount FROM ledger_entries WHERE account_type=? AND asset=? AND account_owner_id=?",
                (account_type, symbol, owner_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT amount FROM ledger_entries WHERE account_type=? AND asset=?",
                (account_type, symbol),
            ).fetchall()
        total = Decimal("0")
        for row in rows:
            total += Decimal(str(row["amount"] or "0"))
        return total

    def withdrawal_history(self, user_id: int, page: int = 1, per_page: int = 10):
        resolved_user_id = self._ensure_user_row(user_id)
        page = max(1, int(page))
        per_page = max(1, int(per_page))
        total = self.conn.execute("SELECT COUNT(*) c FROM withdrawals WHERE user_id=?", (resolved_user_id,)).fetchone()["c"]
        pages = max(1, (int(total) + per_page - 1) // per_page)
        page = min(page, pages)
        offset = (page - 1) * per_page
        rows = self.conn.execute(
            "SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (resolved_user_id, per_page, offset),
        ).fetchall()
        return rows, page, pages
