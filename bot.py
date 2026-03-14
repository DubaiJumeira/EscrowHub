from __future__ import annotations

import html
import logging
import os
import time
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
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
from error_sanitizer import sanitize_runtime_error
from infra.db.database import get_connection, init_db
from signer.signer_service import SignerService
from runtime_preflight import FatalStartupError, PreflightIntegrityError, run_startup_preflight
from tenant_service import TenantService
from wallet_service import NETWORK_LABELS, WalletService
from watcher_status_service import (
    classify_watcher_health_state,
    env_flag_enabled,
    map_operator_health_state,
    normalize_deposit_provider_state,
    read_watcher_status,
)

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


def _validate_role_configuration() -> None:
    if not ADMIN_IDS:
        LOGGER.warning("No ADMIN_USER_IDS configured; admin commands are unavailable")
    if not Settings.moderator_ids:
        LOGGER.warning("No MODERATOR_TELEGRAM_IDS configured; dispute resolution is unavailable")
    if ADMIN_IDS and Settings.moderator_ids and not (ADMIN_IDS & Settings.moderator_ids):
        LOGGER.info("ADMIN_USER_IDS and MODERATOR_TELEGRAM_IDS are configured as distinct role sets")

BASE_ASSETS = ["USDT", "BTC", "ETH", "SOL", "LTC"]
MIN_ESCROW_USD = Decimal("40")
MAX_DISPUTE_REASON_CHARS = 1000
DEPOSIT_ISSUANCE_READY = True
DEPOSIT_ISSUANCE_ERROR: str | None = None


def _sanitize_destination_preview(address: str | None) -> str:
    value = (address or "").strip()
    if not value:
        return "n/a"
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-4:]}"


def _sanitize_failure_summary(reason: str | None) -> str:
    text = sanitize_runtime_error(reason, max_len=120)
    if not text:
        return "n/a"
    return text

WITHDRAWAL_STATUS_LABELS = {
    "pending": "Pending",
    "broadcasted": "Broadcasted",
    "failed": "Failed",
    "signer_retry": "Awaiting reconciliation",
}


def _enabled_assets() -> list[str]:
    assets: list[str] = []
    if env_flag_enabled("BTC_WATCHER_ENABLED", True):
        assets.append("BTC")
    if env_flag_enabled("ETH_WATCHER_ENABLED", True):
        assets.append("ETH")
        if os.getenv("USDT_ERC20_CONTRACT", "").strip():
            assets.insert(0, "USDT")
    if env_flag_enabled("SOL_WATCHER_ENABLED", False):
        assets.append("SOL")
    if env_flag_enabled("LTC_WATCHER_ENABLED", False):
        assets.append("LTC")
    return assets or ["BTC", "ETH"]


def _is_rate_limited(conn, user_id: int, action: str, limit: int, window_s: int) -> bool:
    now = int(time.time())
    cutoff = now - int(window_s)
    # F9: Always prune rows older than 1 hour regardless of the action's own window,
    # so infrequent actions never leave orphan rows that grow without bound.
    prune_cutoff = now - 3600
    managed_tx = not bool(getattr(conn, "in_transaction", False))
    if managed_tx:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("SAVEPOINT rate_limit")
    try:
        conn.execute("DELETE FROM rate_limit_events WHERE created_at < ?", (prune_cutoff,))
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM rate_limit_events WHERE user_id=? AND action=? AND created_at >= ?",
            (int(user_id), action, cutoff),
        ).fetchone()
        if int(row["c"] or 0) >= int(limit):
            limited = True
        else:
            conn.execute(
                "INSERT INTO rate_limit_events(user_id, action, created_at) VALUES(?,?,?)",
                (int(user_id), action, now),
            )
            limited = False
        if managed_tx:
            conn.commit()
        else:
            conn.execute("RELEASE SAVEPOINT rate_limit")
        return limited
    except Exception:
        if managed_tx:
            conn.rollback()
        else:
            conn.execute("ROLLBACK TO SAVEPOINT rate_limit")
            conn.execute("RELEASE SAVEPOINT rate_limit")
        raise


async def _enforce_rate_limit(query, user_id: int, action: str, limit: int = 4, window_s: int = 10) -> bool:
    conn = get_connection()
    try:
        if not _is_rate_limited(conn, user_id, action, limit, window_s):
            return False
    finally:
        conn.close()
    try:
        await query.answer("Too many requests. Please slow down.", show_alert=False)
    except Exception:
        pass
    return True


async def _enforce_text_rate_limit(update: Update, action: str, limit: int = 4, window_s: int = 10) -> bool:
    conn = get_connection()
    try:
        if not _is_rate_limited(conn, update.effective_user.id, action, limit, window_s):
            return False
    finally:
        conn.close()
    await update.effective_message.reply_text("Too many requests. Please slow down.")
    return True


def _runtime_bot_id(conn, tenant: TenantService) -> int:
    bot_id = int(Settings.bot_id)
    if tenant.get_tenant(bot_id):
        return bot_id
    if Settings.is_production:
        raise FatalStartupError("Configured bot tenant missing in production")
    if not Settings.allow_dev_bot_bootstrap:
        raise FatalStartupError("Bot tenant missing; set ALLOW_DEV_BOT_BOOTSTRAP=true in non-production to bootstrap")
    owner_row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    if not owner_row:
        raise FatalStartupError("Cannot bootstrap bot tenant without an existing owner user")
    conn.execute(
        "INSERT OR IGNORE INTO bots(id, owner_user_id, bot_extra_fee_percent, display_name, telegram_username) VALUES(?,?,'0','EscrowHub','escrowhub')",
        (bot_id, int(owner_row["id"])),
    )
    conn.commit()
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
    DEAL_ACTIVE_VIEW,
    DEAL_DISPUTE_REASON,
    DEAL_CANCEL_ACTIVE,
) = range(12)


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
        await context.bot.send_message(chat_id=telegram_user_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception:
        LOGGER.exception("notification failed for telegram_user_id=%s", telegram_user_id)


def _profile_chip(value: object) -> str:
    return f"<code>{html.escape(str(value))}</code>"


def _profile_block(value: object) -> str:
    return f"<blockquote>{html.escape(str(value))}</blockquote>"


def _profile_block_html(value_html: str) -> str:
    return f"<blockquote>{value_html}</blockquote>"


def _usd_text(value: Decimal | int | str) -> str:
    amount = Decimal(str(value))
    return f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}"


