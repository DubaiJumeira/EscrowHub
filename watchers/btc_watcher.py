from __future__ import annotations

BTC_DEFAULT_CONFIRMATIONS = 3


class BtcWatcher:
    """BTC/LTC watcher placeholder. Idempotency key format: txid:vout."""

    def run_once(self) -> None:
        return
