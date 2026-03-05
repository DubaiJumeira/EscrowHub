from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

MIN_ESCROW_USD = Decimal("40")


class PriceService(ABC):
    @abstractmethod
    def get_usd_price(self, asset: str) -> Decimal:
        raise NotImplementedError


class StaticPriceService(PriceService):
    def __init__(self, prices: dict[str, Decimal]) -> None:
        self._prices = {k.upper(): Decimal(v) for k, v in prices.items()}

    def get_usd_price(self, asset: str) -> Decimal:
        symbol = asset.upper()
        if symbol not in self._prices:
            raise ValueError(f"price not available for {symbol}")
        return self._prices[symbol]


def validate_minimum_escrow_usd(price_service: PriceService, asset: str, amount: Decimal) -> None:
    usd_value = Decimal(amount) * price_service.get_usd_price(asset)
    if usd_value < MIN_ESCROW_USD:
        raise ValueError("minimum escrow amount is $40 USD equivalent")
