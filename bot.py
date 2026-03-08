from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
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

def _parse_admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_USER_IDS", "")
    parsed: set[int] = set()
    invalid: list[str] = []
    for token in raw.split(","):
        item = token.strip()
        if not item:
            continue
        try:
            parsed.add(int(item))
        except ValueError:
            invalid.append(item)
    if invalid:
        LOGGER.warning("Ignoring malformed ADMIN_USER_IDS values: %s", ",".join(invalid))
    if not parsed:
        LOGGER.warning("ADMIN_USER_IDS is empty or invalid; admin-only commands will be inaccessible")
    return parsed


ADMIN_IDS = _parse_admin_ids()

BASE_ASSETS = ["USDT", "USDC", "BTC", "ETH", "LTC", "SOL", "XRP"]


def _enabled_assets() -> list[str]:
    return [a for a in BASE_ASSETS if a != "SOL" or Settings.sol_enabled]


_RATE_BUCKETS: dict[tuple[int, str], deque[float]] = defaultdict(deque)
_MAX_RATE_BUCKETS = 10000
_EVICTION_GRACE_S = 60


def _prune_rate_buckets(now: float) -> None:
    # In-process limiter only (per worker). Replaceable with shared backend later if needed.
    stale_keys: list[tuple[int, str]] = []
    for key, bucket in _RATE_BUCKETS.items():
        while bucket and (now - bucket[0]) > _EVICTION_GRACE_S:
            bucket.popleft()
        if not bucket:
            stale_keys.append(key)
    for key in stale_keys:
        _RATE_BUCKETS.pop(key, None)


def _is_rate_limited(user_id: int, action: str, limit: int, window_s: int) -> bool:
    now = time.time()
    _prune_rate_buckets(now)
    if len(_RATE_BUCKETS) > _MAX_RATE_BUCKETS:
        # deterministic shedding of oldest single-event buckets when memory grows too much
        for key in sorted(_RATE_BUCKETS.keys())[: len(_RATE_BUCKETS) - _MAX_RATE_BUCKETS]:
            _RATE_BUCKETS.pop(key, None)
    key = (int(user_id), action)
    bucket = _RATE_BUCKETS[key]
    while bucket and (now - bucket[0]) > window_s:
        bucket.popleft()
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False


async def _enforce_rate_limit(query, user_id: int, action: str, limit: int = 4, window_s: int = 10) -> bool:
    if not _is_rate_limited(user_id, action, limit, window_s):
        return False
    try:
        await query.answer("Too many requests. Please slow down.", show_alert=False)
    except Exception:
        pass
    return True


async def _enforce_text_rate_limit(update: Update, action: str, limit: int = 4, window_s: int = 10) -> bool:
    if not _is_rate_limited(update.effective_user.id, action, limit, window_s):
        return False
    await update.effective_message.reply_text("Too many requests. Please slow down.")
    return True


def _runtime_bot_id(conn, tenant: TenantService) -> int:
    bot_id = int(Settings.bot_id)
    if not tenant.get_tenant(bot_id):
        raise ValueError(f"Tenant bot {bot_id} is not configured")
    return bot_id

(
    DEAL_SEARCH_RESULT,
    DEAL_ENTER_AMOUNT,
    DEAL_ENTER_CONDITIONS,
    DEAL_PENDING_VIEW,
    DEAL_CANCEL_INFO,
    DEAL_RELEASE_CONFIRM,
    DEAL_RATE_SELLER,
    DEAL_RATE_BUYER,
    DEAL_SEARCH_INPUT,
) = range(9)


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
            [InlineKeyboardButton("👤 Profile", callback_data="profile"), InlineKeyboardButton("🤝 Escrow Menu", callback_data="escrow_menu")],
            [InlineKeyboardButton("🔍Check User / 💡​Create Deal ", callback_data="check_user")],
            [InlineKeyboardButton("​💬​Support Team", callback_data="support_team")],
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
    review_stats = conn.execute("SELECT AVG(rating) r, COUNT(*) c FROM reviews WHERE reviewed_id=?", (user_id,)).fetchone()
    rating = review_stats["r"]
    spent_rows = conn.execute("SELECT amount FROM escrows WHERE buyer_id=? AND status='completed'", (user_id,)).fetchall()
    earned_rows = conn.execute("SELECT amount FROM escrows WHERE seller_id=? AND status='completed'", (user_id,)).fetchall()
    spent = sum((Decimal(r["amount"]) for r in spent_rows), Decimal("0"))
    earned = sum((Decimal(r["amount"]) for r in earned_rows), Decimal("0"))

    trust_level = "High" if completed_deals >= 20 and disputes <= 1 else "Medium" if completed_deals >= 5 else "Low"
    return {
        "username": user_row["username"] or "unknown",
        "registered_date": user_row["created_at"],
        "trust_level": trust_level,
        "rating": float(rating) if rating is not None else 0.0,
        "review_count": int(review_stats["c"] or 0),
        "deals": int(completed_deals),
        "spent": spent,
        "earned": earned,
        "user_id": user_id,
        "telegram_id": int(user_row["telegram_id"]),
    }


