from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

from infra.chain_adapters.base import ChainAdapter, ChainDeposit


class EthRpcAdapter(ChainAdapter):
    def __init__(self, address_user_map: dict[str, int]) -> None:
        self.rpc_url = os.getenv("ETH_RPC_URL", "")
        self.address_user_map = {k.lower(): v for k, v in address_user_map.items()}

    def _rpc(self, method: str, params: list) -> dict:
        if not self.rpc_url:
            return {}
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
        req = Request(self.rpc_url, method="POST", headers={"Content-Type": "application/json"}, data=payload)
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    def fetch_deposits(self) -> list[ChainDeposit]:
        # Real implementation should process ETH transfers and ERC-20 Transfer logs.
        if not self.rpc_url:
            return []
        self._rpc("eth_blockNumber", [])
        return []

    def broadcast_raw_transaction(self, asset: str, raw_tx_hex: str) -> str:
        resp = self._rpc("eth_sendRawTransaction", [raw_tx_hex])
        return resp.get("result", "")
