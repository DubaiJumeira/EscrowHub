from __future__ import annotations

from infra.chain_adapters.base import ChainAdapter, ChainDeposit


class MockChainAdapter(ChainAdapter):
    def fetch_deposits(self) -> list[ChainDeposit]:
        return []

    def broadcast_raw_transaction(self, asset: str, raw_tx_hex: str) -> str:
        return f"mock_txid_{asset.lower()}"
