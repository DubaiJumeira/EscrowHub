from __future__ import annotations

from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core.escrow_engine.service import EscrowService
from core.reputation.service import ReputationService
from core.wallet_engine.service import WalletService


class MainBotHandlers:
    def __init__(self, escrow_service: EscrowService, wallet_service: WalletService, reputation_service: ReputationService) -> None:
        self.escrow_service = escrow_service
        self.wallet_service = wallet_service
        self.reputation_service = reputation_service

    @staticmethod
    def base_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Profile", callback_data="profile"), InlineKeyboardButton("Escrow Menu", callback_data="escrow_menu")],
                [InlineKeyboardButton("Check User", callback_data="check_user")],
                [InlineKeyboardButton("Support Team", callback_data="support")],
            ]
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text("Welcome to Escrow platform", reply_markup=self.base_menu())

    async def profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        rep = self.reputation_service.get_profile(user.id)
        lines = [
            f"User ID: {user.id}",
            f"Completed trades: {rep.completed_trades}",
            f"Disputes: {rep.disputes}",
            f"Reputation: {rep.score}",
            "Balance command: /balance <asset>",
        ]
        await update.effective_message.reply_text("\n".join(lines))

    async def create_escrow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # /create_escrow <tenant_bot_id> <seller_id> <amount> <asset> <service_fee_percent> <description...>
        if len(context.args) < 6:
            await update.effective_message.reply_text(
                "Usage: /create_escrow <tenant_bot_id> <seller_id> <amount> <asset> <service_fee_percent> <description>"
            )
            return
        tenant_bot_id = int(context.args[0])
        seller_id = int(context.args[1])
        amount = Decimal(context.args[2])
        asset = context.args[3]
        service_fee_percent = Decimal(context.args[4])
        description = " ".join(context.args[5:])
        buyer_id = update.effective_user.id
        try:
            escrow = self.escrow_service.create_escrow(
                tenant_bot_id=tenant_bot_id,
                buyer_id=buyer_id,
                seller_id=seller_id,
                amount=amount,
                asset=asset,
                description=description,
                bot_service_fee_percent=service_fee_percent,
            )
            await update.effective_message.reply_text(
                f"Escrow #{escrow.escrow_id} created. Status={escrow.status}. Seller payout={escrow.fee_breakdown.seller_payout} {escrow.asset}"
            )
        except Exception as exc:
            await update.effective_message.reply_text(f"Create escrow failed: {exc}")

    async def check_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 1:
            await update.effective_message.reply_text("Usage: /check_user <telegram_id>")
            return
        uid = int(context.args[0])
        rep = self.reputation_service.get_profile(uid)
        await update.effective_message.reply_text(
            f"User {uid}\nCompleted trades: {rep.completed_trades}\nDisputes: {rep.disputes}\nReputation: {rep.score}"
        )

    async def support(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text("Support: @your_support_handle\nOpen ticket: /ticket <message>")