def _decimal_text(value: Decimal | int | str, places: int | None = None) -> str:
    amount = Decimal(str(value))
    if places is not None:
        amount = amount.quantize(Decimal('1').scaleb(-places), rounding=ROUND_HALF_UP)
    text = format(amount, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def _crypto_quote_text(asset: str, amount: Decimal | int | str) -> str:
    precision = {
        'BTC': 8,
        'ETH': 8,
        'LTC': 8,
        'SOL': 9,
        'USDT': 6,
    }.get((asset or '').upper(), 8)
    return f"{_decimal_text(amount, precision)} {(asset or '').upper()}".strip()


def _deposit_quote_amounts(asset: str, usd_amount: Decimal, price_usd: Decimal) -> dict[str, Decimal]:
    """Calculate deposit quote amounts using the real platform fee from Settings.
    There is no external provider fee — this is a self-hosted bot."""
    platform_fee_pct = Decimal(str(Settings.deposit_platform_fee_percent or "1")) / Decimal("100")
    platform_fee_usd = (usd_amount * platform_fee_pct).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    total_invoice_usd = usd_amount + platform_fee_usd
    crypto_amount = total_invoice_usd / price_usd
    return {
        'platform_fee_usd': platform_fee_usd,
        'requested_credit_usd': usd_amount,
        'total_invoice_usd': total_invoice_usd,
        'crypto_amount': crypto_amount,
    }


def _withdraw_quote_amounts(asset: str, amount: Decimal) -> dict[str, Decimal]:
    conn, wallet, _, _ = _services()
    try:
        platform_fee = wallet.withdrawal_platform_fee(asset, amount)
        total_debit = wallet.withdrawal_total_debit(asset, amount)
    finally:
        conn.close()
    return {"platform_fee_amount": platform_fee, "total_debit": total_debit}


def _profile_custom_emoji(env_key: str, fallback: str) -> str:
    emoji_id = (os.getenv(env_key, "") or "").strip()
    if emoji_id.isdigit():
        return f'<tg-emoji emoji-id="{html.escape(emoji_id)}">{html.escape(fallback)}</tg-emoji>'
    return fallback


def _asset_profile_icon(asset: str) -> str:
    asset_code = (asset or "").upper()
    fallbacks = {
        "USDT": "$",
        "BTC": "₿",
        "ETH": "⟠",
        "SOL": "◎",
        "LTC": "Ł",
    }
    return _profile_custom_emoji(f"TG_EMOJI_{asset_code}_ID", fallbacks.get(asset_code, "•"))


def _profile_trust_label(successful: int, disputes_lost: int, review_count: int) -> str:
    if successful >= 50 and disputes_lost == 0 and review_count >= 10:
        return "💎 Maximum"
    if successful >= 20 and disputes_lost <= 1:
        return "🟢 High"
    if successful >= 5:
        return "🟡 Medium"
    return "🔴 Low"


def _profile_rating_stars(rating: float) -> str:
    rounded = int(Decimal(str(rating or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    rounded = max(1, min(5, rounded))
    return "⭐" * rounded


def _profile_stats_line(icon: str, label: str, value: object) -> str:
    spacing = {
        "Successful:": 6,
        "Cancelled:": 7,
        "Disputes lost:": 3,
    }.get(label, 1)
    nbsp = " " * spacing
    return f"• {icon} {html.escape(label)}{nbsp}{html.escape(str(value))}"


def _profile_asset_line(asset: str, amount: Decimal | int | str) -> str:
    return f"• {_asset_profile_icon(asset)} {html.escape(_decimal_text(amount))}"


def _profile_section(title_html: str, rows: list[str] | None = None) -> str:
    parts: list[str] = []
    if title_html:
        parts.append(title_html)
    if rows:
        parts.extend(rows)
    return _profile_block_html("\n".join(parts))


def _profile_review_line(review: dict) -> str:
    reviewer = html.escape(str(review.get("reviewer_username") or "unknown"))
    stars = "⭐" * max(1, min(5, int(review.get("rating") or 0)))
    return f"• {stars} by @{reviewer} ({_date_short(review.get('created_at'))})"


def _user_profile(conn, user_row) -> dict:
    user_id = int(user_row["id"])
    successful = int(conn.execute("SELECT COUNT(*) c FROM escrows WHERE status='completed' AND (buyer_id=? OR seller_id=?)", (user_id, user_id)).fetchone()["c"] or 0)
    cancelled = int(conn.execute("SELECT COUNT(*) c FROM escrows WHERE status='cancelled' AND (buyer_id=? OR seller_id=?)", (user_id, user_id)).fetchone()["c"] or 0)
    disputes_lost = int(
        conn.execute(
            "SELECT COUNT(*) c FROM disputes d JOIN escrows e ON e.id=d.escrow_id WHERE d.status='resolved' AND ((e.buyer_id=? AND json_extract(d.resolution_json,'$.resolution')='release_seller') OR (e.seller_id=? AND json_extract(d.resolution_json,'$.resolution')='refund_buyer'))",
            (user_id, user_id),
        ).fetchone()["c"]
        or 0
    )
    review_stats = conn.execute("SELECT AVG(rating) r, COUNT(*) c FROM reviews WHERE reviewed_id=?", (user_id,)).fetchone()
    rating = float(review_stats["r"] or 0.0)
    review_count = int(review_stats["c"] or 0)

    spent_rows = conn.execute("SELECT asset, amount FROM escrows WHERE buyer_id=? AND status='completed'", (user_id,)).fetchall()
    earned_rows = conn.execute("SELECT asset, amount FROM escrows WHERE seller_id=? AND status='completed'", (user_id,)).fetchall()
    spent = sum((Decimal(r["amount"]) for r in spent_rows), Decimal("0"))
    earned = sum((Decimal(r["amount"]) for r in earned_rows), Decimal("0"))

    asset_totals: dict[str, Decimal] = {}
    for row in list(spent_rows) + list(earned_rows):
        asset = (row["asset"] or "").upper()
        asset_totals[asset] = asset_totals.get(asset, Decimal("0")) + Decimal(row["amount"])

    last_reviews = [
        {
            "rating": int(r["rating"] or 0),
            "reviewer_username": r["reviewer_username"] or "unknown",
            "created_at": r["created_at"],
        }
        for r in conn.execute(
            "SELECT r.rating, r.created_at, u.username reviewer_username FROM reviews r LEFT JOIN users u ON u.id=r.reviewer_id WHERE r.reviewed_id=? ORDER BY r.id DESC LIMIT 3",
            (user_id,),
        ).fetchall()
    ]

    trust_level = _profile_trust_label(successful, disputes_lost, review_count)
    return {
        "username": user_row["username"] or "unknown",
        "registered_date": user_row["created_at"],
        "trust_level": trust_level,
        "rating": rating,
        "review_count": review_count,
        "deals": successful,
        "successful_deals": successful,
        "cancelled_deals": cancelled,
        "disputes_lost": disputes_lost,
        "spent": spent,
        "earned": earned,
        "asset_totals": asset_totals,
        "last_reviews": last_reviews,
        "first_name": None,
        "user_id": user_id,
        "telegram_id": int(user_row["telegram_id"]),
    }


def _render_user_profile(profile: dict) -> str:
    username = html.escape(str(profile.get("username") or "unknown"))
    first_name = html.escape(str(profile.get("first_name") or "unknown"))

    review_count = int(profile.get("review_count", 0) or 0)
    rating = float(profile.get("rating", 0.0) or 0.0)
    rating_line = "⭐ Rating: Too few reviews"
    if review_count >= 3:
        rating_line = f"⭐ Rating: {_profile_rating_stars(rating)} ({rating:.1f}/5 from {review_count} reviews)"

    deals_rows = [
        _profile_stats_line("✅", "Successful:", profile.get("successful_deals", 0)),
        _profile_stats_line("🚫", "Cancelled:", profile.get("cancelled_deals", 0)),
        _profile_stats_line("⚠️", "Disputes lost:", profile.get("disputes_lost", 0)),
    ]

    asset_totals = profile.get("asset_totals") or {}
    totals_rows: list[str] = []
    if asset_totals:
        for asset in sorted(asset_totals.keys(), key=lambda a: BASE_ASSETS.index(a) if a in BASE_ASSETS else 999):
            totals_rows.append(_profile_asset_line(asset, asset_totals[asset]))
    else:
        totals_rows.append("• No completed volume yet")

    reviews = profile.get("last_reviews") or []
    review_rows = [_profile_review_line(review) for review in reviews] if reviews else ["• No reviews yet"]

    sections = [
        _profile_section(
            "<b>👤 Seller Profile</b>",
            [
                f"👤 Profile: @{username}",
                f"👤 Telegram Id: {html.escape(str(profile.get('telegram_id', 'unknown')))}",
                f"👤 First Name: {first_name}",
            ],
        ),
        _profile_section(
            "<b>📋 Account Overview</b>",
            [
                f"📅 Registered: {_date_short(profile.get('registered_date'))}",
                f"🛡️ Trust level: {html.escape(str(profile.get('trust_level') or '🔴 Low'))}",
                rating_line,
            ],
        ),
        _profile_section("<b>🤝 Deals</b>", deals_rows),
        _profile_section("<b>📈 Total Spent/Earned</b>", totals_rows),
        _profile_section("<b>📝 Last 3 reviews</b>", review_rows),
    ]
    return "\n\n".join(sections)


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
    "USDT":"$",
    "BTC": "₿",
    "ETH": "⟠",
    "SOL": "◎",
    "LTC": "Ł",
}


def _asset_icon(asset: str) -> str:
    return ASSET_ICONS.get(asset, "•")


def _asset_with_icon(asset: str) -> str:
    return f"{_asset_icon(asset)} {asset}"


def _asset_display(asset: str) -> str:
    # Use plain asset code in deal flow instead of generic emoji glyphs.
    return (asset or "").upper()


def _format_asset_value(asset: str, amount: Decimal | int | str, *, code: bool = True) -> str:
    amount_text = f"`{amount}`" if code else str(amount)
    return f"{_asset_with_icon(asset)}: {amount_text}"

DEPOSIT_EXPLORERS = {
    "BTC": "https://blockstream.info/address/{address}",
    "ETH": "https://etherscan.io/address/{address}",
    "USDT": "https://etherscan.io/address/{address}",
    "SOL": "https://solscan.io/account/{address}",
    "LTC": "https://litecoinspace.org/address/{address}",
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
        await update.callback_query.edit_message_text(text, reply_markup=_profile_menu(), parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(text, reply_markup=_profile_menu(), parse_mode=ParseMode.HTML)


def _deal_search_prompt_text() -> str:
    return (
        "<blockquote><b>🔍 Check User</b>\n\n"
        "Please send me one of the following:\n"
        "• @username - Telegram username\n"
        "• https://t.me/username - Telegram profile link\n"
        "• username - Just the username without @</blockquote>"
    )


def _deal_search_prompt_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="deal_back_main")]])


def _normalize_deal_lookup(raw: str) -> str:
    lookup = (raw or "").strip()
    if not lookup:
        return ""

    lower_lookup = lookup.lower()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if lower_lookup.startswith(prefix):
            lookup = lookup[len(prefix):].strip()
            break

    lookup = lookup.removeprefix("@").strip()
    lookup = lookup.split("/", 1)[0].split("?", 1)[0].strip()
    return lookup


async def _seller_lookup_and_render(update: Update, context: ContextTypes.DEFAULT_TYPE, lookup: str) -> int:
    normalized_lookup = _normalize_deal_lookup(lookup)
    conn, _, tenant, _ = _services()
    try:
        tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        if not await _require_not_frozen(update, conn):
            return ConversationHandler.END

        row = None
        if normalized_lookup.isdigit():
            row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (int(normalized_lookup),)).fetchone()
        elif normalized_lookup:
            row = conn.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (normalized_lookup,)).fetchone()

        if not row:
            reply_markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Restart", callback_data="deal_search_again")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="deal_back_main")],
                ]
            )
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ User not found", reply_markup=reply_markup)
            else:
                await update.effective_message.reply_text("❌ User not found", reply_markup=reply_markup)
            return DEAL_SEARCH_RESULT

        profile_data = _user_profile(conn, row)
        try:
            seller_chat = await context.bot.get_chat(profile_data["telegram_id"])
            seller_first_name = getattr(seller_chat, "first_name", None)
            if seller_first_name:
                profile_data["first_name"] = seller_first_name
        except Exception:
            LOGGER.debug("Unable to resolve seller first name for telegram_id=%s", profile_data["telegram_id"], exc_info=True)

        context.user_data["seller_id"] = profile_data["user_id"]
        context.user_data["seller_username"] = profile_data["username"]
        context.user_data["seller_telegram_id"] = profile_data["telegram_id"]

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🤝 Create Deal", callback_data="deal_create")],
                [InlineKeyboardButton("⬅️ Back", callback_data="deal_back_main")],
            ]
        )
        profile_text = _render_user_profile(profile_data)
        context.user_data["seller_profile_text"] = profile_text
        if update.callback_query:
            await update.callback_query.edit_message_text(profile_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        else:
            await update.effective_message.reply_text(profile_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        conn.commit()
        return DEAL_SEARCH_RESULT
    finally:
        conn.close()


WITHDRAWAL_SUPPORTED_ASSETS = ("BTC", "LTC", "ETH", "USDT")


def _withdrawal_network_label(asset: str) -> str:
    conn, wallet, _, _ = _services()
    try:
        return wallet.asset_network_label(asset)
    finally:
        conn.close()


def _withdrawal_minimum_context(asset: str) -> dict[str, Decimal | str]:
    conn, wallet, _, _ = _services()
    try:
        minimum_usd = wallet.withdrawal_minimum_usd()
        minimum_asset = wallet.withdrawal_minimum_asset_amount(asset)
        return {
            "minimum_usd": minimum_usd,
            "minimum_asset": minimum_asset,
            "network": wallet.asset_network_label(asset),
        }
    finally:
        conn.close()

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
            [InlineKeyboardButton("💰 Deposit", callback_data="profile_deposit"), InlineKeyboardButton("🏦 Withdraw", callback_data="profile_withdraw")],
            [InlineKeyboardButton("📂 Transaction History", callback_data="profile_tx_history:1")],
            [InlineKeyboardButton("⬅️ Back", callback_data="profile_back")],
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
    user_row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    first_name = html.escape(str(getattr(telegram_user, "first_name", "") or "unknown"))
    telegram_id = html.escape(str(getattr(telegram_user, "id", "unknown")))

    if not user_row:
        return "\n\n".join([
            _profile_section(
                "<b>👤 Your Profile</b>",
                [
                    f"👤 Your Telegram Id: {telegram_id}",
                    f"👤 First Name: {first_name}",
                ],
            ),
            _profile_section("<b>💰 Balance</b>", ["• No balance yet"]),
        ])

    profile = _user_profile(conn, user_row)
    profile["first_name"] = getattr(telegram_user, "first_name", None)

    balance_rows: list[str] = []
    for asset in _enabled_assets():
        available = wallet.available_balance(user_id, asset)
        if available > 0:
            balance_rows.append(_profile_asset_line(asset, available))
    if not balance_rows:
        balance_rows.append("• No balance yet")

    review_count = int(profile.get("review_count", 0) or 0)
    rating = float(profile.get("rating", 0.0) or 0.0)
    rating_line = "⭐ Rating: Too few reviews"
    if review_count >= 3:
        rating_line = f"⭐ Rating: {_profile_rating_stars(rating)} ({rating:.1f}/5 from {review_count} reviews)"

    deals_rows = [
        _profile_stats_line("✅", "Successful:", profile.get("successful_deals", 0)),
        _profile_stats_line("🚫", "Cancelled:", profile.get("cancelled_deals", 0)),
        _profile_stats_line("⚠️", "Disputes lost:", profile.get("disputes_lost", 0)),
    ]

    asset_totals = profile.get("asset_totals") or {}
    totals_rows: list[str] = []
    if asset_totals:
        for asset in sorted(asset_totals.keys(), key=lambda a: BASE_ASSETS.index(a) if a in BASE_ASSETS else 999):
            totals_rows.append(_profile_asset_line(asset, asset_totals[asset]))
    else:
        totals_rows.append("• No completed volume yet")

    reviews = profile.get("last_reviews") or []
    review_rows = [_profile_review_line(review) for review in reviews] if reviews else ["• No reviews yet"]

    sections = [
        _profile_section(
            "<b>👤 Your Profile</b>",
            [
                f"👤 Your Telegram Id: {html.escape(str(profile.get('telegram_id', 'unknown')))}",
                f"👤 First Name: {html.escape(str(profile.get('first_name') or 'unknown'))}",
            ],
        ),
        _profile_section("<b>💰 Balance</b>", balance_rows),
        _profile_section(
            "<b>📋 Account Overview</b>",
            [
                f"📅 Registered: {_date_short(profile.get('registered_date'))}",
                f"🛡️ Trust level: {html.escape(str(profile.get('trust_level') or '🔴 Low'))}",
                rating_line,
            ],
        ),
        _profile_section("<b>🤝 Deals</b>", deals_rows),
        _profile_section("<b>📈 Total Spent/Earned</b>", totals_rows),
        _profile_section("<b>📝 Last 3 reviews</b>", review_rows),
    ]
    return "\n\n".join(sections)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_draft_flow(context)
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
        conn, _, _, escrow_svc = _services()
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
        await query.edit_message_text(text, reply_markup=_profile_menu(), parse_mode=ParseMode.HTML)
        return

    if data == "profile_deposit":
        if not DEPOSIT_ISSUANCE_READY:
            await query.edit_message_text(
                _profile_section(
                    "<b>💰 Deposit</b>",
                    ["Deposit address issuance is currently unavailable. Please try again later."],
                ),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="profile_open")]]),
                parse_mode=ParseMode.HTML,
            )
            return
        assets = _enabled_assets()
        keyboard = [
            [InlineKeyboardButton(_asset_with_icon(asset), callback_data=f"dep_asset:{asset}") for asset in assets[i:i+2]]
            for i in range(0, len(assets), 2)
        ]
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="profile_open")])
        await query.edit_message_text(_profile_block_html("<b>💰 Deposit currency:</b>"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    if data.startswith("profile_tx_history:") or data.startswith("profile_withdraw_history:"):
        raw = data.split(":")[1]
        page = max(1, int(raw))
        per_page = 5
        conn, wallet, tenant, _ = _services()
        try:
            user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
            all_rows = conn.execute(
                """
                SELECT le.id, le.entry_type, le.asset, le.amount, le.ref_type, le.ref_id, le.created_at,
                       e.buyer_id, e.seller_id, e.description
                FROM ledger_entries le
                LEFT JOIN escrows e ON le.ref_type='escrow' AND le.ref_id=e.id
                WHERE le.account_type='USER' AND le.user_id=?
                  AND le.entry_type IN ('DEPOSIT','WITHDRAWAL_RESERVE','ESCROW_RELEASE','ESCROW_UNLOCK','ADJUSTMENT')
                ORDER BY le.id DESC
                """,
                (user_id,)
            ).fetchall()
        finally:
            conn.close()

        total = len(all_rows)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        page_rows = all_rows[(page - 1) * per_page: page * per_page]

        TX_EMOJI = {
            "DEPOSIT":            "📥",
            "WITHDRAWAL_RESERVE": "📤",
            "ESCROW_RELEASE":     "💸",
            "ESCROW_UNLOCK":      "↩️",
            "ADJUSTMENT":         "⚖️",
        }
        TX_LABEL = {
            "DEPOSIT":            "Deposit",
            "WITHDRAWAL_RESERVE": "Withdrawal",
            "ESCROW_RELEASE":     "Paid to Seller",
            "ESCROW_UNLOCK":      "Refund",
            "ADJUSTMENT":         "Adjustment",
        }

        buttons = []
        if not page_rows:
            text = "<b>📂 Transaction History</b>\n\nNo transactions yet."
        else:
            text = f"<b>📂 Transaction History</b>  <i>Page {page}/{pages}</i>"
            for r in page_rows:
                entry_type = r["entry_type"]
                emoji = TX_EMOJI.get(entry_type, "•")
                label = TX_LABEL.get(entry_type, entry_type)
                if entry_type == "ESCROW_RELEASE" and r["seller_id"] and int(r["seller_id"]) == user_id:
                    emoji = "💰"
                    label = "Received"
                amt = Decimal(str(r["amount"]))
                sign = "+" if amt >= 0 else ""
                btn_label = f"{emoji} {label}  {sign}{_decimal_text(amt)} {r['asset']}"
                buttons.append([InlineKeyboardButton(btn_label, callback_data=f"tx_detail:{r['id']}:{page}")])

        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"profile_tx_history:{page-1}"))
        if page < pages:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"profile_tx_history:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("⬅️ Back to Profile", callback_data="profile_open")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)
        return

    if data.startswith("tx_detail:"):
        parts = data.split(":")
        le_id = int(parts[1])
        back_page = parts[2] if len(parts) > 2 else "1"
        conn, wallet, tenant, _ = _services()
        try:
            user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
            r = conn.execute(
                """
                SELECT le.*, e.buyer_id, e.seller_id, e.description, e.status as escrow_status,
                       ub.username as buyer_name, us.username as seller_name,
                       w.destination_address, w.status as wd_status, w.txid, w.failure_reason
                FROM ledger_entries le
                LEFT JOIN escrows e ON le.ref_type='escrow' AND le.ref_id=e.id
                LEFT JOIN users ub ON e.buyer_id=ub.id
                LEFT JOIN users us ON e.seller_id=us.id
                LEFT JOIN withdrawals w ON le.ref_type='withdrawal' AND le.ref_id=w.id
                WHERE le.id=? AND le.user_id=?
                """,
                (le_id, user_id)
            ).fetchone()
        finally:
            conn.close()

        if not r:
            await query.answer("Transaction not found.", show_alert=True)
            return

        entry_type = r["entry_type"]
        amt = Decimal(str(r["amount"]))
        sign = "+" if amt >= 0 else ""
        icon = _asset_icon(r["asset"])
        date = _format_db_timestamp(r["created_at"])

        TX_TITLES = {
            "DEPOSIT":            "📥 Deposit",
            "WITHDRAWAL_RESERVE": "📤 Withdrawal",
            "ESCROW_RELEASE":     "💸 Escrow Release",
            "ESCROW_UNLOCK":      "↩️ Refund / Unlock",
            "ADJUSTMENT":         "⚖️ Adjustment",
        }
        title = TX_TITLES.get(entry_type, f"• {entry_type}")
        if entry_type == "ESCROW_RELEASE" and r["seller_id"] and int(r["seller_id"]) == user_id:
            title = "💰 Received from Buyer"

        lines = [f"<b>{title}</b>\n"]
        lines.append(f"<b>Amount:</b> <code>{sign}{_decimal_text(amt)} {html.escape(str(r['asset']))}</code> {icon}")
        lines.append(f"<b>Date:</b> {date}")

        if r["ref_type"] == "escrow" and r["ref_id"]:
            lines.append(f"<b>Deal #:</b> {r['ref_id']}")
            if r["buyer_name"]:
                lines.append(f"<b>Buyer:</b> @{html.escape(str(r['buyer_name']))}")
            if r["seller_name"]:
                lines.append(f"<b>Seller:</b> @{html.escape(str(r['seller_name']))}")
            if r["description"]:
                lines.append(f"<b>Conditions:</b> {html.escape(str(r['description']))}")
            if r["escrow_status"]:
                lines.append(f"<b>Deal status:</b> {html.escape(str(r['escrow_status']))}")

        if entry_type == "WITHDRAWAL_RESERVE":
            wd_status = str(r["wd_status"] or "").strip().lower()
            status_label = WITHDRAWAL_STATUS_LABELS.get(wd_status, "Awaiting reconciliation")
            if r["destination_address"]:
                addr = wallet._decrypt_field(r["destination_address"])
                lines.append(f"<b>Network:</b> {html.escape(wallet.asset_network_label(str(r['asset'])))}")
                lines.append(f"<b>Address:</b> <code>{html.escape(str(addr))}</code>")
            lines.append(f"<b>Status:</b> {html.escape(status_label)}")
            if wd_status == "broadcasted" and r["txid"]:
                lines.append(f"<b>TxID:</b> <code>{html.escape(str(r['txid']))}</code>")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data=f"profile_tx_history:{back_page}")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="esc_back_menu")],
        ])
        await query.edit_message_text("\n".join(lines), reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data.startswith("esc_wd_open:"):
        # legacy fallback — redirect to tx history
        await query.edit_message_text(
            "<b>📂 Transaction History</b>\n\nPlease use Transaction History in your profile.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📂 Open History", callback_data="profile_tx_history:1")]]),
            parse_mode=ParseMode.HTML,
        )
        return


