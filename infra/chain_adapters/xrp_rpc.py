from __future__ import annotations

import json
import os
from decimal import Decimal
from urllib.request import Request, urlopen

from infra.chain_adapters.base import ChainAdapter, ChainDeposit


class XrpRpcAdapter(ChainAdapter):
    def __init__(self, destination_tag_user_map: dict[str, int]) -> None:
        self.rpc_url = os.getenv("XRP_RPC_URL", "")
        self.hot_wallet = os.getenv("XRP_HOT_WALLET_ADDRESS", "")
        self.tag_map = destination_tag_user_map

    def fetch_deposits(self) -> list[ChainDeposit]:
        if not self.rpc_url or not self.hot_wallet:
            return []
        payload = {"method": "account_tx", "params": [{"account": self.hot_wallet, "ledger_index_min": -1, "ledger_index_max": -1, "limit": 100}]}
        req = Request(self.rpc_url, method="POST", headers={"Content-Type": "application/json"}, data=json.dumps(payload).encode())
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        out: list[ChainDeposit] = []
        for tx in data.get("result", {}).get("transactions", []):
            t = tx.get("tx", {})
            if t.get("TransactionType") != "Payment":
                continue
            tag = str(t.get("DestinationTag", ""))
            user_id = self.tag_map.get(tag)
            if not user_id:
                continue
            if t.get("Destination") != self.hot_wallet:
                continue
            amount_drops = Decimal(str(t.get("Amount", "0")))
            amount = amount_drops / Decimal("1000000")
            txid = t.get("hash")
            validated = tx.get("validated", False)
            out.append(ChainDeposit(user_id, "XRP", amount, txid, f"{txid}:{tag}", 1 if validated else 0, bool(validated)))
        return out

    def broadcast_raw_transaction(self, asset: str, raw_tx_hex: str) -> str:
        return ""
