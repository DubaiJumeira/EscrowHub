from __future__ import annotations

import os
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from escrow_service import EscrowService

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
    await update.effective_message.reply_text("Welcome to EscrowBot", reply_markup=start_menu())


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    balances = escrow_service.wallet_service.get_balances_for_user(user_id)
    escrows = escrow_service.list_active_escrows(user_id)

    balance_lines = [f"{asset}: available={bal.available} locked={bal.locked}" for asset, bal in balances.items()] or ["No balances yet"]
    await update.effective_message.reply_text(
        "\n".join(
            [
                f"User ID: {user_id}",
                "Balances:",
                *balance_lines,
                f"Active escrows: {len(escrows)}",
                "Earnings: platform admin and bot owner earnings are tracked via ledger entries.",
            ]
        )
    )


async def configure_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 3:
        await update.effective_message.reply_text("Usage: /configure_bot <bot_id> <extra_fee_0_to_3> <support_contact>")
        return
    bot_id = int(context.args[0])
    extra_fee = Decimal(context.args[1])
    support_contact = context.args[2]
    owner = update.effective_user.id
    try:
        tenant = escrow_service.tenant_service.create_or_update_tenant(
            bot_id=bot_id,
            owner_user_id=owner,
            bot_display_name=f"{update.effective_user.full_name} bot",
            support_contact=support_contact,
            bot_extra_fee_percent=extra_fee,
        )
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(
        f"Bot configured: id={tenant.bot_id}, owner={tenant.owner_user_id}, extra_fee={tenant.bot_extra_fee_percent}%"
    )


async def escrow_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Escrow Menu:\n"
        "/create_escrow <bot_id> <seller_id> <asset> <amount> <description>\n"
        "/active_escrows\n"
        "/release <escrow_id>\n"
        "/cancel <escrow_id>\n"
        "/dispute <escrow_id> <reason>"
    )


async def create_escrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 5:
        await update.effective_message.reply_text("Usage: /create_escrow <bot_id> <seller_id> <asset> <amount> <description>")
        return

    buyer_id = update.effective_user.id
    bot_id = int(context.args[0])
    seller_id = int(context.args[1])
    asset = context.args[2]
    amount = Decimal(context.args[3])
    description = " ".join(context.args[4:])

    try:
        escrow = escrow_service.create_escrow(bot_id, buyer_id, seller_id, asset, amount, description)
    except Exception as exc:
        await update.effective_message.reply_text(f"Create escrow failed: {exc}")
        return

    await update.effective_message.reply_text(
        f"Escrow #{escrow.escrow_id} created. status={escrow.status}\n"
        f"Platform fee: {escrow.fee_breakdown.platform_fee} {escrow.asset}\n"
        f"Bot extra fee: {escrow.fee_breakdown.bot_extra_fee} {escrow.asset}\n"
        f"Seller payout on release: {escrow.fee_breakdown.seller_payout} {escrow.asset}"
    )


async def active_escrows(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    escrows = escrow_service.list_active_escrows(user_id)
    if not escrows:
        await update.effective_message.reply_text("No active escrows")
        return
    lines = [f"#{e.escrow_id} bot={e.bot_id} {e.amount} {e.asset} status={e.status}" for e in escrows]
    await update.effective_message.reply_text("\n".join(lines))


async def release(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /release <escrow_id>")
        return
    try:
        escrow = escrow_service.release(int(context.args[0]))
    except Exception as exc:
        await update.effective_message.reply_text(f"Release failed: {exc}")
        return
    await update.effective_message.reply_text(f"Escrow #{escrow.escrow_id} released, seller got {escrow.fee_breakdown.seller_payout}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /cancel <escrow_id>")
        return
    try:
        escrow = escrow_service.cancel(int(context.args[0]))
    except Exception as exc:
        await update.effective_message.reply_text(f"Cancel failed: {exc}")
        return
    await update.effective_message.reply_text(f"Escrow #{escrow.escrow_id} cancelled")


async def dispute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /dispute <escrow_id> <reason>")
        return
    escrow_id = int(context.args[0])
    reason = " ".join(context.args[1:])
    try:
        escrow = escrow_service.dispute(escrow_id, reason)
    except Exception as exc:
        await update.effective_message.reply_text(f"Dispute failed: {exc}")
        return
    await update.effective_message.reply_text(f"Escrow #{escrow.escrow_id} moved to disputed")


async def check_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Check User: completed trades/disputes/reputation are available from reputation service integration.")


async def support_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Support Team: @your_support_handle\nUse /ticket <message> to open ticket.")


async def deposit_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /deposit_address <asset>")
        return
    user_id = update.effective_user.id
    try:
        wallet = escrow_service.wallet_service.assign_deposit_address(user_id, context.args[0])
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    tag_part = f" destination_tag={wallet.xrp_destination_tag}" if wallet.xrp_destination_tag else ""
    await update.effective_message.reply_text(f"Deposit {wallet.asset} to {wallet.address}{tag_part}")


async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    mapping = {
        "profile": profile,
        "escrow_menu": escrow_menu,
        "check_user": check_user,
        "support_team": support_team,
    }
    handler = mapping.get(query.data)
    if handler:
        update._effective_message = query.message
        await handler(update, context)


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
    app.add_handler(CommandHandler("check_user", check_user))
    app.add_handler(CommandHandler("support", support_team))
    app.add_handler(CommandHandler("deposit_address", deposit_address))
    app.add_handler(CallbackQueryHandler(on_menu_click))
    app.run_polling()


if __name__ == "__main__":
    main()
