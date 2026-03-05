from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal


class PriceService(ABC):
    """Returns USD quote for 1 unit of the given asset symbol."""

    @abstractmethod
    def get_usd_price(self, asset: str) -> Decimal:
        raise NotImplementedError


class StaticPriceService(PriceService):
    """Dev/test price service. Replace with CoinGecko adapter in production."""

    def __init__(self, prices: dict[str, Decimal]) -> None:
        self._prices = {k.upper(): Decimal(v) for k, v in prices.items()}

    def get_usd_price(self, asset: str) -> Decimal:
        symbol = asset.upper()
        if symbol not in self._prices:
            raise ValueError(f"price not available for {symbol}")
        return self._prices[symbol]


def validate_minimum_usd(price_service: PriceService, asset: str, amount: Decimal, minimum_usd: Decimal = Decimal("40")) -> None:
    usd_value = Decimal(amount) * price_service.get_usd_price(asset)
    if usd_value < Decimal(minimum_usd):
        raise ValueError(f"minimum escrow size is ${minimum_usd} USD equivalent")
