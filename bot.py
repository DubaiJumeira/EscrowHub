from __future__ import annotations

import os
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from escrow_service import EscrowService
from infra.db.database import get_connection, init_db
from signer.signer_service import SignerService
from tenant_service import TenantService
from wallet_service import NETWORK_LABELS, WalletService
from watcher_status_service import read_watcher_status

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()}


def _services():
    conn = get_connection()
    init_db(conn)
    wallet = WalletService(conn)
    tenant = TenantService(conn)
    escrow = EscrowService(conn)
    return conn, wallet, tenant, escrow


def start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Profile", callback_data="profile"), InlineKeyboardButton("Escrow Menu", callback_data="escrow_menu")],
        [InlineKeyboardButton("Check User", callback_data="check_user")],
        [InlineKeyboardButton("Support Team", callback_data="support_team")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Welcome to EscrowHub", reply_markup=start_menu())


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn, wallet, tenant, escrow = _services()
    try:
        user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        lines = [f"User ID: {update.effective_user.id}", "Balances:"]
        for asset in NETWORK_LABELS:
            a = wallet.available_balance(user_id, asset)
            l = wallet.locked_balance(user_id, asset)
            if a or l:
                lines.append(f"- {NETWORK_LABELS[asset]}: available={a} locked={l}")
        lines.append(f"Active escrows: {len(escrow.list_active_escrows(user_id))}")
        await update.effective_message.reply_text("\n".join(lines))
        conn.commit()
    finally:
        conn.close()


async def deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        route = wallet.get_or_create_deposit_address(user_id, context.args[0])
        suffix = f"\nDestination tag: {route.destination_tag}" if route.destination_tag else ""
        await update.effective_message.reply_text(f"Deposit network: {NETWORK_LABELS[route.asset]}\nAddress: {route.address}{suffix}")
        conn.commit()
    finally:
        conn.close()


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        rec = wallet.request_withdrawal(user_id, context.args[0], Decimal(context.args[1]), context.args[2])
        await update.effective_message.reply_text(f"Withdrawal request #{rec['id']} pending")
        conn.commit()
    finally:
        conn.close()


async def create_escrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn, _, tenant, escrow = _services()
    try:
        buyer_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        view = escrow.create_escrow(int(context.args[0]), buyer_id, int(context.args[1]), context.args[2], Decimal(context.args[3]), " ".join(context.args[4:]))
        await update.effective_message.reply_text(f"Escrow #{view.escrow_id} created. Seller payout={view.fee_breakdown.seller_payout}")
        conn.commit()
    finally:
        conn.close()


async def release(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn, _, _, escrow = _services()
    try:
        view = escrow.release(int(context.args[0]))
        await update.effective_message.reply_text(f"Escrow #{view.escrow_id} completed")
        conn.commit()
    finally:
        conn.close()


async def dispute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn, _, tenant, escrow = _services()
    try:
        user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        escrow.dispute(int(context.args[0]), user_id, " ".join(context.args[1:]))
        await update.effective_message.reply_text("Dispute opened")
        conn.commit()
    finally:
        conn.close()


async def resolve_dispute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only")
        return
    conn, _, tenant, escrow = _services()
    try:
        admin_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        split = Decimal(context.args[2]) if len(context.args) > 2 and context.args[1] == "split" else Decimal("50")
        escrow.resolve_dispute(int(context.args[0]), admin_id, context.args[1], split)
        await update.effective_message.reply_text("Dispute resolved")
        conn.commit()
    finally:
        conn.close()


async def run_signer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only")
        return
    conn, wallet, _, _ = _services()
    try:
        count = SignerService().process_pending_withdrawals(wallet)
        conn.commit()
        await update.effective_message.reply_text(f"Signer processed {count} withdrawals")
    finally:
        conn.close()




async def watcher_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only")
        return
    conn, _, _, _ = _services()
    try:
        status = read_watcher_status(conn, ["btc_watcher", "eth_watcher"])
        b = status["btc_watcher"]
        e = status["eth_watcher"]
        msg = (
            f"BTC watcher\n"
            f"- last run: {b['last_run_at']}\n"
            f"- last success: {b['last_success_at']}\n"
            f"- consecutive failures: {b['consecutive_failures']}\n"
            f"- last error: {b['last_error']}\n\n"
            f"ETH watcher\n"
            f"- last run: {e['last_run_at']}\n"
            f"- last success: {e['last_success_at']}\n"
            f"- consecutive failures: {e['consecutive_failures']}\n"
            f"- last error: {e['last_error']}"
        )
        await update.effective_message.reply_text(msg)
    finally:
        conn.close()

async def check_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Check User: reputation/trade stats are tracked in DB services.")


async def support_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Support Team: @your_support_handle")


async def escrow_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("/deposit /withdraw /create_escrow /release /dispute")


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
    conn = get_connection()
    init_db(conn)
    conn.close()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("deposit", deposit))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("create_escrow", create_escrow))
    app.add_handler(CommandHandler("release", release))
    app.add_handler(CommandHandler("dispute", dispute))
    app.add_handler(CommandHandler("resolve_dispute", resolve_dispute))
    app.add_handler(CommandHandler("run_signer", run_signer))
    app.add_handler(CommandHandler("watcher_status", watcher_status))
    app.add_handler(CommandHandler("check_user", check_user))
    app.add_handler(CommandHandler("support", support_team))
    app.add_handler(CallbackQueryHandler(on_menu_click))
    app.run_polling()


if __name__ == "__main__":
    main()
