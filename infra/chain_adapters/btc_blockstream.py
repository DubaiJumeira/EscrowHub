from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from urllib.error import URLError
from urllib.request import Request, urlopen

from infra.chain_adapters.base import ChainAdapter, ChainDeposit


class BlockstreamUtxoAdapter(ChainAdapter):
    def __init__(self, asset: str, address_user_map: dict[str, int]) -> None:
        self.asset = asset.upper()
        self.base = self._resolve_base_url(primary=True)
        self.secondary_base = self._resolve_base_url(primary=False)
        self.address_user_map = address_user_map
        self.min_conf = int(os.getenv(f"{self.asset}_CONFIRMATIONS", "3"))

    def _resolve_base_url(self, primary: bool = True) -> str:
        asset = self.asset.upper()
        if asset == "BTC":
            if primary:
                return os.getenv("BLOCKSTREAM_BASE_URL", os.getenv("BTC_RPC_URL", "https://blockstream.info/api")).rstrip("/")
            return os.getenv("BLOCKSTREAM_SECONDARY_BASE_URL", "").strip().rstrip("/")
        if asset == "LTC":
            if primary:
                return os.getenv("LTC_RPC_URL", "https://litecoinspace.org/api").rstrip("/")
            return os.getenv("LTC_SECONDARY_RPC_URL", "").strip().rstrip("/")
        raise RuntimeError(f"unsupported UTXO adapter asset: {asset}")

    def _fetch_json(self, url: str) -> list[dict]:
        last_exc = None
        for attempt in range(3):
            try:
                with urlopen(url, timeout=20) as resp:
                    return json.loads(resp.read().decode())
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_exc = exc
                time.sleep(0.25 * (2**attempt))
        raise RuntimeError(f"{self.asset} adapter request failed url={url}") from last_exc

    def fetch_deposits(self) -> list[ChainDeposit]:
        out: list[ChainDeposit] = []
        for address, user_id in self.address_user_map.items():
            txs = self._fetch_json(f"{self.base}/address/{address}/txs")
            if self.secondary_base:
                secondary_txs = self._fetch_json(f"{self.secondary_base}/address/{address}/txs")
                secondary_txids = {str(t.get("txid")) for t in secondary_txs}
            else:
                secondary_txids = None
            for tx in txs:
                txid = tx["txid"]
                if secondary_txids is not None and txid not in secondary_txids:
                    continue
                conf = tx.get("status", {}).get("confirmations", 0)
                for idx, vout in enumerate(tx.get("vout", [])):
                    if vout.get("scriptpubkey_address") == address:
                        amount = Decimal(vout["value"]) / Decimal("100000000")
                        out.append(
                            ChainDeposit(
                                user_id=user_id,
                                asset=self.asset,
                                amount=amount,
                                txid=txid,
                                unique_key=f"{txid}:{idx}",
                                confirmations=conf,
                                finalized=conf >= self.min_conf,
                            )
                        )
        return out

    def broadcast_raw_transaction(self, asset: str, raw_tx_hex: str) -> str:
        req = Request(f"{self.base}/tx", method="POST", data=raw_tx_hex.encode())
        with urlopen(req, timeout=20) as resp:
            return resp.read().decode().strip()