async def deposit_select_asset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not DEPOSIT_ISSUANCE_READY:
        await query.edit_message_text(
            _profile_section(
                "<b>💰 Deposit</b>",
                ["Deposit address issuance is currently unavailable. Please try again later."],
            ),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="profile_open")]]),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END
    asset = query.data.split(":", 1)[1]
    if asset not in _enabled_assets():
        await query.edit_message_text("Unsupported deposit currency.", reply_markup=_profile_menu())
        return ConversationHandler.END
    context.user_data["dep_asset"] = asset
    asset_label_html = f"{_asset_profile_icon(asset)} {html.escape(asset)}"

    conn, _, _, escrow_service = _services()
    try:
        price_usd = await escrow_service.price_service.get_usd_price_async(asset)
    finally:
        conn.close()

    example = _deposit_quote_amounts(asset, Decimal("100"), price_usd)
    platform_fee_pct = Settings.deposit_platform_fee_percent or "1"
    text = "\n".join([
        _profile_block_html(f"<b>{asset_label_html} Deposit</b>"),
        _profile_block_html(
            "<b>💰 Enter the amount in USD to deposit</b>\n"
            "Max: $10000.00\n\n"
            f"Network: {html.escape(_withdrawal_network_label(asset))}\n"
            f"Platform fee: {html.escape(str(platform_fee_pct))}%\n\n"
            f"Example request: $100.00 USD\n"
            f"Estimated to send: {_asset_profile_icon(asset)} {html.escape(_crypto_quote_text(asset, example['crypto_amount']))}"
        ),
        "Send /cancel to abort.",
    ])
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="profile_deposit")]]),
        parse_mode=ParseMode.HTML,
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
        usd_amount = Decimal(update.effective_message.text.strip())
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text("Please enter a valid USD amount.")
        return DEPOSIT_ENTER_AMOUNT
    if usd_amount <= Decimal("0"):
        await update.effective_message.reply_text("Please enter a positive amount.")
        return DEPOSIT_ENTER_AMOUNT
    if usd_amount > Decimal("10000"):
        await update.effective_message.reply_text("Maximum deposit is $10,000 USD.")
        return DEPOSIT_ENTER_AMOUNT
    if not DEPOSIT_ISSUANCE_READY:
        await update.effective_message.reply_text("Deposit address issuance is currently unavailable. Please try again later.")
        return ConversationHandler.END

    conn, wallet, tenant, escrow_service = _services()
    try:
        user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        try:
            route = wallet.get_or_create_deposit_address(user_id, asset)
        except Exception:
            LOGGER.exception("deposit address issuance failed for user_id=%s asset=%s", user_id, asset)
            await update.effective_message.reply_text("Deposit address issuance is currently unavailable. Please try again later.")
            return ConversationHandler.END
        try:
            price_usd = await escrow_service.price_service.get_usd_price_async(asset)
        except Exception:
            LOGGER.exception("deposit price lookup failed for user_id=%s asset=%s", user_id, asset)
            await update.effective_message.reply_text("Pricing service is temporarily busy. Please try again again in 30 seconds.")
            return DEPOSIT_ENTER_AMOUNT
        conn.commit()
    finally:
        conn.close()

    quote = _deposit_quote_amounts(asset, usd_amount, price_usd)
    asset_label_html = f"{_asset_profile_icon(asset)} {html.escape(asset)}"
    estimated_crypto_html = f"{_asset_profile_icon(asset)} {html.escape(_crypto_quote_text(asset, quote['crypto_amount']))}"
    platform_fee_text = _usd_text(quote["platform_fee_usd"])
    explorer_template = DEPOSIT_EXPLORERS.get(asset, "https://etherscan.io/address/{address}")
    buttons = [
        [InlineKeyboardButton("⬅️ Back", callback_data="profile_deposit")],
        [InlineKeyboardButton("🔗 View Address", url=explorer_template.format(address=route.address))],
    ]
    details = [
        _profile_block_html(f"<b>📥 Deposit details • {asset_label_html}</b>"),
        f"Selected asset: {_profile_block_html(asset_label_html)}",
        f"Requested wallet credit: {_profile_block('$' + _usd_text(quote['requested_credit_usd']) + ' USD')}",
        f"Estimated crypto to send: {_profile_block_html(estimated_crypto_html)}",
        f"Platform fee ({Settings.deposit_platform_fee_percent or '1'}%): {_profile_block(f'${platform_fee_text} USD')}",
        f"Network: {_profile_block(wallet.asset_network_label(asset))}",
        f"Address: {_profile_block(route.address)}",
        f"Rate: {_profile_block_html(f'1 {asset_label_html} ≈ ${html.escape(_usd_text(price_usd))}')}",
    ]
    if route.destination_tag:
        details.append(f"Destination tag: {_profile_block(route.destination_tag)}")
    details.append(f"Send only {asset_label_html} using the {html.escape(wallet.asset_network_label(asset))} network.")
    await update.effective_message.reply_text(
        "\n".join(details),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def deposit_cancel_to_assets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await profile_actions(update, context)
    return ConversationHandler.END


async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not Settings.withdrawals_enabled:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            _profile_section("<b>🏦 Withdraw</b>", ["Withdrawals are temporarily unavailable while secure chain signing is being finalized."]),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="profile_open")]]),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(query.from_user.id, query.from_user.username)
        if _is_user_frozen(conn, query.from_user.id):
            await _show_frozen_callback(query)
            return ConversationHandler.END
        available_assets = []
        for asset in WITHDRAWAL_SUPPORTED_ASSETS:
            if asset not in _enabled_assets():
                continue
            try:
                minimum_asset = wallet.withdrawal_minimum_asset_amount(asset)
                minimum_total = wallet.withdrawal_total_debit(asset, minimum_asset)
            except Exception:
                continue
            if wallet.available_balance(user_id, asset) >= minimum_total:
                available_assets.append(asset)
    finally:
        conn.close()

    if not available_assets:
        usdt_icon = _asset_icon("USDT")
        btc_icon = _asset_icon("BTC")
        eth_icon = _asset_icon("ETH")
        ltc_icon = _asset_icon("LTC")
        await query.edit_message_text(
            _profile_section(
                "<b>❌ Insufficient Balance</b>",
                [
                    "You don't have sufficient balance in any currency for withdrawal.",
                    "",
                    f"Minimum withdrawal: ${html.escape(_usd_text(Settings.withdrawal_minimum_usd or '10'))} USD equivalent on every supported asset.",
                    "",
                    f"Supported withdrawal networks: {btc_icon} Bitcoin, {eth_icon} Ethereum, {usdt_icon} Ethereum (ERC-20), {ltc_icon} Litecoin.",
                    "Users also pay the 1% withdrawal fee. Blockchain fee policy must be enforced by the withdrawal provider.",
                    "",
                    "Please deposit funds to your account before attempting a withdrawal.",
                ],
            ),
            reply_markup=_profile_menu(),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(a, callback_data=f"wd_asset:{a}")] for a in available_assets]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="wd_back_profile")])
    await query.edit_message_text(_profile_block_html("<b>Select withdrawal currency:</b>"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return WD_SELECT_ASSET


async def withdraw_select_asset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "wd_back_profile":
        await profile_actions(update, context)
        return ConversationHandler.END
    asset = query.data.split(":", 1)[1]
    if asset not in _enabled_assets() or asset not in WITHDRAWAL_SUPPORTED_ASSETS:
        await query.edit_message_text(_profile_section("<b>🏦 Withdraw</b>", ["Unsupported withdrawal currency."]), reply_markup=_profile_menu(), parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    context.user_data["wd_asset"] = asset
    await query.edit_message_text(
        _profile_section(
            "<b>🏦 Withdraw</b>",
            [
                f"Network: {html.escape(_withdrawal_network_label(str(context.user_data['wd_asset'])))}",
                f"Minimum: ${html.escape(_usd_text(Settings.withdrawal_minimum_usd or '10'))} USD equivalent",
                f"Enter {html.escape(str(context.user_data['wd_asset']))} withdrawal amount:",
            ],
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wd_back_assets")]]),
        parse_mode=ParseMode.HTML,
    )
    return WD_ENTER_AMOUNT


async def withdraw_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _enforce_text_rate_limit(update, "withdraw_amount_input", limit=6, window_s=20):
        return WD_ENTER_AMOUNT
    asset = context.user_data.get("wd_asset")
    if not asset:
        await update.effective_message.reply_text(_profile_section("<b>🏦 Withdraw</b>", ["Withdrawal session expired."]), parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    try:
        amount = Decimal(update.effective_message.text.strip())
    except InvalidOperation:
        await update.effective_message.reply_text(_profile_section("<b>🏦 Withdraw</b>", ["Enter a valid numeric amount."]), parse_mode=ParseMode.HTML)
        return WD_ENTER_AMOUNT
    if amount <= 0:
        await update.effective_message.reply_text(_profile_section("<b>🏦 Withdraw</b>", ["Amount must be positive."]), parse_mode=ParseMode.HTML)
        return WD_ENTER_AMOUNT
    minimum_ctx = _withdrawal_minimum_context(asset)
    minimum_asset = Decimal(str(minimum_ctx["minimum_asset"]))
    if amount < minimum_asset:
        await update.effective_message.reply_text(
            _profile_section(
                "<b>🏦 Withdraw</b>",
                [
                    f"Minimum withdrawal is ${html.escape(_usd_text(minimum_ctx['minimum_usd']))} USD equivalent for all supported assets.",
                    f"Approximate {html.escape(str(asset))} minimum right now: {html.escape(_crypto_quote_text(asset, minimum_asset))}.",
                    f"Network: {html.escape(str(minimum_ctx['network']))}",
                ],
            ),
            parse_mode=ParseMode.HTML,
        )
        return WD_ENTER_AMOUNT
    conn, wallet, tenant, _ = _services()
    try:
        user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        total_debit = wallet.withdrawal_total_debit(asset, amount)
        if wallet.available_balance(user_id, asset) < total_debit:
            await update.effective_message.reply_text(_profile_section("<b>🏦 Withdraw</b>", [f"Insufficient available balance including the {Settings.withdrawal_platform_fee_percent or '1'}% withdrawal fee."]), parse_mode=ParseMode.HTML)
            return WD_ENTER_AMOUNT
    finally:
        conn.close()
    context.user_data["wd_amount"] = amount
    await update.effective_message.reply_text(
        _profile_section(
            "<b>🏦 Withdraw</b>",
            [
                f"Network: {html.escape(_withdrawal_network_label(str(asset)))}",
                f"Enter your {html.escape(str(asset))} withdrawal address:",
            ],
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wd_back_amount")]]),
        parse_mode=ParseMode.HTML,
    )
    return WD_ENTER_ADDRESS


async def withdraw_address_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    address = update.effective_message.text.strip()
    if not address:
        await update.effective_message.reply_text(_profile_section("<b>🏦 Withdraw</b>", ["Address is required."]), parse_mode=ParseMode.HTML)
        return WD_ENTER_ADDRESS
    asset = context.user_data.get("wd_asset")
    conn, wallet, _, _ = _services()
    try:
        try:
            wallet.validate_withdrawal_address(asset, address)
        except ValueError as exc:
            await update.effective_message.reply_text(_profile_section("<b>🏦 Withdraw</b>", [html.escape(_sanitize_failure_summary(str(exc)))]), parse_mode=ParseMode.HTML)
            return WD_ENTER_ADDRESS
    finally:
        conn.close()
    context.user_data["wd_address"] = address
    quote = _withdraw_quote_amounts(asset, Decimal(context.user_data["wd_amount"]))
    await update.effective_message.reply_text(
        _profile_section(
            "<b>🏦 Confirm withdrawal</b>",
            [
                f"Network: {html.escape(_withdrawal_network_label(str(asset)))}",
                f"Address: {html.escape(address)}",
                f"Withdrawal amount: {html.escape(_crypto_quote_text(str(asset), context.user_data['wd_amount']))}",
                f"Platform fee ({html.escape(str(Settings.withdrawal_platform_fee_percent or '1'))}%): {html.escape(_crypto_quote_text(str(asset), quote['platform_fee_amount']))}",
                f"Total debit before blockchain fee handling: {html.escape(_crypto_quote_text(str(asset), quote['total_debit']))}",
                "",
                "Is this address correct?",
            ],
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Confirm", callback_data="wd_confirm"), InlineKeyboardButton("Cancel", callback_data="wd_cancel_addr")]]),
        parse_mode=ParseMode.HTML,
    )
    return WD_CONFIRM


async def withdraw_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "wd_cancel_addr":
        await query.edit_message_text(
            _profile_section("<b>🏦 Withdraw</b>", [f"Enter your {html.escape(str(context.user_data.get('wd_asset', 'ASSET')))} withdrawal address:"]),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wd_back_amount")]]),
            parse_mode=ParseMode.HTML,
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
            req = wallet.request_withdrawal(user_id, context.user_data["wd_asset"], Decimal(context.user_data["wd_amount"]), context.user_data["wd_address"])
        except ValueError as exc:
            conn.rollback()
            await query.edit_message_text(_profile_section("<b>🏦 Withdraw</b>", [html.escape(_sanitize_failure_summary(str(exc)))]), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="profile_open")]]), parse_mode=ParseMode.HTML)
            return ConversationHandler.END
        conn.commit()
    finally:
        conn.close()

    for k in ["wd_asset", "wd_amount", "wd_address"]:
        context.user_data.pop(k, None)
    await query.edit_message_text(
        _profile_section("<b>🏦 Withdraw</b>", ["Withdrawal request submitted.", "Funds will arrive within a few minutes."]),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="profile_open")]]),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def withdraw_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "wd_back_assets":
        return await withdraw_start(update, context)
    if query.data == "wd_back_amount":
        await query.edit_message_text(
            _profile_section("<b>🏦 Withdraw</b>", [f"Enter {html.escape(str(context.user_data.get('wd_asset', 'ASSET')))} withdrawal amount:"]),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="wd_back_assets")]]),
            parse_mode=ParseMode.HTML,
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
            [InlineKeyboardButton("⬅️ Back", callback_data="profile_back")],
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
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="esc_back_menu")])
    await query.edit_message_text(f"⌛ Pending escrows (Page {page}/{pages})", reply_markup=InlineKeyboardMarkup(buttons))


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
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="esc_back_menu")])
    await query.edit_message_text(f"🤝 Active escrows (Page {page}/{pages})", reply_markup=InlineKeyboardMarkup(buttons))


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
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="esc_back_menu")])
    await query.edit_message_text(f"⚖️ Disputed escrows (Page {page}/{pages})", reply_markup=InlineKeyboardMarkup(buttons))


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
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="esc_back_menu")])
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
            icon = _asset_icon(row["asset"])
            status_emoji = {
                "pending": "⏳ Pending",
                "active": "✅ Active",
                "disputed": "⚖️ Disputed",
                "completed": "✅ Completed",
                "cancelled": "❌ Cancelled",
            }.get(row["status"], row["status"].capitalize())
            # Fetch counterparty username
            cp_id = escrow.counterparty_user_id(row, user_id)
            cp = conn.execute("SELECT username FROM users WHERE id=?", (cp_id,)).fetchone()
            cp_name = cp["username"] if cp and cp["username"] else "unknown"
            role = "Seller" if int(row["buyer_id"]) == user_id else "Buyer"
            text = (
                f"<b>📋 Deal #{row['id']}</b>\n\n"
                f"<b>Status:</b> {status_emoji}\n"
                f"<b>{role}:</b> @{html.escape(str(cp_name))}\n"
                f"<b>Amount:</b> <code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon}\n"
                f"<b>Created:</b> {_format_db_timestamp(row['created_at'])}\n\n"
                f"<b>Conditions</b>\n{html.escape(str(row['description'] or '-'))}"
            )
            back_cb = {
                "pending": f"esc_pending_page:{page}",
                "active": f"esc_active_page:{page}",
                "disputes": f"esc_disputes_page:{page}",
            }.get(section, "esc_back_menu")
            kb_rows = [[InlineKeyboardButton("⬅️ Back", callback_data=back_cb)]]
            esc_id_val = row["id"]
            is_buyer = int(row["buyer_id"]) == user_id
            if row["status"] == "pending" and is_buyer:
                kb_rows.insert(0, [InlineKeyboardButton("📋 View Pending Request", callback_data=f"esc_view_pending:{esc_id_val}:{back_cb}")])
            elif row["status"] == "active":
                kb_rows.insert(0, [InlineKeyboardButton("📋 View Active Deal", callback_data=f"esc_view_active:{esc_id_val}:{back_cb}")])
            elif row["status"] == "disputed":
                kb_rows.insert(0, [InlineKeyboardButton("⚖️ View Dispute", callback_data=f"esc_view_dispute:{esc_id_val}:{back_cb}")])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode=ParseMode.HTML)
            return
        if data.startswith("esc_hist_open:"):
            parts = _parse_callback_parts(data, "esc_hist_open:", 3)
            if not parts:
                await query.edit_message_text("Deal history item is stale. Please reopen history.")
                return
            _, escrow_id, page = parts
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
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"esc_hist_page:{page}")])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return
        if data.startswith("esc_hist_rate:"):
            parts = _parse_callback_parts(data, "esc_hist_rate:", 4)
            if not parts:
                await query.edit_message_text("Rating action is stale. Please reopen history.")
                return
            _, escrow_id_str, rating_str, page = parts
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
                await query.edit_message_text("You already rated this deal.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"esc_hist_open:{escrow_id}:{page}")]]))
                return
            conn.execute(
                "INSERT INTO reviews(reviewer_id,reviewed_id,escrow_id,rating) VALUES(?,?,?,?)",
                (user_id, reviewed_id, escrow_id, rating_value),
            )
            conn.commit()
            await query.edit_message_text(
                "Your rating has been saved. Waiting for the seller's rating.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"esc_hist_open:{escrow_id}:{page}")]]),
            )
            return
        if data.startswith("esc_hist_profile:"):
            parts = _parse_callback_parts(data, "esc_hist_profile:", 4)
            if not parts:
                await query.edit_message_text("Counter-party view is stale. Please reopen history.")
                return
            _, cp_id, page, escrow_id = parts
            row = conn.execute("SELECT * FROM users WHERE id=?", (int(cp_id),)).fetchone()
            if not row:
                await query.edit_message_text("Counter-party profile not found")
                return
            await query.edit_message_text(_render_user_profile(_user_profile(conn, row)), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"esc_hist_open:{escrow_id}:{page}")]]), parse_mode=ParseMode.HTML)
            return
    finally:
        conn.close()


