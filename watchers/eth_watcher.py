from __future__ import annotations

from decimal import Decimal

from wallet_service import WalletService

REQUIRED_CONFIRMATIONS = 12


class EthWatcher:
    """Ethereum watcher skeleton supporting ETH + ERC-20 deposit idempotency."""

    def __init__(self, wallet_service: WalletService) -> None:
        self.wallet_service = wallet_service

    def process_erc20_transfer(self, user_id: int, asset: str, amount: Decimal, txhash: str, log_index: int, confirmations: int) -> bool:
        unique_key = f"{txhash}:{log_index}"
        finalized = confirmations >= REQUIRED_CONFIRMATIONS
        return self.wallet_service.credit_deposit_if_confirmed(
            user_id=user_id,
            asset=asset,
            amount=Decimal(amount),
            txid=txhash,
            chain_family="ETHEREUM",
            unique_key=unique_key,
            confirmations=confirmations,
            finalized=finalized,
        )
