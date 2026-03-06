from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from typing import Any
from urllib.request import Request, urlopen

from infra.chain_adapters.base import ChainAdapter, ChainDeposit


class XrpRpcAdapter(ChainAdapter):
    def __init__(self, platform_receive_address: str, destination_tag_user_map: dict[str, int]) -> None:
        self.rpc_url = os.getenv("XRP_RPC_URL", "")
        self.platform_receive_address = platform_receive_address
        self.destination_tag_user_map = destination_tag_user_map

    def _rpc(self, method: str, params: dict[str, Any], retries: int = 3) -> Any:
        if not self.rpc_url:
            raise RuntimeError("XRP_RPC_URL not configured")
        req = Request(
            self.rpc_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"method": method, "params": [params]}).encode(),
        )
        delay = 0.5
        for i in range(retries):
            try:
                with urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode())
                return data["result"]
            except Exception:
                if i == retries - 1:
                    raise
                time.sleep(delay)
                delay *= 2

    def get_latest_ledger_index(self) -> int:
        return int(self._rpc("ledger", {"ledger_index": "validated"})["ledger_index"])

    def scan_account_transactions(self, marker: Any = None) -> tuple[list[dict[str, Any]], Any]:
        params: dict[str, Any] = {
            "account": self.platform_receive_address,
            "ledger_index_min": -1,
            "ledger_index_max": -1,
            "binary": False,
            "limit": 200,
            "forward": False,
        }
        if marker is not None:
            params["marker"] = marker
        res = self._rpc("account_tx", params)
        return list(res.get("transactions", [])), res.get("marker")

    def fetch_deposits_from_marker(self, marker: Any = None) -> tuple[list[ChainDeposit], Any]:
        if not self.rpc_url:
            return [], marker
        txs, new_marker = self.scan_account_transactions(marker)
        out: list[ChainDeposit] = []
        for row in txs:
            tx = row.get("tx", {})
            meta = row.get("meta", {})
            if tx.get("TransactionType") != "Payment":
                continue
            if tx.get("Destination") != self.platform_receive_address:
                continue
            tag = str(tx.get("DestinationTag", ""))
            if tag not in self.destination_tag_user_map:
                continue
            if not row.get("validated"):
                continue
            amount_drops = tx.get("Amount")
            if not isinstance(amount_drops, str):
                continue
            amount = Decimal(amount_drops) / Decimal("1000000")
            txhash = tx.get("hash")
            user_id = self.destination_tag_user_map[tag]
            out.append(ChainDeposit(user_id, "XRP", amount, txhash, f"{txhash}:{tag}", 1, True))
        return out, new_marker

    def fetch_deposits(self) -> list[ChainDeposit]:
        deps, _ = self.fetch_deposits_from_marker(None)
        return deps