async def deal_search_input_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _enforce_text_rate_limit(update, "deal_search_input", limit=6, window_s=15):
        return DEAL_SEARCH_INPUT
    lookup = _normalize_deal_lookup(update.effective_message.text)
    if not lookup:
        await update.effective_message.reply_text("Please send a username.")
        return DEAL_SEARCH_INPUT
    return await _seller_lookup_and_render(update, context, lookup)


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

    _clear_draft_flow(context)

    await query.edit_message_text(_deal_search_prompt_text(), reply_markup=_deal_search_prompt_markup(), parse_mode=ParseMode.HTML)
    return DEAL_SEARCH_INPUT


async def deal_check_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_draft_flow(context)
    if not context.args:
        await update.effective_message.reply_text(_deal_search_prompt_text(), reply_markup=_deal_search_prompt_markup(), parse_mode=ParseMode.HTML)
        return DEAL_SEARCH_INPUT
    return await _seller_lookup_and_render(update, context, context.args[0].strip())


async def deal_search_result_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "deal_back_main":
        _clear_draft_flow(context)
        await query.edit_message_text("⬅️ Back to main menu", reply_markup=_start_menu())
        return ConversationHandler.END
    if query.data == "deal_search_again":
        _clear_draft_flow(context)
        await query.edit_message_text(_deal_search_prompt_text(), reply_markup=_deal_search_prompt_markup(), parse_mode=ParseMode.HTML)
        return DEAL_SEARCH_INPUT

    if query.data == "deal_create":
        keyboard: list[list[InlineKeyboardButton]] = []
        assets = _enabled_assets()
        if assets:
            keyboard.append([InlineKeyboardButton(_asset_display(assets[0]), callback_data=f"deal_asset:{assets[0]}")])
            row: list[InlineKeyboardButton] = []
            for asset in assets[1:]:
                row.append(InlineKeyboardButton(_asset_display(asset), callback_data=f"deal_asset:{asset}"))
                if len(row) == 2:
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="deal_back_main")])
        await query.edit_message_text(_profile_block_html('<b>Select the currency for this deal:</b>'), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return DEAL_ENTER_AMOUNT

    return DEAL_SEARCH_RESULT


async def _show_deal_amount_prompt(query, buyer_id: int, asset: str) -> None:
    conn, wallet, _, _ = _services()
    try:
        balance = wallet.available_balance(buyer_id, asset)
    finally:
        conn.close()
    await query.edit_message_text(
        "\n".join([
            _profile_block_html("<b>💰 Your balances:</b>"),
            f"• {_asset_profile_icon(asset)} {_profile_block(_decimal_text(balance))}    ⚠️ min: 40 USD",
            "",
            _profile_block_html("<b>Enter the deal amount in USD 💵 (e.g. 40):</b>"),
        ]),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Deposit", callback_data="profile_deposit")],
                [InlineKeyboardButton("❌ Cancel", callback_data="deal_back_main")],
            ]
        ),
        parse_mode=ParseMode.HTML,
    )


async def deal_enter_amount_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "deal_back_to_search":
        profile_txt = context.user_data.get("seller_profile_text") or (
            f"👤 Profile: @{html.escape(str(context.user_data.get('seller_username','unknown')))}\n\n"
            "Use Create Deal to continue."
        )
        await query.edit_message_text(
            profile_txt,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🤝 Create Deal", callback_data="deal_create")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="deal_back_main")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
        return DEAL_SEARCH_RESULT

    if data == "profile_deposit":
        await profile_actions(update, context)
        return DEAL_ENTER_AMOUNT

    if data == "deal_back_main":
        return await deal_search_result_cb(update, context)

    parts = _parse_callback_parts(data, "deal_asset:", 2)
    if not parts:
        return DEAL_ENTER_AMOUNT
    asset = parts[1]
    if asset not in _enabled_assets():
        await query.edit_message_text("Unsupported escrow currency.", reply_markup=_start_menu())
        return ConversationHandler.END
    conn, _, tenant, _ = _services()
    try:
        buyer_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        if _is_user_frozen(conn, update.effective_user.id):
            await query.edit_message_text("Your account is frozen. Please contact support.")
            return ConversationHandler.END
        seller_id = context.user_data.get("seller_id")
        if seller_id and int(seller_id) == int(buyer_id):
            await query.edit_message_text("You cannot create a deal with yourself", reply_markup=_start_menu())
            return ConversationHandler.END
        context.user_data["buyer_id"] = buyer_id
        context.user_data["asset"] = asset
        conn.commit()
    finally:
        conn.close()
    await _show_deal_amount_prompt(query, int(context.user_data["buyer_id"]), asset)
    return DEAL_ENTER_AMOUNT


def _deal_conditions_prompt(validation_line: str | None = None) -> str:
    lines: list[str] = []
    if validation_line:
        lines.extend([validation_line, ""])
    lines.extend([
        "Describe the deal in detail, including ALL terms.",
        "",
        "‼️ THIS WILL AFFECT HOW DISPUTES ARE RESOLVED LATER",
        "",
        "Describe ALL deal conditions ✍️",
    ])
    return "\n".join(lines)


async def deal_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _enforce_text_rate_limit(update, "deal_amount_input", limit=6, window_s=20):
        return DEAL_ENTER_AMOUNT
    text = update.effective_message.text.strip()
    try:
        usd_amount = Decimal(text)
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text(_profile_block_html("<b>Please enter a valid USD amount (e.g. 40)</b>"), parse_mode=ParseMode.HTML)
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
        if usd_amount <= 0:
            await update.effective_message.reply_text(_profile_block_html("<b>Amount must be positive.</b>"), parse_mode=ParseMode.HTML)
            return DEAL_ENTER_AMOUNT
        if usd_amount < Decimal("40"):
            await update.effective_message.reply_text(
                _profile_block_html(
                    "<b>⚠️ Minimum deal amount is $40 USD</b>\n\n"
                    f"You entered: <code>${_usd_text(usd_amount)}</code>\n"
                    "Please enter $40 or more."
                ),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="deal_back_main")]]),
                parse_mode=ParseMode.HTML,
            )
            return DEAL_ENTER_AMOUNT

        # Convert USD to crypto
        price_usd = await escrow_service.price_service.get_usd_price_async(asset)
        amount = (usd_amount / price_usd).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)

        balance = wallet.available_balance(buyer_id, asset)
        if balance < amount:
            difference = amount - balance
            await update.effective_message.reply_text(
                "\n".join([
                    _profile_block_html("<b>Insufficient balance</b>"),
                    f"Requested: {_profile_block(_crypto_quote_text(asset, amount))} (${_usd_text(usd_amount)} USD)",
                    f"Available: {_profile_block(_crypto_quote_text(asset, balance))}",
                    f"Difference: {_profile_block(_crypto_quote_text(asset, difference))}",
                    "Top up your balance or enter a different amount.",
                ]),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Deposit", callback_data="profile_deposit")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="deal_back_main")],
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
            return DEAL_ENTER_AMOUNT

        context.user_data["amount"] = amount
        context.user_data["usd_amount"] = usd_amount
        context.user_data["asset"] = asset

        await update.effective_message.reply_text(
            _deal_conditions_prompt(),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="deal_back_to_amount")]]),
            parse_mode=ParseMode.HTML,
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
        await update.effective_message.reply_text(
            _deal_conditions_prompt("Please describe all deal conditions."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="deal_back_to_amount")]]),
            parse_mode=ParseMode.HTML,
        )
        return DEAL_ENTER_CONDITIONS
    if len(conditions) > 500:
        await update.effective_message.reply_text(
            _deal_conditions_prompt("Conditions are too long. Maximum is 500 characters."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="deal_back_to_amount")]]),
            parse_mode=ParseMode.HTML,
        )
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

        if _is_rate_limited(conn, update.effective_user.id, "create_escrow", limit=2, window_s=20):
            await update.effective_message.reply_text("Too many requests. Please slow down.")
            return DEAL_ENTER_CONDITIONS
        try:
            runtime_bot_id = _runtime_bot_id(conn, tenant)
            view = escrow.create_escrow(bot_id=runtime_bot_id, buyer_id=buyer_id, seller_id=seller_id, asset=asset, amount=amount, description=conditions)
        except ValueError as exc:
            conn.rollback()
            await update.effective_message.reply_text(_profile_section("<b>🏦 Withdraw</b>", [html.escape(_sanitize_failure_summary(str(exc)))]), parse_mode=ParseMode.HTML)
            return DEAL_ENTER_CONDITIONS
        context.user_data["escrow_id"] = view.escrow_id

        created = conn.execute("SELECT created_at FROM escrows WHERE id=?", (view.escrow_id,)).fetchone()["created_at"]
        created_label = _format_db_timestamp(created)
        context.user_data["created_label"] = created_label
        context.user_data["previous_view"] = "pending"
        usd_amount = context.user_data.get("usd_amount", "")
        usd_str = f" (${_usd_text(usd_amount)} USD)" if usd_amount else ""
        msg = (
            f"✅ Deal sent to @{seller_username}\n\n"
            f"⏳ Waiting for @{seller_username} to accept...\n\n"
            f"💰 {amount} {asset}{usd_str}\n"
            f"📋 {conditions}\n\n"
            f"🕒 Created: {created_label}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📋 View Deal", callback_data="deal_back_to_pending")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="deal_finish_main")],
            ]
        )
        await update.effective_message.reply_text(msg, reply_markup=keyboard)

        buyer_name = update.effective_user.username or str(update.effective_user.id)
        icon = _asset_icon(asset)
        usd_amount = context.user_data.get("usd_amount", "")
        usd_disp = f" (${_usd_text(usd_amount)} USD)" if usd_amount else ""
        seller_msg = (
            "<b>📨 New Deal Request</b>\n\n"
            f"<b>From:</b> @{html.escape(str(buyer_name))}\n"
            f"<b>Amount:</b> <code>{html.escape(str(amount))} {html.escape(str(asset))}</code> {icon}{html.escape(usd_disp)}\n"
            f"<b>Deal #{view.escrow_id}</b> · {html.escape(str(created_label))}\n\n"
            f"<b>Conditions</b>\n{html.escape(str(conditions))}\n\n"
            "❓ <i>Do you accept this deal?</i>"
        )
        seller_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Accept", callback_data=f"escrow_accept:{view.escrow_id}"),
                InlineKeyboardButton("❌ Decline", callback_data=f"escrow_decline:{view.escrow_id}"),
            ]
        ])
        await _notify_safe(
            context,
            context.user_data.get("seller_telegram_id"),
            seller_msg,
            reply_markup=seller_keyboard,
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

    if action == "deal_finish_main":
        _clear_draft_flow(context)
        await query.edit_message_text("🏠 Main Menu", reply_markup=_start_menu())
        return ConversationHandler.END

    if action == "deal_cancel_request":
        escrow_id = context.user_data.get("escrow_id")
        if not escrow_id:
            await query.edit_message_text("Deal context expired.", reply_markup=_start_menu())
            return ConversationHandler.END
        conn, _, _, escrow_service = _services()
        try:
            row = conn.execute("SELECT status FROM escrows WHERE id=?", (int(escrow_id),)).fetchone()
            if not row:
                await query.answer("Deal not found.", show_alert=True)
                return DEAL_PENDING_VIEW
            if row["status"] != "pending":
                await query.answer("Cannot cancel — seller already accepted this deal.", show_alert=True)
                return DEAL_PENDING_VIEW
            try:
                actor_user_id = _resolve_user_id(conn, query.from_user.id)
            except UserResolutionError:
                await query.answer("User not registered.", show_alert=True)
                return DEAL_PENDING_VIEW
            escrow_service.cancel_escrow(int(escrow_id), actor_user_id)
        finally:
            conn.close()
        _clear_draft_flow(context)
        await query.edit_message_text(
            "✅ <b>Deal request cancelled.</b>\n\nYour funds have been unlocked.",
            reply_markup=_start_menu(),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

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
        conn, _, _, escrow_svc = _services()
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?", (int(seller_id),)).fetchone()
            if not row:
                await query.edit_message_text("Counter-party not found", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="deal_back_to_pending")]]))
                return DEAL_PENDING_VIEW
            profile_txt = _render_user_profile(_user_profile(conn, row))
        finally:
            conn.close()
        await query.edit_message_text(
            profile_txt,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="deal_back_to_pending")]]),
            parse_mode=ParseMode.HTML,
        )
        return DEAL_PENDING_VIEW

    if action == "deal_release_prompt":
        context.user_data["previous_view"] = "release_confirm"
        await query.edit_message_text(
            "Are you sure you want to release funds? This means the product/service has been delivered with no problems.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="deal_back_to_pending"), InlineKeyboardButton("Release", callback_data="deal_release_confirm")]]
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
        await query.edit_message_text("No pending deals.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="deal_back_to_pending")]]))
        return DEAL_PENDING_VIEW

    lines = ["Pending deals:"]
    for r in rows[:10]:
        lines.append(f"#{r['id']} | {r['amount']} {r['asset']} | status={r['status']} | {_format_db_timestamp(r['created_at'])}")
    lines.append("⬅️ Tap Back to return to current deal view.")
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="deal_back_to_pending")]]))
    return DEAL_PENDING_VIEW

async def deal_cancel_info_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "deal_back_to_pending":
        return await _show_pending_view(query, context)
    return DEAL_CANCEL_INFO

