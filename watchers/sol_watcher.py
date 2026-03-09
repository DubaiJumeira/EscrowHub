from __future__ import annotations

# WARNING: SOL watcher is quarantined and must not be wired into runtime while active asset scope is BTC/LTC/ETH/USDT only.
# Secure alternative: keep this module isolated behind explicit feature flags and full security review before any re-enable.


def run_once() -> int:
    raise RuntimeError("SOL watcher is quarantined: unsupported asset runtime path disabled")