def _render_user_profile(profile: dict) -> str:
    rating_line = "Rating: Too few reviews" if profile.get("review_count", 0) < 3 else f"Rating: {profile['rating']:.1f}/5 from {profile['review_count']} reviews"
    return (
        f"@{profile['username']}\n"
        "Seller found\n"
        f"Registered date: {_date_short(profile['registered_date'])}\n"
        f"Trust level: {profile['trust_level']}\n"
        f"{rating_line}\n"
        f"Completed deals: {profile['deals']}\n"
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
    "dep_asset",
}

ASSET_ICONS = {
    "USDT": "💲",
    "USDC": "💵",
    "BTC": "₿",
    "ETH": "⟠",
    "LTC": "Ł",
    "SOL": "◎",
    "XRP": "✕",
}

DEPOSIT_EXPLORERS = {
    "BTC": "https://blockstream.info/address/{address}",
    "ETH": "https://etherscan.io/address/{address}",
    "USDT": "https://etherscan.io/address/{address}",
    "USDC": "https://etherscan.io/address/{address}",
    "LTC": "https://litecoinspace.org/address/{address}",
    "XRP": "https://xrpscan.com/account/{address}",
    "SOL": "https://solscan.io/account/{address}",
}


def _is_user_frozen(conn, telegram_user_id: int) -> bool:
    row = conn.execute("SELECT frozen FROM users WHERE telegram_id=?", (telegram_user_id,)).fetchone()
    return bool(row and int(row["frozen"]))


async def _require_not_frozen(update: Update, conn) -> bool:
    if _is_user_frozen(conn, update.effective_user.id):
        if update.callback_query:
            await update.callback_query.edit_message_text("Your account is frozen. Please contact support.")
        elif update.effective_message:
            await update.effective_message.reply_text("Your account is frozen. Please contact support.")
        return False
    return True


def _clear_draft_flow(context: ContextTypes.DEFAULT_TYPE) -> bool:
    removed = False
    for key in DRAFT_FLOW_KEYS:
        if key in context.user_data:
            removed = True
            context.user_data.pop(key, None)
    return removed


def _parse_callback_int(data: str, prefix: str) -> int | None:
    if not data.startswith(prefix):
        return None
    try:
        return int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        return None


def _parse_callback_parts(data: str, expected_prefix: str, expected_len: int) -> list[str] | None:
    parts = data.split(":")
    if len(parts) != expected_len or not data.startswith(expected_prefix):
        return None
    return parts


async def _show_frozen_callback(query) -> None:
    await query.edit_message_text("Your account is frozen. Please contact support.")


def _render_profile_text(conn, wallet: WalletService, tenant: TenantService, telegram_user) -> str:
    user_id = tenant.ensure_user(telegram_user.id, telegram_user.username)
    return _render_self_profile(conn, telegram_user, user_id, wallet)


async def _show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_existing: bool = False) -> None:
    conn, wallet, tenant, _ = _services()
    try:
        text = _render_profile_text(conn, wallet, tenant, update.effective_user)
        conn.commit()
    finally:
        conn.close()

    if edit_existing and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_profile_menu())
    else:
        await update.effective_message.reply_text(text, reply_markup=_profile_menu())


async def _seller_lookup_and_render(update: Update, context: ContextTypes.DEFAULT_TYPE, lookup: str) -> int:
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

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="deal_back_main"), InlineKeyboardButton("Create Deal", callback_data="deal_create")]])
        profile_text = _render_user_profile(profile_data)
        context.user_data["seller_profile_text"] = profile_text
        await update.effective_message.reply_text(profile_text, reply_markup=keyboard)
        conn.commit()
        return DEAL_SEARCH_RESULT
    finally:
        conn.close()

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
    DEPOSIT_ENTER_AMOUNT,
) = range(100, 105)


