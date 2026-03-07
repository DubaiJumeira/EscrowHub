from __future__ import annotations

import logging
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config.settings import Settings
from escrow_service import EscrowService
from infra.db.database import get_connection, init_db
from signer.signer_service import SignerService
from tenant_service import TenantService
from wallet_service import NETWORK_LABELS, WalletService
from watcher_status_service import read_watcher_status

LOGGER = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()}

(
    DEAL_SEARCH_RESULT,
    DEAL_ENTER_AMOUNT,
    DEAL_ENTER_CONDITIONS,
    DEAL_PENDING_VIEW,
    DEAL_CANCEL_INFO,
    DEAL_RELEASE_CONFIRM,
    DEAL_RATE_SELLER,
    DEAL_RATE_BUYER,
) = range(8)


def _services():
    conn = get_connection()
    init_db(conn)
    wallet = WalletService(conn)
    tenant = TenantService(conn)
    escrow = EscrowService(conn)
    return conn, wallet, tenant, escrow


def _start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Profile", callback_data="profile"), InlineKeyboardButton("Escrow Menu", callback_data="escrow_menu")],
            [InlineKeyboardButton("Check User", callback_data="check_user")],
            [InlineKeyboardButton("Support Team", callback_data="support_team")],
        ]
    )


async def _notify_safe(context: ContextTypes.DEFAULT_TYPE, telegram_user_id: int | None, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if not telegram_user_id:
        return
    try:
        await context.bot.send_message(chat_id=telegram_user_id, text=text, reply_markup=reply_markup)
    except Exception:
        LOGGER.exception("notification failed for telegram_user_id=%s", telegram_user_id)


def _user_profile(conn, user_row) -> dict:
    user_id = int(user_row["id"])
    completed_deals = conn.execute("SELECT COUNT(*) c FROM escrows WHERE status='completed' AND (buyer_id=? OR seller_id=?)", (user_id, user_id)).fetchone()["c"]
    disputes = conn.execute("SELECT COUNT(*) c FROM disputes d JOIN escrows e ON e.id=d.escrow_id WHERE (e.buyer_id=? OR e.seller_id=?)", (user_id, user_id)).fetchone()["c"]
    rating = conn.execute("SELECT AVG(rating) r FROM reviews WHERE reviewed_id=?", (user_id,)).fetchone()["r"]
    spent = conn.execute("SELECT COALESCE(SUM(CAST(amount AS REAL)),0) v FROM escrows WHERE buyer_id=? AND status='completed'", (user_id,)).fetchone()["v"]
    earned = conn.execute("SELECT COALESCE(SUM(CAST(amount AS REAL)),0) v FROM escrows WHERE seller_id=? AND status='completed'", (user_id,)).fetchone()["v"]

    trust_level = "High" if completed_deals >= 20 and disputes <= 1 else "Medium" if completed_deals >= 5 else "Low"
    return {
        "username": user_row["username"] or "unknown",
        "registered_date": user_row["created_at"],
        "trust_level": trust_level,
        "rating": float(rating) if rating is not None else 0.0,
        "deals": int(completed_deals),
        "spent": Decimal(str(spent)),
        "earned": Decimal(str(earned)),
        "user_id": user_id,
        "telegram_id": int(user_row["telegram_id"]),
    }


def _render_user_profile(profile: dict) -> str:
    return (
        f"@{profile['username']}\n"
        "Seller found\n"
        f"Registered date: {profile['registered_date']}\n"
        f"Trust level: {profile['trust_level']}\n"
        f"Rating: {profile['rating']:.2f}\n"
        f"Deals: {profile['deals']}\n"
        f"Total Spent: {profile['spent']}\n"
        f"Total Earned: {profile['earned']}"
    )


def _format_db_timestamp(ts: str | None) -> str:
    if not ts:
        return "unknown"
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d %b %Y, %H:%M:%S")
    except ValueError:
        return ts


DRAFT_FLOW_KEYS = {
    "seller_id",
    "seller_username",
    "seller_telegram_id",
    "seller_profile_text",
    "buyer_id",
    "amount",
    "asset",
    "conditions",
    "escrow_id",
    "created_label",
    "previous_view",
    "wd_asset",
    "wd_amount",
    "wd_address",
}


def _is_user_frozen(conn, telegram_user_id: int) -> bool:
    row = conn.execute("SELECT frozen FROM users WHERE telegram_id=?", (telegram_user_id,)).fetchone()
    return bool(row and int(row["frozen"]))


async def _require_not_frozen(update: Update, conn) -> bool:
    if _is_user_frozen(conn, update.effective_user.id):
        msg = update.effective_message
        if msg:
            await msg.reply_text("Your account is frozen. Please contact support.")
        elif update.callback_query:
            await update.callback_query.edit_message_text("Your account is frozen. Please contact support.")
        return False
    return True


def _clear_draft_flow(context: ContextTypes.DEFAULT_TYPE) -> bool:
    removed = False
    for key in DRAFT_FLOW_KEYS:
        if key in context.user_data:
            removed = True
            context.user_data.pop(key, None)
    return removed


WITHDRAW_MINIMUMS = {
    "USDT": Decimal("100"),
    "USDC": Decimal("100"),
    "BTC": Decimal("0.001"),
    "ETH": Decimal("1"),
    "LTC": Decimal("1"),
    "SOL": Decimal("1"),
    "XRP": Decimal("10"),
}

(
    WD_SELECT_ASSET,
    WD_ENTER_AMOUNT,
    WD_ENTER_ADDRESS,
    WD_CONFIRM,
) = range(100, 104)


def _profile_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Deposit", callback_data="profile_deposit"), InlineKeyboardButton("Withdraw", callback_data="profile_withdraw")],
            [InlineKeyboardButton("Withdrawal History", callback_data="profile_withdraw_history:1")],
            [InlineKeyboardButton("Back", callback_data="profile_back")],
        ]
    )


