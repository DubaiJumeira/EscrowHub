from __future__ import annotations

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MIN_ESCROW_USD = Decimal("40")

ASSET_TO_COINGECKO_ID = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "LTC": "litecoin",
    "USDT": "tether",
}


class PriceService(ABC):
    @abstractmethod
    def get_usd_price(self, asset: str) -> Decimal:
        raise NotImplementedError

    def get_usd_value(self, asset: str, amount: Decimal) -> Decimal:
        return Decimal(amount) * self.get_usd_price(asset)

    async def get_usd_price_async(self, asset: str) -> Decimal:
        return await asyncio.to_thread(self.get_usd_price, asset)

    async def get_usd_value_async(self, asset: str, amount: Decimal) -> Decimal:
        return Decimal(amount) * await self.get_usd_price_async(asset)


class CoinGeckoPriceService(PriceService):
    def __init__(self, ttl_seconds: int = 180) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, Decimal]] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": os.getenv(
                "ESCROWHUB_HTTP_USER_AGENT",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 EscrowHub/1.0",
            ),
            "Accept": "application/json",
            "Connection": "close",
        }

    def get_usd_price(self, asset: str) -> Decimal:
        symbol = asset.upper()
        now = time.time()
        hit = self._cache.get(symbol)
        if hit and now - hit[0] < self.ttl_seconds:
            return hit[1]

        coin_id = ASSET_TO_COINGECKO_ID[symbol]
        url = "https://api.coingecko.com/api/v3/simple/price"
        query = urlencode({"ids": coin_id, "vs_currencies": "usd"})

        last_exc = None
        for attempt in range(3):
            try:
                req = Request(f"{url}?{query}", headers=self._headers())
                with urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                price = Decimal(str(data[coin_id]["usd"]))
                self._cache[symbol] = (time.time(), price)
                return price
            except HTTPError as exc:
                last_exc = exc
                if hit:
                    return hit[1]
                if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                    break
                time.sleep(0.5 * (2 ** attempt))
            except (URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
                last_exc = exc
                if hit:
                    return hit[1]
                time.sleep(0.5 * (2 ** attempt))

        raise RuntimeError(f"price lookup failed for {symbol}") from last_exc


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