async def _show_pending_view(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount = context.user_data.get("amount")
    asset = context.user_data.get("asset", "USDT")
    seller_username = context.user_data.get("seller_username", "unknown")
    conditions = context.user_data.get("conditions", "")
    escrow_id = context.user_data.get("escrow_id")
    created_label = context.user_data.get("created_label", "unknown")
    if not escrow_id:
        await query.edit_message_text("Deal context expired.", reply_markup=_start_menu())
        return ConversationHandler.END
    icon = _asset_icon(asset)
    text = (
        "<b>⏳ Pending Deal Request</b>\n\n"
        f"<b>Seller:</b> @{html.escape(str(seller_username))}\n"
        f"<b>Amount:</b> <code>{html.escape(str(amount))} {html.escape(str(asset))}</code> {icon}\n"
        f"<b>Deal #{escrow_id}</b> · Created: {html.escape(str(created_label))}\n\n"
        f"<b>Conditions</b>\n{html.escape(str(conditions))}\n\n"
        "⏳ <i>Waiting for seller to accept…</i>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("❌ Cancel Request", callback_data="deal_cancel_request")],
            [InlineKeyboardButton("👤 View Seller Profile", callback_data="deal_view_counterparty")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="deal_finish_main")],
        ]
    )
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
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
            await query.edit_message_text(_sanitize_failure_summary(str(exc)), reply_markup=_start_menu())
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
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="deal_finish")], stars])
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
        await query.edit_message_text("⬅️ Done.  Back to main menu.", reply_markup=_start_menu())
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
            await query.edit_message_text("You already rated this deal.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="deal_finish")]]))
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
        await query.edit_message_text("Your rating has been saved. Waiting for the seller's rating.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="deal_finish")]]))
        return DEAL_RATE_BUYER
    finally:
        conn.close()




async def deal_rate_buyer_wait(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "deal_finish":
        await query.edit_message_text("⬅️ Done. Back to main menu.", reply_markup=_start_menu())
        return ConversationHandler.END
    await query.edit_message_text("Waiting for seller rating. You can return to main menu.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="deal_finish")]]))
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
        f"👤 Profile: @{html.escape(str(context.user_data.get('seller_username','unknown')))}\n\n"
        "Use Create Deal to continue."
    )
    await query.edit_message_text(
        profile_txt,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🤝 Create Deal", callback_data="deal_create")],
                [InlineKeyboardButton("⬅️ Back", callback_data="deal_back_main")],
            ]
        ),
        parse_mode=ParseMode.HTML,
    )
    return DEAL_SEARCH_RESULT


async def deal_back_from_conditions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    buyer_id = context.user_data.get("buyer_id")
    if not buyer_id:
        await query.edit_message_text("Deal context expired. Run /check_user again.", reply_markup=_start_menu())
        return ConversationHandler.END

    asset = context.user_data.get("asset", "USDT")
    await _show_deal_amount_prompt(query, int(buyer_id), asset)
    return DEAL_ENTER_AMOUNT

def _signer_operator_state(signer_row: dict, signer_ready: bool, signer_reason: str | None) -> tuple[str, str]:
    blocked, persisted_disabled, persisted_degraded = classify_watcher_health_state(signer_row.get("health_state"))
    disabled = (not Settings.withdrawals_enabled) or persisted_disabled
    degraded = ((not signer_ready) or persisted_degraded) and (not blocked) and (not disabled)
    state = map_operator_health_state(ready=signer_ready, blocked=blocked, disabled=disabled, degraded=degraded)
    if state == "blocked":
        return "blocked", _sanitize_failure_summary(signer_row.get("last_error") or signer_reason or "fatal startup blocked")
    if state == "disabled":
        return "disabled", "WITHDRAWALS_ENABLED=false"
    if state == "degraded":
        return "degraded", _sanitize_failure_summary(signer_reason or signer_row.get("last_error") or "not ready")
    return "ready", "ok"


async def watcher_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only")
        return
    conn, _, _, _ = _services()
    try:
        status = read_watcher_status(conn, ["btc_watcher", "eth_watcher", "sol_watcher", "signer_loop"])
        b = status["btc_watcher"]
        e = status["eth_watcher"]
        sol = status["sol_watcher"]
        s = status["signer_loop"]

        signer_service = SignerService()
        signer_ready, signer_reason = signer_service.readiness()
        wallet = WalletService(conn)
        dep_ready, dep_reason = wallet.address_provider.is_ready()

        btc_blocked, btc_disabled, btc_degraded = classify_watcher_health_state(b.get("health_state"))
        eth_blocked, eth_disabled, eth_degraded = classify_watcher_health_state(e.get("health_state"))
        btc_cfg_disabled = not env_flag_enabled("BTC_WATCHER_ENABLED", True)
        eth_cfg_disabled = not env_flag_enabled("ETH_WATCHER_ENABLED", True)
        sol_cfg_disabled = not env_flag_enabled("SOL_WATCHER_ENABLED", False)
        # WARNING: watcher disabled config is enforced at status render to fail closed against stale persisted rows.
        # Secure alternative: persist explicit disabled rows at startup and derive disabled from config as defense in depth.
        btc_state = map_operator_health_state(
            ready=(b.get("health_state") == "ok") and (not btc_cfg_disabled),
            blocked=btc_blocked,
            disabled=btc_disabled or btc_cfg_disabled,
            degraded=btc_degraded,
        )
        eth_state = map_operator_health_state(
            ready=(e.get("health_state") == "ok") and (not eth_cfg_disabled),
            blocked=eth_blocked,
            disabled=eth_disabled or eth_cfg_disabled,
            degraded=eth_degraded,
        )
        sol_blocked, sol_disabled, sol_degraded = classify_watcher_health_state(sol.get("health_state"))
        sol_state = map_operator_health_state(
            ready=(sol.get("health_state") == "ok") and (not sol_cfg_disabled),
            blocked=sol_blocked,
            disabled=sol_disabled or sol_cfg_disabled,
            degraded=sol_degraded,
        )
        signer_state, signer_detail = _signer_operator_state(s, signer_ready, signer_reason)
        deposit_state, deposit_detail = normalize_deposit_provider_state(
            provider_ready=bool(dep_ready),
            issuance_ready=bool(DEPOSIT_ISSUANCE_READY),
            provider_kind=os.getenv("ADDRESS_PROVIDER", "disabled"),
            startup_error=DEPOSIT_ISSUANCE_ERROR,
            provider_reason=dep_reason,
        )

        msg = (
            "watcher_status\n"
            f"- btc: {btc_state} (failures={b['consecutive_failures']}, error={_sanitize_failure_summary(b['last_error'])})\n"
            f"- eth: {eth_state} (failures={e['consecutive_failures']}, error={_sanitize_failure_summary(e['last_error'])})\n"
            f"- sol: {sol_state} (failures={sol['consecutive_failures']}, error={_sanitize_failure_summary(sol['last_error'])})\n"
            f"- deposit_provider: {deposit_state} (detail={_sanitize_failure_summary(deposit_detail)})\n"
            f"- signer: {signer_state} (detail={signer_detail})"
        )
        conn.execute(
            "INSERT INTO admin_actions(admin_user_id,action_type,data_json) VALUES(?,?,?)",
            (update.effective_user.id, "view_watcher_status", json.dumps({"watchers": ["btc_watcher", "eth_watcher", "sol_watcher", "signer_loop"]})),
        )
        conn.commit()
        await update.effective_message.reply_text(msg)
    finally:
        conn.close()





async def signer_retry_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only")
        return
    conn, wallet, _, _ = _services()
    try:
        rows = wallet.signer_retry_withdrawals(limit=20)
        if not rows:
            await update.effective_message.reply_text("No signer_retry withdrawals.")
            return
        lines = ["Signer retry withdrawals (max 20):"]
        for row in rows:
            lines.append(
                f"- id={row['id']} at={row['created_at']} asset={row['asset']} amount={row['amount']} "
                f"dest={_sanitize_destination_preview(row.get('destination_address'))} reason={_sanitize_failure_summary(row.get('failure_reason'))}"
            )
        conn.execute(
            "INSERT INTO admin_actions(admin_user_id,action_type,data_json) VALUES(?,?,?)",
            (update.effective_user.id, "list_signer_retry", json.dumps({"count": len(rows)})),
        )
        conn.commit()
        await update.effective_message.reply_text("\n".join(lines))
    finally:
        conn.close()


async def signer_retry_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only")
        return
    if not update.message or not update.message.text:
        await update.effective_message.reply_text("Usage: /signer_retry_detail <withdrawal_id>")
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await update.effective_message.reply_text("Usage: /signer_retry_detail <withdrawal_id>")
        return
    withdrawal_id = int(parts[1].strip())
    conn, wallet, _, _ = _services()
    try:
        row = wallet.signer_retry_withdrawal(withdrawal_id)
        if not row or row.get("status") != "signer_retry":
            await update.effective_message.reply_text("signer_retry withdrawal not found")
            return
        conn.execute(
            "INSERT INTO admin_actions(admin_user_id,action_type,data_json) VALUES(?,?,?)",
            (update.effective_user.id, "view_signer_retry_detail", json.dumps({"withdrawal_id": withdrawal_id})),
        )
        conn.commit()
        await update.effective_message.reply_text("\n".join([
                f"id: {row['id']}",
                f"created_at: {row['created_at']}",
                f"asset: {row['asset']}",
                f"amount: {row['amount']}",
                f"destination: {_sanitize_destination_preview(row.get('destination_address'))}",
                f"failure: {_sanitize_failure_summary(row.get('failure_reason'))}",
            ])
        )
    finally:
        conn.close()


async def signer_retry_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only")
        return
    if not update.message or not update.message.text:
        await update.effective_message.reply_text("Usage: /signer_retry_action <withdrawal_id> <requeue|fail> CONFIRM")
        return
    parts = update.message.text.split()
    if len(parts) < 4 or not parts[1].isdigit() or parts[3].upper() != "CONFIRM":
        await update.effective_message.reply_text("Usage: /signer_retry_action <withdrawal_id> <requeue|fail> CONFIRM")
        return
    withdrawal_id = int(parts[1])
    action = parts[2].strip().lower()
    if action not in {"requeue", "fail"}:
        await update.effective_message.reply_text("Action must be requeue or fail")
        return
    conn, wallet, _, _ = _services()
    try:
        target_status = "pending" if action == "requeue" else "failed"
        wallet.set_withdrawal_status(withdrawal_id, target_status, f"admin action by {update.effective_user.id}")
        conn.execute(
            "INSERT INTO admin_actions(admin_user_id,action_type,data_json) VALUES(?,?,?)",
            (update.effective_user.id, "resolve_signer_retry", json.dumps({"withdrawal_id": withdrawal_id, "action": action})),
        )
        conn.commit()
        await update.effective_message.reply_text(f"Withdrawal {withdrawal_id} moved to {target_status}.")
    finally:
        conn.close()



async def unresolved_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only")
        return
    conn, wallet, _, _ = _services()
    try:
        rows = wallet.unresolved_withdrawals(limit=20)
        if not rows:
            await update.effective_message.reply_text("No unresolved withdrawals.")
            return
        lines = ["Unresolved withdrawals (max 20):"]
        for row in rows:
            lines.append(f"- id={row['id']} status={row['status']} asset={row['asset']} amount={row['amount']} ref={row.get('provider_ref') or '-'} reason={_sanitize_failure_summary(row.get('failure_reason'))}")
        conn.execute("INSERT INTO admin_actions(admin_user_id,action_type,data_json) VALUES(?,?,?)", (update.effective_user.id, "list_unresolved_withdrawals", json.dumps({"count": len(rows)})))
        conn.commit()
        await update.effective_message.reply_text("\n".join(lines))
    finally:
        conn.close()


async def withdrawal_reconcile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only")
        return
    if not update.message or not update.message.text:
        await update.effective_message.reply_text("Usage: /withdrawal_reconcile <withdrawal_id> CONFIRM [FORCE]")
        return
    parts = update.message.text.split()
    if len(parts) < 3 or not parts[1].isdigit() or parts[2].upper() != "CONFIRM":
        await update.effective_message.reply_text("Usage: /withdrawal_reconcile <withdrawal_id> CONFIRM [FORCE]")
        return
    force = len(parts) >= 4 and parts[3].upper() == "FORCE"
    if len(parts) >= 4 and not force:
        await update.effective_message.reply_text("Usage: /withdrawal_reconcile <withdrawal_id> CONFIRM [FORCE]")
        return
    wid = int(parts[1])
    conn, wallet, _, _ = _services()
    try:
        row = conn.execute("SELECT id,status FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not row or row["status"] not in {"submitted", "broadcasted", "signer_retry"}:
            # WARNING: targeted reconcile intentionally fails closed for non-unresolved states.
            await update.effective_message.reply_text("Withdrawal is not in a reconcilable state")
            return
        signer = SignerService()
        count = signer.reconcile_withdrawal_by_id(wallet, wid, force=force)
        if count == 0:
            # WARNING: targeted reconcile intentionally fails closed when provider outcome remains ambiguous or row is ineligible.
            await update.effective_message.reply_text("Withdrawal is not in a reconcilable state")
            return
        conn.execute("INSERT INTO admin_actions(admin_user_id,action_type,data_json) VALUES(?,?,?)", (update.effective_user.id, "withdrawal_reconcile", json.dumps({"withdrawal_id": wid, "processed": count, "force": force})))
        conn.commit()
        await update.effective_message.reply_text(f"Reconciliation cycle complete (processed={count}).")
    finally:
        conn.close()

async def revenue_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only")
        return
    conn, wallet, _, _ = _services()
    try:
        balances = wallet.platform_revenue_balances()
        lines = ["Platform revenue balances:"]
        for asset in Settings.supported_assets:
            lines.append(f"- {asset}: {balances.get(asset, Decimal('0'))}")
        conn.execute(
            "INSERT INTO admin_actions(admin_user_id,action_type,data_json) VALUES(?,?,?)",
            (update.effective_user.id, "view_revenue", json.dumps({"assets": list(Settings.supported_assets)})),
        )
        conn.commit()
        await update.effective_message.reply_text("\n".join(lines))
    finally:
        conn.close()


async def run_signer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only")
        return
    conn, wallet, _, _ = _services()
    try:
        count = SignerService().process_withdrawals(wallet)
        conn.commit()
        await update.effective_message.reply_text(f"Signer processed {count} withdrawals")
    finally:
        conn.close()




async def _set_frozen_state(update: Update, freeze: bool) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Admin only")
        return
    if not update.message or not update.message.text:
        await update.effective_message.reply_text("Usage: /freeze <telegram_id|@username>" if freeze else "Usage: /unfreeze <telegram_id|@username>")
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.effective_message.reply_text("Usage: /freeze <telegram_id|@username>" if freeze else "Usage: /unfreeze <telegram_id|@username>")
        return
    target = parts[1].strip()
    conn, _, _, _ = _services()
    try:
        if target.lstrip("-").isdigit():
            row = conn.execute("SELECT id, telegram_id, username, frozen FROM users WHERE telegram_id=?", (int(target),)).fetchone()
        else:
            uname = target.lstrip("@").strip().lower()
            row = conn.execute("SELECT id, telegram_id, username, frozen FROM users WHERE LOWER(username)=?", (uname,)).fetchone()
        if not row:
            await update.effective_message.reply_text("User not found.")
            return
        conn.execute("UPDATE users SET frozen=? WHERE id=?", (1 if freeze else 0, int(row["id"])))
        conn.execute(
            "INSERT INTO admin_actions(admin_user_id,action_type,data_json) VALUES(?,?,?)",
            (update.effective_user.id, "freeze_user" if freeze else "unfreeze_user", json.dumps({"target_user_id": int(row["id"]), "target_telegram_id": int(row["telegram_id"]), "target_username": row["username"]})),
        )
        conn.commit()
        action_word = "frozen" if freeze else "unfrozen"
        await update.effective_message.reply_text(f"✅ User {row['telegram_id']} (@{row['username'] or 'unknown'}) is now {action_word}.")
    finally:
        conn.close()


async def freeze_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_frozen_state(update, True)


async def unfreeze_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_frozen_state(update, False)


def _support_contact_text() -> str:
    raw = (Settings.support_handle or "").strip()
    if not raw:
        return "Support Team: currently unavailable. Please try again later."
    normalized = raw if raw.startswith("@") else f"@{raw}"
    return f"Support Team: {normalized}"




def _clear_interactive_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_draft_flow(context)
    for key in ("awaiting_dispute_reason", "dispute_reason", "dispute_escrow_id"):
        context.user_data.pop(key, None)


async def check_user_global(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global /check_user handler that safely resets conflicting flow state."""
    _clear_interactive_state(context)
    await deal_check_user(update, context)


async def recover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rebuild minimal escrow context from DB after restart."""
    _clear_interactive_state(context)
    conn, _, tenant, _ = _services()
    try:
        try:
            user_id = _resolve_user_id(conn, update.effective_user.id)
        except UserResolutionError:
            user_id = tenant.ensure_user(update.effective_user.id, update.effective_user.username)
        row = conn.execute(
            "SELECT id,status,buyer_id,seller_id,amount,asset,description FROM escrows WHERE buyer_id=? OR seller_id=? ORDER BY updated_at DESC, id DESC LIMIT 1",
            (user_id, user_id),
        ).fetchone()
        if not row:
            await update.effective_message.reply_text("No recoverable deal context found.", reply_markup=_start_menu())
            return
        if row["status"] not in {"pending", "active", "disputed"}:
            await update.effective_message.reply_text("No recoverable open deal context found.", reply_markup=_start_menu())
            return
        # F13: Populate all context keys so ConversationHandler states that read
        # buyer_id, seller_id, amount, asset, or conditions never receive None.
        context.user_data["escrow_id"] = int(row["id"])
        context.user_data["buyer_id"] = int(row["buyer_id"])
        context.user_data["seller_id"] = int(row["seller_id"])
        context.user_data["amount"] = str(row["amount"])
        context.user_data["asset"] = str(row["asset"])
        context.user_data["conditions"] = str(row["description"] or "")
        await update.effective_message.reply_text(
            f"Recovered deal context for Deal #{int(row['id'])} ({row['status']}).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 View Deal", callback_data=f"esc_open:{int(row['id'])}")], [InlineKeyboardButton("🏠 Main Menu", callback_data="esc_back_menu")]]),
        )
    finally:
        conn.close()

