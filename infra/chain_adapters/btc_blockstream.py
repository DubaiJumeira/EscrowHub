from __future__ import annotations

import json
import os
from decimal import Decimal
from urllib.request import Request, urlopen

from infra.chain_adapters.base import ChainAdapter, ChainDeposit


class BlockstreamUtxoAdapter(ChainAdapter):
    def __init__(self, asset: str, address_user_map: dict[str, int]) -> None:
        self.asset = asset.upper()
        self.base = os.getenv("BLOCKSTREAM_BASE_URL", "https://blockstream.info/api")
        self.address_user_map = address_user_map
        self.min_conf = int(os.getenv(f"{self.asset}_CONFIRMATIONS", "3"))

    def fetch_deposits(self) -> list[ChainDeposit]:
        out: list[ChainDeposit] = []
        for address, user_id in self.address_user_map.items():
            with urlopen(f"{self.base}/address/{address}/txs", timeout=20) as resp:
                txs = json.loads(resp.read().decode())
            for tx in txs:
                txid = tx["txid"]
                conf = tx.get("status", {}).get("confirmations", 0)
                for idx, vout in enumerate(tx.get("vout", [])):
                    if vout.get("scriptpubkey_address") == address:
                        amount = Decimal(vout["value"]) / Decimal("100000000")
                        out.append(ChainDeposit(user_id, self.asset, amount, txid, f"{txid}:{idx}", conf, conf >= self.min_conf))
        return out

    def broadcast_raw_transaction(self, asset: str, raw_tx_hex: str) -> str:
        req = Request(f"{self.base}/tx", method="POST", data=raw_tx_hex.encode())
        with urlopen(req, timeout=20) as resp:
            return resp.read().decode().strip()
