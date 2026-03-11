from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from urllib import request

LOGGER = logging.getLogger(__name__)


def _send_telegram_text(conn, user_id: int, text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return

    user = conn.execute("SELECT telegram_id FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or not user["telegram_id"]:
        return

    payload = json.dumps({"chat_id": int(user["telegram_id"]), "text": text}).encode("utf-8")
    req = request.Request(
        url=f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10):
            return
    except Exception:
        LOGGER.exception("deposit notification failed for user_id=%s", user_id)


def notify_deposit_detected(conn, user_id: int, asset: str, amount: Decimal, confirmations: int | None = None) -> None:
    seen_amount = Decimal(str(amount))
    text = f"Thank you for your trust. We detected your deposit of {seen_amount} {asset}."
    text += "\nWe are waiting for blockchain confirmations before crediting your balance."
    if confirmations is not None and int(confirmations) > 0:
        text += f"\nCurrent confirmations: {int(confirmations)}"
    text += "\nYou will receive another message once the deposit is confirmed."
    _send_telegram_text(conn, user_id, text)


def notify_deposit_credited(
    conn,
    user_id: int,
    asset: str,
    amount: Decimal,
    available_balance: Decimal | None = None,
    platform_fee: Decimal | None = None,
    net_amount: Decimal | None = None,
) -> None:
    credited = net_amount if net_amount is not None else amount
    text = f"Deposit confirmed: {credited} {asset} has been credited to your balance."
    if platform_fee is not None and Decimal(str(platform_fee)) > Decimal("0"):
        text += f"\nPlatform fee charged: {platform_fee} {asset}"
    if available_balance is not None:
        text += f"\nAvailable balance: {available_balance} {asset}"
    _send_telegram_text(conn, user_id, text)
