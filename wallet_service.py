from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

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
class WalletAddress:
    user_id: int
    asset: str
    chain_family: str
    address: str
    derivation_index: int | None = None
    destination_tag: str | None = None


class WalletService:
    def __init__(self) -> None:
        self.ledger_entries: list[dict] = []
        self.escrow_locks: dict[int, dict] = {}
        self.wallet_addresses: dict[tuple[int, str], WalletAddress] = {}
        self.xrp_shared_address = "xrp_platform_receive_address"
        self.deposits: dict[str, dict] = {}
        self.withdrawals: dict[int, dict] = {}
        self.platform_payout_addresses: dict[str, str] = {}
        self.bot_owner_payout_addresses: dict[tuple[int, str], str] = {}

    @staticmethod
    def _asset(asset: str) -> str:
        symbol = asset.upper()
        if symbol not in SUPPORTED_ASSETS:
            raise ValueError(f"unsupported asset: {symbol}")
        return symbol

    def _chain_family(self, asset: str) -> str:
        symbol = self._asset(asset)
        if symbol in {"USDT", "USDC", "ETH"}:
            return "ETHEREUM"
        return symbol

    def get_or_create_deposit_address(self, user_id: int, asset: str) -> WalletAddress:
        symbol = self._asset(asset)
        key = (user_id, symbol)
        if key in self.wallet_addresses:
            return self.wallet_addresses[key]

        if symbol == "XRP":
            addr = WalletAddress(user_id=user_id, asset=symbol, chain_family="XRP", address=self.xrp_shared_address, destination_tag=str(user_id))
        else:
            addr = WalletAddress(
                user_id=user_id,
                asset=symbol,
                chain_family=self._chain_family(symbol),
                address=f"{symbol.lower()}_addr_{user_id}",
                derivation_index=user_id,
            )
        self.wallet_addresses[key] = addr
        return addr

    def credit_deposit_if_confirmed(
        self,
        user_id: int,
        asset: str,
        amount: Decimal,
        txid: str,
        chain_family: str,
        unique_key: str,
        confirmations: int,
        finalized: bool,
    ) -> bool:
        if unique_key in self.deposits:
            return False

        status = "confirmed" if finalized else "seen"
        deposit = {
            "id": len(self.deposits) + 1,
            "user_id": user_id,
            "asset": self._asset(asset),
            "amount": Decimal(amount),
            "txid": txid,
            "chain_family": chain_family,
            "unique_key": unique_key,
            "confirmations": confirmations,
            "status": status,
        }
        self.deposits[unique_key] = deposit

        if finalized:
            deposit["status"] = "credited"
            self._append_ledger(
                user_id=user_id,
                account_type="USER",
                account_owner_id=user_id,
                asset=asset,
                amount=Decimal(amount),
                entry_type="DEPOSIT",
                ref_type="deposit",
                ref_id=deposit["id"],
            )
            return True
        return False

    def _append_ledger(
        self,
        user_id: int | None,
        account_type: str,
        account_owner_id: int | None,
        asset: str,
        amount: Decimal,
        entry_type: str,
        ref_type: str,
        ref_id: int,
    ) -> None:
        self.ledger_entries.append(
            {
                "id": len(self.ledger_entries) + 1,
                "user_id": user_id,
                "account_type": account_type,
                "account_owner_id": account_owner_id,
                "asset": self._asset(asset),
                "amount": Decimal(amount),
                "entry_type": entry_type,
                "ref_type": ref_type,
                "ref_id": ref_id,
            }
        )

    def total_balance(self, user_id: int, asset: str) -> Decimal:
        symbol = self._asset(asset)
        total = Decimal("0")
        for e in self.ledger_entries:
            if e["account_type"] == "USER" and e["account_owner_id"] == user_id and e["asset"] == symbol:
                total += e["amount"]
        return total

    def locked_balance(self, user_id: int, asset: str) -> Decimal:
        symbol = self._asset(asset)
        total = Decimal("0")
        for lock in self.escrow_locks.values():
            if lock["user_id"] == user_id and lock["asset"] == symbol and lock["status"] == "locked":
                total += lock["amount"]
        for w in self.withdrawals.values():
            if w["user_id"] == user_id and w["asset"] == symbol and w["status"] == "pending":
                total += w["amount"]
        return total

    def available_balance(self, user_id: int, asset: str) -> Decimal:
        return self.total_balance(user_id, asset) - self.locked_balance(user_id, asset)

    def lock_for_escrow(self, escrow_id: int, user_id: int, asset: str, amount: Decimal) -> None:
        amount = Decimal(amount)
        if self.available_balance(user_id, asset) < amount:
            raise ValueError("insufficient available balance")
        self.escrow_locks[escrow_id] = {
            "escrow_id": escrow_id,
            "user_id": user_id,
            "asset": self._asset(asset),
            "amount": amount,
            "status": "locked",
        }
        self._append_ledger(user_id, "USER", user_id, asset, Decimal("0"), "ESCROW_LOCK", "escrow", escrow_id)

    def release_escrow(self, escrow_id: int, seller_id: int, platform_fee: Decimal, bot_fee: Decimal, seller_payout: Decimal, bot_owner_id: int, asset: str) -> None:
        lock = self.escrow_locks.get(escrow_id)
        if not lock or lock["status"] != "locked":
            raise ValueError("escrow lock missing")
        lock["status"] = "released"

        self._append_ledger(lock["user_id"], "USER", lock["user_id"], asset, -Decimal(lock["amount"]), "ESCROW_RELEASE", "escrow", escrow_id)
        self._append_ledger(seller_id, "USER", seller_id, asset, Decimal(seller_payout), "ESCROW_RELEASE", "escrow", escrow_id)
        self._append_ledger(None, "PLATFORM_REVENUE", None, asset, Decimal(platform_fee), "PLATFORM_FEE", "escrow", escrow_id)
        self._append_ledger(bot_owner_id, "BOT_OWNER_REVENUE", bot_owner_id, asset, Decimal(bot_fee), "BOT_FEE", "escrow", escrow_id)

    def cancel_escrow_lock(self, escrow_id: int) -> None:
        lock = self.escrow_locks.get(escrow_id)
        if not lock or lock["status"] != "locked":
            raise ValueError("escrow lock missing")
        lock["status"] = "released"
        self._append_ledger(lock["user_id"], "USER", lock["user_id"], lock["asset"], Decimal("0"), "ADJUSTMENT", "escrow", escrow_id)

    def request_withdrawal(self, user_id: int, asset: str, amount: Decimal, destination_address: str) -> dict:
        amount = Decimal(amount)
        if self.available_balance(user_id, asset) < amount:
            raise ValueError("insufficient available balance")
        wid = len(self.withdrawals) + 1
        rec = {
            "id": wid,
            "user_id": user_id,
            "asset": self._asset(asset),
            "amount": amount,
            "destination_address": destination_address,
            "status": "pending",
            "txid": None,
        }
        self.withdrawals[wid] = rec
        self._append_ledger(user_id, "USER", user_id, asset, -amount, "WITHDRAWAL_RESERVE", "withdrawal", wid)
        return rec

    def mark_withdrawal_broadcasted(self, withdrawal_id: int, txid: str) -> None:
        rec = self.withdrawals[withdrawal_id]
        rec["status"] = "broadcasted"
        rec["txid"] = txid
        self._append_ledger(rec["user_id"], "USER", rec["user_id"], rec["asset"], Decimal("0"), "WITHDRAWAL_SENT", "withdrawal", withdrawal_id)

    def account_revenue_balance(self, account_type: str, owner_id: int | None, asset: str) -> Decimal:
        symbol = self._asset(asset)
        total = Decimal("0")
        for e in self.ledger_entries:
            if e["account_type"] == account_type and e["asset"] == symbol:
                if account_type == "BOT_OWNER_REVENUE" and e["account_owner_id"] != owner_id:
                    continue
                total += e["amount"]
        return total

    def set_platform_payout_address(self, asset: str, address: str) -> None:
        self.platform_payout_addresses[self._asset(asset)] = address

    def set_bot_owner_payout_address(self, owner_user_id: int, asset: str, address: str) -> None:
        self.bot_owner_payout_addresses[(owner_user_id, self._asset(asset))] = address

    def withdraw_platform_revenue(self, asset: str, amount: Decimal) -> dict:
        balance = self.account_revenue_balance("PLATFORM_REVENUE", None, asset)
        if balance < Decimal(amount):
            raise ValueError("insufficient platform revenue")
        address = self.platform_payout_addresses.get(self._asset(asset))
        if not address:
            raise ValueError("platform payout address not configured")
        wid = len(self.withdrawals) + 1
        self.withdrawals[wid] = {"id": wid, "user_id": None, "asset": self._asset(asset), "amount": Decimal(amount), "destination_address": address, "status": "pending", "txid": None}
        self._append_ledger(None, "PLATFORM_REVENUE", None, asset, -Decimal(amount), "WITHDRAWAL_RESERVE", "withdrawal", wid)
        return self.withdrawals[wid]

    def withdraw_bot_owner_revenue(self, owner_user_id: int, asset: str, amount: Decimal) -> dict:
        balance = self.account_revenue_balance("BOT_OWNER_REVENUE", owner_user_id, asset)
        if balance < Decimal(amount):
            raise ValueError("insufficient bot owner revenue")
        address = self.bot_owner_payout_addresses.get((owner_user_id, self._asset(asset)))
        if not address:
            raise ValueError("bot owner payout address not configured")
        wid = len(self.withdrawals) + 1
        self.withdrawals[wid] = {"id": wid, "user_id": owner_user_id, "asset": self._asset(asset), "amount": Decimal(amount), "destination_address": address, "status": "pending", "txid": None}
        self._append_ledger(owner_user_id, "BOT_OWNER_REVENUE", owner_user_id, asset, -Decimal(amount), "WITHDRAWAL_RESERVE", "withdrawal", wid)
        return self.withdrawals[wid]
