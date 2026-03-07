from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from fee_service import FeeBreakdown, FeeService
from price_service import PriceService, StaticPriceService, validate_minimum_escrow_usd
from tenant_service import TenantService
from wallet_service import WalletService


@dataclass
class EscrowView:
    escrow_id: int
    bot_id: int
    buyer_id: int
    seller_id: int
    asset: str
    amount: Decimal
    status: str
    fee_breakdown: FeeBreakdown
    description: str


class EscrowService:
    def __init__(self, conn, price_service: PriceService | None = None) -> None:
        self.conn = conn
        self.wallet_service = WalletService(conn)
        self.tenant_service = TenantService(conn)
        self.fee_service = FeeService()
        self.price_service = price_service or StaticPriceService(
            {"BTC": Decimal("65000"), "ETH": Decimal("3500"), "LTC": Decimal("80"), "USDT": Decimal("1"), "USDC": Decimal("1"), "SOL": Decimal("150"), "XRP": Decimal("0.55")}
        )

    def create_escrow(self, bot_id: int, buyer_id: int, seller_id: int, asset: str, amount: Decimal, description: str) -> EscrowView:
        tenant = self.tenant_service.get_tenant(bot_id)
        if not tenant:
            raise ValueError("tenant bot not found")
        if buyer_id == seller_id:
            raise ValueError("buyer cannot create deal with self")
        validate_minimum_escrow_usd(self.price_service, asset, Decimal(amount))
        if self.wallet_service.available_balance(buyer_id, asset) < Decimal(amount):
            raise ValueError("insufficient available balance")

        fees = self.fee_service.calculate_total_fees(Decimal(amount), tenant.bot_extra_fee_percent)
        cur = self.conn.execute(
            "INSERT INTO escrows(bot_id,buyer_id,seller_id,asset,amount,status,description) VALUES(?,?,?,?,?,?,?)",
            (bot_id, buyer_id, seller_id, asset.upper(), str(Decimal(amount)), "pending", description),
        )
        escrow_id = int(cur.lastrowid)
        self.wallet_service.lock_for_escrow(escrow_id, buyer_id, asset, Decimal(amount))
        self._event(escrow_id, "created", {"amount": str(amount), "asset": asset.upper(), "status": "pending"})
        return EscrowView(escrow_id, bot_id, buyer_id, seller_id, asset.upper(), Decimal(amount), "pending", fees, description)

    def release(self, escrow_id: int, actor_user_id: int) -> EscrowView:
        row = self._escrow(escrow_id)
        if row["status"] not in {"pending", "active"}:
            raise ValueError("escrow must be pending/active")
        if int(row["buyer_id"]) != int(actor_user_id):
            raise ValueError("only buyer can release own escrow")

        tenant = self.tenant_service.get_tenant(int(row["bot_id"]))
        amount = Decimal(row["amount"])
        fees = self.fee_service.apply_payouts(amount, tenant.bot_extra_fee_percent)
        self.wallet_service.release_escrow(escrow_id, int(row["seller_id"]), fees.platform_fee, fees.bot_fee, fees.seller_payout, tenant.owner_user_id, row["asset"])
        self.conn.execute("UPDATE escrows SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (escrow_id,))
        self._event(escrow_id, "released", {"seller_payout": str(fees.seller_payout), "actor_user_id": actor_user_id})
        return EscrowView(escrow_id, int(row["bot_id"]), int(row["buyer_id"]), int(row["seller_id"]), row["asset"], amount, "completed", fees, row["description"] or "")

    def dispute(self, escrow_id: int, opened_by_user_id: int, reason: str) -> None:
        row = self._escrow(escrow_id)
        if row["status"] not in {"pending", "active"}:
            raise ValueError("only pending/active escrow can be disputed")
        self.conn.execute("UPDATE escrows SET status='disputed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (escrow_id,))
        self.conn.execute("INSERT INTO disputes(escrow_id,opened_by_user_id,reason,status) VALUES(?,?,?,?)", (escrow_id, opened_by_user_id, reason, "open"))
        self._event(escrow_id, "disputed", {"reason": reason})

    def resolve_dispute(self, escrow_id: int, admin_user_id: int, resolution: str, split_percent: Decimal = Decimal("50")) -> None:
        row = self._escrow(escrow_id)
        if row["status"] != "disputed":
            raise ValueError("escrow not disputed")

        if resolution == "release_seller":
            tenant = self.tenant_service.get_tenant(int(row["bot_id"]))
            amount = Decimal(row["amount"])
            fees = self.fee_service.apply_payouts(amount, tenant.bot_extra_fee_percent)
            self.wallet_service.release_escrow(escrow_id, int(row["seller_id"]), fees.platform_fee, fees.bot_fee, fees.seller_payout, tenant.owner_user_id, row["asset"])
        elif resolution == "refund_buyer":
            self.wallet_service.cancel_escrow_lock(escrow_id)
        elif resolution == "split":
            lock = self.conn.execute("SELECT * FROM escrow_locks WHERE escrow_id=?", (escrow_id,)).fetchone()
            if not lock or lock["status"] != "locked":
                raise ValueError("escrow lock missing")
            seller_part = Decimal(row["amount"]) * Decimal(split_percent) / Decimal("100")
            buyer_part = Decimal(row["amount"]) - seller_part
            self.conn.execute("UPDATE escrow_locks SET status='released' WHERE escrow_id=?", (escrow_id,))
            self.wallet_service.ledger.add_entry("USER", int(row["seller_id"]), int(row["seller_id"]), row["asset"], seller_part, "ESCROW_RELEASE", "escrow", escrow_id)
            self.wallet_service.ledger.add_entry("USER", int(row["buyer_id"]), int(row["buyer_id"]), row["asset"], buyer_part, "ADJUSTMENT", "escrow", escrow_id)
        else:
            raise ValueError("invalid resolution")

        self.conn.execute("UPDATE escrows SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (escrow_id,))
        self.conn.execute(
            "UPDATE disputes SET status='resolved', resolution_json=?, resolved_at=CURRENT_TIMESTAMP WHERE escrow_id=? AND status='open'",
            (json.dumps({"resolution": resolution, "split_percent": str(split_percent)}), escrow_id),
        )
        self.conn.execute("INSERT INTO admin_actions(admin_user_id,action_type,data_json) VALUES(?,?,?)", (admin_user_id, "resolve_dispute", json.dumps({"escrow_id": escrow_id, "resolution": resolution})))
        self._event(escrow_id, "dispute_resolved", {"resolution": resolution})

    def get_escrow(self, escrow_id: int):
        return self._escrow(escrow_id)

    def list_pending_escrows(self, user_id: int):
        return self.conn.execute("SELECT * FROM escrows WHERE status='pending' AND (buyer_id=? OR seller_id=?) ORDER BY id DESC", (user_id, user_id)).fetchall()

    def list_active_escrows(self, user_id: int):
        return self.conn.execute("SELECT * FROM escrows WHERE status='active' AND (buyer_id=? OR seller_id=?)", (user_id, user_id)).fetchall()

    def list_disputed_escrows(self, user_id: int):
        return self.conn.execute("SELECT * FROM escrows WHERE status='disputed' AND (buyer_id=? OR seller_id=?) ORDER BY id DESC", (user_id, user_id)).fetchall()

    def list_completed_escrows_page(self, user_id: int, page: int = 1, per_page: int = 10):
        page = max(1, int(page))
        per_page = max(1, int(per_page))
        total = self.conn.execute("SELECT COUNT(*) c FROM escrows WHERE status='completed' AND (buyer_id=? OR seller_id=?)", (user_id, user_id)).fetchone()["c"]
        pages = max(1, (int(total) + per_page - 1) // per_page)
        page = min(page, pages)
        offset = (page - 1) * per_page
        rows = self.conn.execute(
            "SELECT * FROM escrows WHERE status='completed' AND (buyer_id=? OR seller_id=?) ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, user_id, per_page, offset),
        ).fetchall()
        return rows, page, pages

    def counterparty_user_id(self, escrow_row, viewer_user_id: int) -> int:
        return int(escrow_row["seller_id"]) if int(escrow_row["buyer_id"]) == int(viewer_user_id) else int(escrow_row["buyer_id"])

    def _event(self, escrow_id: int, event_type: str, data: dict) -> None:
        self.conn.execute("INSERT INTO escrow_events(escrow_id,event_type,data_json) VALUES(?,?,?)", (escrow_id, event_type, json.dumps(data)))

    def _escrow(self, escrow_id: int):
        row = self.conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row:
            raise ValueError("escrow not found")
        return row
