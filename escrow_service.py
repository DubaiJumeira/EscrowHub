"""Backward-compatible facade around the new core escrow engine."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.fees.service import FeeService


@dataclass
class PartnerProfile:
    owner_id: int
    display_name: str
    service_fee_percent: Decimal


@dataclass
class EscrowTransaction:
    tx_id: int
    partner_id: int
    buyer_id: int
    seller_id: int
    total_amount: Decimal
    hold_amount: Decimal
    service_fee_total: Decimal
    platform_fee: Decimal
    partner_fee: Decimal
    seller_payout: Decimal
    status: str = "pending"


class EscrowService:
    """Legacy service retained for compatibility with initial bot handlers."""

    def __init__(self) -> None:
        self._partners: dict[int, PartnerProfile] = {}
        self._transactions: dict[int, EscrowTransaction] = {}
        self._next_tx_id = 1
        self._fee = FeeService()

    def create_partner(self, owner_id: int, display_name: str, service_fee_percent: Decimal) -> PartnerProfile:
        service_fee_percent = Decimal(service_fee_percent)
        if service_fee_percent < 0:
            raise ValueError("Service fee cannot be negative")
        if service_fee_percent > 100:
            raise ValueError("Service fee cannot exceed 100%")

        profile = PartnerProfile(owner_id=owner_id, display_name=display_name, service_fee_percent=service_fee_percent)
        self._partners[owner_id] = profile
        return profile

    def get_partner(self, owner_id: int) -> PartnerProfile | None:
        return self._partners.get(owner_id)

    def create_transaction(self, partner_id: int, buyer_id: int, seller_id: int, total_amount: Decimal) -> EscrowTransaction:
        partner = self._partners.get(partner_id)
        if not partner:
            raise ValueError("Partner profile does not exist")

        if Decimal(total_amount) <= 0:
            raise ValueError("Total amount must be positive")

        fee = self._fee.calculate_fee(Decimal(total_amount), partner.service_fee_percent)
        tx = EscrowTransaction(
            tx_id=self._next_tx_id,
            partner_id=partner_id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            total_amount=Decimal(total_amount),
            hold_amount=fee.escrow_fee,
            service_fee_total=fee.bot_service_fee,
            platform_fee=fee.platform_revenue,
            partner_fee=fee.owner_revenue,
            seller_payout=fee.seller_payout,
        )
        self._transactions[tx.tx_id] = tx
        self._next_tx_id += 1
        return tx

    def get_transaction(self, tx_id: int) -> EscrowTransaction | None:
        return self._transactions.get(tx_id)
