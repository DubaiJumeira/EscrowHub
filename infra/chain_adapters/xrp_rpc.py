from __future__ import annotations

import os

from infra.chain_adapters.base import ChainAdapter


class XrpRpcAdapter(ChainAdapter):
    def __init__(self) -> None:
        self.rpc_url = os.getenv("XRP_RPC_URL", "")

    def fetch_deposits(self):
        return []
