from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal


SUPPORTED_ASSETS = {"BTC", "ETH", "LTC", "USDT", "USDC", "SOL", "XRP"}


@dataclass
class InboundTx:
    tx_hash: str
    address: str
    asset: str
    amount: Decimal
    confirmations: int


class ChainAdapter(ABC):
    @abstractmethod
    def assign_deposit_address(self, user_id: int, asset: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_inbound_transactions(self, address: str, asset: str) -> list[InboundTx]:
        raise NotImplementedError

    @abstractmethod
    def broadcast_withdrawal(self, asset: str, to_address: str, amount: Decimal) -> str:
        raise NotImplementedError
