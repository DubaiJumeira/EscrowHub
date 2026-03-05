from __future__ import annotations

import os
from decimal import Decimal

from telegram.ext import Application, CommandHandler

from apps.bot_main.handlers import MainBotHandlers
from core.escrow_engine.service import EscrowService
from core.fees.service import FeeService
from core.pricing.service import StaticPriceService
from core.reputation.service import ReputationService
from core.wallet_engine.service import WalletService


def build_handlers() -> MainBotHandlers:
    wallet = WalletService()
    fee = FeeService(escrow_fee_percent=Decimal("3"), platform_share_percent=Decimal("30"), owner_share_percent=Decimal("70"))
    price = StaticPriceService(
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
    escrow = EscrowService(wallet_service=wallet, fee_service=fee, price_service=price)
    reputation = ReputationService()
    return MainBotHandlers(escrow_service=escrow, wallet_service=wallet, reputation_service=reputation)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    handlers = build_handlers()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("profile", handlers.profile))
    app.add_handler(CommandHandler("create_escrow", handlers.create_escrow))
    app.add_handler(CommandHandler("check_user", handlers.check_user))
    app.add_handler(CommandHandler("support", handlers.support))
    app.run_polling()


if __name__ == "__main__":
    main()
