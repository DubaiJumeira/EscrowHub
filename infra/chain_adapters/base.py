from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class ChainDeposit:
    user_id: int
    asset: str
    amount: Decimal
    txid: str
    unique_key: str
    confirmations: int
    finalized: bool


class ChainAdapter:
    def fetch_deposits(self) -> list[ChainDeposit]:
        raise NotImplementedError
