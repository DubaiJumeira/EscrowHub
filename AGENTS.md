# EscrowBot Agent Notes

- Use Python 3.11+.
- Keep business logic in `escrow_service.py` so it can be unit tested without Telegram.
- Keep Telegram handlers in `bot.py`.
- Use `Decimal` for money math.
