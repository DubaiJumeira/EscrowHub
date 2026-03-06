from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Balance:
    available: Decimal = Decimal("0")
    locked: Decimal = Decimal("0")


class WalletService:
    """Internal ledger-backed wallet operations (in-memory dev implementation)."""

    def __init__(self) -> None:
        self._balances: dict[tuple[int, str], Balance] = {}
        self.ledger: list[dict] = []

    def _get_balance(self, user_id: int, asset: str) -> Balance:
        key = (user_id, asset.upper())
        if key not in self._balances:
            self._balances[key] = Balance()
        return self._balances[key]

    def credit_deposit(self, user_id: int, asset: str, amount: Decimal, tx_ref: str, idempotency_key: str) -> None:
        bal = self._get_balance(user_id, asset)
        bal.available += Decimal(amount)
        self.ledger.append(
            {
                "user_id": user_id,
                "asset": asset.upper(),
                "amount": Decimal(amount),
                "direction": "credit",
                "entry_type": "deposit",
                "reference": tx_ref,
                "idempotency_key": idempotency_key,
            }
        )

    def lock_funds(self, user_id: int, asset: str, amount: Decimal, escrow_id: int, idempotency_key: str) -> None:
        bal = self._get_balance(user_id, asset)
        amount = Decimal(amount)
        if bal.available < amount:
            raise ValueError("insufficient available balance")
        bal.available -= amount
        bal.locked += amount
        self.ledger.append(
            {
                "user_id": user_id,
                "asset": asset.upper(),
                "amount": amount,
                "direction": "debit",
                "entry_type": "lock",
                "reference": f"escrow:{escrow_id}",
                "idempotency_key": idempotency_key,
            }
        )

    def release_locked(self, user_id: int, asset: str, amount: Decimal, escrow_id: int, idempotency_key: str) -> None:
        bal = self._get_balance(user_id, asset)
        amount = Decimal(amount)
        if bal.locked < amount:
            raise ValueError("insufficient locked balance")
        bal.locked -= amount
        bal.available += amount
        self.ledger.append(
            {
                "user_id": user_id,
                "asset": asset.upper(),
                "amount": amount,
                "direction": "credit",
                "entry_type": "unlock",
                "reference": f"escrow:{escrow_id}",
                "idempotency_key": idempotency_key,
            }
        )

    def debit_withdrawal(self, user_id: int, asset: str, amount: Decimal, withdrawal_id: int, idempotency_key: str) -> None:
        bal = self._get_balance(user_id, asset)
        amount = Decimal(amount)
        if bal.available < amount:
            raise ValueError("insufficient available balance")
        bal.available -= amount
        self.ledger.append(
            {
                "user_id": user_id,
                "asset": asset.upper(),
                "amount": amount,
                "direction": "debit",
                "entry_type": "withdrawal",
                "reference": f"withdrawal:{withdrawal_id}",
                "idempotency_key": idempotency_key,
            }
        )

    def balance_snapshot(self, user_id: int, asset: str) -> Balance:
        return self._get_balance(user_id, asset)
