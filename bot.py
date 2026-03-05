from __future__ import annotations

import os
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from escrow_service import EscrowService
from wallet_service import NETWORK_LABELS

escrow_service = EscrowService()


def start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Profile", callback_data="profile"), InlineKeyboardButton("Escrow Menu", callback_data="escrow_menu")],
            [InlineKeyboardButton("Check User", callback_data="check_user")],
            [InlineKeyboardButton("Support Team", callback_data="support_team")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Welcome to EscrowHub", reply_markup=start_menu())


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lines = [f"User ID: {user_id}", "Balances:"]
    for asset in NETWORK_LABELS:
        available = escrow_service.wallet_service.available_balance(user_id, asset)
        locked = escrow_service.wallet_service.locked_balance(user_id, asset)
        if available != 0 or locked != 0:
            lines.append(f"- {NETWORK_LABELS[asset]}: available={available} locked={locked}")
    lines.append(f"Active escrows: {len(escrow_service.list_active_escrows(user_id))}")
    await update.effective_message.reply_text("\n".join(lines))


async def configure_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 3:
        await update.effective_message.reply_text("Usage: /configure_bot <bot_id> <extra_fee_0_to_3> <support_contact>")
        return
    tenant = escrow_service.tenant_service.create_or_update_tenant(
        bot_id=int(context.args[0]),
        owner_user_id=update.effective_user.id,
        bot_display_name=f"{update.effective_user.full_name} bot",
        support_contact=context.args[2],
        bot_extra_fee_percent=Decimal(context.args[1]),
    )
    await update.effective_message.reply_text(f"Configured bot {tenant.bot_id} with extra fee {tenant.bot_extra_fee_percent}%")


async def escrow_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Escrow Menu:\n"
        "/create_escrow <bot_id> <seller_id> <asset> <amount> <description>\n"
        "/active_escrows\n/release <escrow_id>\n/cancel <escrow_id>\n/dispute <escrow_id> <reason>"
    )


async def create_escrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_id, seller_id, asset, amount = int(context.args[0]), int(context.args[1]), context.args[2], Decimal(context.args[3])
    description = " ".join(context.args[4:])
    escrow = escrow_service.create_escrow(bot_id, update.effective_user.id, seller_id, asset, amount, description)
    await update.effective_message.reply_text(
        f"Escrow #{escrow.escrow_id} created\n"
        f"platform_fee={escrow.fee_breakdown.platform_fee} bot_fee={escrow.fee_breakdown.bot_fee} "
        f"seller_payout={escrow.fee_breakdown.seller_payout}"
    )


async def active_escrows(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = escrow_service.list_active_escrows(update.effective_user.id)
    await update.effective_message.reply_text("\n".join([f"#{e.escrow_id} {e.amount} {e.asset}" for e in rows]) or "No active escrows")


async def release(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    escrow = escrow_service.release(int(context.args[0]))
    await update.effective_message.reply_text(f"Escrow #{escrow.escrow_id} completed. Seller paid fees and received {escrow.fee_breakdown.seller_payout}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    escrow = escrow_service.cancel(int(context.args[0]))
    await update.effective_message.reply_text(f"Escrow #{escrow.escrow_id} cancelled")


async def dispute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    escrow = escrow_service.dispute(int(context.args[0]), " ".join(context.args[1:]))
    await update.effective_message.reply_text(f"Escrow #{escrow.escrow_id} disputed")


async def deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    asset = context.args[0]
    route = escrow_service.wallet_service.get_or_create_deposit_address(update.effective_user.id, asset)
    network = NETWORK_LABELS[route.asset]
    suffix = f"\nDestination tag: {route.destination_tag}" if route.destination_tag else ""
    await update.effective_message.reply_text(f"Deposit network: {network}\nAddress: {route.address}{suffix}")


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    asset, amount, destination = context.args[0], Decimal(context.args[1]), context.args[2]
    rec = escrow_service.wallet_service.request_withdrawal(update.effective_user.id, asset, amount, destination)
    await update.effective_message.reply_text(f"Withdrawal request #{rec['id']} pending: {rec['amount']} {rec['asset']} -> {rec['destination_address']}")


async def check_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Check User: reputation and completed trades available via reputation service integration.")


async def support_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Support Team: @your_support_handle")


async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    mapping = {"profile": profile, "escrow_menu": escrow_menu, "check_user": check_user, "support_team": support_team}
    fn = mapping.get(query.data)
    if fn:
        update._effective_message = query.message
        await fn(update, context)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("configure_bot", configure_bot))
    app.add_handler(CommandHandler("escrow_menu", escrow_menu))
    app.add_handler(CommandHandler("create_escrow", create_escrow))
    app.add_handler(CommandHandler("active_escrows", active_escrows))
    app.add_handler(CommandHandler("release", release))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("dispute", dispute))
    app.add_handler(CommandHandler("deposit", deposit))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("check_user", check_user))
    app.add_handler(CommandHandler("support", support_team))
    app.add_handler(CallbackQueryHandler(on_menu_click))
    app.run_polling()


if __name__ == "__main__":
    main()