def _profile_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💰 Deposit", callback_data="profile_deposit"), InlineKeyboardButton("Withdraw", callback_data="profile_withdraw")],
            [InlineKeyboardButton("📂 Withdrawal History", callback_data="profile_withdraw_history:1")],
            [InlineKeyboardButton("🔙 Back", callback_data="profile_back")],
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
        f"👤 Your Telegram ID: {telegram_user.id}",
        f"👤 First Name: {first_name}\n",
        "💰 Balance:",
    ]
    for asset in _enabled_assets():
        a = wallet.available_balance(user_id, asset)
        icon = ASSET_ICONS.get(asset, "•")
        if a > 0:
            lines.append(f"{icon} {asset}: `{a}`")
        else:
            lines.append(f"{icon} {asset}: 0")
    trust = "High" if successful >= 20 else "Medium" if successful >= 5 else "Low"
    lines.append(f"📅 Registered on : {_date_short(registered)} \n")
    lines.append(f"🛡️ Trust level: {trust}")
    if int(rev["cnt"] or 0) < 3:
        lines.append("⭐ Rating: Too few reviews\n")
    else:
        lines.append(f"⭐ Rating: {float(rev['avg_rating']):.1f}/5 from {int(rev['cnt'])} reviews\n")
    lines.append("🤝  Deals:")
    lines.append(f".✅ Successful: {successful}")
    lines.append(f".🚫 Cancelled: {cancelled}")
    lines.append(f".⚠️ Disputes lost: {disputes_lost}\n")

    spent_rows = conn.execute("SELECT asset, amount FROM escrows WHERE buyer_id=? AND status='completed'", (user_id,)).fetchall()
    earned_rows = conn.execute("SELECT asset, amount FROM escrows WHERE seller_id=? AND status='completed'", (user_id,)).fetchall()
    lines.append("📈  Total Spent/Earned:\n")
    assets = sorted({r['asset'] for r in spent_rows} | {r['asset'] for r in earned_rows})
    for asset in assets:
        spent = sum((Decimal(r['amount']) for r in spent_rows if r['asset'] == asset), Decimal('0'))
        earned = sum((Decimal(r['amount']) for r in earned_rows if r['asset'] == asset), Decimal('0'))
        lines.append(f"• {asset}: spent {spent} / earned {earned}\n")

    last_reviews = conn.execute(
        "SELECT r.created_at, u.username reviewer_username FROM reviews r LEFT JOIN users u ON u.id=r.reviewer_id WHERE r.reviewed_id=? ORDER BY r.id DESC LIMIT 3",
        (user_id,),
    ).fetchall()
    lines.append("📝 Last 3 reviews:")
    if not last_reviews:
        lines.append("• No reviews yet")
    else:
        for r in last_reviews:
            reviewer = r['reviewer_username'] or 'unknown'
            lines.append(f"• by @{reviewer} ({_date_short(r['created_at'])})")
    return "\n".join(lines)



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("There is only 3% commission in bot. The lowest on market!", reply_markup=_start_menu())

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_profile(update, context, edit_existing=False)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        lines = ["Your balances:"]
        for asset in _enabled_assets():
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

async def profile_open_from_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _show_profile(update, context, edit_existing=True)


async def profile_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data not in {"profile_back", "profile_open"}:
        conn, _, _, _ = _services()
        try:
            if _is_user_frozen(conn, query.from_user.id):
                await _show_frozen_callback(query)
                return
        finally:
            conn.close()

    if data == "profile_back":
        await query.edit_message_text("There is only 3% commission in bot. The lowest on market!", reply_markup=_start_menu())
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
        keyboard = [[InlineKeyboardButton(asset, callback_data=f"dep_asset:{asset}")] for asset in _enabled_assets()]
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="profile_open")])
        await query.edit_message_text("Deposit currency:", reply_markup=InlineKeyboardMarkup(keyboard))
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
        buttons = []
        if not rows:
            lines.append("You have no withdrawal history yet.")
        else:
            for r in rows:
                label = f"#{r['id']} | {r['asset']} | {r['amount']} | {r['status']}"
                buttons.append([InlineKeyboardButton(label, callback_data=f"esc_wd_open:{r['id']}:{page}")])
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("Previous", callback_data=f"profile_withdraw_history:{page-1}"))
        if page < pages:
            nav.append(InlineKeyboardButton("Next", callback_data=f"profile_withdraw_history:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("🔙 to Profile", callback_data="profile_open")])
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("esc_wd_open:"):
        parts = _parse_callback_parts(data, "esc_wd_open:", 3)
        if not parts:
            await query.edit_message_text("Withdrawal entry is stale. Please reopen your history.")
            return
        _, withdrawal_id, page = parts
        conn, wallet, tenant, _ = _services()
        try:
            user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
            row = conn.execute("SELECT * FROM withdrawals WHERE id=? AND user_id=?", (int(withdrawal_id), user_id)).fetchone()
        finally:
            conn.close()
        if not row:
            await query.edit_message_text("Withdrawal not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"profile_withdraw_history:{page}")]]))
            return
        await query.edit_message_text(
            "\n".join(
                [
                    f"📤 Withdrawal #{row['id']}",
                    "",
                    f"Asset: {row['asset']}",
                    f"Amount: {row['amount']}",
                    f"Address: {row['destination_address']}",
                    f"Status: {row['status']}",
                    f"Date: {row['created_at']}",
                ]
            ),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"profile_withdraw_history:{page}")]]),
        )
        return


async def deposit_select_asset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    asset = query.data.split(":", 1)[1]
    context.user_data["dep_asset"] = asset
    icon = ASSET_ICONS.get(asset, "💰")
    await query.edit_message_text(
        f"{icon} {asset} Deposit\n\n"
        f"Enter the amount in {asset} to deposit (min 45, max 10000).\n\n"
        "Provider fee (3%): included in invoice.\n"
        "Platform deposit fee (2%): deducted from credited amount.\n\n"
        f"Example: deposit 100 {asset} → pay 103 {asset} → receive 98 {asset}.\n\n"
        "Send /cancel to abort.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="profile_deposit")]]),
    )
    return DEPOSIT_ENTER_AMOUNT


