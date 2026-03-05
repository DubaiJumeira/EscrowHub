from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.fees.service import FeeBreakdown, FeeService
from core.pricing.service import PriceService, validate_minimum_usd
from core.wallet_engine.service import WalletService


@dataclass
class EscrowRecord:
    escrow_id: int
    tenant_bot_id: int
    buyer_id: int
    seller_id: int
    asset: str
    amount: Decimal
    description: str
    status: str
    fee_breakdown: FeeBreakdown


class EscrowService:
    def __init__(
        self,
        wallet_service: WalletService,
        fee_service: FeeService,
        price_service: PriceService,
    ) -> None:
        self.wallet_service = wallet_service
        self.fee_service = fee_service
        self.price_service = price_service
        self._escrows: dict[int, EscrowRecord] = {}
        self._next_id = 1

    def create_escrow(
        self,
        tenant_bot_id: int,
        buyer_id: int,
        seller_id: int,
        amount: Decimal,
        asset: str,
        description: str,
        bot_service_fee_percent: Decimal,
    ) -> EscrowRecord:
        validate_minimum_usd(self.price_service, asset=asset, amount=Decimal(amount), minimum_usd=Decimal("40"))
        fee = self.fee_service.calculate_fee(Decimal(amount), Decimal(bot_service_fee_percent))
        escrow_id = self._next_id

        self.wallet_service.lock_funds(
            user_id=buyer_id,
            asset=asset,
            amount=Decimal(amount),
            escrow_id=escrow_id,
            idempotency_key=f"escrow-lock-{escrow_id}",
        )

        record = EscrowRecord(
            escrow_id=escrow_id,
            tenant_bot_id=tenant_bot_id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            asset=asset.upper(),
            amount=Decimal(amount),
            description=description,
            status="active",
            fee_breakdown=fee,
        )
        self._escrows[escrow_id] = record
        self._next_id += 1
        return record

    def release(self, escrow_id: int) -> EscrowRecord:
        escrow = self._escrows[escrow_id]
        if escrow.status != "active":
            raise ValueError("escrow must be active")

        self.wallet_service.release_locked(
            user_id=escrow.buyer_id,
            asset=escrow.asset,
            amount=escrow.amount,
            escrow_id=escrow_id,
            idempotency_key=f"escrow-unlock-{escrow_id}",
        )
        self.wallet_service.debit_withdrawal(
            user_id=escrow.buyer_id,
            asset=escrow.asset,
            amount=escrow.amount,
            withdrawal_id=escrow_id,
            idempotency_key=f"escrow-transfer-out-{escrow_id}",
        )
        self.wallet_service.credit_deposit(
            user_id=escrow.seller_id,
            asset=escrow.asset,
            amount=escrow.fee_breakdown.seller_payout,
            tx_ref=f"escrow:{escrow_id}",
            idempotency_key=f"escrow-transfer-in-{escrow_id}",
        )

        escrow.status = "completed"
        return escrow

    def dispute(self, escrow_id: int, reason: str) -> EscrowRecord:
        escrow = self._escrows[escrow_id]
        if escrow.status != "active":
            raise ValueError("only active escrows can be disputed")
        if not reason.strip():
            raise ValueError("dispute reason is required")
        escrow.status = "disputed"
        return escrow

    def get_escrow(self, escrow_id: int) -> EscrowRecord | None:
        return self._escrows.get(escrow_id)