def _date_short(ts: str | None) -> str:
    if not ts:
        return "unknown"
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").strftime("%d %b %Y")
    except ValueError:
        return ts


def _render_self_profile(conn, telegram_user, user_id: int, wallet: WalletService) -> str:
    first_name = telegram_user.first_name or "unknown"
    registered = conn.execute("SELECT created_at FROM users WHERE id=?", (user_id,)).fetchone()["created_at"]
    rev = conn.execute("SELECT AVG(rating) avg_rating, COUNT(*) cnt FROM reviews WHERE reviewed_id=?", (user_id,)).fetchone()
    successful = conn.execute("SELECT COUNT(*) c FROM escrows WHERE status='completed' AND (buyer_id=? OR seller_id=?)", (user_id, user_id)).fetchone()["c"]
    cancelled = conn.execute("SELECT COUNT(*) c FROM escrows WHERE status='cancelled' AND (buyer_id=? OR seller_id=?)", (user_id, user_id)).fetchone()["c"]
    disputes_lost = conn.execute(
        "SELECT COUNT(*) c FROM disputes d JOIN escrows e ON e.id=d.escrow_id WHERE d.status='resolved' AND ((e.buyer_id=? AND json_extract(d.resolution_json,'$.resolution')='release_seller') OR (e.seller_id=? AND json_extract(d.resolution_json,'$.resolution')='refund_buyer'))",
        (user_id, user_id),
    ).fetchone()["c"]
    lines = [
        f"Your Telegram ID: {telegram_user.id}",
        f"First Name: {first_name}",
        "Balance:",
    ]
    for asset in ["USDT", "USDC", "BTC", "ETH", "LTC", "SOL", "XRP"]:
        a = wallet.available_balance(user_id, asset)
        l = wallet.locked_balance(user_id, asset)
        lines.append(f"• {asset}: {a} (locked {l})")
    trust = "High" if successful >= 20 else "Medium" if successful >= 5 else "Low"
    lines.append(f"Registered: {_date_short(registered)}")
    lines.append(f"Trust level: {trust}")
    if int(rev["cnt"] or 0) < 3:
        lines.append("Rating: Too few reviews")
    else:
        lines.append(f"Rating: {float(rev['avg_rating']):.1f}/5 from {int(rev['cnt'])} reviews")
    lines.append("Deals:")
    lines.append(f"Successful: {successful}")
    lines.append(f"Cancelled: {cancelled}")
    lines.append(f"Disputes lost: {disputes_lost}")

    spent_rows = conn.execute("SELECT asset, COALESCE(SUM(CAST(amount AS REAL)),0) v FROM escrows WHERE buyer_id=? AND status='completed' GROUP BY asset", (user_id,)).fetchall()
    earned_rows = conn.execute("SELECT asset, COALESCE(SUM(CAST(amount AS REAL)),0) v FROM escrows WHERE seller_id=? AND status='completed' GROUP BY asset", (user_id,)).fetchall()
    lines.append("Total Spent/Earned:")
    assets = sorted({r['asset'] for r in spent_rows} | {r['asset'] for r in earned_rows})
    for asset in assets:
        spent = next((Decimal(str(r['v'])) for r in spent_rows if r['asset'] == asset), Decimal('0'))
        earned = next((Decimal(str(r['v'])) for r in earned_rows if r['asset'] == asset), Decimal('0'))
        lines.append(f"• {asset}: spent {spent} / earned {earned}")

    last_reviews = conn.execute(
        "SELECT r.created_at, u.username reviewer_username FROM reviews r LEFT JOIN users u ON u.id=r.reviewer_id WHERE r.reviewed_id=? ORDER BY r.id DESC LIMIT 3",
        (user_id,),
    ).fetchall()
    lines.append("Last 3 reviews:")
    if not last_reviews:
        lines.append("• No reviews yet")
    else:
        for r in last_reviews:
            reviewer = r['reviewer_username'] or 'unknown'
            lines.append(f"• by @{reviewer} ({_date_short(r['created_at'])})")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Welcome to EscrowHub", reply_markup=_start_menu())


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        text = _render_self_profile(conn, update.effective_user, user_id, wallet)
        await update.effective_message.reply_text(text, reply_markup=_profile_menu())
        conn.commit()
    finally:
        conn.close()


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        lines = ["Your balances:"]
        for asset in ["USDT", "USDC", "BTC", "ETH", "LTC", "SOL", "XRP"]:
            available = wallet.available_balance(user_id, asset)
            locked = wallet.locked_balance(user_id, asset)
            lines.append(f"- {asset}: available={available} | locked={locked}")
        await update.effective_message.reply_text("\n".join(lines))
        conn.commit()
    finally:
        conn.close()


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn, _, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        rows = conn.execute(
            """
            SELECT e.id, e.asset, e.amount, e.status, e.created_at,
                   CASE WHEN e.buyer_id=? THEN su.username ELSE bu.username END AS counterparty
            FROM escrows e
            LEFT JOIN users bu ON bu.id=e.buyer_id
            LEFT JOIN users su ON su.id=e.seller_id
            WHERE e.buyer_id=? OR e.seller_id=?
            ORDER BY e.id DESC
            LIMIT 10
            """,
            (user_id, user_id, user_id),
        ).fetchall()
        if not rows:
            await update.effective_message.reply_text("No deal history yet.")
            return
        lines = ["Recent deals:"]
        for r in rows:
            created = _format_db_timestamp(r["created_at"])
            counterparty = r["counterparty"] or "unknown"
            lines.append(f"#{r['id']} | {r['status']} | {r['amount']} {r['asset']} | @{counterparty} | {created}")
        await update.effective_message.reply_text("\n".join(lines))
    finally:
        conn.close()


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if _clear_draft_flow(context):
        await update.effective_message.reply_text("Current draft deal has been cancelled.", reply_markup=_start_menu())
        return ConversationHandler.END
    await update.effective_message.reply_text("There is no active draft deal to cancel.")
    return ConversationHandler.END

