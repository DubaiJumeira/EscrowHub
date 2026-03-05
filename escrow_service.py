from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fee_service import FeeBreakdown, FeeService
from price_service import PriceService, StaticPriceService, validate_minimum_escrow_usd
from tenant_service import TenantBot, TenantService
from wallet_service import WalletService

ESCROW_STATUSES = {"pending", "active", "completed", "cancelled", "disputed"}


@dataclass
class Escrow:
    escrow_id: int
    bot_id: int
    buyer_id: int
    seller_id: int
    asset: str
    amount: Decimal
    description: str
    status: str
    fee_breakdown: FeeBreakdown


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
    """Business logic layer kept in one module for unit testing."""

    def __init__(
        self,
        wallet_service: WalletService | None = None,
        fee_service: FeeService | None = None,
        price_service: PriceService | None = None,
        tenant_service: TenantService | None = None,
    ) -> None:
        self.wallet_service = wallet_service or WalletService()
        self.fee_service = fee_service or FeeService()
        self.price_service = price_service or StaticPriceService(
            {
                "BTC": Decimal("65000"),
                "ETH": Decimal("3500"),
                "LTC": Decimal("80"),
                "USDT": Decimal("1"),
                "USDC": Decimal("1"),
                "SOL": Decimal("150"),
                "XRP": Decimal("0.55"),
            }
        )
        self.tenant_service = tenant_service or TenantService()
        self._escrows: dict[int, Escrow] = {}
        self._next_escrow_id = 1

    # --- Modern multi-tenant API ---
    def create_escrow(
        self,
        bot_id: int,
        buyer_id: int,
        seller_id: int,
        asset: str,
        amount: Decimal,
        description: str,
    ) -> Escrow:
        tenant = self._require_tenant(bot_id)
        validate_minimum_escrow_usd(self.price_service, asset, Decimal(amount))
        fee = self.fee_service.calculate_total_fees(Decimal(amount), tenant.bot_extra_fee_percent)

        escrow_id = self._next_escrow_id
        self.wallet_service.lock_funds(buyer_id, asset, Decimal(amount), escrow_id)

        escrow = Escrow(
            escrow_id=escrow_id,
            bot_id=bot_id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            asset=asset.upper(),
            amount=Decimal(amount),
            description=description,
            status="active",
            fee_breakdown=fee,
        )
        self._escrows[escrow_id] = escrow
        self._next_escrow_id += 1
        return escrow

    def release(self, escrow_id: int) -> Escrow:
        escrow = self._require_escrow(escrow_id)
        if escrow.status != "active":
            raise ValueError("escrow must be active")

        tenant = self._require_tenant(escrow.bot_id)
        fees = self.fee_service.apply_payouts(escrow.amount, tenant.bot_extra_fee_percent)

        # Seller pays fees: seller receives amount - total_fee.
        self.wallet_service.move_locked_to_user(
            from_user_id=escrow.buyer_id,
            to_user_id=escrow.seller_id,
            asset=escrow.asset,
            amount=fees.seller_payout,
            escrow_id=escrow.escrow_id,
        )
        self.wallet_service.credit_platform_earning(escrow.asset, fees.platform_fee, escrow.escrow_id)
        self.wallet_service.credit_bot_owner_earning(tenant.owner_user_id, escrow.asset, fees.bot_extra_fee, escrow.escrow_id)

        escrow.fee_breakdown = fees
        escrow.status = "completed"
        return escrow

    def cancel(self, escrow_id: int) -> Escrow:
        escrow = self._require_escrow(escrow_id)
        if escrow.status != "active":
            raise ValueError("only active escrow can be cancelled")
        self.wallet_service.move_locked_to_user(escrow.buyer_id, escrow.buyer_id, escrow.asset, escrow.amount, escrow.escrow_id)
        escrow.status = "cancelled"
        return escrow

    def dispute(self, escrow_id: int, reason: str) -> Escrow:
        escrow = self._require_escrow(escrow_id)
        if escrow.status != "active":
            raise ValueError("only active escrow can be disputed")
        if not reason.strip():
            raise ValueError("reason is required")
        escrow.status = "disputed"
        return escrow

    def get_escrow(self, escrow_id: int) -> Escrow | None:
        return self._escrows.get(escrow_id)

    def list_active_escrows(self, user_id: int) -> list[Escrow]:
        return [e for e in self._escrows.values() if e.status == "active" and user_id in {e.buyer_id, e.seller_id}]

    # --- Backward compatible wrappers ---
    def create_partner(self, owner_id: int, display_name: str, service_fee_percent: Decimal) -> PartnerProfile:
        tenant = self.tenant_service.create_or_update_tenant(
            bot_id=owner_id,
            owner_user_id=owner_id,
            bot_display_name=display_name,
            support_contact="@support",
            bot_extra_fee_percent=Decimal(service_fee_percent),
        )
        return PartnerProfile(owner_id=tenant.owner_user_id, display_name=tenant.bot_display_name, service_fee_percent=tenant.bot_extra_fee_percent)

    def get_partner(self, owner_id: int) -> PartnerProfile | None:
        tenant = self.tenant_service.get_tenant(owner_id)
        if not tenant:
            return None
        return PartnerProfile(owner_id=tenant.owner_user_id, display_name=tenant.bot_display_name, service_fee_percent=tenant.bot_extra_fee_percent)

    def create_transaction(self, partner_id: int, buyer_id: int, seller_id: int, total_amount: Decimal) -> EscrowTransaction:
        escrow = self.create_escrow(
            bot_id=partner_id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            asset="USDT",
            amount=Decimal(total_amount),
            description="legacy transaction",
        )
        return EscrowTransaction(
            tx_id=escrow.escrow_id,
            partner_id=partner_id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            total_amount=escrow.amount,
            hold_amount=Decimal("0"),
            service_fee_total=escrow.fee_breakdown.bot_extra_fee,
            platform_fee=escrow.fee_breakdown.platform_fee,
            partner_fee=escrow.fee_breakdown.bot_extra_fee,
            seller_payout=escrow.fee_breakdown.seller_payout,
            status=escrow.status,
        )

    def get_transaction(self, tx_id: int) -> EscrowTransaction | None:
        escrow = self.get_escrow(tx_id)
        if not escrow:
            return None
        return EscrowTransaction(
            tx_id=escrow.escrow_id,
            partner_id=escrow.bot_id,
            buyer_id=escrow.buyer_id,
            seller_id=escrow.seller_id,
            total_amount=escrow.amount,
            hold_amount=Decimal("0"),
            service_fee_total=escrow.fee_breakdown.bot_extra_fee,
            platform_fee=escrow.fee_breakdown.platform_fee,
            partner_fee=escrow.fee_breakdown.bot_extra_fee,
            seller_payout=escrow.fee_breakdown.seller_payout,
            status=escrow.status,
        )

    def _require_tenant(self, bot_id: int) -> TenantBot:
        tenant = self.tenant_service.get_tenant(bot_id)
        if not tenant:
            raise ValueError("tenant bot not found")
        return tenant

    def _require_escrow(self, escrow_id: int) -> Escrow:
        escrow = self._escrows.get(escrow_id)
        if not escrow:
            raise ValueError("escrow not found")
        return escrow
