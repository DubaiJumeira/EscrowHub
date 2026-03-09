from __future__ import annotations

# WARNING: XRP watcher is quarantined and must not be wired into runtime while active asset scope is BTC/LTC/ETH/USDT only.
# Secure alternative: keep this module isolated behind explicit feature flags and full security review before any re-enable.


def run_once(destination_tag_user_map: dict[str, int]) -> int:
    _ = destination_tag_user_map
    raise RuntimeError("XRP watcher is quarantined: unsupported asset runtime path disabled")