async def profile_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "profile_back":
        await query.edit_message_text("Back to main menu", reply_markup=_start_menu())
        return

    if data == "profile_open":
        conn, wallet, tenant, _ = _services()
        try:
            user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
            text = _render_self_profile(conn, query.from_user, user_id, wallet)
        finally:
            conn.close()
        await query.edit_message_text(text, reply_markup=_profile_menu())
        return

    if data == "profile_deposit":
        keyboard = [[InlineKeyboardButton(asset, callback_data=f"dep_asset:{asset}")] for asset in ["USDT", "BTC", "ETH", "LTC", "USDC", "SOL", "XRP"]]
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="profile_open")])
        await query.edit_message_text("Deposit currency:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("dep_asset:"):
        asset = data.split(":", 1)[1]
        conn, wallet, tenant, _ = _services()
        try:
            user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
            route = wallet.get_or_create_deposit_address(user_id, asset)
            lines = [f"Deposit {asset}", f"Address: {route.address}"]
            if route.destination_tag:
                lines.append(f"Destination tag: {route.destination_tag}")
            conn.commit()
        finally:
            conn.close()
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="profile_deposit")]]))
        return

    if data.startswith("profile_withdraw_history:"):
        page = int(data.split(":")[1])
        conn, wallet, tenant, _ = _services()
        try:
            user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
            rows, page, pages = wallet.withdrawal_history(user_id, page=page, per_page=10)
        finally:
            conn.close()
        lines = [f"Your Withdrawal History (Page {page}/{pages})"]
        if not rows:
            lines.append("You have no withdrawal history yet.")
        else:
            for r in rows:
                lines.append(f"#{r['id']} | {r['asset']} | {r['amount']} | {r['status']} | {_date_short(r['created_at'])}")
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("Previous", callback_data=f"profile_withdraw_history:{page-1}"))
        if page < pages:
            nav.append(InlineKeyboardButton("Next", callback_data=f"profile_withdraw_history:{page+1}"))
        buttons = [nav] if nav else []
        buttons.append([InlineKeyboardButton("Back to Profile", callback_data="profile_open")])
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
        available_assets = [a for a, minimum in WITHDRAW_MINIMUMS.items() if wallet.available_balance(user_id, a) >= minimum]
    finally:
        conn.close()

    if not available_assets:
        await query.edit_message_text(
            "Insufficient Balance\nYou don't have sufficient balance in any currency for withdrawal.\nMinimum amounts:\n• 100 USDT\n• 0.001 BTC\n• 1 ETH",
            reply_markup=_profile_menu(),
        )
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(a, callback_data=f"wd_asset:{a}")] for a in available_assets]
    keyboard.append([InlineKeyboardButton("Back", callback_data="wd_back_profile")])
    await query.edit_message_text("Select withdrawal currency:", reply_markup=InlineKeyboardMarkup(keyboard))
    return WD_SELECT_ASSET


async def withdraw_select_asset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "wd_back_profile":
        await profile_actions(update, context)
        return ConversationHandler.END
    context.user_data["wd_asset"] = query.data.split(":", 1)[1]
    await query.edit_message_text(
        f"Enter {context.user_data['wd_asset']} withdrawal amount:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="wd_back_assets")]]),
    )
    return WD_ENTER_AMOUNT


async def withdraw_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    asset = context.user_data.get("wd_asset")
    if not asset:
        await update.effective_message.reply_text("Withdrawal session expired")
        return ConversationHandler.END
    try:
        amount = Decimal(update.effective_message.text.strip())
    except InvalidOperation:
        await update.effective_message.reply_text("Enter a valid numeric amount")
        return WD_ENTER_AMOUNT
    if amount <= 0:
        await update.effective_message.reply_text("Amount must be positive")
        return WD_ENTER_AMOUNT
    if amount < WITHDRAW_MINIMUMS[asset]:
        await update.effective_message.reply_text(f"Minimum withdrawal for {asset} is {WITHDRAW_MINIMUMS[asset]}")
        return WD_ENTER_AMOUNT
    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        if wallet.available_balance(user_id, asset) < amount:
            await update.effective_message.reply_text("Insufficient available balance")
            return WD_ENTER_AMOUNT
    finally:
        conn.close()
    context.user_data["wd_amount"] = amount
    await update.effective_message.reply_text(
        f"Enter your {asset} withdrawal address:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="wd_back_amount")]]),
    )
    return WD_ENTER_ADDRESS


