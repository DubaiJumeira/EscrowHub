from __future__ import annotations

import os
from decimal import Decimal

import json
from urllib.request import Request, urlopen

from infra.chain_adapters.base import ChainAdapter, ChainDeposit


class EthRpcAdapter(ChainAdapter):
    def __init__(self, address_user_map: dict[str, int]) -> None:
        self.rpc_url = os.getenv("ETH_RPC_URL", "")
        self.address_user_map = {k.lower(): v for k, v in address_user_map.items()}
        self.min_conf = int(os.getenv("ETH_CONFIRMATIONS", "12"))

    def fetch_deposits(self) -> list[ChainDeposit]:
        # Production integration should index transfers via Alchemy/Infura webhooks or indexed logs.
        if not self.rpc_url:
            return []
        req = Request(self.rpc_url, method="POST", headers={"Content-Type": "application/json"}, data=json.dumps({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}).encode())
        with urlopen(req, timeout=10) as _:
            pass
        return []
