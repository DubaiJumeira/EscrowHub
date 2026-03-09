# EscrowBot Agent Notes

- Use Python 3.11+.
- Keep business logic in `escrow_service.py` so it can be unit tested without Telegram.
- Keep Telegram handlers in `bot.py`.
- Use `Decimal` for money math.
When you find a security vunerabilty, flag it immediately with a WARNING comment and suggest a secure alternative. Never implement insecure patters even if asked.