async def withdraw_address_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    address = update.effective_message.text.strip()
    if not address:
        await update.effective_message.reply_text("Address is required")
        return WD_ENTER_ADDRESS
    context.user_data["wd_address"] = address
    await update.effective_message.reply_text(
        f"Is this address correct? {address}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Confirm", callback_data="wd_confirm"), InlineKeyboardButton("Cancel", callback_data="wd_cancel_addr")]]),
    )
    return WD_CONFIRM


async def withdraw_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "wd_cancel_addr":
        await query.edit_message_text(
            f"Enter your {context.user_data.get('wd_asset','ASSET')} withdrawal address:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="wd_back_amount")]]),
        )
        return WD_ENTER_ADDRESS

    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
        wallet.request_withdrawal(user_id, context.user_data["wd_asset"], Decimal(context.user_data["wd_amount"]), context.user_data["wd_address"])
        conn.commit()
    finally:
        conn.close()

    for k in ["wd_asset", "wd_amount", "wd_address"]:
        context.user_data.pop(k, None)
    await query.edit_message_text(
        "Withdrawal request submitted. Funds will arrive within a few minutes.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Profile", callback_data="profile_open")]]),
    )
    return ConversationHandler.END


async def withdraw_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "wd_back_assets":
        return await withdraw_start(update, context)
    if query.data == "wd_back_amount":
        await query.edit_message_text(
            f"Enter {context.user_data.get('wd_asset','ASSET')} withdrawal amount:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="wd_back_assets")]]),
        )
        return WD_ENTER_AMOUNT
    if query.data == "wd_back_profile":
        await profile_actions(update, context)
        return ConversationHandler.END
    return ConversationHandler.END


async def escrow_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Escrow Menu",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("New Deal", callback_data="esc_menu:new"), InlineKeyboardButton("Pending", callback_data="esc_menu:pending")],
                [InlineKeyboardButton("Active", callback_data="esc_menu:active"), InlineKeyboardButton("Disputes", callback_data="esc_menu:disputes")],
                [InlineKeyboardButton("History", callback_data="esc_menu:history")],
            ]
        ),
    )


async def _show_escrow_history_page(query, user_id: int, page: int) -> None:
    conn, _, _, escrow = _services()
    try:
        rows, page, pages = escrow.list_completed_escrows_page(user_id, page=page, per_page=10)
        buttons = []
        for row in rows:
            cp_id = escrow.counterparty_user_id(row, user_id)
            cp = conn.execute("SELECT username FROM users WHERE id=?", (cp_id,)).fetchone()
            cp_name = cp["username"] if cp and cp["username"] else "unknown"
            desc = (row["description"] or "")[:24]
            label = f"@{cp_name} {desc}"[:40]
            buttons.append([InlineKeyboardButton(label, callback_data=f"esc_hist_open:{row['id']}:{page}")])
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("Previous", callback_data=f"esc_hist_page:{page-1}"))
        if page < pages:
            nav.append(InlineKeyboardButton("Next", callback_data=f"esc_hist_page:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("Back", callback_data="esc_back_menu")])
    finally:
        conn.close()
    await query.edit_message_text(f"Completed Deals (Page {page}/{pages})", reply_markup=InlineKeyboardMarkup(buttons))


async def escrow_menu_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    conn, _, tenant, escrow = _services()
    try:
        user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
        if action == "new":
            await query.edit_message_text("Use /check_user <@username|telegram_id> to start a new deal.")
            return
        if action == "history":
            await _show_escrow_history_page(query, user_id, 1)
            return
        if action == "pending":
            rows = escrow.list_pending_escrows(user_id)
            title = "Pending escrows"
        elif action == "active":
            rows = escrow.list_active_escrows(user_id)
            title = "Active escrows"
        elif action == "disputes":
            rows = escrow.list_disputed_escrows(user_id)
            title = "Disputed escrows"
        else:
            rows = []
            title = "Escrows"
    finally:
        conn.close()
    lines = [title + ":"]
    if not rows:
        lines.append("None")
    else:
        for r in rows[:10]:
            lines.append(f"#{r['id']} | {r['amount']} {r['asset']} | {r['status']} | {_format_db_timestamp(r['created_at'])}")
    lines.append("\nUse Escrow Menu to navigate.")
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="esc_back_menu")]]))