async def deposit_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _enforce_text_rate_limit(update, "deposit_amount_input", limit=6, window_s=20):
        return DEPOSIT_ENTER_AMOUNT
    asset = context.user_data.get("dep_asset")
    if not asset:
        await update.effective_message.reply_text("Deposit session expired. Please select asset again.")
        return ConversationHandler.END
    try:
        amount = Decimal(update.effective_message.text.strip())
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text(f"Please enter a valid {asset} amount.")
        return DEPOSIT_ENTER_AMOUNT
    if amount < Decimal("45") or amount > Decimal("10000"):
        await update.effective_message.reply_text(f"Enter an amount between 45 and 10000 {asset}.")
        return DEPOSIT_ENTER_AMOUNT

    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        route = wallet.get_or_create_deposit_address(user_id, asset)
        conn.commit()
    finally:
        conn.close()

    explorer_template = DEPOSIT_EXPLORERS.get(asset, "https://etherscan.io/address/{address}")
    buttons = [
        [InlineKeyboardButton("🔙 Back", callback_data="profile_deposit")],
        [InlineKeyboardButton("🔗 View Address", url=explorer_template.format(address=route.address))],
    ]
    await update.effective_message.reply_text(
        f"📥 Deposit\n\nAddress:\n{route.address}\n\nDeposit fee: 2% will be held — {asset} network",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    if route.destination_tag:
        await update.effective_message.reply_text(f"Destination tag: {route.destination_tag}")
    return ConversationHandler.END


async def deposit_cancel_to_assets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await profile_actions(update, context)
    return ConversationHandler.END


async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
        if _is_user_frozen(conn, query.from_user.id):
            await _show_frozen_callback(query)
            return ConversationHandler.END
        available_assets = [a for a, minimum in WITHDRAW_MINIMUMS.items() if a in _enabled_assets() and wallet.available_balance(user_id, a) >= minimum]
    finally:
        conn.close()

    if not available_assets:
        usdt_icon = ASSET_ICONS.get("USDT", "•")
        btc_icon = ASSET_ICONS.get("BTC", "•")
        eth_icon = ASSET_ICONS.get("ETH", "•")
        ltc_icon = ASSET_ICONS.get("LTC", "•")
        sol_icon = ASSET_ICONS.get("SOL", "•")
        xrp_icon = ASSET_ICONS.get("XRP", "•")
        await query.edit_message_text(
            "❌ Insufficient Balance\n\n"
            "You don't have sufficient balance in any currency for withdrawal.\n\n"
            "Minimum amounts:\n"
            f"{usdt_icon} 100 USDT\n"
            f"{btc_icon} 0.001 BTC\n"
            f"{eth_icon} 1 ETH\n"
            f"{ltc_icon} 1 LTC\n"
            f"{sol_icon} 1 SOL\n"
            f"{xrp_icon} 10 XRP\n\n"
            "Please deposit funds to your account before attempting a withdrawal.",
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
    if await _enforce_text_rate_limit(update, "withdraw_amount_input", limit=6, window_s=20):
        return WD_ENTER_AMOUNT
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
    asset = context.user_data.get("wd_asset")
    conn, wallet, _, _ = _services()
    try:
        try:
            wallet.validate_withdrawal_address(asset, address)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return WD_ENTER_ADDRESS
    finally:
        conn.close()
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
    if await _enforce_rate_limit(query, query.from_user.id, "withdraw_confirm", limit=3, window_s=20):
        return WD_CONFIRM

    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
        if _is_user_frozen(conn, query.from_user.id):
            await _show_frozen_callback(query)
            return ConversationHandler.END
        try:
            wallet.request_withdrawal(user_id, context.user_data["wd_asset"], Decimal(context.user_data["wd_amount"]), context.user_data["wd_address"])
        except ValueError as exc:
            conn.rollback()
            await query.edit_message_text(str(exc), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Profile", callback_data="profile_open")]]))
            return ConversationHandler.END
        conn.commit()
    finally:
        conn.close()

    for k in ["wd_asset", "wd_amount", "wd_address"]:
        context.user_data.pop(k, None)
    await query.edit_message_text(
        "Withdrawal request submitted. Funds will arrive within a few minutes.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Profile", callback_data="profile_open")]]),
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


def _escrow_menu_markup(pending_count: int, active_count: int, disputes_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕​ New Deal", callback_data="esc_menu:new")],
            [
                InlineKeyboardButton(f"⌛ Pending — {pending_count}", callback_data="esc_menu:pending"),
                InlineKeyboardButton(f"🤝 Active — {active_count}", callback_data="esc_menu:active"),
            ],
            [
                InlineKeyboardButton(f"⚖️ Disputes — {disputes_count}", callback_data="esc_menu:disputes"),
                InlineKeyboardButton(" 📂 History", callback_data="esc_menu:history"),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="profile_back")],
        ]
    )


def _escrow_counts(conn, user_id: int) -> tuple[int, int, int]:
    pending_count = conn.execute("SELECT COUNT(*) c FROM escrows WHERE status='pending' AND (buyer_id=? OR seller_id=?)", (user_id, user_id)).fetchone()["c"]
    active_count = conn.execute("SELECT COUNT(*) c FROM escrows WHERE status='active' AND (buyer_id=? OR seller_id=?)", (user_id, user_id)).fetchone()["c"]
    disputes_count = conn.execute("SELECT COUNT(*) c FROM escrows WHERE status='disputed' AND (buyer_id=? OR seller_id=?)", (user_id, user_id)).fetchone()["c"]
    return int(pending_count), int(active_count), int(disputes_count)


