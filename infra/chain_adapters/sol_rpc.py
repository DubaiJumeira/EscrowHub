from __future__ import annotations

import os

from infra.chain_adapters.base import ChainAdapter


class SolRpcAdapter(ChainAdapter):
    def __init__(self) -> None:
        self.rpc_url = os.getenv("SOL_RPC_URL", "")

    def fetch_deposits(self):
        return []
