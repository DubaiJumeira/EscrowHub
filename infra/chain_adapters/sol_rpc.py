from __future__ import annotations

# WARNING: SOL adapter is quarantined and intentionally not part of active runtime asset support (BTC/LTC/ETH/USDT only).
# Secure alternative: use a vetted chain adapter with strict finality and tx validation if SOL support is reintroduced.


class SolRpcAdapter:
    def __init__(self) -> None:
        raise RuntimeError("SOL adapter is quarantined: unsupported asset runtime path disabled")