async def escrow_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int | None = None) -> None:
    conn, _, tenant, _ = _services()
    try:
        resolved_user_id = user_id or tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        pending_count, active_count, disputes_count = _escrow_counts(conn, resolved_user_id)
    finally:
        conn.close()
    if update.callback_query:
        await update.callback_query.edit_message_text("Escrow Menu", reply_markup=_escrow_menu_markup(pending_count, active_count, disputes_count))
    else:
        await update.effective_message.reply_text("Escrow Menu", reply_markup=_escrow_menu_markup(pending_count, active_count, disputes_count))


def _paginate_rows(rows, page: int, per_page: int = 10):
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(int(page), pages))
    start = (page - 1) * per_page
    return rows[start : start + per_page], page, pages


async def _show_pending_page(query, user_id: int, page: int) -> None:
    conn, _, _, escrow = _services()
    try:
        rows = escrow.list_pending_escrows(user_id)
        page_rows, page, pages = _paginate_rows(rows, page)
    finally:
        conn.close()
    buttons = [[InlineKeyboardButton(f"#{r['id']} | {r['asset']} | {r['amount']} | {r['status']}", callback_data=f"esc_open:{r['id']}:pending:{page}")] for r in page_rows]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("Previous", callback_data=f"esc_pending_page:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("Next", callback_data=f"esc_pending_page:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("Back", callback_data="esc_back_menu")])
    await query.edit_message_text(f"⌛ Pending escrows\nPage {page}/{pages}", reply_markup=InlineKeyboardMarkup(buttons))


async def _show_active_page(query, user_id: int, page: int) -> None:
    conn, _, _, escrow = _services()
    try:
        rows = escrow.list_active_escrows(user_id)
        page_rows, page, pages = _paginate_rows(rows, page)
    finally:
        conn.close()
    buttons = [[InlineKeyboardButton(f"#{r['id']} | {r['asset']} | {r['amount']} | {r['status']}", callback_data=f"esc_open:{r['id']}:active:{page}")] for r in page_rows]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("Previous", callback_data=f"esc_active_page:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("Next", callback_data=f"esc_active_page:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("Back", callback_data="esc_back_menu")])
    await query.edit_message_text(f"🤝 Active escrows\nPage {page}/{pages}", reply_markup=InlineKeyboardMarkup(buttons))


async def _show_disputes_page(query, user_id: int, page: int) -> None:
    conn, _, _, escrow = _services()
    try:
        rows = escrow.list_disputed_escrows(user_id)
        page_rows, page, pages = _paginate_rows(rows, page)
    finally:
        conn.close()
    buttons = [[InlineKeyboardButton(f"#{r['id']} | {r['asset']} | {r['amount']} | {r['status']}", callback_data=f"esc_open:{r['id']}:disputes:{page}")] for r in page_rows]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("Previous", callback_data=f"esc_disputes_page:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("Next", callback_data=f"esc_disputes_page:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("Back", callback_data="esc_back_menu")])
    await query.edit_message_text(f"⚖️ Disputed escrows\nPage {page}/{pages}", reply_markup=InlineKeyboardMarkup(buttons))


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
        if _is_user_frozen(conn, query.from_user.id):
            await _show_frozen_callback(query)
            return
        if action == "history":
            await _show_escrow_history_page(query, user_id, 1)
            return
        if action == "pending":
            await _show_pending_page(query, user_id, 1)
            return
        elif action == "active":
            await _show_active_page(query, user_id, 1)
            return
        elif action == "disputes":
            await _show_disputes_page(query, user_id, 1)
            return
        else:
            await escrow_menu(update, context, user_id=user_id)
            return
    finally:
        conn.close()