async def check_user_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Use /check_user <@username|telegram_id>")


async def support_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="profile_back")]])
    if update.callback_query:
        await update.callback_query.edit_message_text(_support_contact_text(), reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(_support_contact_text(), reply_markup=reply_markup)


async def deal_start_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_draft_flow(context)
    await start(update, context)
    return ConversationHandler.END


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



async def escrow_accept_decline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global handler for seller Accept/Decline buttons sent via notification."""
    query = update.callback_query
    await query.answer()

    # F5: Rate-limit seller respond actions to prevent spam/exhaustion.
    if await _enforce_rate_limit(query, query.from_user.id, "escrow_respond", limit=3, window_s=10):
        return

    data = query.data  # escrow_accept:ID or escrow_decline:ID
    action, escrow_id_str = data.split(":", 1)
    try:
        escrow_id = int(escrow_id_str)
    except ValueError:
        await query.edit_message_text("Invalid deal reference.")
        return

    conn, _, tenant, escrow_svc = _services()
    try:
        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row:
            await query.edit_message_text("❌ Deal not found.")
            return
        seller_user = conn.execute("SELECT telegram_id, username FROM users WHERE id=?", (int(row["seller_id"]),)).fetchone()
        if not seller_user or int(seller_user["telegram_id"]) != update.effective_user.id:
            await query.answer("This deal is not addressed to you.", show_alert=True)
            return
        if row["status"] != "pending":
            await query.answer(f"This deal is already {row['status']}.", show_alert=True)
            return

        buyer_user = conn.execute("SELECT telegram_id, username FROM users WHERE id=?", (int(row["buyer_id"]),)).fetchone()
        buyer_tg_id = int(buyer_user["telegram_id"]) if buyer_user else None
        buyer_name = buyer_user["username"] if buyer_user else "buyer"
        icon = _asset_icon(row["asset"])

        if action == "escrow_accept":
            escrow_svc.accept_escrow(escrow_id, int(row["seller_id"]))
            # F6: Reuse the already-open conn/escrow_svc for price lookup — no second _services() call.
            try:
                _p2 = escrow_svc.price_service.get_usd_value(row["asset"], Decimal(str(row["amount"])))
                _usd2 = f" (≈ ${_usd_text(_p2)} USD)"
            except Exception:
                _usd2 = ""
            await query.edit_message_text(
                f"✅ <b>Deal #{escrow_id} accepted!</b>\n\n"
                f"<b>From:</b> @{html.escape(str(buyer_name))}\n"
                f"<b>Amount:</b> <code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon}{html.escape(_usd2)}\n\n"
                f"<b>Conditions</b>\n{html.escape(str(row['description'] or ''))}\n\n"
                "💰 <i>Funds are locked. Deliver as agreed.</i>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Request Cancellation", callback_data=f"esc_active_cancel:{escrow_id}")],
                    [InlineKeyboardButton("⚖️ Open Dispute", callback_data=f"esc_active_dispute:{escrow_id}")],
                    [InlineKeyboardButton("👤 View Buyer Profile", callback_data=f"esc_active_profile:{escrow_id}:esc_back_menu")],
                    [InlineKeyboardButton("📋 View Deal", callback_data=f"esc_view_active:{escrow_id}:esc_back_menu")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="esc_back_menu")],
                ]),
                parse_mode=ParseMode.HTML,
            )
            # Notify buyer
            if buyer_tg_id:
                await context.bot.send_message(
                    chat_id=buyer_tg_id,
                    text=(
                        f"✅ <b>@{html.escape(str(seller_user['username'] or 'Seller'))} accepted your deal!</b>\n\n"
                        f"<b>Deal #{escrow_id}</b> is now <b>Active</b>\n"
                        f"<code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon}{html.escape(_usd2)}\n\n"
                        f"<b>Conditions</b>\n{html.escape(str(row['description'] or ''))}\n\n"
                        "🔒 <i>Funds locked. Release when you receive the goods/service.</i>"
                    ),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💰 Release Funds", callback_data=f"esc_active_release:{escrow_id}")],
                        [InlineKeyboardButton("❌ Request Cancellation", callback_data=f"esc_active_cancel:{escrow_id}")],
                        [InlineKeyboardButton("⚖️ Open Dispute", callback_data=f"esc_active_dispute:{escrow_id}")],
                        [InlineKeyboardButton("👤 View Seller Profile", callback_data=f"esc_active_profile:{escrow_id}:esc_back_menu")],
                        [InlineKeyboardButton("📋 View Deal", callback_data=f"esc_view_active:{escrow_id}:esc_back_menu")],
                    ]),
                    parse_mode=ParseMode.HTML,
                )
        else:  # escrow_decline
            escrow_svc.cancel_escrow(escrow_id, int(row["seller_id"]))
            await query.edit_message_text(
                f"❌ <b>Deal #{escrow_id} declined.</b>",
                reply_markup=_start_menu(),
                parse_mode=ParseMode.HTML,
            )
            if buyer_tg_id:
                await context.bot.send_message(
                    chat_id=buyer_tg_id,
                    text=(
                        f"❌ <b>Your deal request was declined.</b>\n\n"
                        f"<b>Deal #{escrow_id}</b> · {html.escape(str(row['asset']))} {icon}\n\n"
                        "<i>Your funds have been unlocked.</i>"
                    ),
                    reply_markup=_start_menu(),
                    parse_mode=ParseMode.HTML,
                )
        conn.commit()
    finally:
        conn.close()


async def esc_cancel_pending_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a pending escrow directly from the Escrow Menu deal view."""
    query = update.callback_query
    await query.answer()
    escrow_id = int(query.data.split(":")[1])
    conn, _, _, escrow_svc = _services()
    try:
        # Resolve internal user_id from Telegram ID
        tg_id = update.effective_user.id
        user_row = conn.execute("SELECT id FROM users WHERE telegram_id=?", (tg_id,)).fetchone()
        if not user_row:
            await query.answer("User not found.", show_alert=True)
            return
        internal_user_id = int(user_row["id"])

        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row or int(row["buyer_id"]) != internal_user_id:
            await query.answer("Not authorised.", show_alert=True)
            return
        if row["status"] != "pending":
            await query.answer(f"Cannot cancel — deal is already {row['status']}.", show_alert=True)
            return
        escrow_svc.cancel_escrow(escrow_id, internal_user_id)
    finally:
        conn.close()
    await query.edit_message_text(
        "✅ <b>Deal request cancelled.</b>\n\nYour funds have been unlocked.",
        reply_markup=_start_menu(),
        parse_mode=ParseMode.HTML,
    )


async def esc_view_pending_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the full Pending Deal Request from Escrow Menu (loads from DB, no conv state needed)."""
    query = update.callback_query
    await query.answer()
    # callback: esc_view_pending:{escrow_id}:{back_cb}
    parts = query.data.split(":", 2)
    escrow_id = int(parts[1])
    back_cb = parts[2] if len(parts) > 2 else "esc_back_menu"

    conn, _, _, escrow_svc = _services()
    try:
        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row or row["status"] != "pending":
            await query.edit_message_text("This deal is no longer pending.", reply_markup=_start_menu())
            return
        seller = conn.execute("SELECT username FROM users WHERE id=?", (int(row["seller_id"]),)).fetchone()
        seller_name = seller["username"] if seller and seller["username"] else "unknown"
        icon = _asset_icon(row["asset"])
        created_label = _format_db_timestamp(row["created_at"])
        text = (
            "<b>⏳ Pending Deal Request</b>\n\n"
            f"<b>Seller:</b> @{html.escape(str(seller_name))}\n"
            f"<b>Amount:</b> <code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon}\n"
            f"<b>Deal #{escrow_id}</b> · Created: {html.escape(str(created_label))}\n\n"
            f"<b>Conditions</b>\n{html.escape(str(row['description'] or '-'))}\n\n"
            "⏳ <i>Waiting for seller to accept…</i>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Request", callback_data=f"esc_cancel_pending:{escrow_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=back_cb)],
        ])
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    finally:
        conn.close()


# ──────────────────────────────────────────────
# ACTIVE DEAL HANDLERS (global, outside conv)
# ──────────────────────────────────────────────

class UserResolutionError(RuntimeError):
    """Raised when a Telegram user has no internal user row."""


def _resolve_user_id(conn, telegram_id: int) -> int:
    row = conn.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    if not row:
        raise UserResolutionError(f"telegram user {int(telegram_id)} is not registered")
    return int(row["id"])


async def esc_active_profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View counterparty profile from active deal."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 2)
    escrow_id = int(parts[1])
    back_cb = parts[2] if len(parts) > 2 else "esc_back_menu"
    conn, _, _, escrow_svc = _services()
    try:
        try:
            user_id = _resolve_user_id(conn, update.effective_user.id)
        except UserResolutionError:
            await query.answer("User not registered.", show_alert=True)
            return
        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row:
            await query.edit_message_text("Deal not found.")
            return
        cp_id = escrow_svc.counterparty_user_id(row, user_id)
        cp_row = conn.execute("SELECT * FROM users WHERE id=?", (cp_id,)).fetchone()
        profile_txt = _render_user_profile(_user_profile(conn, cp_row))
    finally:
        conn.close()
    await query.edit_message_text(
        profile_txt,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=back_cb)]]),
        parse_mode=ParseMode.HTML,
    )


async def esc_active_release_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Release funds confirmation screen."""
    query = update.callback_query
    await query.answer()
    escrow_id = int(query.data.split(":")[1])
    conn, _, _, escrow_svc = _services()
    try:
        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row or row["status"] != "active":
            await query.answer("Deal is no longer active.", show_alert=True)
            return
        try:
            user_id = _resolve_user_id(conn, update.effective_user.id)
        except UserResolutionError:
            await query.answer("User not registered.", show_alert=True)
            return
        if int(row["buyer_id"]) != user_id:
            # WARNING: Fail closed on crafted callback from non-buyer to prevent unauthorized release flow.
            # Secure alternative: only allow buyer role to proceed to release confirmation.
            await query.answer("Only the buyer can release funds.", show_alert=True)
            return
        icon = _asset_icon(row["asset"])
        try:
            usd_val = escrow_svc.price_service.get_usd_value(row["asset"], Decimal(str(row["amount"])))
            usd_str = f"≈ ${_usd_text(usd_val)} USD"
        except Exception:
            usd_str = ""
    finally:
        conn.close()
    await query.edit_message_text(
        f"<b>💰 Release Funds — Deal #{escrow_id}</b>\n\n"
        f"<code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon}\n"
        f"{html.escape(usd_str)}\n\n"
        "⚠️ <b>Are you sure?</b> This confirms delivery and releases funds to the seller.\n"
        "<i>This action cannot be undone.</i>",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm Release", callback_data=f"esc_release_confirm:{escrow_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"esc_view_active:{escrow_id}:esc_back_menu"),
            ]
        ]),
        parse_mode=ParseMode.HTML,
    )


