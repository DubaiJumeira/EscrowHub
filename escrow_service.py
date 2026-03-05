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


class EscrowService:
    """Business logic layer with wallet-custody ledger model."""

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
        self.disputes: list[dict] = []

    def create_escrow(self, bot_id: int, buyer_id: int, seller_id: int, asset: str, amount: Decimal, description: str) -> Escrow:
        tenant = self._require_tenant(bot_id)
        validate_minimum_escrow_usd(self.price_service, asset, Decimal(amount))
        fee = self.fee_service.calculate_total_fees(Decimal(amount), tenant.bot_extra_fee_percent)

        escrow_id = self._next_escrow_id
        self.wallet_service.lock_for_escrow(escrow_id, buyer_id, asset, Decimal(amount))

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
        self.wallet_service.release_escrow(
            escrow_id=escrow.escrow_id,
            seller_id=escrow.seller_id,
            platform_fee=fees.platform_fee,
            bot_fee=fees.bot_fee,
            seller_payout=fees.seller_payout,
            bot_owner_id=tenant.owner_user_id,
            asset=escrow.asset,
        )
        escrow.fee_breakdown = fees
        escrow.status = "completed"
        return escrow

    def cancel(self, escrow_id: int) -> Escrow:
        escrow = self._require_escrow(escrow_id)
        if escrow.status != "active":
            raise ValueError("only active escrow can be cancelled")
        self.wallet_service.cancel_escrow_lock(escrow_id)
        escrow.status = "cancelled"
        return escrow

    def dispute(self, escrow_id: int, reason: str) -> Escrow:
        escrow = self._require_escrow(escrow_id)
        if escrow.status != "active":
            raise ValueError("only active escrow can be disputed")
        if not reason.strip():
            raise ValueError("reason is required")
        escrow.status = "disputed"
        self.disputes.append({"escrow_id": escrow_id, "reason": reason, "status": "open"})
        return escrow

    def resolve_dispute(self, escrow_id: int, resolution: str, split_seller_percent: Decimal = Decimal("50")) -> Escrow:
        escrow = self._require_escrow(escrow_id)
        if escrow.status != "disputed":
            raise ValueError("escrow not disputed")
        lock = self.wallet_service.escrow_locks.get(escrow_id)
        if not lock or lock["status"] != "locked":
            raise ValueError("escrow lock missing")

        amount = escrow.amount
        if resolution == "buyer":
            self.wallet_service.cancel_escrow_lock(escrow_id)
        elif resolution == "seller":
            self.release(escrow_id)
            return escrow
        elif resolution == "split":
            seller_part = (amount * Decimal(split_seller_percent) / Decimal("100"))
            buyer_part = amount - seller_part
            lock["status"] = "released"
            self.wallet_service._append_ledger(escrow.seller_id, "USER", escrow.seller_id, escrow.asset, seller_part, "ESCROW_RELEASE", "escrow", escrow_id)
            self.wallet_service._append_ledger(escrow.buyer_id, "USER", escrow.buyer_id, escrow.asset, buyer_part, "ADJUSTMENT", "escrow", escrow_id)
        else:
            raise ValueError("resolution must be buyer/seller/split")

        escrow.status = "completed"
        return escrow

    def get_escrow(self, escrow_id: int) -> Escrow | None:
        return self._escrows.get(escrow_id)

    def list_active_escrows(self, user_id: int) -> list[Escrow]:
        return [e for e in self._escrows.values() if e.status == "active" and user_id in {e.buyer_id, e.seller_id}]

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
