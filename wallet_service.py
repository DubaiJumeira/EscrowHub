from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

SUPPORTED_ASSETS = {"BTC", "ETH", "LTC", "USDT", "USDC", "SOL", "XRP"}


@dataclass
class WalletAddress:
    user_id: int
    asset: str
    address: str
    xrp_destination_tag: str | None = None


@dataclass
class Balance:
    available: Decimal = Decimal("0")
    locked: Decimal = Decimal("0")


class SignerService:
    """Broadcast service boundary (no private keys in Telegram bot runtime)."""

    def sign_and_broadcast(self, asset: str, to_address: str, amount: Decimal) -> str:
        return f"mock_{asset.lower()}_{to_address[-6:]}_{amount}"


class WalletService:
    def __init__(self, signer_service: SignerService | None = None) -> None:
        self._balances: dict[tuple[int, str], Balance] = {}
        self._addresses: dict[tuple[int, str], WalletAddress] = {}
        self.ledger_entries: list[dict] = []
        self.deposits: list[dict] = []
        self.withdrawals: list[dict] = []
        self._signer = signer_service or SignerService()

    @staticmethod
    def _normalize_asset(asset: str) -> str:
        symbol = asset.upper()
        if symbol not in SUPPORTED_ASSETS:
            raise ValueError(f"unsupported asset: {symbol}")
        return symbol

    def _balance(self, user_id: int, asset: str) -> Balance:
        key = (user_id, self._normalize_asset(asset))
        if key not in self._balances:
            self._balances[key] = Balance()
        return self._balances[key]

    def assign_deposit_address(self, user_id: int, asset: str) -> WalletAddress:
        symbol = self._normalize_asset(asset)
        key = (user_id, symbol)
        if key in self._addresses:
            return self._addresses[key]
        if symbol == "XRP":
            wallet = WalletAddress(user_id=user_id, asset=symbol, address="xrp_shared_hot_wallet", xrp_destination_tag=str(user_id))
        else:
            wallet = WalletAddress(user_id=user_id, asset=symbol, address=f"{symbol.lower()}_deposit_{user_id}")
        self._addresses[key] = wallet
        return wallet

    def credit_deposit(self, user_id: int, asset: str, amount: Decimal, tx_hash: str, confirmations: int) -> None:
        symbol = self._normalize_asset(asset)
        bal = self._balance(user_id, symbol)
        amount = Decimal(amount)
        bal.available += amount
        self.deposits.append({"user_id": user_id, "asset": symbol, "amount": amount, "tx_hash": tx_hash, "confirmations": confirmations})
        self.ledger_entries.append({"type": "deposit_credit", "user_id": user_id, "asset": symbol, "amount": amount, "ref": tx_hash})

    def lock_funds(self, user_id: int, asset: str, amount: Decimal, escrow_id: int) -> None:
        symbol = self._normalize_asset(asset)
        bal = self._balance(user_id, symbol)
        amount = Decimal(amount)
        if bal.available < amount:
            raise ValueError("insufficient available balance")
        bal.available -= amount
        bal.locked += amount
        self.ledger_entries.append({"type": "escrow_lock", "user_id": user_id, "asset": symbol, "amount": amount, "ref": escrow_id})

    def move_locked_to_user(self, from_user_id: int, to_user_id: int, asset: str, amount: Decimal, escrow_id: int) -> None:
        symbol = self._normalize_asset(asset)
        amount = Decimal(amount)
        from_bal = self._balance(from_user_id, symbol)
        if from_bal.locked < amount:
            raise ValueError("insufficient locked balance")
        from_bal.locked -= amount
        to_bal = self._balance(to_user_id, symbol)
        to_bal.available += amount
        self.ledger_entries.append({"type": "escrow_release", "user_id": to_user_id, "asset": symbol, "amount": amount, "ref": escrow_id})

    def credit_platform_earning(self, asset: str, amount: Decimal, escrow_id: int) -> None:
        self.ledger_entries.append({"type": "platform_fee", "asset": self._normalize_asset(asset), "amount": Decimal(amount), "ref": escrow_id})

    def credit_bot_owner_earning(self, owner_user_id: int, asset: str, amount: Decimal, escrow_id: int) -> None:
        symbol = self._normalize_asset(asset)
        bal = self._balance(owner_user_id, symbol)
        fee = Decimal(amount)
        bal.available += fee
        self.ledger_entries.append({"type": "bot_owner_fee", "user_id": owner_user_id, "asset": symbol, "amount": fee, "ref": escrow_id})

    def create_withdrawal(self, user_id: int, asset: str, amount: Decimal, to_address: str) -> dict:
        symbol = self._normalize_asset(asset)
        amount = Decimal(amount)
        bal = self._balance(user_id, symbol)
        if bal.available < amount:
            raise ValueError("insufficient available balance")
        bal.available -= amount
        tx_hash = self._signer.sign_and_broadcast(symbol, to_address, amount)
        withdrawal = {
            "withdrawal_id": len(self.withdrawals) + 1,
            "user_id": user_id,
            "asset": symbol,
            "amount": amount,
            "to_address": to_address,
            "tx_hash": tx_hash,
            "status": "broadcasted",
        }
        self.withdrawals.append(withdrawal)
        self.ledger_entries.append({"type": "withdrawal_debit", "user_id": user_id, "asset": symbol, "amount": amount, "ref": withdrawal["withdrawal_id"]})
        return withdrawal

    def get_balances_for_user(self, user_id: int) -> dict[str, Balance]:
        result: dict[str, Balance] = {}
        for (uid, asset), bal in self._balances.items():
            if uid == user_id:
                result[asset] = bal
        return result