async def esc_release_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Actually release the funds."""
    query = update.callback_query
    await query.answer()
    escrow_id = int(query.data.split(":")[1])
    conn, _, tenant, escrow_svc = _services()
    try:
        try:
            user_id = _resolve_user_id(conn, update.effective_user.id)
        except UserResolutionError:
            await query.answer("User not registered.", show_alert=True)
            return
        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row or row["status"] != "active":
            await query.edit_message_text("Deal is no longer active.", reply_markup=_start_menu())
            return
        if int(row["buyer_id"]) != user_id:
            await query.answer("Only the buyer can release funds.", show_alert=True)
            return
        view = escrow_svc.release(escrow_id, actor_user_id=user_id)
        seller = conn.execute("SELECT username, telegram_id FROM users WHERE id=?", (int(row["seller_id"]),)).fetchone()
        seller_name = seller["username"] if seller else "seller"
        seller_tg = int(seller["telegram_id"]) if seller else None
        icon = _asset_icon(view.asset)
        conn.commit()
    finally:
        conn.close()

    stars_kb = [
        InlineKeyboardButton("⭐ 1", callback_data=f"esc_hist_rate:{escrow_id}:1:1"),
        InlineKeyboardButton("⭐ 2", callback_data=f"esc_hist_rate:{escrow_id}:2:1"),
        InlineKeyboardButton("⭐ 3", callback_data=f"esc_hist_rate:{escrow_id}:3:1"),
        InlineKeyboardButton("⭐ 4", callback_data=f"esc_hist_rate:{escrow_id}:4:1"),
        InlineKeyboardButton("⭐ 5", callback_data=f"esc_hist_rate:{escrow_id}:5:1"),
    ]
    await query.edit_message_text(
        f"✅ <b>Funds Released!</b>\n\n"
        f"<code>{html.escape(str(view.amount))} {html.escape(str(view.asset))}</code> {icon} sent to @{html.escape(str(seller_name))}\n\n"
        "⭐ <b>Rate this deal:</b>",
        reply_markup=InlineKeyboardMarkup([stars_kb, [InlineKeyboardButton("🏠 Skip", callback_data="esc_back_menu")]]),
        parse_mode=ParseMode.HTML,
    )
    if seller_tg:
        try:
            _p3 = _services()[3].price_service.get_usd_value(view.asset, Decimal(str(view.amount)))
            _usd3 = f" (≈ ${_usd_text(_p3)} USD)"
        except Exception:
            _usd3 = ""
        seller_stars_kb = [
            InlineKeyboardButton("⭐ 1", callback_data=f"esc_hist_rate:{escrow_id}:1:1"),
            InlineKeyboardButton("⭐ 2", callback_data=f"esc_hist_rate:{escrow_id}:2:1"),
            InlineKeyboardButton("⭐ 3", callback_data=f"esc_hist_rate:{escrow_id}:3:1"),
            InlineKeyboardButton("⭐ 4", callback_data=f"esc_hist_rate:{escrow_id}:4:1"),
            InlineKeyboardButton("⭐ 5", callback_data=f"esc_hist_rate:{escrow_id}:5:1"),
        ]
        await context.bot.send_message(
            chat_id=seller_tg,
            text=f"✅ <b>Payment received!</b>\n\n"
                 f"<code>{html.escape(str(view.amount))} {html.escape(str(view.asset))}</code> {icon}{html.escape(_usd3)} released to your account.\n"
                 f"<b>Deal #{escrow_id}</b>\n\n"
                 "⭐ <b>Rate the buyer:</b>",
            reply_markup=InlineKeyboardMarkup([seller_stars_kb, [InlineKeyboardButton("🏠 Skip", callback_data="esc_back_menu")]]),
            parse_mode=ParseMode.HTML,
        )


async def esc_active_cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buyer or seller requests cancellation of active deal — notifies counterparty."""
    query = update.callback_query
    await query.answer()
    escrow_id = int(query.data.split(":")[1])
    conn, _, _, _ = _services()
    try:
        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row or row["status"] != "active":
            await query.answer("Deal is no longer active.", show_alert=True)
            return
        try:
            user_id = _resolve_user_id(conn, update.effective_user.id)
        except UserResolutionError:
            await query.answer("User not registered.", show_alert=True)
            return
        is_buyer = int(row["buyer_id"]) == user_id
        is_seller = int(row["seller_id"]) == user_id
        if not is_buyer and not is_seller:
            await query.answer("Not authorised.", show_alert=True)
            return
        requester_name = update.effective_user.username or str(update.effective_user.id)
        icon = _asset_icon(row["asset"])
        requester_id = user_id
        responder_id = int(row["seller_id"] if is_buyer else row["buyer_id"])
        if is_buyer:
            cp = conn.execute("SELECT username, telegram_id FROM users WHERE id=?", (int(row["seller_id"]),)).fetchone()
        else:
            cp = conn.execute("SELECT username, telegram_id FROM users WHERE id=?", (int(row["buyer_id"]),)).fetchone()
        cp_name = cp["username"] if cp else "counterparty"
        cp_tg = int(cp["telegram_id"]) if cp else None
        conn.execute(
            """
            INSERT INTO active_cancel_requests(escrow_id, requester_user_id, responder_user_id, status, created_at, responded_at)
            VALUES(?, ?, ?, 'open', CURRENT_TIMESTAMP, NULL)
            ON CONFLICT(escrow_id) DO UPDATE SET
              requester_user_id=excluded.requester_user_id,
              responder_user_id=excluded.responder_user_id,
              status='open',
              created_at=CURRENT_TIMESTAMP,
              responded_at=NULL
            """,
            (escrow_id, requester_id, responder_id),
        )
        conn.commit()
    finally:
        conn.close()

    await query.edit_message_text(
        f"<b>⏳ Cancellation Request Sent</b>\n\n"
        f"Your request to cancel Deal #{escrow_id} has been sent to @{html.escape(str(cp_name))}.\n\n"
        "<i>Waiting for their response…</i>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="esc_back_menu")]]),
        parse_mode=ParseMode.HTML,
    )
    if cp_tg:
        await context.bot.send_message(
            chat_id=cp_tg,
            text=(
                f"<b>⚠️ Cancellation Request — Deal #{escrow_id}</b>\n\n"
                f"@{html.escape(str(requester_name))} wants to cancel this deal.\n"
                f"<b>Amount:</b> <code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon}\n\n"
                f"<b>Conditions</b>\n{html.escape(str(row['description'] or '-'))}\n\n"
                "Do you accept the cancellation?"
            ),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Accept", callback_data=f"esc_cancel_accept:{escrow_id}"),
                    InlineKeyboardButton("❌ Decline", callback_data=f"esc_cancel_decline:{escrow_id}"),
                ]
            ]),
            parse_mode=ParseMode.HTML,
        )


async def esc_cancel_response_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Counterparty accepts or declines server-side tracked cancellation request."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[0]
    escrow_id = int(parts[1])

    # F8: unpack escrow_svc so we reuse the same service instance throughout.
    conn, _, _, escrow_svc = _services()
    try:
        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row or row["status"] != "active":
            await query.edit_message_text("This deal is no longer active.")
            return
        try:
            responder_id = _resolve_user_id(conn, update.effective_user.id)
        except UserResolutionError:
            await query.answer("User not registered.", show_alert=True)
            return
        req = conn.execute("SELECT * FROM active_cancel_requests WHERE escrow_id=? AND status='open'", (escrow_id,)).fetchone()
        if not req:
            await query.answer("Cancellation request is no longer valid.", show_alert=True)
            return
        requester_internal_id = int(req["requester_user_id"])
        expected_responder_id = int(req["responder_user_id"])
        if requester_internal_id == responder_id:
            await query.answer("You cannot respond to your own request.", show_alert=True)
            return
        participants = {int(row["buyer_id"]), int(row["seller_id"])}
        if requester_internal_id not in participants or expected_responder_id not in participants:
            await query.answer("Cancellation request is invalid.", show_alert=True)
            return
        if responder_id != expected_responder_id:
            await query.answer("Not authorised.", show_alert=True)
            return

        requester = conn.execute("SELECT telegram_id FROM users WHERE id=?", (requester_internal_id,)).fetchone()
        requester_tg = int(requester["telegram_id"]) if requester else None
        icon = _asset_icon(row["asset"])

        if action == "esc_cancel_accept":
            # F1+F8: use cancel_escrow_mutual() which correctly unlocks funds for active escrows.
            # This replaces the old EscrowService(conn).cancel_escrow() call that raised ValueError
            # on active escrows and also created a second service instance sharing the same connection.
            escrow_svc.cancel_escrow_mutual(escrow_id, requester_internal_id, responder_id)
            conn.commit()
            await query.edit_message_text(
                f"✅ <b>Cancellation accepted.</b>\nDeal #{escrow_id} has been cancelled.",
                reply_markup=_start_menu(),
                parse_mode=ParseMode.HTML,
            )
            if requester_tg:
                await context.bot.send_message(
                    chat_id=requester_tg,
                    text=(
                        f"✅ <b>Cancellation accepted!</b>\n\n"
                        f"Deal #{escrow_id} has been cancelled.\n"
                        f"Your <code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon} have been unlocked."
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_start_menu(),
                )
        else:
            conn.execute("UPDATE active_cancel_requests SET status='declined', responded_at=CURRENT_TIMESTAMP WHERE escrow_id=?", (escrow_id,))
            conn.commit()
            await query.edit_message_text(
                f"❌ <b>Cancellation declined.</b>\nDeal #{escrow_id} remains active.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 View Deal", callback_data=f"esc_view_active:{escrow_id}:esc_back_menu"), InlineKeyboardButton("🏠 Main Menu", callback_data="esc_back_menu")]]),
                parse_mode=ParseMode.HTML,
            )
            if requester_tg:
                await context.bot.send_message(
                    chat_id=requester_tg,
                    text=(
                        f"❌ <b>Cancellation declined.</b>\n\n"
                        f"@{html.escape(str(update.effective_user.username or 'Counterparty'))} declined your cancellation request for Deal #{escrow_id}.\n\n"
                        "You can send another request or open a dispute."
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⚖️ Open Dispute", callback_data=f"esc_active_dispute:{escrow_id}")],
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="esc_back_menu")],
                    ]),
                )
    finally:
        conn.close()


async def esc_active_dispute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask user for dispute reason."""
    query = update.callback_query
    await query.answer()
    escrow_id = int(query.data.split(":")[1])
    context.user_data["dispute_escrow_id"] = escrow_id
    await query.edit_message_text(
        f"<b>⚖️ Open Dispute — Deal #{escrow_id}</b>\n\n"
        "Please describe the reason for your dispute in detail.\n"
        "<i>This will be reviewed by our moderation team.</i>\n\n"
        "Type your reason below:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"esc_active_page:1")]]),
        parse_mode=ParseMode.HTML,
    )
    context.user_data["awaiting_dispute_reason"] = True
    context.user_data["dispute_escrow_id"] = escrow_id
    # F7: Clear any stale pending dispute so a new flow never inherits an old reason.
    context.user_data.pop("pending_dispute", None)


async def esc_dispute_reason_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive dispute reason text and show confirm/cancel."""
    if not context.user_data.get("awaiting_dispute_reason"):
        return
    reason = update.effective_message.text.strip()
    if not reason:
        await update.effective_message.reply_text("Please enter a valid reason.")
        return
    if len(reason) > MAX_DISPUTE_REASON_CHARS:
        await update.effective_message.reply_text(f"Reason is too long. Maximum is {MAX_DISPUTE_REASON_CHARS} characters.")
        return
    escrow_id = context.user_data.get("dispute_escrow_id")
    # F7: Store reason bound to escrow_id so a navigation change cannot submit
    # a stale reason against the wrong deal.
    context.user_data["pending_dispute"] = {"escrow_id": escrow_id, "reason": reason}
    context.user_data["awaiting_dispute_reason"] = False
    await update.effective_message.reply_text(
        f"<b>⚖️ Confirm Dispute — Deal #{escrow_id}</b>\n\n"
        f"<b>Reason:</b>\n{html.escape(str(reason))}\n\n"
        "Submit this dispute to the moderation team?",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm", callback_data=f"esc_dispute_submit:{escrow_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"esc_active_page:1"),
            ]
        ]),
        parse_mode=ParseMode.HTML,
    )


async def esc_dispute_submit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Submit the dispute and notify moderator."""
    query = update.callback_query
    await query.answer()
    escrow_id = int(query.data.split(":")[1])
    # F7: Read reason from the scoped pending_dispute dict and verify the escrow_id
    # matches so a navigation change cannot submit a stale reason against a different deal.
    pending = context.user_data.get("pending_dispute") or {}
    if pending.get("escrow_id") != escrow_id:
        await query.answer("Dispute session expired. Please start again.", show_alert=True)
        context.user_data.pop("pending_dispute", None)
        return
    reason = pending.get("reason", "No reason provided")
    context.user_data.pop("pending_dispute", None)

    if await _enforce_rate_limit(query, query.from_user.id, "open_dispute", limit=2, window_s=60):
        return

    conn, _, tenant, escrow_svc = _services()
    try:
        try:
            user_id = _resolve_user_id(conn, update.effective_user.id)
        except UserResolutionError:
            await query.answer("User not registered.", show_alert=True)
            return
        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row or row["status"] not in ("pending", "active"):
            await query.edit_message_text("Deal is not eligible for dispute.", reply_markup=_start_menu())
            return
        escrow_svc.dispute(escrow_id, opened_by_user_id=user_id, reason=reason)
        conn.commit()

        # Notify counterparty
        opener_is_buyer = int(row["buyer_id"]) == user_id
        opener_username = update.effective_user.username or str(update.effective_user.id)
        cp_row = conn.execute(
            "SELECT username, telegram_id FROM users WHERE id=?",
            (int(row["seller_id"]) if opener_is_buyer else int(row["buyer_id"]),)
        ).fetchone()
        if cp_row and cp_row["telegram_id"]:
            try:
                await context.bot.send_message(
                    chat_id=int(cp_row["telegram_id"]),
                    text=(
                        f"<b>⚖️ Dispute Opened — Deal #{escrow_id}</b>\n\n"
                        f"@{html.escape(str(opener_username))} has opened a dispute on this deal.\n\n"
                        f"<b>Reason:</b>\n{html.escape(str(reason))}\n\n"
                        "<i>A moderator will review the case and contact both parties.</i>"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📂 Escrow Menu", callback_data="esc_back_menu")],
                        [InlineKeyboardButton("🏠 Main Menu", callback_data="deal_back_main")],
                    ]),
                )
            except Exception:
                LOGGER.exception("Failed to notify counterparty of dispute")

        # Notify all moderators independently.
        if Settings.moderator_ids:
            buyer = conn.execute("SELECT username FROM users WHERE id=?", (int(row["buyer_id"]),)).fetchone()
            seller = conn.execute("SELECT username FROM users WHERE id=?", (int(row["seller_id"]),)).fetchone()
            buyer_name = buyer["username"] if buyer else "unknown"
            seller_name = seller["username"] if seller else "unknown"
            icon = _asset_icon(row["asset"])
            mod_text = (
                f"<b>🚨 New Dispute — Deal #{escrow_id}</b>\n\n"
                f"<b>Buyer:</b> @{html.escape(str(buyer_name))}\n"
                f"<b>Seller:</b> @{html.escape(str(seller_name))}\n"
                f"<b>Amount:</b> <code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon}\n\n"
                f"<b>Reason:</b>\n{html.escape(str(reason))}"
            )
            mod_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Release to Seller", callback_data=f"mod_resolve:{escrow_id}:release_seller")],
                [InlineKeyboardButton("↩️ Refund Buyer", callback_data=f"mod_resolve:{escrow_id}:refund_buyer")],
                [InlineKeyboardButton("👥 Create Group Chat", callback_data=f"mod_group:{escrow_id}")],
                [InlineKeyboardButton("⬅️ Back to Menu", callback_data="esc_back_menu")],
            ])
            for moderator_id in sorted(Settings.moderator_ids):
                try:
                    await context.bot.send_message(
                        chat_id=int(moderator_id),
                        text=mod_text,
                        reply_markup=mod_kb,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as exc:
                    LOGGER.warning("Failed to notify moderator_id=%s escrow_id=%s reason=%s", int(moderator_id), int(escrow_id), sanitize_runtime_error(str(exc)))
    finally:
        conn.close()

    await query.edit_message_text(
        f"✅ <b>Dispute submitted for Deal #{escrow_id}</b>\n\n"
        "Our moderation team will review your case and contact you shortly.\n\n"
        f"<b>Your reason:</b>\n{html.escape(str(reason))}",
        reply_markup=_start_menu(),
        parse_mode=ParseMode.HTML,
    )


async def esc_view_active_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full active deal view with all action buttons — accessible from Escrow Menu or buyer notification."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 2)
    escrow_id = int(parts[1])
    back_cb = parts[2] if len(parts) > 2 else "esc_back_menu"

    conn, _, _, escrow_svc = _services()
    try:
        tg_id = update.effective_user.id
        user_id = _resolve_user_id(conn, tg_id)
        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row or row["status"] != "active":
            await query.edit_message_text("This deal is no longer active.", reply_markup=_start_menu())
            return
        buyer_id_db = int(row["buyer_id"])
        seller_id_db = int(row["seller_id"])
        # Determine role by checking both buyer and seller telegram IDs
        buyer_user = conn.execute("SELECT id, username, telegram_id FROM users WHERE id=?", (buyer_id_db,)).fetchone()
        seller_user = conn.execute("SELECT id, username, telegram_id FROM users WHERE id=?", (seller_id_db,)).fetchone()
        is_buyer = buyer_user and int(buyer_user["telegram_id"]) == tg_id
        is_seller = seller_user and int(seller_user["telegram_id"]) == tg_id
        if is_buyer:
            cp_name = seller_user["username"] if seller_user and seller_user["username"] else "unknown"
            role = "Buyer"
            counterparty_role = "Seller"
        else:
            cp_name = buyer_user["username"] if buyer_user and buyer_user["username"] else "unknown"
            role = "Seller"
            counterparty_role = "Buyer"
        icon = _asset_icon(row["asset"])
        created_label = _format_db_timestamp(row["created_at"])
        amount = row["amount"]
        asset = row["asset"]
        description = row["description"] or "-"
    finally:
        conn.close()

    try:
        conn3, _, _, esc3 = _services()
        usd_val = esc3.price_service.get_usd_value(asset, Decimal(str(amount)))
        usd_str = f" (≈ ${_usd_text(usd_val)} USD)"
        conn3.close()
    except Exception:
        usd_str = ""
    text = (
        "<b>✅ Active Deal</b>\n\n"
        f"<b>Your role:</b> {role}\n"
        f"<b>{counterparty_role}:</b> @{html.escape(str(cp_name))}\n"
        f"<b>Amount:</b> <code>{html.escape(str(amount))} {html.escape(str(asset))}</code> {icon}{html.escape(usd_str)}\n"
        f"<b>Deal #{escrow_id}</b> · Created: {html.escape(str(created_label))}\n\n"
        f"<b>Conditions</b>\n{html.escape(str(description))}"
    )
    kb = []
    if is_buyer:
        kb.append([InlineKeyboardButton("💰 Release Funds", callback_data=f"esc_active_release:{escrow_id}")])
    kb.append([InlineKeyboardButton("❌ Request Cancellation", callback_data=f"esc_active_cancel:{escrow_id}")])
    kb.append([InlineKeyboardButton("⚖️ Open Dispute", callback_data=f"esc_active_dispute:{escrow_id}")])
    kb.append([InlineKeyboardButton("👤 View Counterparty Profile", callback_data=f"esc_active_profile:{escrow_id}:{back_cb}")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data=back_cb)])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


async def _is_moderator(telegram_id: int) -> bool:
    return int(telegram_id) in Settings.moderator_ids


async def mod_resolve_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Moderator resolves a dispute: release to seller or refund buyer."""
    query = update.callback_query
    await query.answer()

    if not await _is_moderator(update.effective_user.id):
        await query.answer("Not authorised — moderators only.", show_alert=True)
        return

    parts = query.data.split(":")
    escrow_id = int(parts[1])
    resolution = parts[2]  # release_seller or refund_buyer
    if resolution not in {"release_seller", "refund_buyer", "split"}:
        await query.answer("Invalid resolution", show_alert=True)
        return

    conn, _, tenant, escrow_svc = _services()
    try:
        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row:
            await query.edit_message_text("Deal not found.")
            return
        if row["status"] != "disputed":
            await query.answer(f"Deal is already {row['status']}.", show_alert=True)
            return

        mod_user_id = _resolve_user_id(conn, update.effective_user.id)
        buyer = conn.execute("SELECT username, telegram_id FROM users WHERE id=?", (int(row["buyer_id"]),)).fetchone()
        seller = conn.execute("SELECT username, telegram_id FROM users WHERE id=?", (int(row["seller_id"]),)).fetchone()
        buyer_name = buyer["username"] if buyer else "buyer"
        seller_name = seller["username"] if seller else "seller"
        buyer_tg = int(buyer["telegram_id"]) if buyer else None
        seller_tg = int(seller["telegram_id"]) if seller else None
        icon = _asset_icon(row["asset"])

        escrow_svc.resolve_dispute(escrow_id, admin_user_id=mod_user_id, resolution=resolution)
        conn.commit()

        if resolution == "release_seller":
            outcome_text = f"✅ Funds released to @{html.escape(str(seller_name))}"
            buyer_msg = (
                f"⚖️ <b>Dispute resolved — Deal #{escrow_id}</b>\n\n"
                f"The moderator ruled in favour of the seller.\n"
                f"<code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon} released to @{html.escape(str(seller_name))}."
            )
            seller_msg = (
                f"⚖️ <b>Dispute resolved — Deal #{escrow_id}</b>\n\n"
                f"The moderator ruled in your favour. 🎉\n"
                f"<code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon} released to your account."
            )
        else:  # refund_buyer
            outcome_text = f"↩️ Funds refunded to @{html.escape(str(buyer_name))}"
            buyer_msg = (
                f"⚖️ <b>Dispute resolved — Deal #{escrow_id}</b>\n\n"
                f"The moderator ruled in your favour. 🎉\n"
                f"<code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon} refunded to your account."
            )
            seller_msg = (
                f"⚖️ <b>Dispute resolved — Deal #{escrow_id}</b>\n\n"
                f"The moderator ruled in favour of the buyer.\n"
                f"Funds have been returned to @{html.escape(str(buyer_name))}."
            )

        await query.edit_message_text(
            f"<b>⚖️ Dispute #{escrow_id} Resolved</b>\n\n"
            f"{outcome_text}\n\n"
            f"<b>Buyer:</b> @{html.escape(str(buyer_name))}\n"
            f"<b>Seller:</b> @{html.escape(str(seller_name))}\n"
            f"<b>Amount:</b> <code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon}",
            parse_mode=ParseMode.HTML,
        )

        history_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📜 View in History", callback_data="esc_menu:history")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="esc_back_menu")],
        ])
        # Notify both parties
        for tg_id, msg in [(buyer_tg, buyer_msg), (seller_tg, seller_msg)]:
            if tg_id:
                try:
                    await context.bot.send_message(chat_id=tg_id, text=msg, parse_mode=ParseMode.HTML, reply_markup=history_kb)
                except Exception:
                    LOGGER.exception("Failed to notify party tg_id=%s", tg_id)
    finally:
        conn.close()