async def escrow_history_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if await _enforce_rate_limit(query, query.from_user.id, "escrow_history", limit=12, window_s=10):
        return
    data = query.data
    conn, _, tenant, escrow = _services()
    try:
        user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
        if data.startswith("esc_hist_page:"):
            page = _parse_callback_int(data, "esc_hist_page:")
            if page is None:
                await query.edit_message_text("History view is stale. Please reopen Escrow Menu -> History.")
                return
            await _show_escrow_history_page(query, user_id, page)
            return
        if data.startswith("esc_pending_page:"):
            page = _parse_callback_int(data, "esc_pending_page:")
            if page is None:
                await query.edit_message_text("Pending view is stale. Please reopen Escrow Menu.")
                return
            await _show_pending_page(query, user_id, page)
            return
        if data.startswith("esc_active_page:"):
            page = _parse_callback_int(data, "esc_active_page:")
            if page is None:
                await query.edit_message_text("Active view is stale. Please reopen Escrow Menu.")
                return
            await _show_active_page(query, user_id, page)
            return
        if data.startswith("esc_disputes_page:"):
            page = _parse_callback_int(data, "esc_disputes_page:")
            if page is None:
                await query.edit_message_text("Disputes view is stale. Please reopen Escrow Menu.")
                return
            await _show_disputes_page(query, user_id, page)
            return
        if data == "esc_back_menu":
            await escrow_menu(update, context, user_id=user_id)
            return
        if data.startswith("esc_open:"):
            parts = _parse_callback_parts(data, "esc_open:", 4)
            if not parts:
                await query.edit_message_text("Escrow item is stale. Please reopen Escrow Menu.")
                return
            _, escrow_id_str, section, page = parts
            try:
                escrow_id = int(escrow_id_str)
            except ValueError:
                await query.edit_message_text("Escrow item is stale. Please reopen Escrow Menu.")
                return
            row = escrow.get_escrow(escrow_id)
            text = (
                f"Escrow #{row['id']}\n\n"
                f"Status: {row['status']}\n"
                f"Amount: {row['amount']} {row['asset']}\n"
                f"Created: {_format_db_timestamp(row['created_at'])}\n"
                f"Updated: {_format_db_timestamp(row['updated_at'])}\n"
                f"Description: {row['description'] or '-'}"
            )
            back_cb = {
                "pending": f"esc_pending_page:{page}",
                "active": f"esc_active_page:{page}",
                "disputes": f"esc_disputes_page:{page}",
            }.get(section, "esc_back_menu")
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=back_cb)]]))
            return
        if data.startswith("esc_hist_open:"):
            parts = _parse_callback_parts(data, "esc_hist_open:", 4)
            if not parts:
                await query.edit_message_text("Deal history item is stale. Please reopen history.")
                return
            _, _, escrow_id, page = parts
            try:
                row = escrow.get_escrow(int(escrow_id))
            except ValueError:
                await query.edit_message_text("Deal is no longer available.")
                return
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
            buttons = [[InlineKeyboardButton("View Counter-Party Profile", callback_data=f"esc_hist_profile:{cp_id}:{page}:{escrow_id}")]]
            if not rating:
                stars = [InlineKeyboardButton(f"⭐{i}", callback_data=f"esc_hist_rate:{escrow_id}:{i}:{page}") for i in range(1, 6)]
                buttons.append(stars)
            buttons.append([InlineKeyboardButton("Back", callback_data=f"esc_hist_page:{page}")])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return
        if data.startswith("esc_hist_rate:"):
            parts = _parse_callback_parts(data, "esc_hist_rate:", 5)
            if not parts:
                await query.edit_message_text("Rating action is stale. Please reopen history.")
                return
            _, _, escrow_id_str, rating_str, page = parts
            try:
                escrow_id = int(escrow_id_str)
                rating_value = int(rating_str)
            except ValueError:
                await query.edit_message_text("Invalid rating")
                return
            if rating_value < 1 or rating_value > 5:
                await query.edit_message_text("Invalid rating")
                return
            esc = escrow.get_escrow(escrow_id)
            if user_id not in {int(esc["buyer_id"]), int(esc["seller_id"])}:
                await query.edit_message_text("You are not allowed to rate this deal")
                return
            reviewed_id = int(esc["seller_id"]) if int(esc["buyer_id"]) == user_id else int(esc["buyer_id"])
            existing = conn.execute("SELECT 1 FROM reviews WHERE reviewer_id=? AND escrow_id=?", (user_id, escrow_id)).fetchone()
            if existing:
                await query.edit_message_text("You already rated this deal.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"esc_hist_open:{escrow_id}:{page}")]]))
                return
            conn.execute(
                "INSERT INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)",
                (user_id, reviewed_id, escrow_id, rating_value),
            )
            conn.commit()
            await query.edit_message_text(
                "Your rating has been saved. Waiting for the seller's rating.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"esc_hist_open:{escrow_id}:{page}")]]),
            )
            return
        if data.startswith("esc_hist_profile:"):
            parts = _parse_callback_parts(data, "esc_hist_profile:", 5)
            if not parts:
                await query.edit_message_text("Counter-party view is stale. Please reopen history.")
                return
            _, _, cp_id, page, escrow_id = parts
            row = conn.execute("SELECT * FROM users WHERE id=?", (int(cp_id),)).fetchone()
            if not row:
                await query.edit_message_text("Counter-party profile not found")
                return
            await query.edit_message_text(_render_user_profile(_user_profile(conn, row)), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"esc_hist_open:{escrow_id}:{page}")]]))
            return
    finally:
        conn.close()


async def deal_search_input_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _enforce_text_rate_limit(update, "deal_search_input", limit=6, window_s=15):
        return DEAL_SEARCH_INPUT
    lookup = update.effective_message.text.strip()
    lookup = lookup.removeprefix("@").strip()
    lookup = lookup.removeprefix("https://t.me/").strip()
    lookup = lookup.split("/", 1)[0].split("?", 1)[0].strip()
    if not lookup:
        await update.effective_message.reply_text("Please send a username.")
        return DEAL_SEARCH_INPUT
    conn, _, tenant, _ = _services()
    try:
        tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        if not await _require_not_frozen(update, conn):
            return ConversationHandler.END
        row = conn.execute("SELECT * FROM users WHERE username=?", (lookup,)).fetchone()
        if not row:
            await update.effective_message.reply_text(
                f"❌ User not found\n\nUser with username @{lookup} not found in the database.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🔍 Search Again", callback_data="deal_search_again")],
                        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="deal_back_main")],
                    ]
                ),
            )
            return DEAL_SEARCH_RESULT
    finally:
        conn.close()
    return await _seller_lookup_and_render(update, context, f"@{lookup}")