async def escrow_history_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    conn, _, tenant, escrow = _services()
    try:
        user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
        if data.startswith("esc_hist_page:"):
            await _show_escrow_history_page(query, user_id, int(data.split(":")[2]))
            return
        if data == "esc_back_menu":
            await query.edit_message_text(
                "Escrow Menu",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("New Deal", callback_data="esc_menu:new"), InlineKeyboardButton("Pending", callback_data="esc_menu:pending")],
                        [InlineKeyboardButton("Active", callback_data="esc_menu:active"), InlineKeyboardButton("Disputes", callback_data="esc_menu:disputes")],
                        [InlineKeyboardButton("History", callback_data="esc_menu:history")],
                    ]
                ),
            )
            return
        if data.startswith("esc_hist_open:"):
            _, _, escrow_id, page = data.split(":")
            row = escrow.get_escrow(int(escrow_id))
            cp_id = escrow.counterparty_user_id(row, user_id)
            cp = conn.execute("SELECT username FROM users WHERE id=?", (cp_id,)).fetchone()
            cp_name = cp["username"] if cp and cp["username"] else "unknown"
            d = conn.execute("SELECT resolution_json FROM disputes WHERE escrow_id=? ORDER BY id DESC LIMIT 1", (int(escrow_id),)).fetchone()
            outcome = "Completed successfully"
            if d and d["resolution_json"]:
                if "refund_buyer" in d["resolution_json"]:
                    outcome = "Resolved in favor of Buyer"
                elif "release_seller" in d["resolution_json"]:
                    outcome = "Resolved in favor of Seller"
                elif "split" in d["resolution_json"]:
                    outcome = "Split resolution"
            rating = conn.execute("SELECT rating FROM reviews WHERE reviewer_id=? AND escrow_id=?", (user_id, int(escrow_id))).fetchone()
            your_rating = "⭐" * int(rating["rating"]) if rating else "Not rated yet"
            text = (
                "Deal history\n"
                f"@{cp_name} {row['description'] or ''}\n"
                f"{row['amount']} {row['asset']}\n"
                f"Created: {_format_db_timestamp(row['created_at'])}\n"
                f"Finished: {_format_db_timestamp(row['updated_at'])}\n"
                f"Outcome: {outcome}\n"
                f"Your rating: {your_rating}"
            )
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("View Counter-Party Profile", callback_data=f"esc_hist_profile:{cp_id}:{page}")], [InlineKeyboardButton("Back", callback_data=f"esc_hist_page:{page}")]]))
            return
        if data.startswith("esc_hist_profile:"):
            _, _, cp_id, page = data.split(":")
            row = conn.execute("SELECT * FROM users WHERE id=?", (int(cp_id),)).fetchone()
            if not row:
                await query.edit_message_text("Counter-party profile not found")
                return
            await query.edit_message_text(_render_user_profile(_user_profile(conn, row)), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"esc_hist_page:{page}")]]))
            return
    finally:
        conn.close()


async def deal_check_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.args:
        await update.effective_message.reply_text("Usage: /check_user <@username|telegram_id>")
        return ConversationHandler.END

    lookup = context.args[0].strip()
    conn, _, tenant, _ = _services()
    try:
        tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        if not await _require_not_frozen(update, conn):
            return ConversationHandler.END
        if lookup.startswith("@"):
            row = conn.execute("SELECT * FROM users WHERE username=?", (lookup[1:],)).fetchone()
        else:
            try:
                tg = int(lookup)
            except ValueError:
                row = None
            else:
                row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (tg,)).fetchone()
        if not row:
            await update.effective_message.reply_text("Seller not found")
            return ConversationHandler.END

        profile_data = _user_profile(conn, row)
        context.user_data["seller_id"] = profile_data["user_id"]
        context.user_data["seller_username"] = profile_data["username"]
        context.user_data["seller_telegram_id"] = profile_data["telegram_id"]

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Back", callback_data="deal_back_main"), InlineKeyboardButton("Create Deal", callback_data="deal_create")]]
        )
        profile_text = _render_user_profile(profile_data)
        context.user_data["seller_profile_text"] = profile_text
        await update.effective_message.reply_text(profile_text, reply_markup=keyboard)
        conn.commit()
        return DEAL_SEARCH_RESULT
    finally:
        conn.close()


async def deal_search_result_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "deal_back_main":
        await query.edit_message_text("Back to main menu", reply_markup=_start_menu())
        return ConversationHandler.END

    conn, wallet, tenant, _ = _services()
    try:
        buyer_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        if _is_user_frozen(conn, update.effective_user.id):
            await query.edit_message_text("Your account is frozen. Please contact support.")
            return ConversationHandler.END
        context.user_data["buyer_id"] = buyer_id
        seller_id = context.user_data.get("seller_id")
        if seller_id and int(seller_id) == int(buyer_id):
            await query.edit_message_text("You cannot create a deal with yourself", reply_markup=_start_menu())
            return ConversationHandler.END
        balances = []
        for asset in ["USDT", "BTC", "ETH", "LTC", "USDC", "SOL", "XRP"]:
            bal = wallet.available_balance(buyer_id, asset)
            if bal > 0:
                balances.append(f"{asset}: {bal}")
        context.user_data["asset"] = "USDT"
        balance_txt = " | ".join(balances) if balances else "No available balances"
        await query.edit_message_text(
            f"Your balances: {balance_txt} | min: 40 | Enter the deal amount:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="deal_back_to_search")]]),
        )
        conn.commit()
        return DEAL_ENTER_AMOUNT
    finally:
        conn.close()


async def deal_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text("Please enter a valid numeric amount")
        return DEAL_ENTER_AMOUNT

    conn, wallet, tenant, escrow_service = _services()
    try:
        buyer_id = context.user_data.get("buyer_id") or tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        if not await _require_not_frozen(update, conn):
            return ConversationHandler.END
        seller_id = context.user_data.get("seller_id")
        asset = context.user_data.get("asset", "USDT")

        if not seller_id:
            await update.effective_message.reply_text("Seller context missing. Run /check_user again.")
            return ConversationHandler.END
        if buyer_id == seller_id:
            await update.effective_message.reply_text("You cannot create a deal with yourself")
            return ConversationHandler.END
        if amount <= 0:
            await update.effective_message.reply_text("Amount must be positive")
            return DEAL_ENTER_AMOUNT

        if escrow_service.price_service.get_usd_value(asset, amount) < Decimal("40"):
            await update.effective_message.reply_text("Amount must be at least $40 equivalent")
            return DEAL_ENTER_AMOUNT
        if wallet.available_balance(buyer_id, asset) < amount:
            await update.effective_message.reply_text("Insufficient available balance")
            return DEAL_ENTER_AMOUNT

        context.user_data["amount"] = amount
        context.user_data["asset"] = asset

        await update.effective_message.reply_text(
            "Describe the deal in detail, including ALL terms. THIS WILL AFFECT HOW DISPUTES ARE RESOLVED LATER. Describe ALL deal conditions",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="deal_back_to_amount")]]),
        )
        conn.commit()
        return DEAL_ENTER_CONDITIONS
    finally:
        conn.close()