async def mod_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send instructions to moderator on how to create the group — Telegram Bot API cannot create groups directly."""
    query = update.callback_query
    await query.answer()

    if not await _is_moderator(update.effective_user.id):
        await query.answer("Not authorised — moderators only.", show_alert=True)
        return

    parts = query.data.split(":")
    escrow_id = int(parts[1])

    conn, _, _, _ = _services()
    try:
        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row:
            await query.answer("Deal not found.", show_alert=True)
            return
        buyer = conn.execute("SELECT username FROM users WHERE id=?", (int(row["buyer_id"]),)).fetchone()
        seller = conn.execute("SELECT username FROM users WHERE id=?", (int(row["seller_id"]),)).fetchone()
        buyer_name = buyer["username"] if buyer else "unknown"
        seller_name = seller["username"] if seller else "unknown"
        icon = _asset_icon(row["asset"])
    finally:
        conn.close()

    # Telegram Bot API cannot create groups, so we give the moderator the message to paste
    group_welcome = (
        f"🚨 Dispute Resolution — Deal #{escrow_id}\n\n"
        f"Parties involved:\n"
        f"• Buyer: @{buyer_name}\n"
        f"• Seller: @{seller_name}\n\n"
        f"Amount: {row['amount']} {row['asset']}\n\n"
        f"Conditions:\n{row['description'] or '-'}\n\n"
        "Please explain your side of the situation clearly.\n"
        "The moderator will review and make a final decision.\n\n"
        "📌 Both parties must provide evidence if available (screenshots, receipts, etc.)"
    )

    await query.answer()
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text=(
            f"<b>👥 Create a group with both parties:</b>\n\n"
            f"1. Create a new Telegram group\n"
            f"2. Add @{html.escape(str(buyer_name))}, @{html.escape(str(seller_name))}, and all moderators\n"
            f"3. Paste the message below as the first message:\n\n"
            f"<code>{html.escape(group_welcome)}</code>"
        ),
        parse_mode=ParseMode.HTML,
    )


async def esc_view_dispute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full dispute view — parties see deal info + Open Dispute button; moderator sees control panel."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 2)
    escrow_id = int(parts[1])
    back_cb = parts[2] if len(parts) > 2 else "esc_back_menu"

    conn, _, _, escrow_svc = _services()
    try:
        tg_id = update.effective_user.id
        user_id = _resolve_user_id(conn, tg_id)
        row = conn.execute("SELECT * FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        if not row:
            await query.edit_message_text("Deal not found.", reply_markup=_start_menu())
            return
        buyer = conn.execute("SELECT username, telegram_id FROM users WHERE id=?", (int(row["buyer_id"]),)).fetchone()
        seller = conn.execute("SELECT username, telegram_id FROM users WHERE id=?", (int(row["seller_id"]),)).fetchone()
        buyer_name = buyer["username"] if buyer else "unknown"
        seller_name = seller["username"] if seller else "unknown"
        dispute = conn.execute(
            "SELECT reason, opened_by_user_id FROM disputes WHERE escrow_id=? ORDER BY id DESC LIMIT 1",
            (escrow_id,)
        ).fetchone()
        reason = dispute["reason"] if dispute else "No reason provided"
        icon = _asset_icon(row["asset"])
        created_label = _format_db_timestamp(row["created_at"])
        is_mod = await _is_moderator(tg_id)
    finally:
        conn.close()

    text = (
        f"<b>⚖️ Dispute — Deal #{escrow_id}</b>\n\n"
        f"<b>Buyer:</b> @{html.escape(str(buyer_name))}\n"
        f"<b>Seller:</b> @{html.escape(str(seller_name))}\n"
        f"<b>Amount:</b> <code>{html.escape(str(row['amount']))} {html.escape(str(row['asset']))}</code> {icon}\n"
        f"<b>Created:</b> {html.escape(str(created_label))}\n\n"
        f"<b>Conditions</b>\n{html.escape(str(row['description'] or '-'))}\n\n"
        f"<b>Dispute Reason</b>\n{html.escape(str(reason))}"
    )

    kb = []
    if is_mod:
        kb.append([InlineKeyboardButton("✅ Release to Seller", callback_data=f"mod_resolve:{escrow_id}:release_seller")])
        kb.append([InlineKeyboardButton("↩️ Refund Buyer", callback_data=f"mod_resolve:{escrow_id}:refund_buyer")])
        kb.append([InlineKeyboardButton("👥 Create Group Chat", callback_data=f"mod_group:{escrow_id}")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data=back_cb)])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise FatalStartupError("TELEGRAM_BOT_TOKEN is required")

    global DEPOSIT_ISSUANCE_READY, DEPOSIT_ISSUANCE_ERROR
    _validate_role_configuration()
    try:
        preflight = run_startup_preflight("bot")
    except PreflightIntegrityError as exc:
        # WARNING: bot startup fails closed on route-integrity failures to prevent operating with tampered routes.
        LOGGER.error("bot startup aborted by fatal integrity preflight: %s", "; ".join(exc.status.reasons) or str(exc))
        raise
    DEPOSIT_ISSUANCE_READY = bool(preflight.deposit_issuance_ready)
    DEPOSIT_ISSUANCE_ERROR = preflight.deposit_issuance_error
    if not DEPOSIT_ISSUANCE_READY:
        LOGGER.warning("bot startup degraded mode: deposit issuance unavailable: %s", DEPOSIT_ISSUANCE_ERROR or "unknown")

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
                CallbackQueryHandler(deal_enter_amount_callbacks, pattern=r"^(deal_asset:[A-Z]+|deal_back_to_search|profile_deposit|deal_back_main)$"),
            ],
            DEAL_ENTER_CONDITIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, deal_conditions_input),
                CallbackQueryHandler(deal_back_from_conditions, pattern="^deal_back_to_amount$"),
            ],
            DEAL_PENDING_VIEW: [CallbackQueryHandler(deal_pending_actions, pattern="^deal_(cancel_request|back_pending|view_counterparty|release_prompt|back_to_pending|finish_main)$")],
            DEAL_CANCEL_INFO: [CallbackQueryHandler(deal_cancel_info_actions, pattern="^deal_back_to_pending$")],
            DEAL_RELEASE_CONFIRM: [CallbackQueryHandler(deal_release_confirm, pattern="^deal_(release_confirm|back_to_pending)$")],
            DEAL_RATE_SELLER: [CallbackQueryHandler(deal_rate_seller, pattern=r"^deal_(rate_seller:\d+|finish)$")],
            DEAL_RATE_BUYER: [CallbackQueryHandler(deal_rate_buyer_wait, pattern="^deal_finish$")],
        },
        fallbacks=[
            CallbackQueryHandler(deal_search_result_cb, pattern=r"^deal_back_main$"),
            CommandHandler("start", deal_start_fallback),
            CommandHandler("cancel", cancel_flow),
        ],
        allow_reentry=True,
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
                CallbackQueryHandler(withdraw_back, pattern=r"^wd_(back_amount|back_assets|back_profile)$"),
            ],
            WD_CONFIRM: [CallbackQueryHandler(withdraw_confirm, pattern=r"^wd_(confirm|cancel_addr|back_amount)$")],
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
    app.add_handler(CommandHandler("check_user", check_user_global), group=-1)
    app.add_handler(CommandHandler("recover", recover_command), group=-1)
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("cancel", cancel_flow))
    app.add_handler(CommandHandler("watcher_status", watcher_status))
    app.add_handler(CommandHandler("signer_retry_list", signer_retry_list))
    app.add_handler(CommandHandler("signer_retry_detail", signer_retry_detail))
    app.add_handler(CommandHandler("signer_retry_action", signer_retry_action))
    app.add_handler(CommandHandler("run_signer", run_signer))
    app.add_handler(CommandHandler("unresolved_withdrawals", unresolved_withdrawals))
    app.add_handler(CommandHandler("withdrawal_reconcile", withdrawal_reconcile))
    app.add_handler(CommandHandler("revenue", revenue_report))
    app.add_handler(CommandHandler("support", support_team))
    app.add_handler(CommandHandler("freeze", freeze_user))
    app.add_handler(CommandHandler("unfreeze", unfreeze_user))
    app.add_handler(CallbackQueryHandler(seller_rate_buyer_callback, pattern=r"^deal_rate_buyer:\d+:\d+$"))
    app.add_handler(withdraw_conv)
    app.add_handler(deposit_conv)
    app.add_handler(CallbackQueryHandler(profile_open_from_menu, pattern=r"^profile$"))
    app.add_handler(CallbackQueryHandler(profile_actions, pattern=r"^(profile_(?!withdraw$)|esc_wd_open:|profile_tx_history:|tx_detail:).*$"))
    app.add_handler(CallbackQueryHandler(escrow_menu_actions, pattern=r"^esc_menu:(pending|active|disputes|history)$"))
    app.add_handler(CallbackQueryHandler(escrow_history_actions, pattern=r"^(esc_hist_|esc_back_menu|esc_pending_page:|esc_active_page:|esc_disputes_page:|esc_open:).*$"))
    app.add_handler(CallbackQueryHandler(esc_view_pending_handler, pattern=r"^esc_view_pending:\d+:.+$"))
    app.add_handler(CallbackQueryHandler(esc_view_active_handler, pattern=r"^esc_view_active:\d+(:.+)?$"))
    app.add_handler(CallbackQueryHandler(esc_active_profile_handler, pattern=r"^esc_active_profile:\d+:.+$"))
    app.add_handler(CallbackQueryHandler(esc_active_release_handler, pattern=r"^esc_active_release:\d+$"))
    app.add_handler(CallbackQueryHandler(esc_release_confirm_handler, pattern=r"^esc_release_confirm:\d+$"))
    app.add_handler(CallbackQueryHandler(esc_active_cancel_handler, pattern=r"^esc_active_cancel:\d+$"))
    app.add_handler(CallbackQueryHandler(esc_cancel_response_handler, pattern=r"^esc_cancel_(accept|decline):\d+$"))
    app.add_handler(CallbackQueryHandler(esc_active_dispute_handler, pattern=r"^esc_active_dispute:\d+$"))
    app.add_handler(CallbackQueryHandler(esc_dispute_submit_handler, pattern=r"^esc_dispute_submit:\d+$"))
    app.add_handler(CallbackQueryHandler(esc_view_dispute_handler, pattern=r"^esc_view_dispute:\d+(:.+)?$"))
    app.add_handler(CallbackQueryHandler(mod_resolve_handler, pattern=r"^mod_resolve:\d+:(release_seller|refund_buyer|split)$"))
    app.add_handler(CallbackQueryHandler(mod_group_handler, pattern=r"^mod_group:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, esc_dispute_reason_message))
    app.add_handler(CallbackQueryHandler(esc_cancel_pending_handler, pattern=r"^esc_cancel_pending:\d+$"))
    app.add_handler(CallbackQueryHandler(escrow_accept_decline, pattern=r"^escrow_(accept|decline):\d+$"))
    app.add_handler(CallbackQueryHandler(on_menu_click))
    app.run_polling()


if __name__ == "__main__":
    main()
