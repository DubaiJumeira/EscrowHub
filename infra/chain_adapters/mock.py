from __future__ import annotations

from decimal import Decimal

from infra.chain_adapters.base import ChainAdapter, InboundTx


class MockChainAdapter(ChainAdapter):
    """Dev-only adapter. Replace with real per-chain providers in production."""

    def assign_deposit_address(self, user_id: int, asset: str) -> str:
        return f"mock_{asset.lower()}_{user_id}_addr"

    def get_inbound_transactions(self, address: str, asset: str) -> list[InboundTx]:
        return []

    def broadcast_withdrawal(self, asset: str, to_address: str, amount: Decimal) -> str:
        return f"mock_tx_{asset.lower()}_{to_address[-6:]}_{amount}"