async def deal_conditions_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    conditions = update.effective_message.text.strip()
    if not conditions:
        await update.effective_message.reply_text("Conditions are required")
        return DEAL_ENTER_CONDITIONS

    conn, _, tenant, escrow = _services()
    try:
        buyer_id = context.user_data.get("buyer_id") or tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        if not await _require_not_frozen(update, conn):
            return ConversationHandler.END
        seller_id = int(context.user_data["seller_id"])
        seller_username = context.user_data.get("seller_username", "unknown")
        amount = Decimal(context.user_data["amount"])
        asset = context.user_data.get("asset", "USDT")
        context.user_data["conditions"] = conditions

        view = escrow.create_escrow(bot_id=1, buyer_id=buyer_id, seller_id=seller_id, asset=asset, amount=amount, description=conditions)
        context.user_data["escrow_id"] = view.escrow_id

        created = conn.execute("SELECT created_at FROM escrows WHERE id=?", (view.escrow_id,)).fetchone()["created_at"]
        created_label = _format_db_timestamp(created)
        context.user_data["created_label"] = created_label
        context.user_data["previous_view"] = "pending"
        msg = (
            "Pending Deal Request:\n"
            f"@{seller_username}\n"
            f"{conditions}\n"
            f"{amount} {asset}\n"
            f"Created: {created_label}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Cancel Request", callback_data="deal_cancel_request")],
                [InlineKeyboardButton("Back to Pending List", callback_data="deal_back_pending")],
                [InlineKeyboardButton("View Counter-Party Profile", callback_data="deal_view_counterparty")],
                [InlineKeyboardButton("Release Funds", callback_data="deal_release_prompt")],
            ]
        )
        await update.effective_message.reply_text(msg, reply_markup=keyboard)

        buyer_name = update.effective_user.username or str(update.effective_user.id)
        await _notify_safe(
            context,
            context.user_data.get("seller_telegram_id"),
            f"New Deal Request from @{buyer_name} | {amount} {asset} | {conditions} | Created: {created_label}",
        )
        conn.commit()
        return DEAL_PENDING_VIEW
    finally:
        conn.close()


async def deal_pending_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data
    if action != "deal_back_to_pending" and "escrow_id" not in context.user_data:
        await query.edit_message_text("Deal context expired. Run /check_user again.", reply_markup=_start_menu())
        return ConversationHandler.END

    if action == "deal_cancel_request":
        moderator = Settings.MODERATOR_USERNAME or Settings.moderator_username or "moderator"
        context.user_data["previous_view"] = "cancel_info"
        await query.edit_message_text(
            f"To cancel this deal, please contact the moderators: @{moderator}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="deal_back_to_pending")]]),
        )
        return DEAL_CANCEL_INFO

    if action == "deal_back_pending":
        return await _show_pending_list(query, context)

    if action == "deal_back_to_pending":
        return await _show_pending_view(query, context)

    if action == "deal_view_counterparty":
        context.user_data["previous_view"] = "counterparty"
        seller_id = context.user_data.get("seller_id")
        if not seller_id:
            await query.edit_message_text("Deal context expired. Run /check_user again.", reply_markup=_start_menu())
            return ConversationHandler.END
        conn, _, _, _ = _services()
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?", (int(seller_id),)).fetchone()
            if not row:
                await query.edit_message_text("Counter-party not found", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="deal_back_to_pending")]]))
                return DEAL_PENDING_VIEW
            profile_txt = _render_user_profile(_user_profile(conn, row))
        finally:
            conn.close()
        await query.edit_message_text(
            profile_txt,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="deal_back_to_pending")]]),
        )
        return DEAL_PENDING_VIEW

    if action == "deal_release_prompt":
        context.user_data["previous_view"] = "release_confirm"
        await query.edit_message_text(
            "Are you sure you want to release funds? This means the product/service has been delivered with no problems.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Back", callback_data="deal_back_to_pending"), InlineKeyboardButton("Release", callback_data="deal_release_confirm")]]
            ),
        )
        return DEAL_RELEASE_CONFIRM

    return DEAL_PENDING_VIEW






async def _show_pending_list(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    conn, _, tenant, escrow = _services()
    try:
        user_id = context.user_data.get("buyer_id") or tenant.ensure_user(query.from_user.id, query.from_user.username)
        rows = escrow.list_pending_escrows(user_id)
    finally:
        conn.close()

    if not rows:
        await query.edit_message_text("No pending deals.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="deal_back_to_pending")]]))
        return DEAL_PENDING_VIEW

    lines = ["Pending deals:"]
    for r in rows[:10]:
        lines.append(f"#{r['id']} | {r['amount']} {r['asset']} | status={r['status']} | {_format_db_timestamp(r['created_at'])}")
    lines.append("Tap Back to return to current deal view.")
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="deal_back_to_pending")]]))
    return DEAL_PENDING_VIEW