async def deal_new_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    conn, _, tenant, _ = _services()
    try:
        tenant.ensure_user(query.from_user.id, query.from_user.username)
        if _is_user_frozen(conn, query.from_user.id):
            await _show_frozen_callback(query)
            return ConversationHandler.END
    finally:
        conn.close()

    await query.edit_message_text(
        "🔍 Check User\n\n"
        "Please send me one of the following:\n"
        "• @username - Telegram username\n"
        "• https://t.me/username - Telegram profile link\n"
        "• username - Just the username without @",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="deal_back_main")]]),
    )
    return DEAL_SEARCH_INPUT


async def deal_check_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.args:
        await update.effective_message.reply_text("Usage: /check_user <@username|telegram_id>")
        return ConversationHandler.END
    return await _seller_lookup_and_render(update, context, context.args[0].strip())


async def deal_search_result_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "deal_back_main":
        await query.edit_message_text("🔙 Back to main menu", reply_markup=_start_menu())
        return ConversationHandler.END
    if query.data == "deal_search_again":
        await query.edit_message_text(
            "🔍 Check User\n\n"
            "Please send me one of the following:\n"
            "• @username - Telegram username\n"
            "• https://t.me/username - Telegram profile link\n"
            "• username - Just the username without @",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="deal_back_main")]]),
        )
        return DEAL_SEARCH_INPUT

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
        for asset in _enabled_assets():
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
    if await _enforce_text_rate_limit(update, "deal_amount_input", limit=6, window_s=20):
        return DEAL_ENTER_AMOUNT
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
    if await _enforce_text_rate_limit(update, "deal_conditions_input", limit=4, window_s=20):
        return DEAL_ENTER_CONDITIONS
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

        if _is_rate_limited(update.effective_user.id, "create_escrow", limit=2, window_s=20):
            await update.effective_message.reply_text("Too many requests. Please slow down.")
            return DEAL_ENTER_CONDITIONS
        try:
            runtime_bot_id = _runtime_bot_id(conn, tenant)
            view = escrow.create_escrow(bot_id=runtime_bot_id, buyer_id=buyer_id, seller_id=seller_id, asset=asset, amount=amount, description=conditions)
        except ValueError as exc:
            conn.rollback()
            await update.effective_message.reply_text(str(exc))
            return DEAL_ENTER_CONDITIONS
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
                [InlineKeyboardButton("❌ Cancel Request", callback_data="deal_cancel_request")],
                [InlineKeyboardButton("🔙 Back to Pending List", callback_data="deal_back_pending")],
                [InlineKeyboardButton("👀 View Counter-Party Profile", callback_data="deal_view_counterparty")],
                [InlineKeyboardButton("💰 Release Funds", callback_data="deal_release_prompt")],
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
    if await _enforce_rate_limit(query, query.from_user.id, "deal_pending", limit=8, window_s=10):
        return DEAL_PENDING_VIEW
    action = query.data
    if action != "deal_back_to_pending" and "escrow_id" not in context.user_data:
        await query.edit_message_text("Deal context expired. Run /check_user again.", reply_markup=_start_menu())
        return ConversationHandler.END

    if action == "deal_cancel_request":
        moderator = Settings.moderator_username or "moderator"
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
    lines.append("🔙 Tap Back to return to current deal view.")
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
            [InlineKeyboardButton("🔙 Back to Pending List", callback_data="deal_back_pending")],
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
    if await _enforce_rate_limit(query, query.from_user.id, "release_escrow", limit=2, window_s=20):
        return DEAL_RELEASE_CONFIRM

    conn, _, tenant, escrow = _services()
    try:
        buyer_id = context.user_data.get("buyer_id") or tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        if not await _require_not_frozen(update, conn):
            return ConversationHandler.END
        if "escrow_id" not in context.user_data:
            await query.edit_message_text("Deal context expired. Run /check_user again.", reply_markup=_start_menu())
            return ConversationHandler.END
        escrow_id = int(context.user_data["escrow_id"])
        try:
            view = escrow.release(escrow_id, actor_user_id=buyer_id)
        except ValueError as exc:
            conn.rollback()
            await query.edit_message_text(str(exc), reply_markup=_start_menu())
            return ConversationHandler.END

        seller_username = context.user_data.get("seller_username", "unknown")
        date_row = conn.execute("SELECT updated_at FROM escrows WHERE id=?", (escrow_id,)).fetchone()["updated_at"]
        date_label = _format_db_timestamp(date_row)
        description = context.user_data.get("conditions", "")
        release_msg = (
            f"@{seller_username} | {view.amount} {view.asset} | {date_label} - {description} | "
            f"Payment of {view.amount} {view.asset} has been released to the seller."
        )
        stars = [InlineKeyboardButton("⭐", callback_data=f"deal_rate_seller:{i}") for i in range(1, 6)]
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="deal_finish")], stars])
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
    if await _enforce_rate_limit(query, query.from_user.id, "deal_rate", limit=12, window_s=10):
        return DEAL_RATE_SELLER
    data = query.data

    if data == "deal_finish":
        await query.edit_message_text("🔙 Done.  Back to main menu.", reply_markup=_start_menu())
        return ConversationHandler.END

    parts = _parse_callback_parts(data or "", "deal_rate_seller:", 2)
    if not parts:
        await query.edit_message_text("Invalid rating")
        return DEAL_RATE_SELLER
    try:
        rating = int(parts[1])
    except ValueError:
        await query.edit_message_text("Invalid rating")
        return DEAL_RATE_SELLER
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
        existing = conn.execute("SELECT 1 FROM reviews WHERE reviewer_id=? AND escrow_id=?", (buyer_id, escrow_id)).fetchone()
        if existing:
            await query.edit_message_text("You already rated this deal.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="deal_finish")]]))
            return DEAL_RATE_BUYER
        conn.execute(
            "INSERT INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)",
            (buyer_id, seller_id, escrow_id, rating),
        )
        conn.commit()

        stars = [InlineKeyboardButton("⭐", callback_data=f"deal_rate_buyer:{escrow_id}:{i}") for i in range(1, 6)]
        try:
            await _notify_safe(
                context,
                context.user_data.get("seller_telegram_id"),
                f"The buyer has released funds. Please rate your experience with @{update.effective_user.username or update.effective_user.id}",
                InlineKeyboardMarkup([stars]),
            )
        except Exception:
            LOGGER.exception("seller rating notification failed")
        await query.edit_message_text("Your rating has been saved. Waiting for the seller's rating.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="deal_finish")]]))
        return DEAL_RATE_BUYER
    finally:
        conn.close()




async def deal_rate_buyer_wait(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "deal_finish":
        await query.edit_message_text("Done. Back to main menu.", reply_markup=_start_menu())
        return ConversationHandler.END
    await query.edit_message_text("Waiting for seller rating. You can return to main menu.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="deal_finish")]]))
    return DEAL_RATE_BUYER

async def seller_rate_buyer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = _parse_callback_parts(query.data or "", "deal_rate_buyer:", 3)
    if not parts:
        await query.edit_message_text("Invalid rating")
        return
    _, escrow_id_str, rating_str = parts
    try:
        escrow_id = int(escrow_id_str)
        rating = int(rating_str)
    except ValueError:
        await query.edit_message_text("Invalid rating")
        return

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
        existing = conn.execute("SELECT 1 FROM reviews WHERE reviewer_id=? AND escrow_id=?", (reviewer_id, escrow_id)).fetchone()
        if existing:
            await query.edit_message_text("You already rated this deal")
            return
        conn.execute(
            "INSERT INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)",
            (reviewer_id, int(esc["buyer_id"]), escrow_id, rating),
        )
        conn.commit()
        await query.edit_message_text("Your rating has been saved. Waiting for the seller's rating.")
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
        for asset in _enabled_assets():
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
    if update.callback_query:
        await update.callback_query.edit_message_text("Support Team: @your_support_handle")
    else:
        await update.effective_message.reply_text("Support Team: @your_support_handle")


async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    mapping = {
        "profile": profile,
        "escrow_menu": escrow_menu,
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
        entry_points=[
            CommandHandler("check_user", deal_check_user),
            CallbackQueryHandler(deal_new_entry, pattern=r"^esc_menu:new$"),
            CallbackQueryHandler(deal_new_entry, pattern=r"^check_user$"),
        ],
        states={
            DEAL_SEARCH_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deal_search_input_text)],
            DEAL_SEARCH_RESULT: [CallbackQueryHandler(deal_search_result_cb, pattern="^deal_(create|back_main|search_again)$")],
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
            DEAL_RATE_BUYER: [CallbackQueryHandler(deal_rate_buyer_wait, pattern="^deal_finish$")],
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

    deposit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(deposit_select_asset, pattern=r"^dep_asset:[A-Z]+$")],
        states={
            DEPOSIT_ENTER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount_input),
                CallbackQueryHandler(deposit_cancel_to_assets, pattern=r"^profile_deposit$"),
            ],
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
    app.add_handler(deposit_conv)
    app.add_handler(CallbackQueryHandler(profile_open_from_menu, pattern=r"^profile$"))
    app.add_handler(CallbackQueryHandler(profile_actions, pattern=r"^(profile_(?!withdraw$)|esc_wd_open:).*$"))
    app.add_handler(CallbackQueryHandler(escrow_menu_actions, pattern=r"^esc_menu:(pending|active|disputes|history)$"))
    app.add_handler(CallbackQueryHandler(escrow_history_actions, pattern=r"^(esc_hist_|esc_back_menu|esc_pending_page:|esc_active_page:|esc_disputes_page:|esc_open:).*$"))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_menu_click))
    app.run_polling()


if __name__ == "__main__":
    main()
