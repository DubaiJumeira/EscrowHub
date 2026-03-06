from __future__ import annotations

import time
from abc import ABC, abstractmethod
from decimal import Decimal

import json
from urllib.parse import urlencode
from urllib.request import urlopen

MIN_ESCROW_USD = Decimal("40")

ASSET_TO_COINGECKO_ID = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "LTC": "litecoin",
    "USDT": "tether",
    "USDC": "usd-coin",
    "SOL": "solana",
    "XRP": "ripple",
}


class PriceService(ABC):
    @abstractmethod
    def get_usd_price(self, asset: str) -> Decimal:
        raise NotImplementedError

    def get_usd_value(self, asset: str, amount: Decimal) -> Decimal:
        return Decimal(amount) * self.get_usd_price(asset)


class CoinGeckoPriceService(PriceService):
    def __init__(self, ttl_seconds: int = 60) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, Decimal]] = {}

    def get_usd_price(self, asset: str) -> Decimal:
        symbol = asset.upper()
        now = time.time()
        hit = self._cache.get(symbol)
        if hit and now - hit[0] < self.ttl_seconds:
            return hit[1]

        coin_id = ASSET_TO_COINGECKO_ID[symbol]
        url = "https://api.coingecko.com/api/v3/simple/price"
        query = urlencode({"ids": coin_id, "vs_currencies": "usd"})
        with urlopen(f"{url}?{query}", timeout=10) as resp:
            data = json.loads(resp.read().decode())
        price = Decimal(str(data[coin_id]["usd"]))
        self._cache[symbol] = (now, price)
        return price


class StaticPriceService(PriceService):
    def __init__(self, prices: dict[str, Decimal]) -> None:
        self._prices = {k.upper(): Decimal(v) for k, v in prices.items()}

    def get_usd_price(self, asset: str) -> Decimal:
        symbol = asset.upper()
        if symbol not in self._prices:
            raise ValueError(f"price not available for {symbol}")
        return self._prices[symbol]


def validate_minimum_escrow_usd(price_service: PriceService, asset: str, amount: Decimal) -> None:
    if price_service.get_usd_value(asset, amount) < MIN_ESCROW_USD:
        raise ValueError("minimum escrow amount is $40 USD equivalent")