async def deal_cancel_info_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "deal_back_to_pending":
        return await _show_pending_view(query, context)
    return DEAL_CANCEL_INFO

async def _show_pending_view(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount = context.user_data.get("amount")
    asset = context.user_data.get("asset")
    seller_username = context.user_data.get("seller_username", "unknown")
    conditions = context.user_data.get("conditions", "")
    escrow_id = context.user_data.get("escrow_id")
    created_label = context.user_data.get("created_label", "unknown")
    if not escrow_id:
        await query.edit_message_text("Deal context expired. Run /check_user again.", reply_markup=_start_menu())
        return ConversationHandler.END
    text = (
        "Pending Deal Request:\n"
        f"@{seller_username}\n"
        f"{conditions}\n"
        f"{amount} {asset}\n"
        f"Created: {created_label}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Cancel Request", callback_data="deal_cancel_request")],
            [InlineKeyboardButton("Back to Pending List", callback_data="deal_back_pending")],
            [InlineKeyboardButton("View Counter-Party Profile", callback_data="deal_view_counterparty")],
            [InlineKeyboardButton("Release Funds", callback_data="deal_release_prompt")],
        ]
    )
    await query.edit_message_text(text, reply_markup=keyboard)
    return DEAL_PENDING_VIEW


async def deal_release_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "deal_back_to_pending":
        return await _show_pending_view(query, context)

    conn, _, tenant, escrow = _services()
    try:
        buyer_id = context.user_data.get("buyer_id") or tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        if not await _require_not_frozen(update, conn):
            return ConversationHandler.END
        if "escrow_id" not in context.user_data:
            await query.edit_message_text("Deal context expired. Run /check_user again.", reply_markup=_start_menu())
            return ConversationHandler.END
        escrow_id = int(context.user_data["escrow_id"])
        view = escrow.release(escrow_id, actor_user_id=buyer_id)

        seller_username = context.user_data.get("seller_username", "unknown")
        date_row = conn.execute("SELECT updated_at FROM escrows WHERE id=?", (escrow_id,)).fetchone()["updated_at"]
        date_label = _format_db_timestamp(date_row)
        description = context.user_data.get("conditions", "")
        release_msg = (
            f"@{seller_username} | {view.amount} {view.asset} | {date_label} - {description} | "
            f"Payment of {view.amount} {view.asset} has been released to the seller."
        )
        stars = [InlineKeyboardButton("⭐", callback_data=f"deal_rate_seller:{i}") for i in range(1, 6)]
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Back to Main Menu", callback_data="deal_finish")], stars])
        await query.edit_message_text(release_msg, reply_markup=keyboard)

        buyer_name = update.effective_user.username or str(update.effective_user.id)
        await _notify_safe(
            context,
            context.user_data.get("seller_telegram_id"),
            f"Payment of {view.amount} {view.asset} has been released to your account by @{buyer_name}",
        )
        conn.commit()
        return DEAL_RATE_SELLER
    finally:
        conn.close()


async def deal_rate_seller(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "deal_finish":
        await query.edit_message_text("Done. Back to main menu.", reply_markup=_start_menu())
        return ConversationHandler.END

    rating = int(data.split(":")[1])
    if rating < 1 or rating > 5:
        await query.edit_message_text("Invalid rating")
        return DEAL_RATE_SELLER
    conn, _, tenant, _ = _services()
    try:
        buyer_id = context.user_data.get("buyer_id") or tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        if "seller_id" not in context.user_data or "escrow_id" not in context.user_data:
            await query.edit_message_text("Deal context expired. Run /check_user again.", reply_markup=_start_menu())
            return ConversationHandler.END
        seller_id = int(context.user_data["seller_id"])
        escrow_id = int(context.user_data["escrow_id"])
        conn.execute(
            "INSERT OR IGNORE INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)",
            (buyer_id, seller_id, escrow_id, rating),
        )
        conn.commit()

        stars = [InlineKeyboardButton("⭐", callback_data=f"deal_rate_buyer:{escrow_id}:{i}") for i in range(1, 6)]
        await _notify_safe(
            context,
            context.user_data.get("seller_telegram_id"),
            f"The buyer has released funds. Please rate your experience with @{update.effective_user.username or update.effective_user.id}",
            InlineKeyboardMarkup([stars]),
        )
        await query.edit_message_text("Seller rating saved. Waiting seller rating.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Main Menu", callback_data="deal_finish")]]))
        return DEAL_RATE_BUYER
    finally:
        conn.close()


async def seller_rate_buyer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, escrow_id_str, rating_str = query.data.split(":")
    escrow_id = int(escrow_id_str)
    rating = int(rating_str)

    conn, _, tenant, _ = _services()
    try:
        reviewer_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        esc = conn.execute("SELECT buyer_id FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not esc:
            await query.edit_message_text("Escrow not found")
            return
        if rating < 1 or rating > 5:
            await query.edit_message_text("Invalid rating")
            return
        seller_match = conn.execute("SELECT 1 FROM escrows WHERE id=? AND seller_id=?", (escrow_id, reviewer_id)).fetchone()
        if not seller_match:
            await query.edit_message_text("Only the seller can rate this buyer")
            return
        conn.execute(
            "INSERT OR IGNORE INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)",
            (reviewer_id, int(esc["buyer_id"]), escrow_id, rating),
        )
        conn.commit()
        await query.edit_message_text("Thank you. Your rating was saved.")
    finally:
        conn.close()


async def deal_back_from_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    profile_txt = context.user_data.get("seller_profile_text") or (
        f"@{context.user_data.get('seller_username','unknown')}\n"
        "Seller found\n"
        "Use Create Deal to continue."
    )
    await query.edit_message_text(
        profile_txt,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="deal_back_main"), InlineKeyboardButton("Create Deal", callback_data="deal_create")]]),
    )
    return DEAL_SEARCH_RESULT


