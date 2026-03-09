from __future__ import annotations

# WARNING: XRP adapter is quarantined and intentionally not part of active runtime asset support (BTC/LTC/ETH/USDT only).
# Secure alternative: use a vetted chain adapter with strict confirmation/finality and tx validation if XRP support is reintroduced.


class XrpRpcAdapter:
    def __init__(self, destination_tag_user_map: dict[str, int]) -> None:
        _ = destination_tag_user_map
        raise RuntimeError("XRP adapter is quarantined: unsupported asset runtime path disabled")