async def deal_back_from_conditions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    buyer_id = context.user_data.get("buyer_id")
    if not buyer_id:
        await query.edit_message_text("Deal context expired. Run /check_user again.", reply_markup=_start_menu())
        return ConversationHandler.END

    conn, wallet, _, _ = _services()
    try:
        balances = []
        for asset in ["USDT", "BTC", "ETH", "LTC", "USDC", "SOL", "XRP"]:
            bal = wallet.available_balance(buyer_id, asset)
            if bal > 0:
                balances.append(f"{asset}: {bal}")
        balance_txt = " | ".join(balances) if balances else "No available balances"
    finally:
        conn.close()

    await query.edit_message_text(
        f"Your balances: {balance_txt} | min: 40 | Enter the deal amount:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="deal_back_to_search")]]),
    )
    return DEAL_ENTER_AMOUNT


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
            f"BTC watcher\n- last run: {b['last_run_at']}\n- last success: {b['last_success_at']}\n"
            f"- consecutive failures: {b['consecutive_failures']}\n- last error: {b['last_error']}\n\n"
            f"ETH watcher\n- last run: {e['last_run_at']}\n- last success: {e['last_success_at']}\n"
            f"- consecutive failures: {e['consecutive_failures']}\n- last error: {e['last_error']}"
        )
        await update.effective_message.reply_text(msg)
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


async def check_user_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Use /check_user <@username|telegram_id>")


async def support_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Support Team: @your_support_handle")


async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    mapping = {
        "profile": profile,
        "escrow_menu": escrow_menu,
        "check_user": check_user_hint,
        "support_team": support_team,
    }
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

    conv = ConversationHandler(
        entry_points=[CommandHandler("check_user", deal_check_user)],
        states={
            DEAL_SEARCH_RESULT: [CallbackQueryHandler(deal_search_result_cb, pattern="^deal_(create|back_main)$")],
            DEAL_ENTER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, deal_amount_input),
                CallbackQueryHandler(deal_back_from_amount, pattern="^deal_back_to_search$"),
            ],
            DEAL_ENTER_CONDITIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, deal_conditions_input),
                CallbackQueryHandler(deal_back_from_conditions, pattern="^deal_back_to_amount$"),
            ],
            DEAL_PENDING_VIEW: [CallbackQueryHandler(deal_pending_actions, pattern="^deal_(cancel_request|back_pending|view_counterparty|release_prompt|back_to_pending)$")],
            DEAL_CANCEL_INFO: [CallbackQueryHandler(deal_cancel_info_actions, pattern="^deal_back_to_pending$")],
            DEAL_RELEASE_CONFIRM: [CallbackQueryHandler(deal_release_confirm, pattern="^deal_(release_confirm|back_to_pending)$")],
            DEAL_RATE_SELLER: [CallbackQueryHandler(deal_rate_seller, pattern=r"^deal_(rate_seller:\d+|finish)$")],
            DEAL_RATE_BUYER: [CallbackQueryHandler(deal_rate_seller, pattern="^deal_finish$")],
        },
        fallbacks=[CommandHandler("start", start), CommandHandler("cancel", cancel_flow)],
        per_chat=True,
        per_user=True,
    )

    withdraw_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_start, pattern=r"^profile_withdraw$")],
        states={
            WD_SELECT_ASSET: [CallbackQueryHandler(withdraw_select_asset, pattern=r"^wd_(asset:[A-Z]+|back_profile)$")],
            WD_ENTER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_input),
                CallbackQueryHandler(withdraw_back, pattern=r"^wd_(back_assets|back_profile)$"),
            ],
            WD_ENTER_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_address_input),
                CallbackQueryHandler(withdraw_back, pattern=r"^wd_back_amount$"),
            ],
            WD_CONFIRM: [CallbackQueryHandler(withdraw_confirm, pattern=r"^wd_(confirm|cancel_addr)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
        per_chat=True,
        per_user=True,
    )

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("cancel", cancel_flow))
    app.add_handler(CommandHandler("watcher_status", watcher_status))
    app.add_handler(CommandHandler("run_signer", run_signer))
    app.add_handler(CommandHandler("support", support_team))
    app.add_handler(CallbackQueryHandler(seller_rate_buyer_callback, pattern=r"^deal_rate_buyer:\d+:\d+$"))
    app.add_handler(withdraw_conv)
    app.add_handler(CallbackQueryHandler(profile_actions, pattern=r"^(profile_(?!withdraw$)|dep_asset:).*$"))
    app.add_handler(CallbackQueryHandler(escrow_menu_actions, pattern=r"^esc_menu:"))
    app.add_handler(CallbackQueryHandler(escrow_history_actions, pattern=r"^(esc_hist_|esc_back_menu).*$"))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_menu_click))
    app.run_polling()


if __name__ == "__main__":
    main()
